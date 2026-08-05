"""run_rename_refresh 后台刷新调度单测：纯逻辑（无 GTK 依赖，不起真实线程）。

覆盖：成功落地、seq 防乱序丢弃、DB 错误弹框、异常兜底日志、
多刷新乱序（只落地最新序列号）。
"""
import unittest

from stores.session_refresh import run_rename_refresh


class TestRunRenameRefresh(unittest.TestCase):
    """run_rename_refresh 纯逻辑测试：所有依赖注入，schedule 用列表收集。"""

    def setUp(self):
        self.scheduled = []
        self.applied = []
        self.shown_errors = []
        self.logs = []
        self.current_seq = 1
        self.renamed = []

    # ---- 基础设施 ----

    def _rename(self, *args):
        self.renamed.append(args)
        return None

    def _load(self):
        return [{"id": "s1", "title": "New"}]

    def _get_seq(self):
        return self.current_seq

    def _schedule(self, cb):
        self.scheduled.append(cb)

    def _apply(self, sessions):
        self.applied.append(sessions)

    def _run(self, rename_fn=None, load_fn=None, seq=1):
        run_rename_refresh(
            "s1", "New Title",
            rename_fn=rename_fn or self._rename,
            load_fn=load_fn or self._load,
            seq=seq,
            get_seq=self._get_seq,
            schedule=self._schedule,
            apply_fn=self._apply,
            show_error_fn=lambda err: self.shown_errors.append(err),
            log_error=lambda msg: self.logs.append(msg),
        )

    # ---- 用例 ----

    def test_success_applies_with_current_seq(self):
        """rename 成功 + 刷新落地时 seq 仍当前 → apply_fn 被调用。"""
        self._run()
        self.assertEqual(self.renamed, [("s1", "New Title")])  # 参数透传
        self.assertEqual(len(self.scheduled), 1)
        self.scheduled[0]()  # 模拟 GTK idle 执行
        self.assertEqual(self.applied, [[{"id": "s1", "title": "New"}]])
        self.assertEqual(self.shown_errors, [])

    def test_apply_callback_returns_false(self):
        """apply 回调返回 False（GLib.idle_add 一次性回调约定）。"""
        self._run()
        self.assertIs(self.scheduled[0](), False)

    def test_stale_seq_discards_apply(self):
        """apply 时 seq 已过期（期间发生了新刷新/面板重开）→ 不落地。"""
        self._run()
        self.current_seq = 2  # 刷新排队期间序列号被新操作推进
        self.scheduled[0]()
        self.assertEqual(self.applied, [])  # 旧刷新被丢弃

    def test_rename_error_shows_error(self):
        """rename 返回错误 → schedule 错误回调，不再加载列表。"""
        loads = []
        self._run(rename_fn=lambda sid, title: "数据库错误",
                  load_fn=lambda: loads.append(1))
        self.assertEqual(len(self.scheduled), 1)
        self.scheduled[0]()
        self.assertEqual(self.shown_errors, ["数据库错误"])
        self.assertEqual(loads, [])          # 错误路径不触发加载
        self.assertEqual(self.applied, [])

    def test_rename_exception_logs_only(self):
        """rename 抛异常 → 仅打印日志（与 _bg_load 约定一致），无弹框无刷新。"""
        self._run(rename_fn=lambda sid, title: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(len(self.logs), 1)
        self.assertIn("Error renaming session", self.logs[0])
        self.assertIn("boom", self.logs[0])
        self.assertEqual(self.scheduled, [])   # 无任何 idle 回调

    def test_load_exception_logs_only(self):
        """加载列表抛异常 → 仅打印日志，不落地 apply。"""
        def bad_load():
            raise RuntimeError("load fail")
        self._run(load_fn=bad_load)
        self.assertEqual(len(self.logs), 1)
        self.assertIn("Error refreshing sessions after rename", self.logs[0])
        self.assertEqual(self.scheduled, [])
        self.assertEqual(self.applied, [])

    def test_out_of_order_only_newest_applies(self):
        """乱序：旧刷新（seq=1）与新刷新（seq=2）都排队，只落地最新。"""
        self.current_seq = 2
        applied_old = []
        applied_new = []

        def apply_old(sessions):
            applied_old.append(sessions)

        def apply_new(sessions):
            applied_new.append(sessions)

        run_rename_refresh(
            "s1", "Old",
            rename_fn=self._rename, load_fn=lambda: [{"id": "s1", "title": "Old"}],
            seq=1, get_seq=self._get_seq, schedule=self._schedule,
            apply_fn=apply_old, show_error_fn=lambda e: None,
            log_error=lambda m: self.logs.append(m),
        )
        run_rename_refresh(
            "s1", "New",
            rename_fn=self._rename, load_fn=lambda: [{"id": "s1", "title": "New"}],
            seq=2, get_seq=self._get_seq, schedule=self._schedule,
            apply_fn=apply_new, show_error_fn=lambda e: None,
            log_error=lambda m: self.logs.append(m),
        )
        # 模拟 GTK idle 按调度顺序执行（旧刷新先完成）
        for cb in self.scheduled:
            cb()
        self.assertEqual(applied_old, [])  # 旧刷新被 seq 校验丢弃
        self.assertEqual(applied_new, [[{"id": "s1", "title": "New"}]])
        self.assertEqual(self.shown_errors, [])

    def test_rename_failure_does_not_bump_or_apply(self):
        """rename 失败（返回错误）→ 无 apply 排队（列表保持现状）。"""
        self._run(rename_fn=lambda sid, title: "失败")
        self.assertEqual(self.applied, [])
        self.assertEqual(len(self.scheduled), 1)  # 只有错误回调
        self.scheduled[0]()
        self.assertEqual(self.shown_errors, ["失败"])


if __name__ == "__main__":
    unittest.main()
