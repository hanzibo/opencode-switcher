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


if __name__ == "__main__":
    unittest.main()
