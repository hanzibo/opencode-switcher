"""MCP Server 连接配置数据模型。

支持两种传输方式：
- stdio：通过子进程标准输入/输出通信（本地）
- http：通过 Streamable HTTP 协议通信（远程）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MCPServerConfig:
    """单个 MCP Server 的配置项。

    支持两种传输方式：
    - stdio：本地子进程
    - http：远程 Streamable HTTP
    """

    # 唯一标识符（用于工具命名空间和 UI 显示）
    name: str = ""
    # 传输方式："stdio" | "http"
    transport: str = "stdio"

    # ── stdio 模式参数 ──
    command: str = ""
    args: List[str] = field(default_factory=list)
    cwd: Optional[str] = None

    # ── http 模式参数 ──
    url: str = ""
    api_key: str = ""
    # HTTP 认证类型："none" | "bearer" | "oauth2"
    auth_type: str = "bearer"
    # OAuth 2.1 参数（预留）
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_token_url: str = ""

    # ── 协议参数 ──
    protocol_version: str = "2025-11-25"
    enable_2026_headers: bool = False

    # ── 通用 ──
    enabled: bool = True
    auto_connect: bool = True
    # 最近一次连接状态：空 "disconnected" | "connected" | "error:xxx"
    last_status: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": self.args,
            "cwd": self.cwd,
            "url": self.url,
            "api_key": self.api_key,
            "auth_type": self.auth_type,
            "oauth_client_id": self.oauth_client_id,
            "oauth_client_secret": self.oauth_client_secret,
            "oauth_token_url": self.oauth_token_url,
            "protocol_version": self.protocol_version,
            "enable_2026_headers": self.enable_2026_headers,
            "enabled": self.enabled,
            "auto_connect": self.auto_connect,
            "last_status": self.last_status,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MCPServerConfig":
        valid_keys = {
            "name", "transport", "command", "args", "cwd",
            "url", "api_key", "auth_type",
            "oauth_client_id", "oauth_client_secret", "oauth_token_url",
            "protocol_version", "enable_2026_headers",
            "enabled", "auto_connect", "last_status",
        }
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        # 向后兼容：旧配置可能没有这些字段
        filtered.setdefault("auth_type", "bearer")
        filtered.setdefault("protocol_version", "2025-11-25")
        filtered.setdefault("enable_2026_headers", False)
        filtered.setdefault("oauth_client_id", "")
        filtered.setdefault("oauth_client_secret", "")
        filtered.setdefault("oauth_token_url", "")
        filtered.setdefault("last_status", "")
        return cls(**filtered)

    def validate(self) -> Optional[str]:
        """验证配置是否合法。返回 None 表示合法，返回字符串表示错误信息。"""
        if not self.name.strip():
            return "服务器名称不能为空"
        if self.transport not in ("stdio", "http"):
            return f"不支持的传输方式: {self.transport}"
        if self.transport == "stdio":
            if not self.command.strip():
                return "stdio 模式需要指定命令"
        elif self.transport == "http":
            if not self.url.strip():
                return "http 模式需要指定 URL"
            if not self.url.startswith(("http://", "https://")):
                return "http 模式 URL 需以 http:// 或 https:// 开头"
        # 验证协议版本
        if self.protocol_version not in ("2024-11-05", "2025-03-26", "2025-11-25", "2026-07-28", "draft"):
            return f"不支持的协议版本: {self.protocol_version}"
        return None
