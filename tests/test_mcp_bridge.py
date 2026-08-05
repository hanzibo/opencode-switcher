"""MCP 桥接层与工具循环取消/透传的聚焦测试。

覆盖链路：
  → MCPClientManager.call_tool / call_tool_by_name（timeout 透传）
  → GtkAsyncioBridge.run_coroutine（cancel_event 取消传播）
  → ai_tool_loop._execute_tool_call（取消 → TOOL_CANCELLED，异常格式化不变）

与 tests/test_mcp_timeout.py（JsonRpcSession / MCPSession 超时与取消）互补，
本文件只关注管理器、桥接与工具循环层。全部基于 stdlib unittest + asyncio，
headless 运行，不依赖 GTK 主循环。
"""

import asyncio
import threading
import unittest
from types import SimpleNamespace


async def _quick_coroutine() -> int:
    return 42


def _make_hang_coroutine(started: threading.Event):
    async def _hang() -> None:
        started.set()
        await asyncio.sleep(30)
    return _hang()


# ═══════════════════════════════════════════════════════════════════
#  MCPClientManager：timeout 透传
# ═══════════════════════════════════════════════════════════════════

class TestMCPClientManagerTimeoutPassThrough(unittest.IsolatedAsyncioTestCase):
    async def _make_manager(self):
        from mcp_integration.client_manager import MCPClientManager

        recorded: dict = {}

        class FakeSession:
            @property
            def is_connected(self) -> bool:
                return True

            async def list_tools(self):
                return [{"name": "echo"}]

            async def call_tool(self, tool_name, arguments, timeout=None):
                recorded["tool_name"] = tool_name
                recorded["arguments"] = arguments
                recorded["timeout"] = timeout
                return "ok"

        mgr = MCPClientManager()
        mgr._sessions["srv"] = FakeSession()
        return mgr, recorded

    async def test_call_tool_passes_explicit_timeout(self):
        mgr, recorded = await self._make_manager()
        result = await mgr.call_tool("srv", "echo", {"x": 1}, timeout=5)
        self.assertEqual(result, "ok")
        self.assertEqual(recorded["tool_name"], "echo")
        self.assertEqual(recorded["timeout"], 5)

    async def test_call_tool_default_timeout_is_passthrough_none(self):
        # 不传 timeout → 透传 None，由 MCPSession 应用有界默认值
        mgr, recorded = await self._make_manager()
        await mgr.call_tool("srv", "echo", {"x": 1})
        self.assertIsNone(recorded["timeout"])

    async def test_call_tool_by_name_passes_timeout(self):
        mgr, recorded = await self._make_manager()
        result = await mgr.call_tool_by_name("echo", {"x": 1}, timeout=9)
        self.assertEqual(result, "ok")
        self.assertEqual(recorded["timeout"], 9)


# ═══════════════════════════════════════════════════════════════════
#  GtkAsyncioBridge.run_coroutine：cancel_event 取消传播
# ═══════════════════════════════════════════════════════════════════

class TestGtkAsyncioBridgeCancellation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from mcp_integration.gtk_asyncio_bridge import GtkAsyncioBridge
        cls.bridge = GtkAsyncioBridge.get()
        cls.bridge.start()

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()

    def _run_in_thread(self, coro, cancel_event):
        from mcp_integration.gtk_asyncio_bridge import CoroutineCancelledError
        results: dict = {}

        def worker():
            try:
                results["value"] = self.bridge.run_coroutine(
                    coro, cancel_event=cancel_event,
                )
                results["returned"] = "completed"
            except CoroutineCancelledError:
                results["returned"] = "cancelled"
            except Exception as e:
                results["returned"] = f"error:{e}"

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return t, results

    def test_cancel_event_cancels_pending_coroutine(self):
        cancel_event = threading.Event()
        started = threading.Event()
        t, results = self._run_in_thread(_make_hang_coroutine(started), cancel_event)

        self.assertTrue(started.wait(timeout=2))
        self.assertTrue(t.is_alive())  # run_coroutine 仍阻塞等待
        cancel_event.set()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(results.get("returned"), "cancelled")

    def test_cancel_event_unsets_returns_result(self):
        # 传入 cancel_event 但未置位 → 正常返回，不影响成功路径
        t, results = self._run_in_thread(_quick_coroutine(), threading.Event())
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(results.get("value"), 42)
        self.assertEqual(results.get("returned"), "completed")

    def test_without_cancel_event_returns_result(self):
        # 未传 cancel_event → 与原行为一致
        results: dict = {}

        def worker():
            results["value"] = self.bridge.run_coroutine(_quick_coroutine())

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(results.get("value"), 42)


# ═══════════════════════════════════════════════════════════════════
#  ai_tool_loop._execute_tool_call：取消 → TOOL_CANCELLED，格式化不变
# ═══════════════════════════════════════════════════════════════════

class TestAiToolLoopMCPCancellation(unittest.TestCase):
    def _make_ctx(self, bridge):
        class _FakeManager:
            def __init__(self, bridge):
                self.bridge = bridge

            def call_tool(self, server_name, tool_name, arguments, timeout=None):
                # 返回非协程占位：FakeBridge 不会 await 它，避免 unawaited coroutine 警告
                return object()

        return SimpleNamespace(
            mcp_client_manager=_FakeManager(bridge),
            cancel_event=threading.Event(),
            handle_ask_user_question_fn=None,
            mcp_tool_definitions=None,
        )

    def _make_tc(self):
        from system.event_types import ToolCallData
        return ToolCallData(
            id="1", name="filesystem__read_file", arguments='{"path": "/tmp/x"}',
        )

    def test_cancelled_mcp_call_returns_tool_cancelled(self):
        from ai_engine.ai_tool_loop import _execute_tool_call
        from mcp_integration.gtk_asyncio_bridge import CoroutineCancelledError
        import tool_registry

        received: dict = {}

        class FakeBridge:
            def run_coroutine(self, coro, cancel_event=None):
                received["cancel_event"] = cancel_event
                raise CoroutineCancelledError()

        ctx = self._make_ctx(FakeBridge())
        result = _execute_tool_call(self._make_tc(), ctx)
        self.assertEqual(result, tool_registry.TOOL_CANCELLED)
        self.assertIs(received["cancel_event"], ctx.cancel_event)

    def test_mcp_exception_formatting_unchanged(self):
        from ai_engine.ai_tool_loop import _execute_tool_call

        class FakeBridge:
            def run_coroutine(self, coro, cancel_event=None):
                raise RuntimeError("boom")

        ctx = self._make_ctx(FakeBridge())
        result = _execute_tool_call(self._make_tc(), ctx)
        self.assertEqual(
            result,
            "❌ MCP 工具 'filesystem__read_file' 执行异常: boom",
        )

    def test_mcp_success_returns_result(self):
        from ai_engine.ai_tool_loop import _execute_tool_call

        class FakeBridge:
            def run_coroutine(self, coro, cancel_event=None):
                return "hello"

        ctx = self._make_ctx(FakeBridge())
        result = _execute_tool_call(self._make_tc(), ctx)
        self.assertEqual(result, "hello")


if __name__ == "__main__":
    unittest.main()
