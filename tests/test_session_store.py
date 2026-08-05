"""delete_session 硬删除改造测试：官方命令优先 + 软删除兜底 + 交叉验证。"""
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from stores import session_store

SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    title TEXT,
    directory TEXT,
    time_created INTEGER,
    time_updated INTEGER,
    time_archived INTEGER
);
CREATE TABLE part (
    session_id TEXT,
    data TEXT,
    time_created INTEGER
);
"""


class TestDeleteSession(unittest.TestCase):
    """delete_session 数据层逻辑（patch DB_PATH 隔离真实库）。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "opencode.db")
        conn = sqlite3.connect(self._db)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO session (id, title, directory, time_created, time_updated) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ses_test1", "Test Session", "/tmp/proj", 1000, 2000),
        )
        conn.commit()
        conn.close()
        self._patcher = patch.object(session_store, "DB_PATH", self._db)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _archived(self):
        conn = sqlite3.connect(self._db)
        try:
            row = conn.execute(
                "SELECT time_archived FROM session WHERE id='ses_test1'").fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _exists(self):
        conn = sqlite3.connect(self._db)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM session WHERE id='ses_test1'").fetchone()
            return row[0] > 0
        finally:
            conn.close()

    def test_hard_delete_success(self):
        """官方命令成功（且行真的消失）→ 返回 None，物理删除。"""
        def fake_run(cmd, **kwargs):
            # 模拟 CLI 真实效果：删除临时库中的行
            conn = sqlite3.connect(self._db)
            conn.execute("DELETE FROM session WHERE id='ses_test1'")
            conn.commit()
            conn.close()
            return subprocess.CompletedProcess(cmd, 0, stdout="deleted", stderr="")

        with patch.object(session_store.shutil, "which",
                          return_value="/usr/bin/opencode"), \
             patch.object(session_store.subprocess, "run", side_effect=fake_run):
            err = session_store.delete_session("ses_test1")

        self.assertIsNone(err)
        self.assertFalse(self._exists())          # 物理删除
        self.assertIsNone(self._archived())       # 未走软删

    def test_opencode_missing_fallback(self):
        """opencode 不在 PATH → 返回警告 + 软删兜底。"""
        with patch.object(session_store.shutil, "which", return_value=None):
            err = session_store.delete_session("ses_test1")

        self.assertIsNotNone(err)
        self.assertIn("警告", err)
        self.assertIn("opencode 不在 PATH", err)
        self.assertTrue(self._exists())           # 行仍在
        self.assertIsNotNone(self._archived())    # 已软删

    def test_cli_failure_fallback(self):
        """CLI 返回非零 → 返回警告（含 stderr）+ 软删兜底。"""
        proc = subprocess.CompletedProcess(
            ["opencode", "session", "delete", "ses_test1"],
            1, stdout="", stderr="boom error")
        with patch.object(session_store.shutil, "which",
                          return_value="/usr/bin/opencode"), \
             patch.object(session_store.subprocess, "run", return_value=proc):
            err = session_store.delete_session("ses_test1")

        self.assertIsNotNone(err)
        self.assertIn("警告", err)
        self.assertIn("boom error", err)
        self.assertTrue(self._exists())
        self.assertIsNotNone(self._archived())

    def test_cli_timeout_fallback(self):
        """CLI 超时 → 返回警告 + 软删兜底。"""
        with patch.object(session_store.shutil, "which",
                          return_value="/usr/bin/opencode"), \
             patch.object(session_store.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("opencode", 30)):
            err = session_store.delete_session("ses_test1")

        self.assertIsNotNone(err)
        self.assertIn("警告", err)
        self.assertTrue(self._exists())
        self.assertIsNotNone(self._archived())

    def test_cli_success_but_row_remains(self):
        """CLI 返回 0 但行仍在（静默 no-op）→ 交叉验证失败，走软删兜底。"""
        proc = subprocess.CompletedProcess(
            ["opencode", "session", "delete", "ses_test1"],
            0, stdout="deleted", stderr="")
        with patch.object(session_store.shutil, "which",
                          return_value="/usr/bin/opencode"), \
             patch.object(session_store.subprocess, "run", return_value=proc):
            err = session_store.delete_session("ses_test1")

        self.assertIsNotNone(err)
        self.assertIn("CLI 返回成功但 session 仍存在", err)
        self.assertTrue(self._exists())
        self.assertIsNotNone(self._archived())


class TestSessionCache(unittest.TestCase):
    """get_sessions 短 TTL 缓存 + single-flight + 显式失效 API。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._projdir = os.path.join(self._tmpdir, "proj")
        os.makedirs(self._projdir)
        self._db = os.path.join(self._tmpdir, "opencode.db")
        conn = sqlite3.connect(self._db)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO session (id, title, directory, time_created, time_updated) "
            "VALUES ('ses_cache1', 'Cache Session', ?, 1000, 2000)",
            (self._projdir,),
        )
        conn.commit()
        conn.close()
        self._patcher = patch.object(session_store, "DB_PATH", self._db)
        self._patcher.start()
        session_store.invalidate_sessions_cache()  # 隔离前序用例的缓存残留

    def tearDown(self):
        session_store.invalidate_sessions_cache()
        self._patcher.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _count_computes(self, slow=False):
        """统计 _compute_sessions 真实调用次数（可选加延时放大并发竞争）。"""
        counter = {"n": 0}
        original = session_store._compute_sessions

        def counting(limit):
            counter["n"] += 1
            if slow:
                time.sleep(0.1)
            return original(limit)

        patcher = patch.object(session_store, "_compute_sessions", side_effect=counting)
        patcher.start()
        self.addCleanup(patcher.stop)
        return counter

    def test_ttl_hit_reuses_result(self):
        counter = self._count_computes()
        first = session_store.get_sessions()
        second = session_store.get_sessions()
        self.assertEqual(counter["n"], 1)  # TTL 内第二次调用复用缓存
        self.assertEqual([s.id for s in first], [s.id for s in second])
        self.assertIsNot(first, second)  # 返回副本而非缓存对象本身

    def test_ttl_expiry_recomputes(self):
        counter = self._count_computes()
        session_store.get_sessions()
        # 把缓存条目时间戳拨回 TTL 之前，模拟过期
        with session_store._cache_lock:
            entry = session_store._cache[100]
            session_store._cache[100] = (time.monotonic() - 10, entry[1])
        again = session_store.get_sessions()
        self.assertEqual(counter["n"], 2)  # 过期后重新计算
        self.assertEqual([s.id for s in again], ["ses_cache1"])

    def test_invalidate_api_forces_recompute(self):
        counter = self._count_computes()
        session_store.get_sessions()
        session_store.invalidate_sessions_cache()
        session_store.get_sessions()
        self.assertEqual(counter["n"], 2)

    def test_rename_invalidates_cache(self):
        counter = self._count_computes()
        titles = [s.title for s in session_store.get_sessions()]
        self.assertEqual(titles, ["Cache Session"])
        self.assertIsNone(session_store.rename_session("ses_cache1", "Renamed"))
        titles = [s.title for s in session_store.get_sessions()]
        self.assertEqual(titles, ["Renamed"])  # 缓存已失效 → 读到新标题
        self.assertEqual(counter["n"], 2)

    def test_delete_invalidates_cache(self):
        counter = self._count_computes()
        self.assertEqual(len(session_store.get_sessions()), 1)
        with patch.object(session_store.shutil, "which", return_value=None):
            err = session_store.delete_session("ses_cache1")
        self.assertIsNotNone(err)  # opencode 缺失 → 软删兜底，行被归档
        self.assertEqual(session_store.get_sessions(), [])  # 缓存已失效 → 空列表
        self.assertEqual(counter["n"], 2)

    def test_concurrent_misses_share_one_computation(self):
        counter = self._count_computes(slow=True)
        barrier = threading.Barrier(2)
        results = []

        def worker():
            barrier.wait()  # 两个线程同时发起冷缓存请求
            results.append(session_store.get_sessions())

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(counter["n"], 1)  # single-flight：两次未命中共享一次计算
        self.assertEqual([s.id for s in results[0]], [s.id for s in results[1]])

    def test_returned_state_is_isolated_from_cache(self):
        counter = self._count_computes()
        first = session_store.get_sessions()
        first[0].title = "CORRUPTED"  # 调用方改写返回值的字段
        first.append(session_store.Session(  # 以及增删列表元素
            id="fake", title="x", directory="", project_name="",
            status="closed", snippet="", started_at=0, updated_at=0))
        del first[0]
        second = session_store.get_sessions()  # TTL 内仍命中缓存
        self.assertEqual(counter["n"], 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].title, "Cache Session")  # 缓存未被污染

    def test_limit_is_cached_separately(self):
        counter = self._count_computes()
        session_store.get_sessions(limit=50)
        session_store.get_sessions(limit=100)
        self.assertEqual(counter["n"], 2)  # 不同 limit 不共享缓存条目


if __name__ == "__main__":
    unittest.main()
