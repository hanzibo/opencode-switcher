"""OAuth 2.1 发现模块 — WWW-Authenticate 解析、PRM / AS 元数据发现。

实现 MCP 2025-11-25 Authorization 规范的发现要求：
- 解析 WWW-Authenticate（RFC 9728 §5.1 / RFC 6750 §3）
- PRM 发现：header 优先，well-known URI 回退（RFC 9728 §3.1 路径插入）
- AS 元数据发现：RFC 8414 → OIDC Discovery 回退（含路径插入规则）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from mcp_integration.oauth.models import OAuthMetadata, ProtectedResourceMetadata

logger = logging.getLogger(__name__)

# RFC 9728 注册的 well-known URI 后缀
_PRMW_SUFFIX = "oauth-protected-resource"
# RFC 8414 注册的 well-known URI 后缀
_AS_METADATA_SUFFIX = "oauth-authorization-server"
# OIDC Discovery well-known URI 后缀
_OIDC_SUFFIX = "openid-configuration"

# 元数据文档大小上限（1MB），防止恶意服务器内存放大
_MAX_METADATA_BYTES = 1_000_000
# 元数据文档仅允许 http/https
_ALLOWED_SCHEMES = ("http", "https")


class OAuthDiscoveryError(Exception):
    """OAuth 发现过程错误。"""


# ── WWW-Authenticate 解析 ─────────────────────────────────────────


@dataclass
class WwwAuthenticateChallenge:
    """解析后的 WWW-Authenticate Bearer challenge。"""

    scheme: str = ""
    error: str = ""
    error_description: str = ""
    scope: str = ""
    resource_metadata: str = ""

    @property
    def is_oauth_challenge(self) -> bool:
        """是否为需要走 OAuth 流程的 challenge。

        RFC 9728 §5.1：含 resource_metadata 即表明受保护资源发布了 PRM；
        Bearer scheme + error 也足以触发重认证（401 时）。
        """
        return bool(self.resource_metadata) or self.scheme.lower() == "bearer"


def parse_www_authenticate(value: str) -> WwwAuthenticateChallenge:
    """解析 WWW-Authenticate header 值。

    支持 Bearer scheme + 逗号分隔参数：
        Bearer error="invalid_token", resource_metadata="https://...", scope="a b"

    参数值可能带引号（含空格/逗号）也可能不带。
    """
    result = WwwAuthenticateChallenge()
    if not value:
        return result

    # 提取 scheme（第一个 token）
    parts = value.split(None, 1)
    result.scheme = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    # 逐参数解析：兼容带引号值中的逗号
    for key, val in _parse_auth_params(rest):
        if key == "error":
            result.error = val
        elif key == "error_description":
            result.error_description = val
        elif key == "scope":
            result.scope = val
        elif key == "resource_metadata":
            result.resource_metadata = val
    return result


def _parse_auth_params(rest: str) -> List[Tuple[str, str]]:
    """解析 auth-param 列表，正确处理带引号的值。"""
    params: List[Tuple[str, str]] = []
    i = 0
    n = len(rest)
    while i < n:
        # 跳过空白与逗号
        while i < n and (rest[i].isspace() or rest[i] == ","):
            i += 1
        if i >= n:
            break
        # key
        eq = rest.find("=", i)
        if eq < 0:
            break
        key = rest[i:eq].strip().strip('"')
        i = eq + 1
        # value（可能带引号）
        if i < n and rest[i] == '"':
            i += 1
            buf = []
            while i < n and rest[i] != '"':
                buf.append(rest[i])
                i += 1
            i += 1  # 跳过闭合引号
            val = "".join(buf)
        else:
            j = i
            while j < n and rest[j] != ",":
                j += 1
            val = rest[i:j].strip()
            i = j
        params.append((key, val))
    return params


# ── URL 构造 ───────────────────────────────────────────────────────


def build_protected_resource_metadata_urls(server_url: str) -> List[str]:
    """按 RFC 9728 §3.1 构造 PRM well-known URL 候选列表（含路径插入 + 根）。

    server_url 如 https://mcp.smithery.ai/jibo96701436 →
        https://mcp.smithery.ai/.well-known/oauth-protected-resource/jibo96701436
        https://mcp.smithery.ai/.well-known/oauth-protected-resource
    """
    candidates: List[str] = []
    parsed = urlparse(server_url)
    if not parsed.scheme or not parsed.netloc:
        return candidates
    base = f"{parsed.scheme}://{parsed.netloc}"
    # 去掉尾部斜杠，避免生成 //.well-known
    path = parsed.path.rstrip("/")
    if path:
        candidates.append(f"{base}/.well-known/{_PRMW_SUFFIX}{path}")
    candidates.append(f"{base}/.well-known/{_PRMW_SUFFIX}")
    return candidates


def build_oauth_metadata_urls(issuer: str) -> List[str]:
    """按 RFC 8414 §3.1 + 兼容性说明构造 AS 元数据 URL 候选列表。

    无路径 issuer（https://auth.example.com）：
        1. https://auth.example.com/.well-known/oauth-authorization-server
        2. https://auth.example.com/.well-known/openid-configuration

    有路径 issuer（https://auth.example.com/tenant1）：
        1. https://auth.example.com/.well-known/oauth-authorization-server/tenant1
        2. https://auth.example.com/.well-known/openid-configuration/tenant1
        3. https://auth.example.com/tenant1/.well-known/openid-configuration
    """
    candidates: List[str] = []
    parsed = urlparse(issuer)
    if not parsed.scheme or not parsed.netloc:
        return candidates
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    if path:
        candidates.append(f"{base}/.well-known/{_AS_METADATA_SUFFIX}{path}")
        candidates.append(f"{base}/.well-known/{_OIDC_SUFFIX}{path}")
        candidates.append(f"{base}{path}/.well-known/{_OIDC_SUFFIX}")
    else:
        candidates.append(f"{base}/.well-known/{_AS_METADATA_SUFFIX}")
        candidates.append(f"{base}/.well-known/{_OIDC_SUFFIX}")
    return candidates


def canonical_server_uri(server_url: str) -> str:
    """计算 MCP Server 规范 URI（RFC 8707 §2，MCP 规范「Canonical Server URI」）。

    - 小写 scheme/host
    - 移除 fragment
    - 移除尾随斜杠（除非是根路径）
    - 保留 path 与 port
    """
    parsed = urlparse(server_url)
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()
    path = parsed.path
    if path and path != "/" and path.endswith("/"):
        path = path[:-1]
    if not path:
        path = ""
    return f"{scheme}://{host}{path}"


# ── 发现流程 ───────────────────────────────────────────────────────


async def discover_protected_resource_metadata(
    server_url: str,
    www_authenticate: Optional[str] = None,
    *,
    fetch_json=None,
) -> Optional[ProtectedResourceMetadata]:
    """发现受保护资源元数据（PRM）。

    优先级（RFC 9728 §5.1 + MCP 规范）：
    1. WWW-Authenticate 的 resource_metadata 参数（当 header 提供时）
    2. well-known URI 候选（路径插入 → 根）

    Parameters
    ----------
    server_url : str
        MCP Server 的 HTTP 端点 URL。
    www_authenticate : str, optional
        HTTP 401/403 响应中的 WWW-Authenticate header 值。
    fetch_json : callable, optional
        异步 GET JSON 的注入函数（测试用）；默认为 aiohttp 实现。

    Returns
    -------
    ProtectedResourceMetadata or None
        发现成功返回元数据；全部失败返回 None。
    """
    if fetch_json is None:
        fetch_json = _fetch_json_default

    # 1. header 提供的 resource_metadata URL
    urls: List[str] = []
    if www_authenticate:
        challenge = parse_www_authenticate(www_authenticate)
        if challenge.resource_metadata:
            urls.append(challenge.resource_metadata)

    # 2. well-known 候选
    urls.extend(build_protected_resource_metadata_urls(server_url))

    for url in urls:
        try:
            data = await fetch_json(url)
            prm = ProtectedResourceMetadata.from_dict(data)
            if not prm.resource:
                logger.debug("PRM 缺少 resource 字段，忽略: %s", url)
                continue
            # 校验 resource 与请求 URL 一致（RFC 9728 §3.3 防冒充）
            expected = canonical_server_uri(server_url)
            if prm.resource != expected:
                logger.warning(
                    "PRM resource 不匹配: %s != %s（忽略 %s）",
                    prm.resource, expected, url,
                )
                continue
            logger.debug("PRM 发现成功: %s → AS=%s", url, prm.authorization_servers)
            return prm
        except Exception as e:
            logger.debug("PRM 获取失败 %s: %s", url, e)
    return None


async def discover_oauth_metadata(
    issuer: str,
    *,
    fetch_json=None,
) -> Optional[OAuthMetadata]:
    """发现授权服务器元数据（RFC 8414 → OIDC 回退）。

    Parameters
    ----------
    issuer : str
        PRM authorization_servers 中的 AS issuer URL。
    fetch_json : callable, optional
        异步 GET JSON 的注入函数（测试用）。

    Returns
    -------
    OAuthMetadata or None
    """
    if fetch_json is None:
        fetch_json = _fetch_json_default

    for url in build_oauth_metadata_urls(issuer):
        try:
            data = await fetch_json(url)
            meta = OAuthMetadata.from_dict(data)
            if not meta.authorization_endpoint or not meta.token_endpoint:
                logger.debug("AS 元数据缺少关键端点，忽略: %s", url)
                continue
            logger.debug("AS 元数据发现成功: %s", url)
            return meta
        except Exception as e:
            logger.debug("AS 元数据获取失败 %s: %s", url, e)
    return None


# ── 默认抓取实现 ─────────────────────────────────────────────────

_shared_session: Any = None  # 懒创建的 aiohttp.ClientSession（连接池复用）


async def _get_shared_session() -> Any:
    """获取共享 aiohttp session（首次调用创建；连接池/SSL 上下文复用）。

    M4：避免每次发现新建 ClientSession（单次授权最多 5 次 TCP+TLS 握手）。
    """
    import aiohttp

    global _shared_session
    if _shared_session is None or _shared_session.closed:
        _shared_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20)
        )
    return _shared_session


async def close_shared_session() -> None:
    """关闭共享 session（应用退出/测试清理时调用）。"""
    global _shared_session
    if _shared_session is not None and not _shared_session.closed:
        await _shared_session.close()
    _shared_session = None


async def _fetch_json_default(url: str) -> Dict[str, Any]:
    """默认异步 GET JSON 实现（aiohttp，共享 session）。

    安全约束（RFC 9728 §7.7 SSRF 防护）：
    - 仅允许 http/https scheme 且必须有主机
    - 不跟随重定向（服务器可控 URL 可能 302 到内网/云元数据）
    - 响应体大小上限 1MB
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.netloc:
        raise OAuthDiscoveryError(f"非法发现 URL: {url}")

    session = await _get_shared_session()
    async with session.get(url, allow_redirects=False) as resp:
        if resp.status != 200:
            raise OAuthDiscoveryError(f"HTTP {resp.status} @ {url}")
        body = await resp.read()
        if len(body) > _MAX_METADATA_BYTES:
            raise OAuthDiscoveryError(f"元数据文档过大 @ {url}")
        return json.loads(body.decode("utf-8"))
