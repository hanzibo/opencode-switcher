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
import unittest
from urllib.parse import parse_qs, urlparse

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
        port = self._site._server.sockets[0].getsockname()[1]
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


if __name__ == "__main__":
    unittest.main()
