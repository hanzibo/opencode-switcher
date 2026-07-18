"""MCP 客户端管理器 — 管理多个 MCP Server 连接的生命周期与工具调用。

使用分层架构：
  StdioTransport → JsonRpcSession → MCPSession → MCPClientManager
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from mcp_integration.gtk_asyncio_bridge import GtkAsyncioBridge
from mcp_integration.server_config import MCPServerConfig
from mcp_integration.tool_adapter import (
    mcp_tool_to_openai_schema,
)

logger = logging.getLogger(__name__)


class MCPClientManager:
    """管理多个 MCP Server 连接的生命周期与工具调用。"""

    def __init__(self, bridge: Optional[GtkAsyncioBridge] = None) -> None:
        self._bridge = bridge or GtkAsyncioBridge.get()
        # session 字典：name -> MCPSession
        self._sessions: Dict[str, Any] = {}

    # ── 连接管理 ────────────────────────────────────────────────

    async def connect_stdio(self, config: MCPServerConfig) -> Tuple[bool, str]:
        """通过 stdio 连接到本地 MCP Server。"""
        if config.name in self._sessions:
            existing = self._sessions[config.name]
            if existing.is_connected:
                return False, f"Server '{config.name}' 已连接"
            # 有旧连接但已断开，先清理
            await existing.close()

        from mcp_integration.transports.stdio import StdioTransport
        from mcp_integration.mcp_session import MCPSession
        from mcp_integration.json_rpc import JsonRpcSession

        transport = StdioTransport(config.command, config.args)
        jrpc = JsonRpcSession(transport)
        session = MCPSession(jrpc)

        try:
            await session.connect()
            info = await session.initialize()
            self._sessions[config.name] = session
            logger.info("MCP Server 已连接: %s (%s)", config.name, info)
            return True, f"Server '{config.name}' 已连接 ({info})"
        except Exception as e:
            await session.close()
            return False, f"连接失败: {e}"

    async def disconnect(self, name: str) -> Tuple[bool, str]:
        """断开指定 Server 的连接。"""
        session = self._sessions.pop(name, None)
        if session is None:
            return False, f"Server '{name}' 不存在"
        await session.close()
        logger.info("MCP Server 已断开: %s", name)
        return True, f"Server '{name}' 已断开"

    async def disconnect_all(self) -> None:
        """断开所有连接。"""
        names = list(self._sessions.keys())
        for name in names:
            await self.disconnect(name)

    # ── 工具发现 ────────────────────────────────────────────────

    async def list_tools(self, server_name: str) -> List[dict]:
        """获取指定 Server 的工具列表。"""
        session = self._sessions.get(server_name)
        if session is None or not session.is_connected:
            return []
        try:
            return await session.list_tools()
        except Exception as e:
            logger.error("获取工具列表失败: %s", e)
            return []

    async def list_all_tools(self) -> List[Dict[str, Any]]:
        """聚合所有已连接 Server 的工具，返回 OpenAI schema 列表。"""
        result: List[Dict[str, Any]] = []
        for name, session in self._sessions.items():
            if not session.is_connected:
                continue
            tools = await self.list_tools(name)
            for tool in tools:
                schema = mcp_tool_to_openai_schema(name, self._tool_dict_to_obj(tool))
                result.append(schema)
        return result

    async def refresh_all_tools(self) -> None:
        """刷新所有 Server 的工具缓存。"""
        for name in self._sessions:
            await self.list_tools(name)

    # ── 工具调用 ────────────────────────────────────────────────

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> str:
        """在指定 Server 上调用工具。"""
        session = self._sessions.get(server_name)
        if session is None:
            return f"❌ MCP Server '{server_name}' 未连接"
        if not session.is_connected:
            return f"❌ MCP Server '{server_name}' 已断开"

        try:
            return await session.call_tool(tool_name, arguments)
        except Exception as e:
            logger.error("调用 %s:%s 失败: %s", server_name, tool_name, e)
            return f"❌ MCP 工具 '{server_name}:{tool_name}' 执行异常: {e}"

    async def call_tool_by_name(self, tool_name: str, arguments: dict) -> str:
        """仅按工具名在所有已连接 Server 中查找并调用。"""
        for server_name, session in self._sessions.items():
            if not session.is_connected:
                continue
            try:
                tools = await self.list_tools(server_name)
                if any(t.get("name") == tool_name for t in tools):
                    return await session.call_tool(tool_name, arguments)
            except Exception:
                continue
        return f"❌ 找不到 MCP 工具 '{tool_name}'"

    # ── 状态查询 ────────────────────────────────────────────────

    @property
    def bridge(self) -> GtkAsyncioBridge:
        """获取底层的 asyncio 桥接器引用（供 ai_tool_loop 使用）。"""
        return self._bridge

    def is_connected(self, name: str) -> bool:
        session = self._sessions.get(name)
        return session is not None and session.is_connected

    def get_connected_servers(self) -> List[str]:
        return [name for name, s in self._sessions.items() if s.is_connected]

    def get_server_count(self) -> int:
        return len(self.get_connected_servers())

    def get_all_server_names(self) -> List[str]:
        return list(self._sessions.keys())

    # ── 辅助 ────────────────────────────────────────────────────

    @staticmethod
    def _tool_dict_to_obj(tool_dict: dict):
        """将工具 dict 转为 MCP Tool 对象（用于 tool_adapter）。"""
        from mcp import Tool
        return Tool(
            name=tool_dict.get("name", ""),
            description=tool_dict.get("description", ""),
            inputSchema=tool_dict.get("inputSchema", {"type": "object", "properties": {}}),
        )
