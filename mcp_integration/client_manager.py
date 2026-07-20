"""MCP 客户端管理器 — 管理多个 MCP Server 连接的生命周期与工具调用。

使用分层架构：
  StdioTransport → JsonRpcSession → MCPSession → MCPClientManager
  或
  HttpTransport → JsonRpcSession → MCPSession → MCPClientManager
"""

from __future__ import annotations

import asyncio
import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

from mcp_integration.gtk_asyncio_bridge import GtkAsyncioBridge
from mcp_integration.server_config import MCPServerConfig
from mcp_integration.tool_adapter import (
    mcp_tool_to_openai_schema,
)

if TYPE_CHECKING:
    from mcp_integration.mcp_session import MCPSession

logger = logging.getLogger(__name__)

# 连接状态变化回调类型
StatusCallback = Callable[[str, str], None]
# str = server_name, str = 状态描述 ("connected" / "disconnected" / "error:xxx")


class MCPClientManager:
    """管理多个 MCP Server 连接的生命周期与工具调用。"""

    def __init__(self, bridge: Optional[GtkAsyncioBridge] = None) -> None:
        self._bridge = bridge or GtkAsyncioBridge.get()
        # session 字典：name -> MCPSession
        self._sessions: Dict[str, MCPSession] = {}
        # 连接配置缓存（用于重连）
        self._configs: Dict[str, MCPServerConfig] = {}
        # 连接状态回调
        self._status_callbacks: List[StatusCallback] = []
        # 自动重连定时器
        self._reconnect_timers: Dict[str, asyncio.Task] = {}

    # ── 状态回调 ────────────────────────────────────────────────

    def add_status_callback(self, callback: StatusCallback) -> None:
        """添加连接状态变化监听器。"""
        self._status_callbacks.append(callback)

    def remove_status_callback(self, callback: StatusCallback) -> None:
        """移除连接状态变化监听器。"""
        if callback in self._status_callbacks:
            self._status_callbacks.remove(callback)

    def _notify_status(self, name: str, status: str) -> None:
        """通知所有监听器连接状态已变化。"""
        for cb in self._status_callbacks:
            try:
                cb(name, status)
            except Exception as e:
                logger.warning("状态回调异常: %s", e)

    # ── 连接管理 ────────────────────────────────────────────────

    async def connect_stdio(self, config: MCPServerConfig) -> Tuple[bool, str]:
        """通过 stdio 连接到本地 MCP Server。"""
        # 清除旧连接及重连定时器
        await self._cleanup_connection(config.name)

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
            self._configs[config.name] = config
            self._notify_status(config.name, "connected")
            logger.info("MCP Server 已连接: %s (%s)", config.name, info)
            return True, f"Server '{config.name}' 已连接 ({info})"
        except Exception as e:
            await session.close()
            err_msg = f"连接失败: {e}"
            self._notify_status(config.name, f"error:{e}")
            return False, err_msg

    async def connect_http(self, config: MCPServerConfig) -> Tuple[bool, str]:
        """通过 Streamable HTTP 连接到远程 MCP Server。

        使用现有分层架构：HttpTransport → JsonRpcSession → MCPSession。
        """
        # 清除旧连接及重连定时器
        await self._cleanup_connection(config.name)

        from mcp_integration.transports.http import HttpTransport
        from mcp_integration.mcp_session import MCPSession
        from mcp_integration.json_rpc import JsonRpcSession

        # 构建 HttpTransport
        transport = HttpTransport(
            url=config.url,
            api_key=config.api_key,
            protocol_version=config.protocol_version,
            enable_2026_headers=config.enable_2026_headers,
        )
        jrpc = JsonRpcSession(transport)
        session = MCPSession(jrpc)

        try:
            await session.connect()
            info = await session.initialize()

            # initialize 成功后，将 session_id 注入 transport（2025-11-25 协议）
            if hasattr(transport, "update_session_id"):
                transport.update_session_id(info or "")

            self._sessions[config.name] = session
            self._configs[config.name] = config
            self._notify_status(config.name, "connected")
            logger.info("MCP HTTP Server 已连接: %s (%s)", config.name, info)
            return True, f"Server '{config.name}' 已连接 ({info})"
        except Exception as e:
            await session.close()
            err_msg = f"连接失败: {e}"
            self._notify_status(config.name, f"error:{e}")
            logger.error("MCP HTTP Server 连接失败: %s - %s", config.name, e)
            return False, err_msg

    async def connect_by_config(self, config: MCPServerConfig) -> Tuple[bool, str]:
        """根据传输方式自动选择连接方法。"""
        if config.transport == "http":
            return await self.connect_http(config)
        else:
            return await self.connect_stdio(config)

    async def disconnect(self, name: str) -> Tuple[bool, str]:
        """断开指定 Server 的连接。"""
        # 取消重连定时器
        self._cancel_reconnect(name)

        session = self._sessions.pop(name, None)
        self._configs.pop(name, None)
        if session is None:
            return False, f"Server '{name}' 不存在"
        await session.close()
        self._notify_status(name, "disconnected")
        logger.info("MCP Server 已断开: %s", name)
        return True, f"Server '{name}' 已断开"

    async def disconnect_all(self) -> None:
        """断开所有连接。"""
        names = list(self._sessions.keys())
        for name in names:
            await self.disconnect(name)

    async def reconnect(self, name: str) -> Tuple[bool, str]:
        """重新连接指定 Server。"""
        config = self._configs.get(name)
        if config is None:
            return False, f"Server '{name}' 没有保存的配置"
        return await self.connect_by_config(config)

    # ── 自动重连 ────────────────────────────────────────────────

    def _cancel_reconnect(self, name: str) -> None:
        """取消指定 Server 的重连定时器。"""
        task = self._reconnect_timers.pop(name, None)
        if task and not task.done():
            task.cancel()

    async def _auto_reconnect_loop(self, name: str, config: MCPServerConfig) -> None:
        """自动重连循环（指数退避）。"""
        delays = [1, 2, 4, 8, 15, 30]
        attempt = 0
        while attempt < len(delays):
            await asyncio.sleep(delays[attempt])
            # 检查是否已连接（可能被手动重连）
            if name not in self._configs:
                return
            if name in self._sessions and self._sessions[name].is_connected:
                return
            logger.info("自动重连 %s（尝试 %d/%d）", name, attempt + 1, len(delays))
            try:
                ok, msg = await self.connect_by_config(config)
                if ok:
                    logger.info("自动重连成功: %s", name)
                    return
                logger.warning("自动重连失败 %s: %s", name, msg)
            except Exception as e:
                logger.warning("自动重连异常 %s: %s", name, e)
            attempt += 1
        logger.warning("自动重连已达最大尝试次数: %s", name)

    def _schedule_reconnect(self, name: str) -> None:
        """调度自动重连。"""
        config = self._configs.get(name)
        if config is None or not config.auto_connect:
            return
        self._cancel_reconnect(name)
        task = asyncio.create_task(self._auto_reconnect_loop(name, config))
        self._reconnect_timers[name] = task

    async def _cleanup_connection(self, name: str) -> None:
        """清除指定 Server 的旧连接和重连定时器。"""
        self._cancel_reconnect(name)
        old = self._sessions.pop(name, None)
        if old:
            await old.close()

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

    # ── 健康检测 ────────────────────────────────────────────────

    async def ping_server(self, name: str) -> bool:
        """检测指定 Server 是否存活。

        利用 MCPSession.ping() 发送 ping 请求。
        """
        session = self._sessions.get(name)
        if session is None or not session.is_connected:
            return False
        return await session.ping()

    async def ping_all(self) -> Dict[str, bool]:
        """检测所有已连接 Server 的健康状态。"""
        results: Dict[str, bool] = {}
        for name in list(self._sessions.keys()):
            try:
                results[name] = await self.ping_server(name)
            except Exception as e:
                results[name] = False
                logger.warning("ping %s 异常: %s", name, e)
        return results

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

    def get_server_status(self, name: str) -> str:
        """获取指定 Server 的状态描述。"""
        if not self.is_connected(name):
            return "disconnected"
        return "connected"

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
