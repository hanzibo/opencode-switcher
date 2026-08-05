"""MCP 关闭路径的聚焦测试。

覆盖：
  GtkAsyncioBridge.shutdown（幂等 / 有界等待 cleanup / 失败日志）
  MCPClientManager.shutdown（取消重连定时器 + 断开全部会话，幂等）
  集成：bridge.shutdown(cleanup=mgr.shutdown()) 在停桥前真正 await 断开
  真实 stdio 子进程在 manager shutdown 后已被终止并回收

全部基于 stdlib unittest + asyncio，headless 运行，不依赖 GTK 主循环。
"""

import asyncio
import time
import unittest
from typing import Optional

from mcp_integration.gtk_asyncio_bridge import GtkAsyncioBridge


def _fresh_bridge() -> GtkAsyncioBridge:
    """重置单例并返回一个全新桥接器实例（测试隔离，避免跨用例污染）。"""
    GtkAsyncioBridge._instance = None
    return GtkAsyncioBridge.get()


def _teardown_bridge() -> None:
    """清理单例引用，避免影响其它测试模块。"""
    GtkAsyncioBridge._instance = None


# ═══════════════════════════════════════════════════════════════════
#  GtkAsyncioBridge.shutdown
# ═══════════════════════════════════════════════════════════════════

class TestGtkAsyncioBridgeShutdown(unittest.TestCase):

    def test_shutdown_when_never_started_is_noop(self):
        bridge = _fresh_bridge()
        try:
            bridge.shutdown()
            self.assertFalse(bridge._running)
            self.assertIsNone(bridge._loop)
            self.assertIsNone(bridge._thread)
        finally:
            _teardown_bridge()

    def test_shutdown_twice_is_idempotent(self):
        bridge = _fresh_bridge()
        try:
            bridge.start()
            bridge.shutdown()
            bridge.shutdown()  # 已停止后再次调用：安全无操作
            self.assertFalse(bridge._running)
            self.assertIsNone(bridge._loop)
            self.assertIsNone(bridge._thread)
        finally:
            _teardown_bridge()

    def test_shutdown_after_stop_is_idempotent(self):
        bridge = _fresh_bridge()
        try:
            bridge.start()
            bridge.stop()
            bridge.shutdown()  # 先 stop 再 shutdown：无异常
            self.assertIsNone(bridge._loop)
        finally:
            _teardown_bridge()

    def test_shutdown_awaits_cleanup_coroutine(self):
        bridge = _fresh_bridge()
        try:
            bridge.start()
            ran: list = []

            async def cleanup() -> None:
                ran.append("started")
                await asyncio.sleep(0.05)
                ran.append("done")

            bridge.shutdown(timeout=5, cleanup=cleanup())
            # cleanup 被完整 await（非 fire-and-forget），且发生在停桥之前
            self.assertEqual(ran, ["started", "done"])
            self.assertIsNone(bridge._loop)
        finally:
            _teardown_bridge()

    def test_shutdown_waits_before_stopping_loop(self):
        # 证明：cleanup 完成时事件循环仍在运行；停桥发生在 cleanup 之后
        bridge = _fresh_bridge()
        try:
            bridge.start()
            loop_running_during_cleanup = []

            async def cleanup() -> None:
                loop_running_during_cleanup.append(bridge._loop.is_running())

            bridge.shutdown(timeout=5, cleanup=cleanup())
            self.assertEqual(loop_running_during_cleanup, [True])
            self.assertIsNone(bridge._loop)
        finally:
            _teardown_bridge()

    def test_shutdown_timeout_bounds_hanging_cleanup(self):
        # 挂起的 cleanup 被有界等待，不会无限阻塞；且取消被真正处理完，
        # 事件循环上不遗留 pending task（否则 GC 时会有 "Task was destroyed" 警告）
        bridge = _fresh_bridge()
        try:
            bridge.start()
            loop = bridge.get_loop()

            async def hang() -> None:
                await asyncio.sleep(30)

            t0 = time.monotonic()
            bridge.shutdown(timeout=0.2, cleanup=hang())
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 5)
            self.assertIsNone(bridge._loop)
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            self.assertEqual(pending, [])
        finally:
            _teardown_bridge()

    def test_hanging_cleanup_processes_cancellation_before_close(self):
        # 回归：超时取消后，底层清理协程应获得处理 CancelledError 的机会
        # （可执行清理收尾），且该处理在关停事件循环前完成
        bridge = _fresh_bridge()
        try:
            bridge.start()
            events: list = []

            async def hang() -> None:
                events.append("started")
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    events.append("cancelled")
                    await asyncio.sleep(0.05)
                    events.append("cleaned")
                    raise

            bridge.shutdown(timeout=0.2, cleanup=hang())
            self.assertIn("cancelled", events)
            self.assertIn("cleaned", events)
            self.assertIsNone(bridge._loop)
        finally:
            _teardown_bridge()

    def test_shutdown_closes_cleanup_when_never_started(self):
        # 桥接器从未启动：传入的清理协程被显式 close()（cr_frame 清空），
        # 而非遗留未 await 的协程（避免 "coroutine was never awaited" 警告）
        bridge = _fresh_bridge()
        try:
            async def cleanup() -> None:
                await asyncio.sleep(0.01)

            coro = cleanup()
            bridge.shutdown(cleanup=coro)
            self.assertIsNone(coro.cr_frame)
            self.assertIsNone(bridge._loop)
        finally:
            _teardown_bridge()

    def test_shutdown_closes_cleanup_when_already_stopped(self):
        bridge = _fresh_bridge()
        try:
            bridge.start()
            bridge.stop()  # 桥已停止

            async def cleanup() -> None:
                await asyncio.sleep(0.01)

            coro = cleanup()
            bridge.shutdown(cleanup=coro)
            self.assertIsNone(coro.cr_frame)
        finally:
            _teardown_bridge()

    def test_shutdown_logs_cleanup_failure_and_still_stops(self):
        bridge = _fresh_bridge()
        try:
            bridge.start()

            async def boom() -> None:
                raise RuntimeError("disconnect failed")

            with self.assertLogs("mcp_integration.gtk_asyncio_bridge", level="ERROR"):
                bridge.shutdown(timeout=5, cleanup=boom())
            self.assertIsNone(bridge._loop)  # 清理失败不阻止停桥
        finally:
            _teardown_bridge()

    def test_shutdown_without_cleanup_stops_bridge(self):
        bridge = _fresh_bridge()
        try:
            bridge.start()
            bridge.shutdown()
            self.assertIsNone(bridge._loop)
            with self.assertRaises(RuntimeError):
                bridge.get_loop()
        finally:
            _teardown_bridge()


# ═══════════════════════════════════════════════════════════════════
#  MCPClientManager.shutdown
# ═══════════════════════════════════════════════════════════════════

class TestMCPClientManagerShutdown(unittest.IsolatedAsyncioTestCase):

    async def test_shutdown_closes_sessions_and_cancels_reconnect_timers(self):
        from mcp_integration.client_manager import MCPClientManager

        closed: list = []

        class FakeSession:
            @property
            def is_connected(self) -> bool:
                return True

            async def close(self) -> None:
                closed.append("s1")

        mgr = MCPClientManager()
        mgr._sessions["s1"] = FakeSession()

        async def _noop() -> None:
            pass

        mgr._reconnect_timers["orphan"] = asyncio.create_task(_noop())

        await mgr.shutdown()
        self.assertEqual(closed, ["s1"])
        self.assertEqual(mgr.get_all_server_names(), [])
        self.assertEqual(mgr._reconnect_timers, {})

    async def test_shutdown_is_idempotent(self):
        from mcp_integration.client_manager import MCPClientManager

        mgr = MCPClientManager()
        await mgr.shutdown()  # 无会话：无操作
        await mgr.shutdown()  # 再次调用：仍安全
        self.assertEqual(mgr.get_all_server_names(), [])


# ═══════════════════════════════════════════════════════════════════
#  集成：bridge.shutdown(cleanup=mgr.shutdown())
# ═══════════════════════════════════════════════════════════════════

class TestBridgeManagerShutdownIntegration(unittest.TestCase):

    def test_manager_disconnect_awaited_before_bridge_stops(self):
        from mcp_integration.client_manager import MCPClientManager

        bridge = _fresh_bridge()
        try:
            mgr = MCPClientManager(bridge)
            closed: list = []

            class FakeSession:
                @property
                def is_connected(self) -> bool:
                    return True

                async def close(self) -> None:
                    closed.append("srv")

            mgr._sessions["srv"] = FakeSession()

            bridge.start()
            bridge.shutdown(timeout=5, cleanup=mgr.shutdown())
            # 停桥返回时，manager 的 disconnect 已被真正 await
            self.assertEqual(closed, ["srv"])
            self.assertEqual(mgr.get_all_server_names(), [])
            self.assertIsNone(bridge._loop)
        finally:
            _teardown_bridge()

    def test_stdio_subprocess_reaped_on_manager_shutdown(self):
        from mcp_integration.client_manager import MCPClientManager
        from mcp_integration.json_rpc import JsonRpcSession
        from mcp_integration.mcp_session import MCPSession
        from mcp_integration.transports.stdio import StdioTransport

        bridge = _fresh_bridge()
        transport: Optional[StdioTransport] = None
        try:
            bridge.start()
            transport = StdioTransport("sleep", ["30"])
            jrpc = JsonRpcSession(transport)
            session = MCPSession(jrpc)
            connect_fut = asyncio.run_coroutine_threadsafe(
                session.connect(), bridge.get_loop()
            )
            connect_fut.result(timeout=5)
            proc = transport._process
            self.assertIsNotNone(proc)
            self.assertIsNone(proc.returncode)  # shutdown 前子进程存活

            mgr = MCPClientManager(bridge)
            mgr._sessions["sleep_srv"] = session
            bridge.shutdown(timeout=10, cleanup=mgr.shutdown())

            # stdio 子进程应已被终止并回收（returncode 已填充）
            self.assertEqual(mgr.get_all_server_names(), [])
            self.assertIsNotNone(proc.returncode)
        finally:
            _teardown_bridge()
            if transport is not None and transport._process is not None:
                transport._process.kill()


if __name__ == "__main__":
    unittest.main()
