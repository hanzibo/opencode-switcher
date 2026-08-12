"""OAuth 2.1 / RFC 9728 数据模型 — MCP 授权流程使用的数据类。

对齐 MCP 2025-11-25 Authorization 规范：
- ProtectedResourceMetadata（RFC 9728 受保护资源元数据）
- OAuthMetadata（RFC 8414 授权服务器元数据）
- OAuthToken（access/refresh token）
- PKCEParameters（S256 证明密钥）
- OAuthClientInformationFull（注册后的客户端信息，RFC 7591）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ProtectedResourceMetadata:
    """OAuth 2.0 Protected Resource Metadata（RFC 9728 §2）。"""

    resource: str = ""
    authorization_servers: List[str] = field(default_factory=list)
    scopes_supported: List[str] = field(default_factory=list)
    bearer_methods_supported: List[str] = field(default_factory=list)
    resource_name: str = ""
    resource_documentation: str = ""
    jwks_uri: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ProtectedResourceMetadata":
        return cls(
            resource=str(d.get("resource", "") or ""),
            authorization_servers=list(d.get("authorization_servers") or []),
            scopes_supported=list(d.get("scopes_supported") or []),
            bearer_methods_supported=list(d.get("bearer_methods_supported") or []),
            resource_name=str(d.get("resource_name", "") or ""),
            resource_documentation=str(d.get("resource_documentation", "") or ""),
            jwks_uri=str(d.get("jwks_uri", "") or ""),
        )


@dataclass
class OAuthMetadata:
    """OAuth 2.0 Authorization Server Metadata（RFC 8414 §2）/ OIDC Discovery。

    仅保留 MCP 授权流程关心的字段，未知字段忽略。
    """

    issuer: str = ""
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    registration_endpoint: str = ""
    jwks_uri: str = ""
    scopes_supported: List[str] = field(default_factory=list)
    response_types_supported: List[str] = field(default_factory=list)
    grant_types_supported: List[str] = field(default_factory=list)
    token_endpoint_auth_methods_supported: List[str] = field(default_factory=list)
    code_challenge_methods_supported: List[str] = field(default_factory=list)
    client_id_metadata_document_supported: bool = False

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OAuthMetadata":
        return cls(
            issuer=str(d.get("issuer", "") or ""),
            authorization_endpoint=str(d.get("authorization_endpoint", "") or ""),
            token_endpoint=str(d.get("token_endpoint", "") or ""),
            registration_endpoint=str(d.get("registration_endpoint", "") or ""),
            jwks_uri=str(d.get("jwks_uri", "") or ""),
            scopes_supported=list(d.get("scopes_supported") or []),
            response_types_supported=list(d.get("response_types_supported") or []),
            grant_types_supported=list(d.get("grant_types_supported") or []),
            token_endpoint_auth_methods_supported=list(
                d.get("token_endpoint_auth_methods_supported") or []
            ),
            code_challenge_methods_supported=list(
                d.get("code_challenge_methods_supported") or []
            ),
            client_id_metadata_document_supported=bool(
                d.get("client_id_metadata_document_supported", False)
            ),
        )

    def supports_pkce_s256(self) -> bool:
        """AS 是否声明支持 PKCE S256（MCP 规范要求客户端先校验再继续）。"""
        return "S256" in self.code_challenge_methods_supported

    def supports_refresh_token(self) -> bool:
        return "refresh_token" in self.grant_types_supported


@dataclass
class OAuthToken:
    """OAuth 2.1 Token 响应（RFC 6749 §5.1）。"""

    access_token: str = ""
    token_type: str = "Bearer"
    expires_in: Optional[int] = None
    refresh_token: str = ""
    scope: str = ""
    # 本地计算的过期时刻（epoch 秒）；不持久化，由 token_store 重建
    expires_at: Optional[float] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OAuthToken":
        return cls(
            access_token=str(d.get("access_token", "") or ""),
            token_type=str(d.get("token_type", "Bearer") or "Bearer"),
            expires_in=d.get("expires_in"),
            refresh_token=str(d.get("refresh_token", "") or ""),
            scope=str(d.get("scope", "") or ""),
            expires_at=d.get("expires_at"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "scope": self.scope,
            "expires_at": self.expires_at,
        }

    def is_expired(self, leeway: float = 30.0) -> bool:
        """token 是否已过期（带 leeway 秒的提前量）。无过期时间视为不过期。"""
        if not self.expires_at:
            return False
        return time.time() >= (self.expires_at - leeway)

    def is_valid(self, leeway: float = 30.0) -> bool:
        return bool(self.access_token) and not self.is_expired(leeway)


@dataclass
class PKCEParameters:
    """PKCE（RFC 7636）参数。"""

    code_verifier: str = ""
    code_challenge: str = ""
    code_challenge_method: str = "S256"

    @classmethod
    def generate(cls) -> "PKCEParameters":
        """生成新的 PKCE 参数（S256）。

        code_verifier：43–128 位，取 128 位随机 unreserved 字符；
        code_challenge：verifier 的 SHA-256 → base64url 去 padding。
        """
        import base64
        import hashlib
        import secrets
        import string

        alphabet = string.ascii_letters + string.digits + "-._~"
        code_verifier = "".join(secrets.choice(alphabet) for _ in range(128))
        digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
        return cls(
            code_verifier=code_verifier,
            code_challenge=code_challenge,
            code_challenge_method="S256",
        )

    def is_complete(self) -> bool:
        return bool(self.code_verifier) and bool(self.code_challenge)


@dataclass
class OAuthClientInformationFull:
    """注册/预注册后的客户端信息（RFC 7591 §2 响应 + 本地字段）。"""

    client_id: str = ""
    client_secret: str = ""
    token_endpoint_auth_method: str = "none"
    redirect_uris: List[str] = field(default_factory=list)
    grant_types: List[str] = field(default_factory=list)
    response_types: List[str] = field(default_factory=list)
    client_name: str = "opencode-switcher"
    # 注册是否来自 DCR（true）或预注册/用户输入（false）
    dynamically_registered: bool = False

    @classmethod
    def from_registration_response(cls, d: Dict[str, Any]) -> "OAuthClientInformationFull":
        return cls(
            client_id=str(d.get("client_id", "") or ""),
            client_secret=str(d.get("client_secret", "") or ""),
            token_endpoint_auth_method=str(
                d.get("token_endpoint_auth_method", "none") or "none"
            ),
            redirect_uris=list(d.get("redirect_uris") or []),
            grant_types=list(d.get("grant_types") or []),
            response_types=list(d.get("response_types") or []),
            client_name=str(d.get("client_name", "opencode-switcher") or "opencode-switcher"),
            dynamically_registered=True,
        )
