"""GLib main loop ↔ asyncio event loop bridge.

MCP SDK 基于 asyncio，而 GTK 应用使用 GLib 主循环。
此桥接器在独立线程中运行 asyncio event loop，通过线程安全 API
提交协程并通过 GLib.idle_add 将结果回调到主线程。
"""

import asyncio
import threading
from typing import Any, Callable, Coroutine, Optional

from gi.repository import GLib


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
        """停止 asyncio event loop 并等待线程退出。"""
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        if self._loop:
            self._loop.close()

    # ── 协程执行 ────────────────────────────────────────────────

    def run_coroutine(self, coro: Coroutine) -> Any:
        """同步等待协程完成（阻塞当前线程）。

        适用于后台线程中调用 MCP 异步操作（如工具执行）。
        注意：不要在 GTK 主线程中调用（会阻塞 UI）。
        """
        if self._loop is None:
            raise RuntimeError("Bridge not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

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
