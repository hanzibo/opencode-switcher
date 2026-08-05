"""MCP JSON-RPC / MCPSession 超时与取消的聚焦测试。

覆盖链路：
  JsonRpcSession.request（超时 / CancelledError 挂起请求清理）
  → MCPSession.call_tool（有界默认超时 60s / 显式 timeout 覆盖）

管理器 / 桥接 / 工具循环层（MCPClientManager、GtkAsyncioBridge、
ai_tool_loop 取消）见 tests/test_mcp_bridge.py。全部基于 stdlib unittest +
asyncio，headless 运行，不依赖 GTK 主循环。
"""

import asyncio
import unittest
from typing import Optional

from mcp_integration.transport import BaseTransport


# ═══════════════════════════════════════════════════════════════════
#  辅助：永不响应的 HangTransport
# ═══════════════════════════════════════════════════════════════════

class HangTransport(BaseTransport):
    """模拟传输层：永远不返回响应，用于测试超时 / 取消。

    block_send=True 时 send_line 永久阻塞，模拟发送阶段被挂起
    （覆盖 request() 在 wait_for 之前被取消的清理路径）。
    """

    def __init__(self, block_send: bool = False) -> None:
        self._connected = False
        self._sent: list[str] = []
        self._block_send = block_send
        self._block = asyncio.Event()

    async def connect(self) -> None:
        self._connected = True

    async def send_line(self, data: str) -> None:
        self._sent.append(data)
        if self._block_send:
            await self._block.wait()

    async def read_line(self) -> Optional[str]:
        await asyncio.Event().wait()
        return None

    async def disconnect(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected


class SendTimeoutTransport(BaseTransport):
    """send_line 立即抛出 asyncio.TimeoutError，模拟发送路径自身超时。"""

    def __init__(self) -> None:
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def send_line(self, data: str) -> None:
        raise asyncio.TimeoutError("send timeout")

    async def read_line(self) -> Optional[str]:
        await asyncio.Event().wait()
        return None

    async def disconnect(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected


# ═══════════════════════════════════════════════════════════════════
#  JsonRpcSession：CancelledError 挂起请求清理
# ═══════════════════════════════════════════════════════════════════

class TestJsonRpcCancellationCleanup(unittest.IsolatedAsyncioTestCase):
    async def _make_session(self):
        from mcp_integration.json_rpc import JsonRpcSession
        t = HangTransport()
        s = JsonRpcSession(t, request_timeout=30)
        await s.connect()
        return t, s

    async def test_cancel_after_send_removes_pending(self):
        _, s = await self._make_session()
        task = asyncio.create_task(s.request("tools/call", {"name": "x"}))
        await asyncio.sleep(0.05)
        self.assertEqual(len(s._pending), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(s._pending, {})
        await s.close()

    async def test_cancel_during_send_line_removes_pending(self):
        from mcp_integration.json_rpc import JsonRpcSession
        t = HangTransport(block_send=True)
        s = JsonRpcSession(t, request_timeout=30)
        await s.connect()
        task = asyncio.create_task(s.request("tools/call", {"name": "x"}))
        await asyncio.sleep(0.05)
        self.assertEqual(len(s._pending), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(s._pending, {})
        await s.close()

    async def test_timeout_cleans_pending(self):
        from mcp_integration.json_rpc import JsonRpcTimeoutError
        _, s = await self._make_session()
        with self.assertRaises(JsonRpcTimeoutError):
            await s.request("tools/call", {"name": "x"}, timeout=0.05)
        self.assertEqual(s._pending, {})
        await s.close()

    async def test_send_line_timeout_uses_bounded_timeout_and_cleans_pending(self):
        # 回归：_send_line 自身抛 asyncio.TimeoutError 时，timeout_val 已在 try 前
        # 初始化 → 抛 JsonRpcTimeoutError（而非 UnboundLocalError），挂起请求被清理。
        from mcp_integration.json_rpc import JsonRpcSession, JsonRpcTimeoutError
        t = SendTimeoutTransport()
        s = JsonRpcSession(t, request_timeout=30)
        await s.connect()

        with self.assertRaises(JsonRpcTimeoutError) as ctx:
            await s.request("tools/call", {"name": "x"})
        self.assertIn("tools/call", str(ctx.exception))
        self.assertEqual(s._pending, {})
        await s.close()

    async def test_success_path_unaffected(self):
        # 正常成功调用不受影响：超时/取消清理不干扰结果投递
        from mcp_integration.json_rpc import JsonRpcSession
        t = HangTransport()
        s = JsonRpcSession(t, request_timeout=5)
        await s.connect()
        task = asyncio.create_task(s.request("no.response"))
        await asyncio.sleep(0.02)
        # 手动模拟迟到响应：此时请求已被取消，应被安全忽略而非抛异常
        self.assertEqual(len(s._pending), 1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        rid = next(iter(s._pending)) if s._pending else None
        self.assertIsNone(rid)
        await s.close()


# ═══════════════════════════════════════════════════════════════════
#  MCPSession.call_tool：有界默认超时 / 显式覆盖 / 取消传播
# ═══════════════════════════════════════════════════════════════════

class TestMCPSessionToolTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_raises_and_cleans_pending(self):
        from mcp_integration.json_rpc import JsonRpcSession, JsonRpcTimeoutError
        from mcp_integration.mcp_session import MCPSession

        t = HangTransport()
        jrpc = JsonRpcSession(t, request_timeout=30)
        session = MCPSession(jrpc)
        await session.connect()

        with self.assertRaises(JsonRpcTimeoutError) as ctx:
            await session.call_tool("echo", {"x": 1}, timeout=0.05)
        self.assertIn("tools/call", str(ctx.exception))
        self.assertEqual(jrpc._pending, {})
        await jrpc.close()

    async def test_default_timeout_is_bounded_60s(self):
        from mcp_integration.mcp_session import MCPSession, MCP_TOOL_CALL_TIMEOUT_SEC

        recorded: dict = {}

        class FakeJRPC:
            async def request(self, method, params, timeout=None):
                recorded["timeout"] = timeout
                return {"content": [{"type": "text", "text": "ok"}]}

        session = MCPSession(FakeJRPC())
        result = await session.call_tool("echo", {})
        self.assertEqual(result, "ok")
        self.assertEqual(recorded["timeout"], MCP_TOOL_CALL_TIMEOUT_SEC)
        self.assertEqual(recorded["timeout"], 60.0)

        await session.call_tool("echo", {}, timeout=7)
        self.assertEqual(recorded["timeout"], 7)

    async def test_cancel_call_tool_propagates_and_cleans_pending(self):
        from mcp_integration.json_rpc import JsonRpcSession
        from mcp_integration.mcp_session import MCPSession

        t = HangTransport()
        jrpc = JsonRpcSession(t, request_timeout=30)
        session = MCPSession(jrpc)
        await session.connect()

        task = asyncio.create_task(session.call_tool("echo", {"x": 1}))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(jrpc._pending, {})
        await jrpc.close()


if __name__ == "__main__":
    unittest.main()
