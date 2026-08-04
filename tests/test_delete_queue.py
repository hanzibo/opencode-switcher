"""DeleteQueue 并发删除队列单测：串行消费、批次边界、异常自愈、错误汇总。"""
import threading
import time
import unittest

from stores.delete_queue import DeleteQueue


class TestDeleteQueue(unittest.TestCase):
    """纯逻辑测试（无 GTK 依赖），覆盖并发/异常核心风险点。"""

    def setUp(self):
        self.processed = []
        self.errors = []
        self.refreshes = []
        self.done_batches = []
        self._queue = None

    def _make_queue(self, do_delete, idle_timeout=0.05):
        q = DeleteQueue(do_delete=do_delete,
                        on_refresh=lambda: self.refreshes.append(1),
                        on_batch_done=lambda errs: self.done_batches.append(list(errs)))
        q.IDLE_TIMEOUT = idle_timeout  # 测试用短超时加速收尾
        return q

    def _wait_worker_exit(self, q, timeout=5.0):
        """等待 worker 线程退出（批次收尾）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with q._lock:
                if q._worker is None or not q._worker.is_alive():
                    return
            time.sleep(0.01)
        self.fail("worker 未在超时内退出")

    def test_serial_order(self):
        """enqueue 3 个 → 按入队顺序串行消费。"""
        q = self._make_queue(do_delete=lambda sid: self.processed.append(sid))
        for sid in ["a", "b", "c"]:
            q.enqueue(sid)
        self._wait_worker_exit(q)
        self.assertEqual(self.processed, ["a", "b", "c"])

    def test_batch_boundary_no_stall(self):
        """批次边界：worker 即将退出（空闲超时后）再入队 → 不滞留、被处理。"""
        q = self._make_queue(do_delete=lambda sid: self.processed.append(sid))
        q.enqueue("first")
        self._wait_worker_exit(q)
        self.assertEqual(self.processed, ["first"])
        # worker 已退出后入队 → 新建 worker 处理
        q.enqueue("second")
        self._wait_worker_exit(q)
        self.assertEqual(self.processed, ["first", "second"])

    def test_exception_self_heal(self):
        """do_delete 抛异常 → 不杀线程，错误入汇总，后续条目继续处理。"""
        def do_delete(sid):
            if sid == "bad":
                raise RuntimeError("boom")
            self.processed.append(sid)

        q = self._make_queue(do_delete=do_delete)
        q.enqueue("bad")
        q.enqueue("good")
        self._wait_worker_exit(q)

        self.assertEqual(self.processed, ["good"])          # 后续条目仍处理
        self.assertEqual(len(self.done_batches), 1)          # 批次结束汇总一次
        self.assertEqual(len(self.done_batches[0]), 1)
        self.assertIn("删除异常：boom", self.done_batches[0][0])

    def test_batch_summary_errors(self):
        """多条错误 → 批次结束时一次性汇总全部。"""
        def do_delete(sid):
            return f"警告：{sid} 删除失败"

        q = self._make_queue(do_delete=do_delete)
        q.enqueue("x")
        q.enqueue("y")
        q.enqueue("z")
        self._wait_worker_exit(q)

        self.assertEqual(len(self.done_batches), 1)
        self.assertEqual(len(self.done_batches[0]), 3)
        self.assertIn("警告：y 删除失败", self.done_batches[0])

    def test_no_error_no_summary(self):
        """全部成功 → 不触发错误汇总。"""
        q = self._make_queue(do_delete=lambda sid: None)
        q.enqueue("ok")
        self._wait_worker_exit(q)
        self.assertEqual(self.done_batches, [])             # 无错误不弹框


if __name__ == "__main__":
    unittest.main()
