"""MCP Server 连接配置数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MCPServerConfig:
    """单个 MCP Server 的配置项。

    支持两种传输方式：
    - stdio：通过子进程标准输入/输出通信（本地）
    - http：通过 HTTP 协议通信（远程，预留）
    """

    # 唯一标识符（用于工具命名空间和 UI 显示）
    name: str = ""
    # 传输方式："stdio" | "http"
    transport: str = "stdio"
    # stdio 模式：可执行文件路径（如 "npx"、"uvx"、"python3"）
    command: str = ""
    # stdio 模式：命令行参数列表
    args: List[str] = field(default_factory=list)
    # stdio 模式：工作目录（可选）
    cwd: Optional[str] = None
    # http 模式：服务器 URL（预留）
    url: str = ""
    # http 模式：Bearer token（预留）
    api_key: str = ""
    # 是否启用
    enabled: bool = True
    # 启动时自动连接
    auto_connect: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": self.args,
            "cwd": self.cwd,
            "url": self.url,
            "api_key": self.api_key,
            "enabled": self.enabled,
            "auto_connect": self.auto_connect,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MCPServerConfig":
        valid_keys = {
            "name", "transport", "command", "args", "cwd",
            "url", "api_key", "enabled", "auto_connect",
        }
        filtered = {k: v for k, v in d.items() if k in valid_keys}
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
        return None
