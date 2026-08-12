"""MCP OAuth 2.1 客户端认证 — 单元测试。

覆盖（Step 1–3 范围）：
- WWW-Authenticate 解析（smithery / GitHub Copilot 真实样本）
- PRM / AS 元数据 well-known URL 构造（含路径插入）
- canonical_server_uri（RFC 8707 规范 URI）
- PRM / AS 元数据发现流程（注入 mock fetch_json）
- PKCE S256 生成
- OAuthToken 过期判断与序列化

全部基于 stdlib unittest + asyncio，headless 运行，不依赖 GTK。
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from mcp_integration.oauth.discovery import (
    build_oauth_metadata_urls,
    build_protected_resource_metadata_urls,
    canonical_server_uri,
    discover_oauth_metadata,
    discover_protected_resource_metadata,
    parse_www_authenticate,
)
from mcp_integration.oauth.flow import (
    build_authorization_url,
    exchange_code,
    refresh_token,
)
from mcp_integration.oauth.models import (
    OAuthMetadata,
    OAuthToken,
    PKCEParameters,
    ProtectedResourceMetadata,
)


# ═══════════════════════════════════════════════════════════════════
#  WWW-Authenticate 解析
# ═══════════════════════════════════════════════════════════════════

SMITHERY_WWW_AUTH = (
    'Bearer error="invalid_token", error_description="Missing Authorization header", '
    'resource_metadata="https://mcp.smithery.ai/.well-known/oauth-protected-resource/jibo96701436", '
    'scope="connections:execute"'
)

GITHUB_COPILOT_WWW_AUTH = (
    'Bearer error="invalid_request", '
    'error_description="No access token was provided in this request", '
    'resource_metadata="https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/"'
)


class TestParseWwwAuthenticate(unittest.TestCase):
    def test_smithery_real_sample(self):
        ch = parse_www_authenticate(SMITHERY_WWW_AUTH)
        self.assertEqual(ch.scheme, "Bearer")
        self.assertEqual(ch.error, "invalid_token")
        self.assertEqual(ch.error_description, "Missing Authorization header")
        self.assertEqual(
            ch.resource_metadata,
            "https://mcp.smithery.ai/.well-known/oauth-protected-resource/jibo96701436",
        )
        self.assertEqual(ch.scope, "connections:execute")
        self.assertTrue(ch.is_oauth_challenge)

    def test_github_copilot_real_sample(self):
        ch = parse_www_authenticate(GITHUB_COPILOT_WWW_AUTH)
        self.assertEqual(ch.scheme, "Bearer")
        self.assertEqual(ch.error, "invalid_request")
        self.assertEqual(
            ch.resource_metadata,
            "https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/",
        )
        self.assertEqual(ch.scope, "")
        self.assertTrue(ch.is_oauth_challenge)

    def test_insufficient_scope_403(self):
        ch = parse_www_authenticate(
            'Bearer error="insufficient_scope", scope="files:read files:write", '
            'resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
        )
        self.assertEqual(ch.error, "insufficient_scope")
        self.assertEqual(ch.scope, "files:read files:write")
        self.assertTrue(ch.is_oauth_challenge)

    def test_empty_and_plain(self):
        self.assertEqual(parse_www_authenticate("").scheme, "")
        ch = parse_www_authenticate("Basic realm=foo")
        self.assertEqual(ch.scheme, "Basic")
        self.assertFalse(ch.is_oauth_challenge)  # 非 Bearer 非 resource_metadata

    def test_value_with_commas_in_quotes(self):
        ch = parse_www_authenticate(
            'Bearer error="a,b", error_description="hello, world", scope="s1 s2"'
        )
        self.assertEqual(ch.error, "a,b")
        self.assertEqual(ch.error_description, "hello, world")
        self.assertEqual(ch.scope, "s1 s2")


# ═══════════════════════════════════════════════════════════════════
#  well-known URL 构造
# ═══════════════════════════════════════════════════════════════════


class TestBuildDiscoveryUrls(unittest.TestCase):
    def test_prm_with_path_insertion(self):
        urls = build_protected_resource_metadata_urls("https://mcp.smithery.ai/jibo96701436")
        self.assertEqual(urls, [
            "https://mcp.smithery.ai/.well-known/oauth-protected-resource/jibo96701436",
            "https://mcp.smithery.ai/.well-known/oauth-protected-resource",
        ])

    def test_prm_root_only(self):
        urls = build_protected_resource_metadata_urls("https://mcp.example.com/mcp")
        self.assertEqual(urls[0], "https://mcp.example.com/.well-known/oauth-protected-resource/mcp")
        self.assertIn("https://mcp.example.com/.well-known/oauth-protected-resource", urls)

    def test_prm_invalid_url(self):
        self.assertEqual(build_protected_resource_metadata_urls("not-a-url"), [])

    def test_as_metadata_no_path(self):
        urls = build_oauth_metadata_urls("https://connect-auth.smithery.ai")
        self.assertEqual(urls, [
            "https://connect-auth.smithery.ai/.well-known/oauth-authorization-server",
            "https://connect-auth.smithery.ai/.well-known/openid-configuration",
        ])

    def test_as_metadata_with_path(self):
        urls = build_oauth_metadata_urls("https://auth.example.com/tenant1")
        self.assertEqual(urls, [
            "https://auth.example.com/.well-known/oauth-authorization-server/tenant1",
            "https://auth.example.com/.well-known/openid-configuration/tenant1",
            "https://auth.example.com/tenant1/.well-known/openid-configuration",
        ])

    def test_canonical_server_uri(self):
        self.assertEqual(canonical_server_uri("https://MCP.Example.com/jibo96701436/"), "https://mcp.example.com/jibo96701436")
        self.assertEqual(canonical_server_uri("https://mcp.example.com#frag"), "https://mcp.example.com")
        self.assertEqual(canonical_server_uri("https://mcp.example.com:8443/mcp"), "https://mcp.example.com:8443/mcp")


# ═══════════════════════════════════════════════════════════════════
#  发现流程（注入 mock fetch_json）
# ═══════════════════════════════════════════════════════════════════


def _make_fetch_json(routes):
    """构造按 URL 精确路由的 mock fetch_json。"""
    async def fetch_json(url):
        if url in routes:
            return routes[url]
        raise FileNotFoundError(f"no route: {url}")
    return fetch_json


class TestDiscoverProtectedResourceMetadata(unittest.IsolatedAsyncioTestCase):
    async def test_header_resource_metadata_wins(self):
        prm_url = "https://mcp.smithery.ai/.well-known/oauth-protected-resource/jibo96701436"
        prm = {
            "resource": "https://mcp.smithery.ai/jibo96701436",
            "authorization_servers": ["https://connect-auth.smithery.ai"],
            "scopes_supported": ["connections:execute"],
        }
        fetch = _make_fetch_json({prm_url: prm})
        result = await discover_protected_resource_metadata(
            "https://mcp.smithery.ai/jibo96701436",
            www_authenticate=SMITHERY_WWW_AUTH,
            fetch_json=fetch,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.authorization_servers, ["https://connect-auth.smithery.ai"])
        self.assertEqual(result.scopes_supported, ["connections:execute"])

    async def test_wellknown_fallback_without_header(self):
        prm_url = "https://mcp.smithery.ai/.well-known/oauth-protected-resource/jibo96701436"
        prm = {
            "resource": "https://mcp.smithery.ai/jibo96701436",
            "authorization_servers": ["https://connect-auth.smithery.ai"],
        }
        fetch = _make_fetch_json({prm_url: prm})
        result = await discover_protected_resource_metadata(
            "https://mcp.smithery.ai/jibo96701436", fetch_json=fetch
        )
        self.assertIsNotNone(result)

    async def test_resource_mismatch_rejected(self):
        prm_url = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
        prm = {"resource": "https://evil.example.com/", "authorization_servers": ["https://evil.example.com"]}
        fetch = _make_fetch_json({prm_url: prm})
        result = await discover_protected_resource_metadata(
            "https://mcp.example.com/mcp", fetch_json=fetch
        )
        self.assertIsNone(result)

    async def test_all_fail_returns_none(self):
        fetch = _make_fetch_json({})
        result = await discover_protected_resource_metadata(
            "https://mcp.example.com/mcp", fetch_json=fetch
        )
        self.assertIsNone(result)


class TestDiscoverOAuthMetadata(unittest.IsolatedAsyncioTestCase):
    AS_META = {
        "issuer": "https://connect-auth.smithery.ai",
        "authorization_endpoint": "https://connect-auth.smithery.ai/authorize",
        "token_endpoint": "https://connect-auth.smithery.ai/token",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "registration_endpoint": "https://connect-auth.smithery.ai/register",
    }

    async def test_rfc8414_discovery(self):
        url = "https://connect-auth.smithery.ai/.well-known/oauth-authorization-server"
        fetch = _make_fetch_json({url: self.AS_META})
        meta = await discover_oauth_metadata("https://connect-auth.smithery.ai", fetch_json=fetch)
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertEqual(meta.authorization_endpoint, "https://connect-auth.smithery.ai/authorize")
        self.assertTrue(meta.supports_pkce_s256())
        self.assertTrue(meta.supports_refresh_token())
        self.assertEqual(meta.registration_endpoint, "https://connect-auth.smithery.ai/register")

    async def test_oidc_fallback(self):
        url = "https://auth.example.com/.well-known/oauth-authorization-server"
        oidc = "https://auth.example.com/.well-known/openid-configuration"
        fetch = _make_fetch_json({
            oidc: {
                "issuer": "https://auth.example.com",
                "authorization_endpoint": "https://auth.example.com/authorize",
                "token_endpoint": "https://auth.example.com/token",
            }
        })
        meta = await discover_oauth_metadata("https://auth.example.com", fetch_json=fetch)
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertEqual(meta.authorization_endpoint, "https://auth.example.com/authorize")

    async def test_missing_endpoints_skipped(self):
        fetch = _make_fetch_json({
            "https://auth.example.com/.well-known/oauth-authorization-server": {"issuer": "x"},
        })
        meta = await discover_oauth_metadata("https://auth.example.com", fetch_json=fetch)
        self.assertIsNone(meta)

    async def test_path_issuer_tries_path_metadata(self):
        url = "https://auth.example.com/.well-known/oauth-authorization-server/tenant1"
        fetch = _make_fetch_json({
            url: {
                "issuer": "https://auth.example.com/tenant1",
                "authorization_endpoint": "https://auth.example.com/tenant1/authorize",
                "token_endpoint": "https://auth.example.com/tenant1/token",
            }
        })
        meta = await discover_oauth_metadata("https://auth.example.com/tenant1", fetch_json=fetch)
        self.assertIsNotNone(meta)


# ═══════════════════════════════════════════════════════════════════
#  PKCE / Token 模型
# ═══════════════════════════════════════════════════════════════════


class TestPKCE(unittest.TestCase):
    def test_generate_s256(self):
        pkce = PKCEParameters.generate()
        self.assertGreaterEqual(len(pkce.code_verifier), 43)
        self.assertLessEqual(len(pkce.code_verifier), 128)
        self.assertEqual(pkce.code_challenge_method, "S256")
        # S256 challenge 应为 43 字符（base64url 无 padding，32 字节）
        self.assertEqual(len(pkce.code_challenge), 43)
        self.assertTrue(pkce.is_complete())

    def test_generate_randomness(self):
        a = PKCEParameters.generate()
        b = PKCEParameters.generate()
        self.assertNotEqual(a.code_verifier, b.code_verifier)


class TestOAuthToken(unittest.TestCase):
    def test_to_from_dict_roundtrip(self):
        tok = OAuthToken(
            access_token="at", refresh_token="rt", expires_in=3600, scope="a b",
            expires_at=1234567890.0,
        )
        d = tok.to_dict()
        tok2 = OAuthToken.from_dict(d)
        self.assertEqual(tok2.access_token, "at")
        self.assertEqual(tok2.refresh_token, "rt")
        self.assertEqual(tok2.expires_at, 1234567890.0)

    def test_is_expired(self):
        tok = OAuthToken(access_token="at", expires_at=9999999999.0)
        self.assertFalse(tok.is_expired())
        tok2 = OAuthToken(access_token="at", expires_at=1.0)
        self.assertTrue(tok2.is_expired())
        tok3 = OAuthToken(access_token="at")
        self.assertFalse(tok3.is_expired())  # 无过期时间视为不过期
        self.assertTrue(tok3.is_valid())

    def test_is_valid_requires_access_token(self):
        tok = OAuthToken(access_token="")
        self.assertFalse(tok.is_valid())


# ═══════════════════════════════════════════════════════════════════
#  TokenStore 持久化
# ═══════════════════════════════════════════════════════════════════


class TestOAuthTokenStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from mcp_integration.oauth.token_store import OAuthTokenStore

        self.store = OAuthTokenStore("test-server", config_dir=self._tmp.name)

    def _make_token(self, access="at", refresh="rt", expires_at=None):
        return OAuthToken(
            access_token=access, refresh_token=refresh, expires_at=expires_at,
        )

    def test_save_load_roundtrip(self):
        from mcp_integration.oauth.models import OAuthClientInformationFull

        client = OAuthClientInformationFull(
            client_id="cid", token_endpoint_auth_method="none",
            redirect_uris=["http://127.0.0.1:1234/callback"],
        )
        self.store.save(self._make_token(), client=client)
        data = self.store.load()
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["token"].access_token, "at")
        self.assertEqual(data["client"].client_id, "cid")

    def test_load_missing_returns_none(self):
        self.assertIsNone(self.store.load())

    def test_permissions_0600(self):
        self.store.save(self._make_token())
        mode = os.stat(self.store.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_permissions_reset_on_existing_file(self):
        """H1 回归：已存在 0644 文件在 save 后被强制重置为 0600。"""
        # 先写入一个宽松权限的文件
        os.makedirs(os.path.dirname(self.store.path), exist_ok=True)
        with open(self.store.path, "w", encoding="utf-8") as f:
            f.write("{}")
        os.chmod(self.store.path, 0o644)
        # save 后权限必须重置
        self.store.save(self._make_token())
        mode = os.stat(self.store.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        # 内容仍可正常读取
        data = self.store.load()
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["token"].access_token, "at")

    def test_dir_permissions_0700(self):
        """L4 回归：存储目录权限为 0700。"""
        self.store.save(self._make_token())
        dir_mode = os.stat(self.store._dir).st_mode & 0o777
        self.assertEqual(dir_mode, 0o700)

    def test_no_tmp_leftover_after_save(self):
        """save 成功后不留 .tmp 残留文件。"""
        self.store.save(self._make_token())
        self.assertFalse(os.path.exists(self.store.path + ".tmp"))

    def test_has_token_validity(self):
        import time
        self.store.save(self._make_token(expires_at=time.time() + 3600))
        self.assertTrue(self.store.has_token())
        self.store.save(self._make_token(expires_at=time.time() - 10))
        self.assertFalse(self.store.has_token())

    def test_clear(self):
        self.store.save(self._make_token())
        self.assertTrue(os.path.isfile(self.store.path))
        self.store.clear()
        self.assertFalse(os.path.isfile(self.store.path))
        self.assertIsNone(self.store.load())

    def test_status(self):
        import time
        self.assertEqual(self.store.get_status(), "未授权")
        self.store.save(self._make_token(expires_at=time.time() + 3600))
        self.assertEqual(self.store.get_status(), "已授权")
        self.store.save(self._make_token(expires_at=time.time() - 10))
        self.assertEqual(self.store.get_status(), "已过期")
        # 已过期且无 refresh token → 无法刷新，视为未授权
        self.store.save(self._make_token(expires_at=time.time() - 10, refresh=""))
        self.assertEqual(self.store.get_status(), "未授权")

    def test_server_name_sanitization(self):
        from mcp_integration.oauth.token_store import OAuthTokenStore

        store = OAuthTokenStore("My Server/With:Chars", config_dir=self._tmp.name)
        filename = os.path.basename(store.path)
        self.assertNotIn("/", filename)
        self.assertNotIn(":", filename)
        self.assertEqual(filename, "My_Server_With_Chars.json")
        self.assertTrue(filename.endswith(".json"))


# ═══════════════════════════════════════════════════════════════════
#  Discovery：默认抓取 SSRF 防护（H2）与共享 session（M4）
# ═══════════════════════════════════════════════════════════════════


class _MockDiscoveryServer:
    """测试 _fetch_json_default 的 mock HTTP 服务器。"""

    def __init__(self):
        from aiohttp import web

        self.app = web.Application()
        self.app.router.add_get("/ok", self._handle_ok)
        self.app.router.add_get("/redirect", self._handle_redirect)
        self.app.router.add_get("/big", self._handle_big)
        self._web = web
        self._runner = None
        self.base_url = ""

    async def _handle_ok(self, request):
        return self._web.json_response({"x": 1})

    async def _handle_redirect(self, request):
        return self._web.Response(
            status=302, headers={"Location": self.base_url + "/ok"}
        )

    async def _handle_big(self, request):
        # 2MB 响应体，超过 1MB 上限
        return self._web.Response(body=b"x" * (2_000_000), content_type="application/json")

    async def start(self):
        from aiohttp import web

        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = self._runner.addresses[0][1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self.base_url

    async def close(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None


class TestDiscoveryFetchDefault(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = _MockDiscoveryServer()
        self.base_url = await self.server.start()

    async def asyncTearDown(self):
        await self.server.close()
        from mcp_integration.oauth.discovery import close_shared_session
        await close_shared_session()

    async def test_rejects_non_http_scheme(self):
        """H2：拒绝非 http/https URL（防 SSRF）。"""
        from mcp_integration.oauth.discovery import (
            OAuthDiscoveryError,
            _fetch_json_default,
        )

        for bad in ("file:///etc/passwd", "ftp://host/x", "http://"):
            with self.assertRaises(OAuthDiscoveryError, msg=bad):
                await _fetch_json_default(bad)

    async def test_does_not_follow_redirect(self):
        """H2：不跟随重定向（302 → 报错，防止跳转内网/云元数据）。"""
        from mcp_integration.oauth.discovery import (
            OAuthDiscoveryError,
            _fetch_json_default,
        )

        with self.assertRaises(OAuthDiscoveryError):
            await _fetch_json_default(self.base_url + "/redirect")

    async def test_limits_response_size(self):
        """H2：响应体超过 1MB 上限报错。"""
        from mcp_integration.oauth.discovery import (
            OAuthDiscoveryError,
            _fetch_json_default,
        )

        with self.assertRaises(OAuthDiscoveryError):
            await _fetch_json_default(self.base_url + "/big")

    async def test_fetch_success(self):
        """正常抓取仍可用。"""
        from mcp_integration.oauth.discovery import _fetch_json_default

        data = await _fetch_json_default(self.base_url + "/ok")
        self.assertEqual(data, {"x": 1})

    async def test_shared_session_reused(self):
        """M4：两次抓取复用同一 session（非 None 且未关闭）。"""
        import mcp_integration.oauth.discovery as discovery_mod
        from mcp_integration.oauth.discovery import _fetch_json_default

        await _fetch_json_default(self.base_url + "/ok")
        s1 = discovery_mod._shared_session
        self.assertIsNotNone(s1)
        await _fetch_json_default(self.base_url + "/ok")
        self.assertIs(discovery_mod._shared_session, s1)


# ═══════════════════════════════════════════════════════════════════
#  Flow：授权码流（PKCE + loopback + token 交换/刷新）
# ═══════════════════════════════════════════════════════════════════


class _MockTokenServer:
    """本地 mock token 端点（aiohttp AppRunner），记录请求并返回模拟 token。"""

    def __init__(self, resource="https://mcp.example.com/mcp"):
        from aiohttp import web

        self.resource = resource
        self.requests = []
        self.app = web.Application()
        self.app.router.add_post("/token", self._handle)
        self._web = web
        self._runner = None
        self._site = None
        self.url = ""

    async def _handle(self, request):
        web = self._web
        form = await request.post()
        self.requests.append(dict(form))
        grant = form.get("grant_type")
        if grant == "authorization_code":
            return web.json_response({
                "access_token": f"at-{form.get('code')}",
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "rt-1",
                "scope": form.get("scope", ""),
            })
        if grant == "refresh_token":
            return web.json_response({
                "access_token": "at-refreshed",
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "rt-2",
                "scope": form.get("scope", ""),
            })
        return web.json_response({"error": "unsupported_grant_type"}, status=400)

    async def start(self):
        from aiohttp import web

        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        port = self._runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{port}"

    async def close(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None


def _make_meta(token_url):
    return OAuthMetadata(
        issuer="https://auth.example.com",
        authorization_endpoint="https://auth.example.com/authorize",
        token_endpoint=token_url,
        code_challenge_methods_supported=["S256"],
        grant_types_supported=["authorization_code", "refresh_token"],
        token_endpoint_auth_methods_supported=["none"],
    )


def _make_client(client_id="cid"):
    from mcp_integration.oauth.models import OAuthClientInformationFull

    return OAuthClientInformationFull(
        client_id=client_id, token_endpoint_auth_method="none",
        redirect_uris=["http://127.0.0.1:0/callback"],
    )


def _fake_browser_factory(records):
    """构造 fake browser：解析 auth_url 中的 redirect_uri/state，自动回调。"""
    async def fake_browser(auth_url):
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(auth_url)
        params = parse_qs(parsed.query)
        redirect_uri = params["redirect_uri"][0]
        state = params["state"][0]
        records.append(auth_url)
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{redirect_uri}?code=TESTCODE&state={state}"):
                pass
    return fake_browser


class TestBuildAuthorizationUrl(unittest.TestCase):
    def test_url_contains_all_params(self):
        from urllib.parse import parse_qs, urlparse

        meta = _make_meta("http://127.0.0.1:1/token")
        pkce = PKCEParameters.generate()
        url = build_authorization_url(
            meta, "cid", "http://127.0.0.1:9999/callback",
            pkce, "STATE123", "https://mcp.example.com/mcp", "a b",
        )
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/authorize")
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["client_id"], ["cid"])
        self.assertEqual(params["redirect_uri"], ["http://127.0.0.1:9999/callback"])
        self.assertEqual(params["code_challenge"], [pkce.code_challenge])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(params["state"], ["STATE123"])
        self.assertEqual(params["resource"], ["https://mcp.example.com/mcp"])
        self.assertEqual(params["scope"], ["a b"])

    def test_scope_omitted_when_empty(self):
        meta = _make_meta("http://127.0.0.1:1/token")
        url = build_authorization_url(
            meta, "cid", "http://127.0.0.1:1/callback",
            PKCEParameters.generate(), "s", "https://mcp.example.com/mcp",
        )
        self.assertNotIn("scope=", url)


class TestLoopbackRedirectServer(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_uri_and_callback(self):
        from mcp_integration.oauth.flow import LoopbackRedirectServer
        import aiohttp

        server = LoopbackRedirectServer(timeout=10)
        redirect_uri = await server.start()
        self.assertTrue(redirect_uri.startswith("http://127.0.0.1:"))
        self.assertTrue(redirect_uri.endswith("/callback"))

        async with aiohttp.ClientSession() as s:
            async with s.get(f"{redirect_uri}?code=ABC&state=STATE1"):
                pass

        code, state = await server.wait_for_callback()
        self.assertEqual(code, "ABC")
        self.assertEqual(state, "STATE1")
        await server.close()

    async def test_timeout(self):
        from mcp_integration.oauth.flow import LoopbackRedirectServer, OAuthFlowTimeoutError

        server = LoopbackRedirectServer(timeout=0.2)
        await server.start()
        with self.assertRaises(OAuthFlowTimeoutError):
            await server.wait_for_callback()
        await server.close()

    async def test_port_conflict_falls_back_to_dynamic(self):
        """H4：固定端口被占用时回退动态端口，授权不失败。"""
        import socket
        from mcp_integration.oauth.flow import LoopbackRedirectServer

        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        occupied = blocker.getsockname()[1]
        try:
            server = LoopbackRedirectServer(timeout=10, port=occupied)
            redirect_uri = await server.start()
            # 成功监听且不在被占用的端口上
            self.assertNotIn(f":{occupied}/callback", redirect_uri)
            self.assertTrue(redirect_uri.startswith("http://127.0.0.1:"))
            await server.close()
        finally:
            blocker.close()

    async def test_forged_state_callback_ignored(self):
        """L3：伪造 state 回调不完成 future，合法回调仍可成功。"""
        from mcp_integration.oauth.flow import LoopbackRedirectServer
        import aiohttp

        server = LoopbackRedirectServer(timeout=10, expected_state="GOOD")
        redirect_uri = await server.start()

        async with aiohttp.ClientSession() as s:
            # 伪造回调：state 不匹配 → 400，不完成 future
            async with s.get(f"{redirect_uri}?code=EVIL&state=BAD") as resp:
                self.assertEqual(resp.status, 400)
            # 合法回调
            async with s.get(f"{redirect_uri}?code=OK&state=GOOD") as resp:
                self.assertEqual(resp.status, 200)

        code, state = await server.wait_for_callback()
        self.assertEqual(code, "OK")
        self.assertEqual(state, "GOOD")
        await server.close()

    def test_run_authorization_flow_no_redirect_uri_param(self):
        """M5：run_authorization_flow 不再接受必失败的 redirect_uri 参数。"""
        import inspect
        from mcp_integration.oauth.flow import run_authorization_flow

        sig = inspect.signature(run_authorization_flow)
        self.assertNotIn("redirect_uri", sig.parameters)


class TestExtractErrorDetail(unittest.TestCase):
    """L7：token 端点错误消息脱敏。"""

    def test_json_error_fields(self):
        from mcp_integration.oauth.flow import _extract_error_detail

        detail = _extract_error_detail(
            '{"error":"invalid_grant","error_description":"token expired"}'
        )
        self.assertEqual(detail, "invalid_grant: token expired")

    def test_masks_sensitive_fields(self):
        from mcp_integration.oauth.flow import _extract_error_detail

        body = '{"error":"x","access_token":"SECRET123","refresh_token":"RT-SECRET"}'
        detail = _extract_error_detail(body)
        self.assertNotIn("SECRET123", detail)
        self.assertNotIn("RT-SECRET", detail)

    def test_non_json_truncated(self):
        from mcp_integration.oauth.flow import _extract_error_detail

        detail = _extract_error_detail("plain text " * 100)
        self.assertLessEqual(len(detail), 200)


class TestTokenExchange(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token_srv = _MockTokenServer()
        await self.token_srv.start()
        self.meta = _make_meta(self.token_srv.url + "/token")
    async def asyncTearDown(self):
        await self.token_srv.close()

    async def test_exchange_code(self):
        token = await exchange_code(
            self.meta, _make_client(), "http://127.0.0.1:1/callback",
            "CODE1", "VERIFIER1", "https://mcp.example.com/mcp",
        )
        self.assertEqual(token.access_token, "at-CODE1")
        self.assertEqual(token.refresh_token, "rt-1")
        self.assertIsNotNone(token.expires_at)
        form = self.token_srv.requests[-1]
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["code_verifier"], "VERIFIER1")
        self.assertEqual(form["resource"], "https://mcp.example.com/mcp")

    async def test_refresh_token(self):
        token = await refresh_token(
            self.meta, _make_client(), "RT-OLD", "https://mcp.example.com/mcp",
        )
        self.assertEqual(token.access_token, "at-refreshed")
        self.assertEqual(token.refresh_token, "rt-2")
        form = self.token_srv.requests[-1]
        self.assertEqual(form["grant_type"], "refresh_token")
        self.assertEqual(form["refresh_token"], "RT-OLD")

    async def test_token_error(self):
        from mcp_integration.oauth.flow import OAuthTokenError

        meta = _make_meta("http://127.0.0.1:1/nonexistent")
        with self.assertRaises(OAuthTokenError):
            await exchange_code(
                meta, _make_client(), "http://127.0.0.1:1/callback",
                "C", "V", "https://mcp.example.com/mcp",
            )


class TestRunAuthorizationFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token_srv = _MockTokenServer()
        await self.token_srv.start()
        self.meta = _make_meta(self.token_srv.url + "/token")

    async def asyncTearDown(self):
        await self.token_srv.close()

    async def test_full_auto_flow(self):
        from mcp_integration.oauth.flow import run_authorization_flow

        records = []
        token = await run_authorization_flow(
            self.meta, _make_client(),
            resource="https://mcp.example.com/mcp",
            scope="a b",
            timeout=10,
            browser_opener=_fake_browser_factory(records),
        )
        self.assertEqual(token.access_token, "at-TESTCODE")
        self.assertTrue(records, "浏览器应被调用")
        # 验证 token 请求携带 resource + PKCE verifier
        form = self.token_srv.requests[-1]
        self.assertEqual(form["resource"], "https://mcp.example.com/mcp")
        self.assertTrue(form["code_verifier"])

    async def test_rejects_non_s256_as(self):
        from mcp_integration.oauth.flow import OAuthFlowError, run_authorization_flow

        meta = _make_meta(self.token_srv.url + "/token")
        meta.code_challenge_methods_supported = ["plain"]
        with self.assertRaises(OAuthFlowError):
            await run_authorization_flow(
                meta, _make_client(), resource="https://mcp.example.com/mcp",
                timeout=5, browser_opener=_fake_browser_factory([]),
            )

    async def test_timeout_when_no_callback(self):
        from mcp_integration.oauth.flow import OAuthFlowTimeoutError, run_authorization_flow

        async def noop_browser(url):
            pass

        with self.assertRaises(OAuthFlowTimeoutError):
            await run_authorization_flow(
                self.meta, _make_client(), resource="https://mcp.example.com/mcp",
                timeout=0.3, browser_opener=noop_browser,
            )

    async def test_state_mismatch_rejected(self):
        from mcp_integration.oauth.flow import OAuthFlowError, run_authorization_flow

        async def evil_browser(auth_url):
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(auth_url)
            redirect_uri = parse_qs(parsed.query)["redirect_uri"][0]
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{redirect_uri}?code=X&state=EVIL"):
                    pass

        with self.assertRaises(OAuthFlowError):
            await run_authorization_flow(
                self.meta, _make_client(), resource="https://mcp.example.com/mcp",
                timeout=5, browser_opener=evil_browser,
            )


if __name__ == "__main__":
    unittest.main()
