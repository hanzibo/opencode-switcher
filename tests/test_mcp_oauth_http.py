"""HttpTransport + OAuth2AuthProvider 集成测试。

模拟 smithery 风格的 OAuth 保护 MCP 服务器，全自动验证：
1. 无 token 请求 → 401 + WWW-Authenticate（RFC 9728）
2. OAuth2AuthProvider 自动：发现 PRM/AS → DCR 注册 → PKCE 授权码流（fake 浏览器）→ token 持久化
3. HttpTransport 用新 token 重试原请求 → initialize 成功
4. 缓存 token 复用，不重复授权
5. token 过期 → 自动刷新
6. 静态 Bearer 行为不变（401 → 抛 HttpTransportAuthError）

全部基于 stdlib unittest + asyncio + aiohttp，headless 运行。
"""

import asyncio
import json
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

# ── 测试隔离：所有 OAuth token 写入临时目录，避免污染真实配置 ──
import mcp_integration.oauth.token_store as _token_store_mod
_TEST_CONFIG_DIR = tempfile.mkdtemp(prefix="mcp-oauth-test-")
_token_store_mod.DEFAULT_CONFIG_DIR = _TEST_CONFIG_DIR

from mcp_integration.transports.http import (
    HttpTransport,
    HttpTransportAuthError,
)


# ═══════════════════════════════════════════════════════════════════
#  Mock OAuth 保护 MCP 服务器（模拟 smithery）
# ═══════════════════════════════════════════════════════════════════

TEST_CALLBACK_PORT = 43121
MCP_PATH = "/mcp"


class _MockOAuthMCPServer:
    """OAuth 保护 MCP 服务器（aiohttp AppRunner）。

    行为对齐 smithery：401 + WWW-Authenticate resource_metadata、
    PRM/AS 元数据、DCR、authorize 302、token code/refresh。
    """

    def __init__(self):
        from aiohttp import web

        self._web = web
        self.app = web.Application()
        self.app.router.add_post(MCP_PATH, self._handle_mcp)
        self.app.router.add_get("/.well-known/oauth-protected-resource/mcp", self._handle_prm)
        self.app.router.add_get("/.well-known/oauth-authorization-server", self._handle_as_meta)
        self.app.router.add_post("/register", self._handle_register)
        self.app.router.add_get("/authorize", self._handle_authorize)
        self.app.router.add_post("/token", self._handle_token)

        # 已签发的有效 access token
        self.valid_tokens = set()
        # 记录到达 MCP 端点的 Authorization（用于断言缓存复用）
        self.mcp_auth_headers = []
        # token 端点记录
        self.token_requests = []
        self.registered_clients = []
        # 授权码 → 状态
        self._issued_codes = {}
        self.base_url = ""

    # ── 启动 ──
    async def start(self):
        web = self._web
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        port = self._runner.addresses[0][1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self.base_url

    async def close(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    # ── MCP 端点 ──
    async def _handle_mcp(self, request):
        web = self._web
        auth = request.headers.get("Authorization", "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        self.mcp_auth_headers.append(auth)
        if not token or token not in self.valid_tokens:
            return web.Response(
                status=401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer error="invalid_token", '
                        f'resource_metadata="{self.base_url}/.well-known/oauth-protected-resource/mcp", '
                        'scope="connections:execute"'
                    )
                },
                text='{"error":"invalid_token"}',
            )
        body = await request.json()
        method = body.get("method")
        if method == "initialize":
            return web.json_response({
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock-oauth-mcp", "version": "1.0.0"},
                },
            })
        if method == "ping":
            return web.json_response({"jsonrpc": "2.0", "id": body.get("id"), "result": {}})
        return web.json_response({
            "jsonrpc": "2.0", "id": body.get("id"),
            "error": {"code": -32601, "message": f"unknown method {method}"},
        })

    # ── 发现 ──
    async def _handle_prm(self, request):
        web = self._web
        return web.json_response({
            "resource": self.base_url + MCP_PATH,
            "authorization_servers": [self.base_url],
            "scopes_supported": ["connections:execute"],
        })

    async def _handle_as_meta(self, request):
        web = self._web
        return web.json_response({
            "issuer": self.base_url,
            "authorization_endpoint": self.base_url + "/authorize",
            "token_endpoint": self.base_url + "/token",
            "registration_endpoint": self.base_url + "/register",
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "scopes_supported": ["connections:read", "connections:write", "connections:execute"],
        })

    # ── DCR ──
    async def _handle_register(self, request):
        web = self._web
        data = await request.json()
        self.registered_clients.append(data)
        client_id = f"client-{len(self.registered_clients)}"
        return web.json_response({
            "client_id": client_id,
            "token_endpoint_auth_method": "none",
            "redirect_uris": data.get("redirect_uris", []),
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "client_name": data.get("client_name", ""),
        }, status=201)

    # ── authorize（302 → redirect_uri?code=...&state=...） ──
    async def _handle_authorize(self, request):
        web = self._web
        params = parse_qs(request.query_string)
        redirect_uri = params.get("redirect_uri", [""])[0]
        state = params.get("state", [""])[0]
        resource = params.get("resource", [""])[0]
        scope = params.get("scope", [""])[0]
        if not redirect_uri:
            return web.Response(status=400, text="missing redirect_uri")
        code = "AUTHCODE-1"
        self._issued_codes[code] = {"resource": resource, "scope": scope}
        location = f"{redirect_uri}?code={code}&state={state}"
        return web.Response(status=302, headers={"Location": location})

    # ── token ──
    async def _handle_token(self, request):
        web = self._web
        form = await request.post()
        self.token_requests.append(dict(form))
        grant = form.get("grant_type")
        if grant == "authorization_code":
            code = form.get("code")
            # 校验 PKCE verifier 与 resource（对齐 RFC 7636 / RFC 8707）
            if not form.get("code_verifier"):
                return web.json_response({"error": "invalid_request", "error_description": "missing code_verifier"}, status=400)
            if form.get("resource") != self.base_url + MCP_PATH:
                return web.json_response({"error": "invalid_target"}, status=400)
            if code not in self._issued_codes:
                return web.json_response({"error": "invalid_grant"}, status=400)
            at = f"at-{len(self.valid_tokens) + 1}"
            self.valid_tokens.add(at)
            return web.json_response({
                "access_token": at,
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "RT-1",
                "scope": self._issued_codes[code].get("scope", ""),
            })
        if grant == "refresh_token":
            at = f"at-refreshed-{len(self.valid_tokens) + 1}"
            self.valid_tokens.add(at)
            return web.json_response({
                "access_token": at,
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "RT-2",
                "scope": form.get("scope", ""),
            })
        return web.json_response({"error": "unsupported_grant_type"}, status=400)


def _fake_browser(url):
    """模拟真实浏览器：访问 authorize URL 并跟随 302 到 loopback 回调。"""

    async def _open(auth_url):
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(auth_url, allow_redirects=True):
                pass
    return _open


class TestHttpTransportOAuthAutoAuth(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from mcp_integration.oauth.provider import OAuth2AuthProvider

        self.mock = _MockOAuthMCPServer()
        self.base_url = await self.mock.start()
        self.server_url = self.base_url + MCP_PATH
        self.provider = OAuth2AuthProvider(
            self.server_url,
            callback_port=TEST_CALLBACK_PORT,
            browser_opener=_fake_browser(self.server_url),
            flow_timeout=10,
        )

    async def asyncTearDown(self):
        await self.mock.close()

    def _make_transport(self):
        return HttpTransport(url=self.server_url, auth_provider=self.provider)

    async def test_401_auto_auth_then_retry_success(self):
        """首次 initialize：401 → 自动认证 → 重试 → 成功。"""
        transport = self._make_transport()
        await transport.connect()
        try:
            await transport.send_line(
                json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {}},
                })
            )
            line = await transport.read_line()
            self.assertIsNotNone(line)
            msg = json.loads(line)
            self.assertEqual(msg["id"], 1)
            self.assertEqual(msg["result"]["serverInfo"]["name"], "mock-oauth-mcp")
        finally:
            await transport.disconnect()

        # 认证链完整执行：PRM 发现 + DCR 注册 + token 端点
        self.assertEqual(len(self.mock.registered_clients), 1)
        self.assertGreaterEqual(len(self.mock.token_requests), 1)
        # 最终 MCP 请求携带有效 Bearer
        self.assertIn("Bearer at-1", self.mock.mcp_auth_headers)
        # token 已持久化
        data = self.provider.store.load()
        self.assertIsNotNone(data)
        assert data is not None
        self.assertTrue(data["token"].is_valid())

    async def test_cached_token_reused_no_reauth(self):
        """第二次请求复用缓存 token，不再触发授权。"""
        transport = self._make_transport()
        await transport.connect()
        try:
            for i in (1, 2):
                await transport.send_line(
                    json.dumps({
                        "jsonrpc": "2.0", "id": i, "method": "ping",
                    })
                )
                line = await transport.read_line()
                self.assertIsNotNone(line)
                self.assertEqual(json.loads(line)["id"], i)
        finally:
            await transport.disconnect()

        # 只注册过一次客户端；access token 未新增（未重新授权）
        self.assertEqual(len(self.mock.registered_clients), 1)
        self.assertEqual(len(self.mock.token_requests), 1)
        self.assertEqual(len(self.mock.valid_tokens), 1)
        # 两次请求都带有效 Bearer
        self.assertTrue(all(h.startswith("Bearer at-") for h in self.mock.mcp_auth_headers))

    async def test_token_expired_auto_refresh(self):
        """token 过期后自动刷新，无需重新授权。"""
        # 先完成一次认证
        transport = self._make_transport()
        await transport.connect()
        try:
            await transport.send_line(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            )
            await transport.read_line()
        finally:
            await transport.disconnect()

        # 人为使 token 立即过期（模拟访问令牌过期；模拟新会话，重建 provider）
        data = self.provider.store.load()
        assert data is not None
        token = data["token"]
        token.expires_at = 0.0
        self.provider.store.save(token, client=data.get("client"), oauth_metadata=data.get("oauth_metadata"))

        from mcp_integration.oauth.provider import OAuth2AuthProvider
        fresh_provider = OAuth2AuthProvider(
            self.server_url,
            callback_port=TEST_CALLBACK_PORT,
            browser_opener=_fake_browser(self.server_url),
            flow_timeout=10,
        )

        # 再次请求 → 过期 → 刷新（token 端点出现 refresh_token grant）
        transport2 = HttpTransport(url=self.server_url, auth_provider=fresh_provider)
        await transport2.connect()
        try:
            await transport2.send_line(
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
            )
            line = await transport2.read_line()
            self.assertIsNotNone(line)
            self.assertEqual(json.loads(line)["id"], 2)
        finally:
            await transport2.disconnect()

        grants = [r.get("grant_type") for r in self.mock.token_requests]
        self.assertIn("refresh_token", grants)
        self.assertEqual(len(self.mock.registered_clients), 1)  # 未重新注册

    async def test_401_revoked_token_refreshes_not_reauth(self):
        """H3：服务端吊销 token 后 401 → 静默刷新，不重走浏览器授权。"""
        transport = self._make_transport()
        await transport.connect()
        try:
            # 第一次成功（authorization_code）
            await transport.send_line(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            )
            await transport.read_line()
            self.assertEqual(len(self.mock.token_requests), 1)
            self.assertEqual(self.mock.token_requests[0]["grant_type"], "authorization_code")

            # 服务端吊销 access token（模拟被撤销/服务端过期）
            self.mock.valid_tokens.clear()

            # 第二次 → 401 → handle_challenge → 应走 refresh（无新授权码流）
            await transport.send_line(
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
            )
            line = await transport.read_line()
            self.assertIsNotNone(line)
            self.assertEqual(json.loads(line)["id"], 2)
        finally:
            await transport.disconnect()

        grants = [r.get("grant_type") for r in self.mock.token_requests]
        self.assertIn("refresh_token", grants)
        # 未重新注册客户端、未出现第二个 authorization_code
        self.assertEqual(len(self.mock.registered_clients), 1)
        self.assertEqual(grants.count("authorization_code"), 1)
        # 最终请求携带刷新后的 Bearer
        self.assertTrue(any(
            h.startswith("Bearer at-refreshed") for h in self.mock.mcp_auth_headers
        ))

    async def test_401_no_refresh_token_falls_back_to_full_auth(self):
        """H3：无 refresh_token 时 401 → 完整授权流（回退路径）。"""
        transport = self._make_transport()
        await transport.connect()
        try:
            await transport.send_line(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            )
            await transport.read_line()
            # 移除 refresh_token 并吊销 token，使刷新不可用
            data = self.provider.store.load()
            assert data is not None
            token = data["token"]
            token.refresh_token = ""
            token.expires_at = 0.0
            self.provider.store.save(
                token, client=data.get("client"), oauth_metadata=data.get("oauth_metadata")
            )
            self.provider._current_token = None
            self.mock.valid_tokens.clear()

            # 再次请求 → 401 → 刷新不可用 → 完整授权流（出现第二个 authorization_code）
            await transport.send_line(
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
            )
            line = await transport.read_line()
            self.assertIsNotNone(line)
            self.assertEqual(json.loads(line)["id"], 2)
        finally:
            await transport.disconnect()

        grants = [r.get("grant_type") for r in self.mock.token_requests]
        self.assertEqual(grants.count("authorization_code"), 2)

    async def test_401_no_expiry_token_refreshes(self):
        """L8：无 expires_at 的 token 被服务端拒绝时，401 → 静默刷新自愈。"""
        transport = self._make_transport()
        await transport.connect()
        try:
            await transport.send_line(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            )
            await transport.read_line()
            # 清除过期时间（模拟 AS 未返回 expires_in）并吊销服务端 token
            data = self.provider.store.load()
            assert data is not None
            token = data["token"]
            token.expires_at = None
            self.provider.store.save(
                token, client=data.get("client"), oauth_metadata=data.get("oauth_metadata")
            )
            self.mock.valid_tokens.clear()

            await transport.send_line(
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
            )
            line = await transport.read_line()
            self.assertIsNotNone(line)
            self.assertEqual(json.loads(line)["id"], 2)
        finally:
            await transport.disconnect()

        grants = [r.get("grant_type") for r in self.mock.token_requests]
        self.assertIn("refresh_token", grants)
        self.assertEqual(grants.count("authorization_code"), 1)


class TestHttpTransportStaticBearer(unittest.IsolatedAsyncioTestCase):
    """静态 Bearer 行为不变（向后兼容）。"""

    async def asyncSetUp(self):
        self.mock = _MockOAuthMCPServer()
        self.base_url = await self.mock.start()
        self.server_url = self.base_url + MCP_PATH

    async def asyncTearDown(self):
        await self.mock.close()

    async def test_invalid_static_key_raises_auth_error(self):
        transport = HttpTransport(url=self.server_url, api_key="wrong-key")
        await transport.connect()
        try:
            with self.assertRaises(HttpTransportAuthError):
                await transport.send_line(
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
                )
        finally:
            await transport.disconnect()

    async def test_valid_static_key_works(self):
        transport = HttpTransport(url=self.server_url, api_key="whatever")
        # 预置一个有效 token（模拟用户已有 API key 被服务器接受）
        self.mock.valid_tokens.add("whatever")
        await transport.connect()
        try:
            await transport.send_line(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            )
            line = await transport.read_line()
            self.assertIsNotNone(line)
            self.assertEqual(json.loads(line)["id"], 1)
        finally:
            await transport.disconnect()


class TestClientManagerOAuth(unittest.IsolatedAsyncioTestCase):
    """client_manager.connect_http 对 auth_type=oauth2 配置端到端自动认证。"""

    async def asyncSetUp(self):
        from mcp_integration.server_config import MCPServerConfig

        self.mock = _MockOAuthMCPServer()
        self.base_url = await self.mock.start()
        self.server_url = self.base_url + MCP_PATH
        self.config = MCPServerConfig(
            name="oauth-server",
            transport="http",
            url=self.server_url,
            auth_type="oauth2",
            auto_connect=False,
        )

    async def asyncTearDown(self):
        await self.mock.close()

    async def test_connect_http_auto_auth(self):
        from mcp_integration import MCPClientManager, MCPServerConfig

        mgr = MCPClientManager()
        ok, msg = await mgr.connect_http(self.config)
        self.assertTrue(ok, msg)
        try:
            self.assertTrue(mgr.is_connected("oauth-server"))
            tools = await mgr.list_tools("oauth-server")
            self.assertIsInstance(tools, list)
            # 认证链路执行
            self.assertEqual(len(self.mock.registered_clients), 1)
            self.assertIn("Bearer at-1", self.mock.mcp_auth_headers)
        finally:
            await mgr.disconnect("oauth-server")

    async def test_reconnect_uses_cached_token(self):
        from mcp_integration import MCPClientManager

        mgr = MCPClientManager()
        ok, msg = await mgr.connect_http(self.config)
        self.assertTrue(ok, msg)
        await mgr.disconnect("oauth-server")

        # 重连：应复用已持久化的 token，不再注册/授权
        mgr2 = MCPClientManager()
        ok2, msg2 = await mgr2.connect_http(self.config)
        self.assertTrue(ok2, msg2)
        await mgr2.disconnect("oauth-server")

        self.assertEqual(len(self.mock.registered_clients), 1)
        self.assertEqual(len(self.mock.token_requests), 1)


class TestMCPOAuthStoreKey(unittest.TestCase):
    """M6：token 存储以 canonical URL 为 key，URL 微调不丢失授权状态。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_trailing_slash_does_not_change_key(self):
        from mcp_integration.oauth.provider import OAuth2AuthProvider

        p1 = OAuth2AuthProvider("https://mcp.example.com/mcp/", config_dir=self._tmp.name)
        p2 = OAuth2AuthProvider("https://mcp.example.com/mcp", config_dir=self._tmp.name)
        self.assertEqual(p1.store.path, p2.store.path)

    def test_query_fragment_stripped(self):
        from mcp_integration.oauth.provider import OAuth2AuthProvider

        p1 = OAuth2AuthProvider("https://mcp.example.com/mcp?x=1", config_dir=self._tmp.name)
        p2 = OAuth2AuthProvider("https://mcp.example.com/mcp#frag", config_dir=self._tmp.name)
        self.assertEqual(p1.store.path, p2.store.path)


class TestMCPServerConfigOAuthFields(unittest.TestCase):
    """server_config oauth 字段序列化与校验。"""
    def test_oauth_scopes_roundtrip(self):
        from mcp_integration.server_config import MCPServerConfig

        cfg = MCPServerConfig(
            name="s", transport="http", url="https://mcp.example.com/mcp",
            auth_type="oauth2", oauth_client_id="cid",
            oauth_client_secret="sec", oauth_token_url="https://as.example.com/token",
            oauth_scopes="a b",
        )
        d = cfg.to_dict()
        self.assertEqual(d["oauth_scopes"], "a b")
        cfg2 = MCPServerConfig.from_dict(d)
        self.assertEqual(cfg2.auth_type, "oauth2")
        self.assertEqual(cfg2.oauth_client_id, "cid")
        self.assertEqual(cfg2.oauth_scopes, "a b")

    def test_old_config_backward_compat(self):
        from mcp_integration.server_config import MCPServerConfig

        # 旧配置无 oauth_scopes 字段
        cfg = MCPServerConfig.from_dict({
            "name": "s", "transport": "http", "url": "https://mcp.example.com/mcp",
        })
        self.assertEqual(cfg.oauth_scopes, "")
        self.assertIsNone(cfg.validate())

    def test_invalid_auth_type_rejected(self):
        from mcp_integration.server_config import MCPServerConfig

        cfg = MCPServerConfig(
            name="s", transport="http", url="https://mcp.example.com/mcp",
            auth_type="ntlm",
        )
        self.assertIsNotNone(cfg.validate())


class TestToolNameSanitization(unittest.TestCase):
    """工具名净化 + 净化名→原始名还原（Smithery 点号工具名回归）。"""

    def test_schema_name_sanitizes_dots(self):
        from mcp_integration.tool_adapter import mcp_tool_to_openai_schema

        schema = mcp_tool_to_openai_schema("Smithery", {
            "name": "oevortex-ddg-search.web-search",
            "description": "web search",
            "inputSchema": {"type": "object", "properties": {}},
        })
        self.assertEqual(
            schema["function"]["name"],
            "Smithery__oevortex-ddg-search_web-search",
        )
        # OpenAI 允许的字符集
        import re
        self.assertRegex(schema["function"]["name"], r"^[a-zA-Z0-9_-]+$")

    def test_plain_names_unchanged(self):
        from mcp_integration.tool_adapter import mcp_tool_to_openai_schema

        schema = mcp_tool_to_openai_schema("Firecrawl", {
            "name": "search", "description": "s",
            "inputSchema": {"type": "object", "properties": {}},
        })
        self.assertEqual(schema["function"]["name"], "Firecrawl__search")


class TestClientManagerToolNameMapping(unittest.IsolatedAsyncioTestCase):
    """client_manager 净化名→原始名映射与并发安全。"""

    def _make_fake_session(self, raw_tool_names):
        """记录实际收到的工具名。"""
        class FakeSession:
            def __init__(self, tools):
                self._tools = tools
                self.called_with = []

            @property
            def is_connected(self):
                return True

            async def list_tools(self):
                return [{"name": n, "description": "", "inputSchema": {"type": "object"}} for n in self._tools]

            async def call_tool(self, name, arguments, timeout=None):
                self.called_with.append(name)
                return "ok"
        return FakeSession(raw_tool_names)

    async def test_call_tool_restores_original_name(self):
        from mcp_integration.client_manager import MCPClientManager

        mgr = MCPClientManager()
        session = self._make_fake_session(["oevortex-ddg-search.web-search"])
        mgr._sessions["Smithery"] = session

        tools = await mgr.list_all_tools()
        self.assertEqual(
            tools[0]["function"]["name"],
            "Smithery__oevortex-ddg-search_web-search",
        )
        # 用 LLM 返回的净化名调用 → 应还原为原始名发往 MCP Server
        result = await mgr.call_tool("Smithery", "oevortex-ddg-search_web-search", {})
        self.assertEqual(result, "ok")
        self.assertEqual(session.called_with, ["oevortex-ddg-search.web-search"])

    async def test_plain_name_unchanged(self):
        from mcp_integration.client_manager import MCPClientManager

        mgr = MCPClientManager()
        session = self._make_fake_session(["search"])
        mgr._sessions["Firecrawl"] = session

        await mgr.list_all_tools()
        await mgr.call_tool("Firecrawl", "search", {})
        self.assertEqual(session.called_with, ["search"])

    async def test_concurrent_list_all_tools_no_runtime_error(self):
        """并发 list_all_tools 期间字典被修改不再抛 RuntimeError。"""
        from mcp_integration.client_manager import MCPClientManager

        mgr = MCPClientManager()
        for i in range(3):
            mgr._sessions[f"srv{i}"] = self._make_fake_session([f"tool.{i}"])

        async def mutate():
            # 模拟连接回调：list_all_tools 执行期间添加/删除 session
            await asyncio.sleep(0)
            mgr._sessions["srv-extra"] = self._make_fake_session(["extra.tool"])
            await asyncio.sleep(0)
            mgr._sessions.pop("srv-extra", None)

        t1 = asyncio.create_task(mgr.list_all_tools())
        t2 = asyncio.create_task(mgr.list_all_tools())
        await mutate()
        results = await asyncio.gather(t1, t2)
        # 不抛 RuntimeError 即通过；结果包含净化后的工具
        names = {t["function"]["name"] for r in results for t in r}
        self.assertIn("srv0__tool_0", names)

    async def test_tool_name_map_cleaned_on_refresh(self):
        """M2：工具改名后旧映射被清理，不残留陈旧条目。"""
        from mcp_integration.client_manager import MCPClientManager

        mgr = MCPClientManager()
        # 第一次：工具名为 tool.old（净化后 tool_old）
        session = self._make_fake_session(["tool.old"])
        mgr._sessions["srv"] = session
        await mgr.list_all_tools()
        self.assertIn("srv__tool_old", mgr._tool_name_map)
        self.assertEqual(mgr._tool_name_map["srv__tool_old"], "tool.old")

        # 第二次：工具改名（tool.old → tool.new，仍需净化）
        session._tools = ["tool.new"]
        await mgr.list_all_tools()
        self.assertNotIn("srv__tool_old", mgr._tool_name_map)  # 旧映射被清理
        self.assertIn("srv__tool_new", mgr._tool_name_map)     # 新映射已更新
        self.assertEqual(mgr._tool_name_map["srv__tool_new"], "tool.new")
        # 调用净化名 → 还原为新的原始名
        result = await mgr.call_tool("srv", "tool_new", {})
        self.assertEqual(result, "ok")
        self.assertEqual(session.called_with, ["tool.new"])


if __name__ == "__main__":
    unittest.main()
