"""MCP 集成包 — 为 AI 面板提供 MCP（Model Context Protocol）支持。

通过此包，AI 面板可以：
- 连接外部 MCP Server，扩展工具生态
- 聚合多个 Server 的工具列表供 LLM 选择
- 将工具调用路由到对应的 MCP Server 执行
"""

from mcp_integration.gtk_asyncio_bridge import GtkAsyncioBridge
from mcp_integration.server_config import MCPServerConfig
from mcp_integration.client_manager import MCPClientManager
from mcp_integration.tool_adapter import (
    mcp_tool_to_openai_schema,
    parse_mcp_tool_name,
)
from mcp_integration.transports.http import HttpTransport

__all__ = [
    "GtkAsyncioBridge",
    "MCPServerConfig",
    "MCPClientManager",
    "mcp_tool_to_openai_schema",
    "parse_mcp_tool_name",
    "HttpTransport",
]
