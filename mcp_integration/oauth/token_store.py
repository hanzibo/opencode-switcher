"""OAuth token 持久化 — 按 MCP Server 分别存储于 0o600 权限文件。

存储位置：~/.config/opencode-switcher/mcp_oauth/<server>.json

沿用项目 GmailOAuthStore 的 os.open(0o600) 安全写入模式。
存储内容：
- token：OAuthToken（access/refresh/expires_at 等）
- client：OAuthClientInformationFull（DCR 注册结果或预注册信息，避免重复注册）
- oauth_metadata：OAuthMetadata（AS 元数据缓存，避免每次启动重新发现）
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from mcp_integration.oauth.models import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
)

logger = logging.getLogger(__name__)

# 默认配置目录（与项目 stores/clipboard_store.py CONFIG_DIR 一致）
DEFAULT_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "opencode-switcher")
OAUTH_DIRNAME = "mcp_oauth"

# server name → 文件名安全化：仅保留 [A-Za-z0-9_-]，其余转 _
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_filename(server_name: str) -> str:
    return _SAFE_NAME_RE.sub("_", server_name).strip("_") or "server"


class OAuthTokenStore:
    """MCP Server 的 OAuth token 存储。

    Parameters
    ----------
    server_name : str
        MCP Server 名称（用于生成唯一存储文件名）。
    config_dir : str, optional
        配置根目录；测试可注入临时目录。
    """

    def __init__(self, server_name: str, config_dir: Optional[str] = None) -> None:
        self._server_name = server_name
        self._config_dir = config_dir or DEFAULT_CONFIG_DIR
        self._dir = os.path.join(self._config_dir, OAUTH_DIRNAME)

    @property
    def path(self) -> str:
        return os.path.join(self._dir, f"{_safe_filename(self._server_name)}.json")

    # ── 读取 ─────────────────────────────────────────────────────

    def load(self) -> Optional[Dict[str, Any]]:
        """加载存储的 token/client/metadata。

        Returns
        -------
        dict or None
            {"token": OAuthToken, "client": OAuthClientInformationFull|None,
             "oauth_metadata": OAuthMetadata|None}
            文件不存在或损坏返回 None。
        """
        if not os.path.isfile(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            token_data = data.get("token") or {}
            token = OAuthToken.from_dict(token_data) if token_data.get("access_token") else None
            client = None
            client_data = data.get("client")
            if client_data:
                client = OAuthClientInformationFull(
                    client_id=str(client_data.get("client_id", "") or ""),
                    client_secret=str(client_data.get("client_secret", "") or ""),
                    token_endpoint_auth_method=str(
                        client_data.get("token_endpoint_auth_method", "none") or "none"
                    ),
                    redirect_uris=list(client_data.get("redirect_uris") or []),
                    grant_types=list(client_data.get("grant_types") or []),
                    response_types=list(client_data.get("response_types") or []),
                    client_name=str(client_data.get("client_name", "opencode-switcher") or "opencode-switcher"),
                    dynamically_registered=bool(client_data.get("dynamically_registered", False)),
                )
            meta = None
            meta_data = data.get("oauth_metadata")
            if meta_data:
                meta = OAuthMetadata.from_dict(meta_data)
            return {"token": token, "client": client, "oauth_metadata": meta}
        except Exception as e:
            logger.warning("OAuth token 加载失败 %s: %s", self.path, e)
            return None

    def has_token(self) -> bool:
        """是否存在有效（未过期）的 access token。"""
        data = self.load()
        if not data or not data.get("token"):
            return False
        return data["token"].is_valid()

    # ── 写入 ─────────────────────────────────────────────────────

    def save(
        self,
        token: OAuthToken,
        client: Optional[OAuthClientInformationFull] = None,
        oauth_metadata: Optional[OAuthMetadata] = None,
    ) -> None:
        """保存 token（及可选的客户端信息、AS 元数据缓存）。

        安全写入模式（对齐 stores/clipboard_store.py）：同目录临时文件
        mode=0o600 → flush + fsync → os.replace() 原子替换，保证：
        - 已存在文件权限位也被强制重置为 0o600（os.open mode 仅新建时生效）
        - 读取方永远不会观察到半写文件
        """
        os.makedirs(self._dir, mode=0o700, exist_ok=True)
        payload: Dict[str, Any] = {"token": token.to_dict()}
        if client is not None:
            payload["client"] = {
                "client_id": client.client_id,
                "client_secret": client.client_secret,
                "token_endpoint_auth_method": client.token_endpoint_auth_method,
                "redirect_uris": client.redirect_uris,
                "grant_types": client.grant_types,
                "response_types": client.response_types,
                "client_name": client.client_name,
                "dynamically_registered": client.dynamically_registered,
            }
        if oauth_metadata is not None:
            payload["oauth_metadata"] = {
                "issuer": oauth_metadata.issuer,
                "authorization_endpoint": oauth_metadata.authorization_endpoint,
                "token_endpoint": oauth_metadata.token_endpoint,
                "registration_endpoint": oauth_metadata.registration_endpoint,
                "jwks_uri": oauth_metadata.jwks_uri,
                "scopes_supported": oauth_metadata.scopes_supported,
                "response_types_supported": oauth_metadata.response_types_supported,
                "grant_types_supported": oauth_metadata.grant_types_supported,
                "token_endpoint_auth_methods_supported": oauth_metadata.token_endpoint_auth_methods_supported,
                "code_challenge_methods_supported": oauth_metadata.code_challenge_methods_supported,
                "client_id_metadata_document_supported": oauth_metadata.client_id_metadata_document_supported,
            }
        tmp_path = self.path + ".tmp"
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(tmp_path, flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
            logger.debug("OAuth token 已保存: %s", self.path)
        except Exception as e:
            logger.error("OAuth token 保存失败 %s: %s", self.path, e)
            # 清理临时文件，避免残留半写状态
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def clear(self) -> None:
        """删除存储文件。"""
        try:
            if os.path.isfile(self.path):
                os.remove(self.path)
                logger.debug("OAuth token 已清除: %s", self.path)
        except Exception as e:
            logger.warning("OAuth token 清除失败 %s: %s", self.path, e)

    # ── 状态 ─────────────────────────────────────────────────────

    def get_status(self) -> str:
        """授权状态描述（供 UI 显示）：未授权 / 已授权 / 已过期。"""
        data = self.load()
        if not data or not data.get("token"):
            return "未授权"
        token = data["token"]
        if not token.access_token:
            return "未授权"
        if token.is_expired():
            return "已过期" if token.refresh_token else "未授权"
        return "已授权"
