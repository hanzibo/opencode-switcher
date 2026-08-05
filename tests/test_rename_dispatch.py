"""App._on_rename_session 派发单测：验证 DB 写入/读取/UI 更新全部移出 GTK 主线程。

不创建真实 GTK 组件（object.__new__ 跳过 App.__init__），不启动真实线程
（FakeThread 捕获 target/kwargs 后由测试确定性驱动），因此无需显示环境。
"""
import unittest
from unittest.mock import patch

import main
from stores.session_refresh import run_rename_refresh


class FakePanel:
    """最小 panel 替身：仅记录 load_sessions 调用。"""

    def __init__(self):
        self.loaded = []

    def load_sessions(self, sessions):
        self.loaded.append(sessions)


class FakeThread:
    """捕获 target/kwargs 的线程替身：start() 不真正起线程。"""

    def __init__(self, *args, **kwargs):
        self.target = kwargs.pop("target", None)
        self.kwargs = kwargs.pop("kwargs", {})
        self.daemon = kwargs.pop("daemon", False)
        self.started = False

    def start(self):
        self.started = True


class TestRenameDispatch(unittest.TestCase):

    def setUp(self):
        self.panel = FakePanel()
        self.app = object.__new__(main.App)  # 跳过 __init__，不创建真实 GTK 组件
        self.app._session_load_seq = 0
        self.app._panel = self.panel
        self.shown_errors = []
        self.app._show_error = self.shown_errors.append
        self.created = []

        patcher = patch.object(main.threading, "Thread",
                               side_effect=self._capture_thread)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.idle_cbs = []
        idle_patcher = patch.object(main.GLib, "idle_add",
                                    side_effect=lambda cb: self.idle_cbs.append(cb))
        idle_patcher.start()
        self.addCleanup(idle_patcher.stop)

    def _capture_thread(self, *args, **kwargs):
        t = FakeThread(*args, **kwargs)
        self.created.append(t)
        return t

    def _invoke_background(self, t=None):
        """确定性执行后台线程体（不真正起线程）。"""
        t = t or self.created[-1]
        t.target(**t.kwargs)

    # ---- 用例 ----

    def test_no_synchronous_db_or_ui_calls(self):
        """GTK 线程内不执行 rename/get_sessions/load_sessions（全部后台派发）。"""
        with patch.object(main, "rename_session") as mock_rename, \
             patch.object(main, "get_sessions") as mock_get:
            self.app._on_rename_session("sid1", "New Title")

        mock_rename.assert_not_called()
        mock_get.assert_not_called()
        self.assertEqual(self.panel.loaded, [])   # UI 也未同步更新

        self.assertEqual(len(self.created), 1)    # 派发了后台线程
        t = self.created[0]
        self.assertTrue(t.daemon)
        self.assertIs(t.target, run_rename_refresh)
        self.assertEqual(t.kwargs["session_id"], "sid1")
        self.assertEqual(t.kwargs["new_title"], "New Title")
        self.assertEqual(t.kwargs["seq"], 1)      # 首次调用 seq = 1

    def test_seq_bump_on_each_rename(self):
        """每次重命名递增 _session_load_seq（使在途面板加载失效）。"""
        self.app._on_rename_session("s1", "A")
        self.app._on_rename_session("s1", "B")
        self.assertEqual(self.created[0].kwargs["seq"], 1)
        self.assertEqual(self.created[1].kwargs["seq"], 2)
        self.assertEqual(self.app._session_load_seq, 2)

    def test_background_success_path_updates_panel(self):
        """后台执行成功 → 经 idle 回调更新面板列表。"""
        sessions = [{"id": "sid1", "title": "New Title"}]
        with patch.object(main, "rename_session", return_value=None) as mock_rename, \
             patch.object(main, "get_sessions", return_value=sessions) as mock_get:
            self.app._on_rename_session("sid1", "New Title")
            self._invoke_background()

        mock_rename.assert_called_once_with("sid1", "New Title")
        mock_get.assert_called_once()
        self.assertEqual(len(self.idle_cbs), 1)   # apply 经 idle 切回主线程
        self.idle_cbs[0]()
        self.assertEqual(self.panel.loaded, [sessions])

    def test_stale_background_refresh_discarded(self):
        """后台刷新排队期间 seq 被推进 → idle apply 被丢弃（防旧标题覆盖）。"""
        sessions = [{"id": "sid1", "title": "Stale"}]
        with patch.object(main, "rename_session", return_value=None), \
             patch.object(main, "get_sessions", return_value=sessions):
            self.app._on_rename_session("sid1", "Stale")
            self._invoke_background()
            # 模拟队列期间发生了更新操作（面板重开/再次重命名）
            self.app._session_load_seq = 99
            for cb in self.idle_cbs:
                cb()
        self.assertEqual(self.panel.loaded, [])   # 旧刷新未落地

    def test_error_path_shows_error_dialog(self):
        """rename 返回错误 → 经 idle 弹 _show_error，不刷新列表。"""
        with patch.object(main, "rename_session", return_value="数据库错误") as mock_rename, \
             patch.object(main, "get_sessions") as mock_get:
            self.app._on_rename_session("sid1", "New")
            self._invoke_background()

        mock_get.assert_not_called()
        self.assertEqual(len(self.idle_cbs), 1)
        self.idle_cbs[0]()
        self.assertEqual(self.shown_errors, ["数据库错误"])
        self.assertEqual(self.panel.loaded, [])

    def test_rapid_renames_only_last_applies(self):
        """快速连续两次重命名：仅最新一次刷新落地。"""
        with patch.object(main, "rename_session", return_value=None) as mock_rename, \
             patch.object(main, "get_sessions",
                          side_effect=[[{"id": "s1", "title": "A"}],
                                       [{"id": "s1", "title": "B"}]]):
            self.app._on_rename_session("s1", "A")
            self.app._on_rename_session("s1", "B")
            self._invoke_background(self.created[0])  # 旧刷新先完成
            self._invoke_background(self.created[1])  # 新刷新后完成
            for cb in self.idle_cbs:
                cb()
        self.assertEqual(self.panel.loaded, [[{"id": "s1", "title": "B"}]])
        self.assertEqual(mock_rename.call_count, 2)


if __name__ == "__main__":
    unittest.main()
