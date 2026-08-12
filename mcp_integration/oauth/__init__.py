"""OAuth 2.1 / RFC 9728 客户端认证子包。

实现 MCP 2025-11-25 Authorization 规范的客户端侧 OAuth 2.1 授权码流
（PRM/AS 发现、PKCE、token 持久化与刷新、401/403 自动重认证）。
纯 asyncio + aiohttp 实现，不依赖 anyio（Python 3.14 兼容）。
"""

from mcp_integration.oauth.models import (
    OAuthMetadata,
    OAuthToken,
    PKCEParameters,
    ProtectedResourceMetadata,
    OAuthClientInformationFull,
)
from mcp_integration.oauth.discovery import (
    WwwAuthenticateChallenge,
    build_oauth_metadata_urls,
    build_protected_resource_metadata_urls,
    canonical_server_uri,
    discover_oauth_metadata,
    discover_protected_resource_metadata,
    parse_www_authenticate,
)

__all__ = [
    "OAuthMetadata",
    "OAuthToken",
    "PKCEParameters",
    "ProtectedResourceMetadata",
    "OAuthClientInformationFull",
    "WwwAuthenticateChallenge",
    "build_oauth_metadata_urls",
    "build_protected_resource_metadata_urls",
    "canonical_server_uri",
    "discover_oauth_metadata",
    "discover_protected_resource_metadata",
    "parse_www_authenticate",
]
