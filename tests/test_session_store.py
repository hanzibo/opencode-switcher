"""delete_session 硬删除改造测试：官方命令优先 + 软删除兜底 + 交叉验证。"""
import os
import shutil
import sqlite3
import subprocess
import tempfile
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


if __name__ == "__main__":
    unittest.main()
