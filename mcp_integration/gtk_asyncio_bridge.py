"""GLib main loop ↔ asyncio event loop bridge.

MCP SDK 基于 asyncio，而 GTK 应用使用 GLib 主循环。
此桥接器在独立线程中运行 asyncio event loop，通过线程安全 API
提交协程并通过 GLib.idle_add 将结果回调到主线程。
"""

import asyncio
import concurrent.futures
import logging
import threading
from threading import Event
from typing import Any, Callable, Coroutine, Optional

from gi.repository import GLib

logger = logging.getLogger(__name__)


class CoroutineCancelledError(Exception):
    """协程因外部取消事件被终止（用户取消）。

    当 run_coroutine 传入 cancel_event 且该事件置位时抛出，
    替代底层 concurrent.futures.CancelledError 向调用方传递取消语义。
    """


# run_coroutine 在等待期间的取消轮询间隔（秒）
_CANCEL_POLL_INTERVAL_SEC = 0.05
# 取消传播后等待协程清理完成的宽限时间（秒）
_CANCEL_CLEANUP_WAIT_SEC = 1.0
# shutdown() 等待清理协程完成的默认硬上限（秒），避免无限阻塞
_SHUTDOWN_TIMEOUT_SEC = 10.0


class GtkAsyncioBridge:
    """Singleton bridge: asyncio event loop runs in a dedicated thread."""

    _instance: Optional["GtkAsyncioBridge"] = None

    @classmethod
    def get(cls) -> "GtkAsyncioBridge":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        if self._instance is not None:
            raise RuntimeError("Use GtkAsyncioBridge.get() instead")
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self) -> None:
        """在独立线程中启动 asyncio event loop。"""
        if self._running:
            return
        self._running = True
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=_run_loop,
            args=(self._loop,),
            daemon=True,
            name="mcp-asyncio",
        )
        self._thread.start()

    def stop(self) -> None:
        """停止 asyncio event loop 并等待线程退出。

        幂等：从未启动 / 已重复调用时为无操作，不会对已关闭的 loop 二次 close。
        """
        self._running = False
        loop = self._loop
        thread = self._thread
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        if loop is not None and not loop.is_closed():
            loop.close()
        self._loop = None
        self._thread = None

    def shutdown(
        self,
        timeout: float = _SHUTDOWN_TIMEOUT_SEC,
        cleanup: Optional[Coroutine] = None,
    ) -> None:
        """显式关闭桥接器：可选先有界等待清理协程，再停止事件循环。

        供面板 destroy / 应用退出路径使用，保证在停止事件循环之前
        MCP 连接被断开（stdio 子进程经既有 transport.disconnect 被终止回收）。

        Parameters
        ----------
        timeout : float
            等待清理协程完成的硬上限（秒），避免无限阻塞。
        cleanup : Coroutine, optional
            停止前需等待完成的清理协程（如 ``client_manager.shutdown()``）。

        Notes
        -----
        - 超时会先取消底层清理协程，并有界等待其处理完取消后再关停事件循环
          （不遗留 pending task）。
        - 事件循环不可用（从未启动 / 已停止）时，清理协程被显式关闭，
          不会遗留未 await 的协程。
        - 清理失败 / 超时仅记录日志，不阻止停止流程；整体幂等。
        """
        if cleanup is not None:
            if (self._loop is None or self._loop.is_closed()
                    or not self._loop.is_running()):
                logger.warning(
                    "GtkAsyncioBridge.shutdown: 已请求清理但事件循环不可用，显式关闭协程"
                )
                self._close_unawaited_cleanup(cleanup)
            else:
                self._run_cleanup_bounded(self._loop, cleanup, timeout)
        if not self._running and self._loop is None and self._thread is None:
            return
        self.stop()

    @staticmethod
    def _close_unawaited_cleanup(cleanup: Coroutine) -> None:
        """关闭从未被 await 的清理协程，避免 "coroutine was never awaited" 警告。"""
        close = getattr(cleanup, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception as e:
            logger.error(
                "GtkAsyncioBridge.shutdown: 关闭未运行的清理协程失败: %s", e
            )

    @staticmethod
    def _run_cleanup_bounded(loop, cleanup: Coroutine, timeout: float) -> None:
        """在事件循环上运行清理协程，超时则取消并等待其处理完取消（有界）。

        asyncio.wait_for 在超时后会先取消内部任务并等待其真正结束
        （_cancel_and_wait），因此关停事件循环前不会遗留 pending task。
        """

        async def _runner() -> Any:
            return await asyncio.wait_for(cleanup, timeout=timeout)

        future = asyncio.run_coroutine_threadsafe(_runner(), loop)
        try:
            future.result(timeout=timeout + _CANCEL_CLEANUP_WAIT_SEC)
        except concurrent.futures.TimeoutError:
            logger.error(
                "GtkAsyncioBridge.shutdown: 清理协程 %.1fs 内未完成，已取消",
                timeout,
            )
        except Exception as e:
            logger.error("GtkAsyncioBridge.shutdown: 清理协程失败: %s", e)

    # ── 协程执行 ────────────────────────────────────────────────

    def run_coroutine(
        self,
        coro: Coroutine,
        cancel_event: Optional[Event] = None,
    ) -> Any:
        """同步等待协程完成（阻塞当前线程）。

        适用于后台线程中调用 MCP 异步操作（如工具执行）。
        注意：不要在 GTK 主线程中调用（会阻塞 UI）。

        传入 cancel_event 时，等待期间会轮询该事件：
        - 事件置位 → 取消底层 asyncio 任务（CancelledError 传播进协程，
          触发 JsonRpcSession.request 的挂起请求清理），并抛出
          CoroutineCancelledError。
        - 未传入 → 与原行为完全一致（纯阻塞等待）。
        """
        if self._loop is None:
            raise RuntimeError("Bridge not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        if cancel_event is None:
            return future.result()

        while True:
            try:
                return future.result(timeout=_CANCEL_POLL_INTERVAL_SEC)
            except concurrent.futures.TimeoutError:
                if cancel_event.is_set():
                    future.cancel()
                    try:
                        future.result(timeout=_CANCEL_CLEANUP_WAIT_SEC)
                    except BaseException:
                        pass
                    raise CoroutineCancelledError() from None
            except concurrent.futures.CancelledError:
                raise CoroutineCancelledError() from None

    def call_async(
        self,
        coro: Coroutine,
        callback: Optional[Callable[[Any, Optional[Exception]], None]] = None,
    ) -> None:
        """异步启动协程，完成后通过 GLib.idle_add 调用回调。

        适用于从 GTK 主线程启动后台 MCP 操作。
        回调在主线程执行，可安全操作 GTK 控件。
        """
        if self._loop is None:
            raise RuntimeError("Bridge not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _on_done(f: asyncio.Future) -> None:
            try:
                result = f.result()
                if callback:
                    GLib.idle_add(callback, result, None)
            except Exception as e:
                if callback:
                    GLib.idle_add(callback, None, e)

        future.add_done_callback(_on_done)

    def get_loop(self) -> asyncio.AbstractEventLoop:
        """获取底层 asyncio event loop。"""
        if self._loop is None:
            raise RuntimeError("Bridge not started")
        return self._loop


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    """在独立线程中运行 asyncio event loop。"""
    asyncio.set_event_loop(loop)
    loop.run_forever()
