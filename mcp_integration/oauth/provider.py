"""AuthProvider — HTTP 传输层的认证策略抽象。

两种实现：
- StaticBearerAuthProvider：固定 api_key（现有行为，向后兼容）
- OAuth2AuthProvider：OAuth 2.1 全自动（发现 → 注册 → PKCE → token 管理 → 401/403 重认证）

HttpTransport 在每次请求前调用 get_auth_headers() 注入凭据；
收到 401/403 时调用 handle_challenge() 尝试自动重认证并返回新 headers。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Protocol

from mcp_integration.oauth.discovery import (
    canonical_server_uri,
    discover_oauth_metadata,
    discover_protected_resource_metadata,
    parse_www_authenticate,
)
from mcp_integration.oauth.flow import (
    OAuthFlowError,
    refresh_token,
    run_authorization_flow,
)
from mcp_integration.oauth.models import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from mcp_integration.oauth.registration import (
    OAuthRegistrationError,
    dynamic_register,
)
from mcp_integration.oauth.token_store import OAuthTokenStore, _safe_filename

logger = logging.getLogger(__name__)

# DCR 场景的固定回调端口（桌面 OAuth 客户端惯例；可配置避免冲突）
DEFAULT_CALLBACK_PORT = 39876


class AuthProvider(Protocol):
    """HTTP 传输层认证策略接口。"""

    async def get_auth_headers(self) -> Dict[str, str]:
        """返回当前请求应携带的认证 headers（无认证返回空 dict）。"""
        ...

    async def handle_challenge(
        self, www_authenticate: str = "",
    ) -> Optional[Dict[str, str]]:
        """处理 401/403 challenge。

        返回新的认证 headers（重试请求用）；无法自动解决返回 None。
        """
        ...


# ── 静态 Bearer ────────────────────────────────────────────────────


class StaticBearerAuthProvider:
    """固定 API key（Bearer token）认证。"""

    def __init__(self, api_key: str = "") -> None:
        self._api_key = (api_key or "").strip()

    async def get_auth_headers(self) -> Dict[str, str]:
        if not self._api_key:
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}

    async def handle_challenge(self, www_authenticate: str = "") -> Optional[Dict[str, str]]:
        # 静态凭据无法自动解决 401/403
        return None


# ── OAuth 2.1 ──────────────────────────────────────────────────────


class OAuth2AuthProvider:
    """OAuth 2.1 全自动认证（PKCE 授权码流 + token 持久化/刷新）。

    Parameters
    ----------
    server_url : str
        MCP Server 的 HTTP 端点 URL（也是 RFC 8707 resource 参数来源）。
    client_id : str, optional
        预注册 client_id（提供则跳过 DCR）。
    client_secret : str, optional
        预注册 client_secret（public client 留空）。
    token_url : str, optional
        覆盖 token 端点（默认从 AS 元数据发现）。
    scopes : str, optional
        请求的 scope（空格分隔）；优先于 PRM scopes_supported。
    callback_port : int
        回调服务器固定端口（DCR 注册 redirect_uri 使用）。
    config_dir : str, optional
        token 存储根目录（测试注入）。
    browser_opener : callable, optional
        浏览器打开器注入（测试用）。
    flow_timeout : float
        授权流总超时秒数。
    """

    def __init__(
        self,
        server_url: str,
        *,
        client_id: str = "",
        client_secret: str = "",
        token_url: str = "",
        scopes: str = "",
        callback_port: int = DEFAULT_CALLBACK_PORT,
        config_dir: Optional[str] = None,
        browser_opener: Optional[Callable[[str], Any]] = None,
        flow_timeout: float = 300.0,
    ) -> None:
        self._server_url = server_url
        self._pre_client_id = (client_id or "").strip()
        self._pre_client_secret = (client_secret or "").strip()
        self._override_token_url = (token_url or "").strip()
        self._configured_scopes = (scopes or "").strip()
        self._callback_port = callback_port
        self._browser_opener = browser_opener
        self._flow_timeout = flow_timeout

        # M6：token 存储以 canonical URL 为 key，URL 微调不丢失授权状态
        self._store = OAuthTokenStore(
            canonical_server_uri(server_url), config_dir=config_dir,
        )

        # 内存缓存
        self._current_token: Optional[OAuthToken] = None
        self._prm: Optional[ProtectedResourceMetadata] = None
        self._oauth_metadata: Optional[OAuthMetadata] = None
        self._client: Optional[OAuthClientInformationFull] = None
        self._lock: Any = None  # asyncio.Lock，首次使用时创建

    @property
    def store(self) -> OAuthTokenStore:
        return self._store

    @property
    def server_url(self) -> str:
        return self._server_url

    # ── AuthProvider 接口 ────────────────────────────────────────

    async def get_auth_headers(self) -> Dict[str, str]:
        token = await self._ensure_token()
        return {"Authorization": f"Bearer {token.access_token}"}

    async def handle_challenge(
        self, www_authenticate: str = "",
    ) -> Optional[Dict[str, str]]:
        """401/403 时自动重认证（token 失效 / scope 不足）。

        策略（对齐 _ensure_token）：先尝试静默刷新（401 常因 token 被吊销 /
        服务端侧过期，持有 refresh_token 时应优先复用），刷新失败或不可用
        才走完整浏览器授权流，避免用户被无谓弹窗。
        """
        if self._lock is None:
            import asyncio
            self._lock = asyncio.Lock()
        async with self._lock:
            # 1. 尝试静默刷新
            candidate = self._current_token
            if candidate is None:
                stored = self._store.load()
                if stored:
                    self._client = stored.get("client")
                    self._oauth_metadata = stored.get("oauth_metadata")
                    candidate = stored.get("token")
            if candidate is not None:
                refreshed = await self._try_refresh(candidate)
                if refreshed is not None:
                    return {"Authorization": f"Bearer {refreshed.access_token}"}

            # 2. 刷新失败/不可用 → 完整授权流
            self._current_token = None
            token = await self._authorize(www_authenticate=www_authenticate)
            return {"Authorization": f"Bearer {token.access_token}"}

    # ── token 生命周期 ───────────────────────────────────────────

    async def _ensure_token(self) -> OAuthToken:
        """返回有效 token：内存 → 磁盘 → 刷新 → 完整授权流。"""
        if self._lock is None:
            import asyncio
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._current_token and self._current_token.is_valid():
                return self._current_token

            # 从磁盘加载
            stored = self._store.load()
            if stored:
                self._client = stored.get("client")
                self._oauth_metadata = stored.get("oauth_metadata")
                token = stored.get("token")
                if token and token.is_valid():
                    self._current_token = token
                    return token
                # 尝试刷新
                refreshed = await self._try_refresh(token)
                if refreshed is not None:
                    return refreshed

            # 无可用 token → 完整授权流
            return await self._authorize()

    async def _try_refresh(self, token: Optional[OAuthToken]) -> Optional[OAuthToken]:
        """用 refresh_token 刷新；成功返回新 token，失败返回 None。"""
        if not token or not token.refresh_token:
            return None
        if not self._oauth_metadata or not self._client:
            return None
        try:
            resource = canonical_server_uri(self._server_url)
            scope = self._resolve_scope("")
            new_token = await refresh_token(
                self._oauth_metadata, self._client, token.refresh_token,
                resource, scope=scope,
            )
            if new_token is None:
                return None
            self._current_token = new_token
            self._store.save(
                new_token, client=self._client, oauth_metadata=self._oauth_metadata
            )
            logger.info("OAuth token 刷新成功: %s", self._server_url)
            return new_token
        except Exception as e:
            logger.warning("OAuth token 刷新失败，将重走授权: %s", e)
            return None

    # ── 完整授权流程 ─────────────────────────────────────────────

    async def _authorize(self, www_authenticate: str = "") -> OAuthToken:
        """发现 → 注册 → PKCE 授权码流 → 持久化。"""
        # 1. 发现 AS
        if not self._oauth_metadata:
            await self._discover(www_authenticate)

        meta = self._oauth_metadata
        assert meta is not None

        # 2. 客户端注册
        if not self._client:
            self._client = await self._obtain_client(meta)

        # 3. 授权码流
        resource = canonical_server_uri(self._server_url)
        scope = self._resolve_scope(www_authenticate)
        try:
            token = await run_authorization_flow(
                meta, self._client, resource, scope=scope,
                callback_port=self._callback_port,
                timeout=self._flow_timeout,
                browser_opener=self._browser_opener,
            )
        except OAuthFlowError:
            raise
        except Exception as e:
            raise OAuthFlowError(f"授权码流执行失败: {e}")

        self._current_token = token
        self._store.save(token, client=self._client, oauth_metadata=meta)
        logger.info("OAuth 授权完成: %s", self._server_url)
        return token

    async def _discover(self, www_authenticate: str = "") -> None:
        """发现 PRM + AS 元数据；支持 token_url 覆盖。"""
        prm = await discover_protected_resource_metadata(
            self._server_url, www_authenticate=www_authenticate or None,
        )
        if prm is not None:
            self._prm = prm

        # 收集 issuer 候选：PRM authorization_servers → 服务器 URL 本身
        issuers: list = []
        if prm and prm.authorization_servers:
            issuers.extend(prm.authorization_servers)
        if not issuers:
            # 兜底：以 server 自身为 issuer 尝试发现
            issuers.append(self._server_url)

        meta: Optional[OAuthMetadata] = None
        for issuer in issuers:
            meta = await discover_oauth_metadata(issuer)
            if meta is not None:
                break
        if meta is None:
            raise OAuthFlowError(
                f"无法发现授权服务器元数据: {self._server_url}"
            )

        # token_url 覆盖（用户手动配置）
        if self._override_token_url:
            meta.token_endpoint = self._override_token_url
        self._oauth_metadata = meta

    async def _obtain_client(self, meta: OAuthMetadata) -> OAuthClientInformationFull:
        """按优先级获取客户端信息：预注册 → DCR。"""
        # 1. 预注册
        if self._pre_client_id:
            auth_method = (
                "client_secret_post" if self._pre_client_secret else "none"
            )
            return OAuthClientInformationFull(
                client_id=self._pre_client_id,
                client_secret=self._pre_client_secret,
                token_endpoint_auth_method=auth_method,
                redirect_uris=[f"http://127.0.0.1:{self._callback_port}/callback"],
            )

        # 2. DCR
        if meta.registration_endpoint:
            try:
                redirect_uri = f"http://127.0.0.1:{self._callback_port}/callback"
                return await dynamic_register(
                    meta.registration_endpoint,
                    redirect_uris=[redirect_uri],
                )
            except OAuthRegistrationError as e:
                logger.warning("DCR 失败: %s", e)

        raise OAuthFlowError(
            "无法获取客户端凭据：AS 不支持动态注册且未配置 client_id。"
            "请在设置中填写 OAuth Client ID。"
        )

    # ── scope 选择 ───────────────────────────────────────────────

    def _resolve_scope(self, www_authenticate: str = "") -> str:
        """Scope Selection Strategy（MCP 规范）：

        1. 401/403 challenge 的 scope 参数（最权威）
        2. 用户配置的 scopes
        3. PRM scopes_supported
        4. 空
        """
        if www_authenticate:
            ch = parse_www_authenticate(www_authenticate)
            if ch.scope:
                return ch.scope
        if self._configured_scopes:
            return self._configured_scopes
        if self._prm and self._prm.scopes_supported:
            return " ".join(self._prm.scopes_supported)
        return ""
