"""MCP 客户端管理器 — 基于 asyncio 的纯 JSON-RPC 实现。

不依赖 anyio，直接在 asyncio 上实现 MCP stdio 传输，
避免 MCP SDK v1.28 在 Python 3.14 上的兼容性问题。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from mcp_integration.gtk_asyncio_bridge import GtkAsyncioBridge
from mcp_integration.server_config import MCPServerConfig
from mcp_integration.tool_adapter import (
    mcp_tool_to_openai_schema,
)

logger = logging.getLogger(__name__)


class _MCPClient:
    """单个 MCP Server 的底层连接。

    直接实现 JSON-RPC 2.0 协议，通过 asyncio 子进程 stdin/stdout 通信。
    """

    def __init__(self) -> None:
        self._process: Optional[asyncio.subprocess.Process] = None
        self._request_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._tools_cache: Optional[List[dict]] = None

    @property
    def is_connected(self) -> bool:
        return (self._process is not None
                and self._process.returncode is None
                and self._reader_task is not None
                and not self._reader_task.done())

    async def connect_stdio(self, command: str, args: List[str]) -> str:
        """启动子进程并完成 MCP 初始化握手。

        Returns
        -------
        str
            Server info 描述（成功）或错误信息。
        """
        # 流缓冲区 10MB，避免 Playwright 大 JSON（截图/base64）溢出
        self._process = await asyncio.create_subprocess_exec(
            command, *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,
        )

        # 启动后台读取任务
        self._reader_task = asyncio.create_task(self._reader())

        # 1. 发送 initialize 请求
        server_info = await self._request("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "opencode-switcher", "version": "1.0"},
        })
        # 2. 发送 initialized 通知
        await self._notify("notifications/initialized")

        name = server_info.get("serverInfo", {}).get("name", "unknown")
        version = server_info.get("serverInfo", {}).get("version", "?")
        return f"{name} v{version}"

    async def list_tools(self) -> List[dict]:
        """获取工具列表。

        Returns
        -------
        list of dict
            每个工具包含 name, description, inputSchema 等字段。
        """
        result = await self._request("tools/list")
        tools = result.get("tools", [])
        self._tools_cache = tools
        return tools

    async def call_tool(self, name: str, arguments: dict) -> str:
        """调用工具并返回文本结果。

        Parameters
        ----------
        name : str
            工具名称。
        arguments : dict
            工具参数。

        Returns
        -------
        str
            工具结果的文本表示。
        """
        result = await self._request("tools/call", {
            "name": name,
            "arguments": arguments,
        })
        content = result.get("content", [])
        is_error = result.get("isError", False)

        texts = []
        for item in content:
            if isinstance(item, dict):
                texts.append(item.get("text", json.dumps(item, ensure_ascii=False)))
            else:
                texts.append(str(item))
        text = "\n".join(texts)
        if is_error:
            text = f"❌ {text}"
        return text

    async def disconnect(self) -> None:
        """断开连接，清理资源。"""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                if self._process and self._process.returncode is None:
                    self._process.kill()
                    await self._process.wait()

        # 清理所有等待中的请求
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("连接已关闭"))
        self._pending.clear()

    # ── JSON-RPC 消息 ──────────────────────────────────────────

    async def _request(self, method: str, params: dict = None) -> dict:
        """发送请求并等待响应。"""
        self._request_id += 1
        req_id = self._request_id

        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut

        msg = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            msg["params"] = params
        await self._send_line(msg)

        try:
            response = await asyncio.wait_for(fut, timeout=120)
            return response
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"MCP 请求 '{method}' 超时")

    async def _notify(self, method: str, params: dict = None) -> None:
        """发送通知（无需响应）。"""
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send_line(msg)

    async def _send_line(self, obj: dict) -> None:
        """发送一行 JSON 到子进程 stdin。"""
        if not self._process or not self._process.stdin:
            raise RuntimeError("MCP 未连接")
        data = json.dumps(obj, ensure_ascii=False) + "\n"
        self._process.stdin.write(data.encode("utf-8"))
        await self._process.stdin.drain()

    async def _reader(self) -> None:
        """后台任务：从子进程 stdout 读取 JSON-RPC 响应。"""
        try:
            assert self._process and self._process.stdout
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("MCP 非JSON输出（已忽略）: %s", line[:100])
                    continue

                self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("MCP reader 异常: %s", e)
        finally:
            # 通知所有等待中的请求连接已断开
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("MCP 连接已断开"))
            self._pending.clear()

    def _dispatch(self, msg: dict) -> None:
        """分发收到的消息到对应的等待者。"""
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in self._pending:
            fut = self._pending.pop(msg_id)
            if "error" in msg:
                err = msg["error"]
                fut.set_exception(RuntimeError(
                    f"MCP 错误 [{err.get('code')}]: {err.get('message')}"
                ))
            else:
                fut.set_result(msg.get("result", {}))


# ═══════════════════════════════════════════════════════════════════
#  MCP 客户端管理器
# ═══════════════════════════════════════════════════════════════════

class MCPClientManager:
    """管理多个 MCP Server 连接的生命周期与工具调用。"""

    def __init__(self, bridge: Optional[GtkAsyncioBridge] = None) -> None:
        self._bridge = bridge or GtkAsyncioBridge.get()
        self._clients: Dict[str, _MCPClient] = {}

    # ── 连接管理 ────────────────────────────────────────────────

    async def connect_stdio(self, config: MCPServerConfig) -> Tuple[bool, str]:
        """通过 stdio 连接到本地 MCP Server。"""
        if config.name in self._clients:
            existing = self._clients[config.name]
            if existing.is_connected:
                return False, f"Server '{config.name}' 已连接"
            # 有旧连接但已断开（如 reader 崩溃），先清理
            await existing.disconnect()
            del self._clients[config.name]

        client = _MCPClient()
        try:
            info = await client.connect_stdio(config.command, config.args)
            self._clients[config.name] = client
            logger.info("MCP Server 已连接: %s (%s)", config.name, info)
            return True, f"Server '{config.name}' 已连接 ({info})"
        except Exception as e:
            await client.disconnect()
            return False, f"连接失败: {e}"

    async def disconnect(self, name: str) -> Tuple[bool, str]:
        """断开指定 Server 的连接。"""
        client = self._clients.pop(name, None)
        if client is None:
            return False, f"Server '{name}' 不存在"
        await client.disconnect()
        logger.info("MCP Server 已断开: %s", name)
        return True, f"Server '{name}' 已断开"

    async def disconnect_all(self) -> None:
        """断开所有连接。"""
        names = list(self._clients.keys())
        for name in names:
            await self.disconnect(name)

    # ── 工具发现 ────────────────────────────────────────────────

    async def list_tools(self, server_name: str) -> List[dict]:
        """获取指定 Server 的工具列表。"""
        client = self._clients.get(server_name)
        if client is None or not client.is_connected:
            return []
        try:
            return await client.list_tools()
        except Exception as e:
            logger.error("获取工具列表失败: %s", e)
            return []

    async def list_all_tools(self) -> List[Dict[str, Any]]:
        """聚合所有已连接 Server 的工具，返回 OpenAI schema 列表。"""
        result: List[Dict[str, Any]] = []
        for name, client in self._clients.items():
            if not client.is_connected:
                continue
            tools = await self.list_tools(name)
            for tool in tools:
                schema = mcp_tool_to_openai_schema(name, self._tool_dict_to_obj(tool))
                result.append(schema)
        return result

    async def refresh_all_tools(self) -> None:
        """刷新所有 Server 的工具缓存。"""
        for name in self._clients:
            await self.list_tools(name)

    # ── 工具调用 ────────────────────────────────────────────────

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> str:
        """在指定 Server 上调用工具。"""
        client = self._clients.get(server_name)
        if client is None:
            return f"❌ MCP Server '{server_name}' 未连接"
        if not client.is_connected:
            return f"❌ MCP Server '{server_name}' 已断开"

        try:
            return await client.call_tool(tool_name, arguments)
        except Exception as e:
            logger.error("调用 %s:%s 失败: %s", server_name, tool_name, e)
            return f"❌ MCP 工具 '{server_name}:{tool_name}' 执行异常: {e}"

    async def call_tool_by_name(self, tool_name: str, arguments: dict) -> str:
        """仅按工具名在所有已连接 Server 中查找并调用。

        用于 LLM 未携带 ``server:`` 前缀时回退。
        若有多个 Server 提供同名工具，使用第一个匹配的。
        """
        for server_name, client in self._clients.items():
            if not client.is_connected:
                continue
            try:
                tools = await self.list_tools(server_name)
                if any(t.get("name") == tool_name for t in tools):
                    return await client.call_tool(tool_name, arguments)
            except Exception:
                continue

        return f"❌ 找不到 MCP 工具 '{tool_name}'"

    # ── 状态查询 ────────────────────────────────────────────────

    @property
    def bridge(self) -> GtkAsyncioBridge:
        """获取底层的 asyncio 桥接器引用（供 ai_tool_loop 使用）。"""
        return self._bridge

    def is_connected(self, name: str) -> bool:
        client = self._clients.get(name)
        return client is not None and client.is_connected

    def get_connected_servers(self) -> List[str]:
        return [name for name, c in self._clients.items() if c.is_connected]

    def get_server_count(self) -> int:
        return len(self.get_connected_servers())

    def get_all_server_names(self) -> List[str]:
        return list(self._clients.keys())

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
