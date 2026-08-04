"""并发安全删除队列：单 worker 串行消费，批次边界安全，异常自愈。

设计（基于代码评审修订）：
- 单 worker 串行消费：避免多个 opencode 进程并发写同一 SQLite 的锁竞争
- queue.get(timeout=IDLE_TIMEOUT)：2 秒无新任务自动收尾批次
- 批次边界：break 前复查队列，杜绝"worker 退出窗口入队滞留"
- 异常自愈：单条失败不杀线程，计入 errors 继续消费，finally 清理引用
"""
import queue
import threading
from typing import Callable, List, Optional


class DeleteQueue:
    """串行删除队列（可注入，便于单测并发/异常逻辑）。

    Parameters
    ----------
    do_delete : Callable[[str], Optional[str]]
        执行删除（入参 session_id，返回 None=成功 / str=警告或错误）。
    on_refresh : Callable[[], None]
        每删完一条后调用（刷新 UI 的调度，调用方负责线程安全）。
    on_batch_done : Callable[[List[str]], None]
        批次结束（队列空闲）时调用，携带本批次全部错误/警告。
    """

    IDLE_TIMEOUT = 2.0

    def __init__(self, do_delete: Callable[[str], Optional[str]],
                 on_refresh: Callable[[], None],
                 on_batch_done: Callable[[List[str]], None]):
        self._do_delete = do_delete
        self._on_refresh = on_refresh
        self._on_batch_done = on_batch_done
        self._queue: queue.Queue = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._errors: List[str] = []

    def enqueue(self, session_id: str) -> None:
        """入队并确保 worker 存活（幂等，锁内检查防竞态）。"""
        with self._lock:
            self._queue.put(session_id)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._worker_loop, daemon=True)
                self._worker.start()

    def _worker_loop(self) -> None:
        try:
            while True:
                try:
                    session_id = self._queue.get(timeout=self.IDLE_TIMEOUT)
                except queue.Empty:
                    # 批次边界：退出前复查，用户刚入队的任务不滞留
                    if not self._queue.empty():
                        continue
                    break
                try:
                    err = self._do_delete(session_id)
                except Exception as e:  # 异常自愈：不杀线程，继续消费
                    err = f"删除异常：{e}"
                if err:
                    self._errors.append(err)
                self._queue.task_done()
                self._on_refresh()
        finally:
            # 批次收尾：汇总错误（若期间有新任务，由下次 enqueue 启动新 worker）
            if self._errors:
                errs = list(self._errors)
                self._errors.clear()
                self._on_batch_done(errs)
            with self._lock:
                self._worker = None
