"""针对 sub_agent 状态显示修复（commit dffe8e0 + failed 状态传播）的回归测试。

覆盖：
- P0-1: _notify_subagent_status_change 非主线程必须走 idle_add，不得同步调用 UI 回调
- P1-2: check_background_subagents 按 sid 精确消费（UI 主线程 conv_id=None 无法按 conv_id 匹配）
- P1-2: remove_subagent_status 同步清理未消费结果
- P1-3: _run 在状态被移除后不复活（不写 results、不通知、仅落盘文件）
- P2-5: _cleanup_expired_subagents 清理过期 completed 状态与结果
- P2-6: _update_action 相同 action 只广播一次（但首次必广播）
- P2-7: get_subagent_status_map 深拷贝隔离
- failed 状态传播：执行失败标记 failed 且不被覆盖为 completed
"""

import os
import time
import unittest
from unittest.mock import MagicMock, patch

from tool_registry import subagent


def _flush_glib_idle():
    """迭代 GLib 主上下文，执行 idle_add 排队的回调（测试环境无 GTK 主循环）。"""
    from gi.repository import GLib
    ctx = GLib.main_context_default()
    guard = 0
    while ctx.pending() and guard < 200:
        ctx.iteration(False)
        guard += 1


class TestSubagentFixes(unittest.TestCase):
    def setUp(self):
        subagent._background_subagent_status.clear()
        subagent._background_subagent_results.clear()
        subagent._subagent_status_listeners.clear()

    # ── P0-1: 线程安全 ──────────────────────────────────────────────────────

    def test_notify_non_main_thread_uses_idle_add(self):
        """非主线程（is_owner()=False）必须 idle_add 投递，绝不能同步调用 UI 回调。"""
        with patch("gi.repository.GLib.main_context_default") as mock_ctx, \
             patch("gi.repository.GLib.idle_add") as mock_idle:
            mock_ctx.return_value.is_owner.return_value = False
            cb = MagicMock()
            subagent.register_subagent_status_listener(cb)
            subagent._notify_subagent_status_change("t-1", {"status": "running"})
            mock_idle.assert_called_once()
            cb.assert_not_called()

    def test_notify_main_thread_calls_directly(self):
        """主线程（is_owner()=True）直接同步调用回调。"""
        with patch("gi.repository.GLib.main_context_default") as mock_ctx:
            mock_ctx.return_value.is_owner.return_value = True
            cb = MagicMock()
            subagent.register_subagent_status_listener(cb)
            subagent._notify_subagent_status_change("t-2", {"status": "running"})
            cb.assert_called_once_with("t-2", {"status": "running"})

    # ── P1-2: 按 sid 精确消费 ───────────────────────────────────────────────

    def test_check_background_subagents_by_sid(self):
        subagent._background_subagent_results["t-3"] = "r3"
        subagent._background_subagent_status["t-3"] = {"conv_id": "conv-a", "status": "completed"}
        subagent._background_subagent_results["t-4"] = "r4"
        subagent._background_subagent_status["t-4"] = {"conv_id": "conv-b", "status": "completed"}

        out = subagent.check_background_subagents(subagent_ids=["t-4", "t-3"])
        self.assertIn("t-3", out)
        self.assertIn("t-4", out)
        # 消费后 results 应被清空
        self.assertEqual(subagent._background_subagent_results, {})

    def test_ui_thread_conv_none_cannot_match_by_conv(self):
        """UI 主线程 conv_id=None，按 conv_id 匹配必然失败 → 结果残留；按 sid 可消费。"""
        subagent._background_subagent_results["t-5"] = "r5"
        subagent._background_subagent_status["t-5"] = {"conv_id": "conv-c", "status": "completed"}

        # 模拟 UI 主线程（conv_id=None）：匹配不到，结果不消费
        self.assertEqual(subagent.check_background_subagents(), "")
        self.assertIn("t-5", subagent._background_subagent_results)

        # 按 sid 精确消费成功
        out = subagent.check_background_subagents(subagent_ids=["t-5"])
        self.assertIn("t-5", out)
        self.assertEqual(subagent._background_subagent_results, {})

    def test_remove_subagent_status_clears_results(self):
        """remove_subagent_status 必须同步清理未消费结果，避免泄漏。"""
        subagent._background_subagent_status["t-6"] = {"status": "completed"}
        subagent._background_subagent_results["t-6"] = "r6"
        events = []
        subagent.register_subagent_status_listener(lambda sid, info: events.append((sid, info)))

        subagent.remove_subagent_status("t-6")
        self.assertNotIn("t-6", subagent._background_subagent_status)
        self.assertNotIn("t-6", subagent._background_subagent_results)
        _flush_glib_idle()  # 删除事件经 idle_add 异步投递，需 flush 后才可见
        self.assertIn(("t-6", None), events)  # 删除事件已广播

    # ── P1-3: 状态被移除后不复活 ────────────────────────────────────────────

    def test_run_background_no_resurrect_after_remove(self):
        """运行中被 remove 的子代理，完成后不得复活状态/写 results，仅落盘文件。"""
        events = []
        subagent.register_subagent_status_listener(lambda sid, info: events.append((sid, info)))
        result_path = "/tmp/opencode_subagent_t-7_result.txt"
        if os.path.exists(result_path):
            os.remove(result_path)

        subagent._background_subagent_status["t-7"] = {
            "task": "task", "started_at": time.time(),
            "status": "running", "action": "Thinking", "conv_id": "conv-x",
        }
        subagent.remove_subagent_status("t-7")  # 运行中被移除

        with patch("tool_registry.subagent._execute_subagent_sync", return_value="结果正文"):
            subagent._run_subagent_background("task", "general", "t-7")
            # 轮询等待后台线程落盘
            deadline = time.time() + 5
            while time.time() < deadline and not os.path.exists(result_path):
                time.sleep(0.05)

        self.assertTrue(os.path.exists(result_path), "结果文件应已落盘")
        self.assertNotIn("t-7", subagent._background_subagent_status, "状态不应复活")
        self.assertNotIn("t-7", subagent._background_subagent_results, "results 不应写入")
        # 除 remove 广播的 None 删除事件外，不应有运行/完成事件
        non_delete = [e for e in events if e[1] is not None]
        self.assertEqual(non_delete, [])

    # ── P2-5: 过期清理 ──────────────────────────────────────────────────────

    def test_cleanup_expired_subagents(self):
        now = time.time()
        subagent._background_subagent_status["old"] = {"status": "completed", "completed_at": now - 9999}
        subagent._background_subagent_results["old"] = "old"
        subagent._background_subagent_status["fresh"] = {"status": "completed", "completed_at": now - 10}
        subagent._background_subagent_results["fresh"] = "fresh"
        subagent._background_subagent_status["running"] = {"status": "running", "started_at": now - 9999}

        subagent._cleanup_expired_subagents()
        self.assertNotIn("old", subagent._background_subagent_status)
        self.assertNotIn("old", subagent._background_subagent_results)
        self.assertIn("fresh", subagent._background_subagent_status)   # 未过期保留
        self.assertIn("running", subagent._background_subagent_status)  # running 不清理

    # ── P2-6: 事件去重（节流） ──────────────────────────────────────────────

    def test_update_action_dedup_same_action(self):
        """连续相同 action 只广播一次；初始 Thinking 由预置广播覆盖不再重复（🟡-1）。"""
        events = []

        def listener(sid, info):
            if info:
                events.append((sid, info.get("action")))

        subagent.register_subagent_status_listener(listener)
        subagent._background_subagent_status["t-8"] = {"status": "running", "action": "Thinking"}

        mock_config = MagicMock(api_key="k", base_url="http://x", model_name="m", max_tokens=10)
        mock_llm = MagicMock()
        # 第一轮含两个同名 bash 调用（连续相同应去重），第二轮收尾
        mock_llm.sync_chat_completion.side_effect = [
            {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "bash", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "bash", "arguments": "{}"}},
            ]},
            {"content": "done", "tool_calls": None},
        ]

        with patch("tool_registry.subagent._get_llm_config", return_value=mock_config), \
             patch("ai_engine.llm_client._LLMHttpClient", return_value=mock_llm), \
             patch("tool_registry.subagent._BashSession"), \
             patch("tool_registry.execute_tool_call", return_value="ok"):
            subagent._execute_subagent_sync("task", "general", subagent_id="t-8")

        _flush_glib_idle()
        # 进度事件语义（A+B 方案）：Thinking = turn0 bump + turn1 bump（各 1）；
        # Tool Call: bash = action 1 + 轮末 flush 1（🟡-2 合并广播）共 2
        self.assertEqual(events.count(("t-8", "Thinking")), 2, "每轮 bump 广播一次 Thinking")
        self.assertEqual(events.count(("t-8", "Tool Call: bash")), 2, "action 1 + 轮末 flush 1")
        self.assertEqual(events.count(("t-8", "Answering")), 1)

    # ── P2-7: 深拷贝隔离 ───────────────────────────────────────────────────

    def test_get_status_map_deepcopy(self):
        subagent._background_subagent_status["t-9"] = {"action": "Thinking", "status": "running"}
        m = subagent.get_subagent_status_map()
        m["t-9"]["action"] = "HACK"
        self.assertEqual(subagent._background_subagent_status["t-9"]["action"], "Thinking")

    # ── failed 状态传播 ─────────────────────────────────────────────────────

    def test_execute_sync_marks_failed_on_error(self):
        """执行失败（配置错误）必须标记 failed 并广播。"""
        events = []

        def listener(sid, info):
            if info:
                events.append((sid, info.get("status")))

        subagent.register_subagent_status_listener(listener)
        subagent._background_subagent_status["t-10"] = {"status": "running", "action": "Thinking"}

        with patch("tool_registry.subagent._get_llm_config", side_effect=RuntimeError("no key")), \
             patch("tool_registry.subagent._BashSession"):
            res = subagent._execute_subagent_sync("task", "general", subagent_id="t-10")

        self.assertTrue(res.startswith("错误："))
        self.assertEqual(subagent._background_subagent_status["t-10"]["status"], "failed")
        _flush_glib_idle()
        self.assertIn("failed", [s for _, s in events])

    def test_run_background_keeps_failed_status(self):
        """_run 不得把 failed 状态覆盖为 completed。"""
        subagent._background_subagent_status["t-11"] = {
            "task": "task", "started_at": time.time(),
            "status": "failed", "action": "失败", "conv_id": "conv-x",
        }
        with patch("tool_registry.subagent._execute_subagent_sync", return_value="错误：LLM 挂了"):
            subagent._run_subagent_background("task", "general", "t-11")
            deadline = time.time() + 5
            while time.time() < deadline and "t-11" not in subagent._background_subagent_results:
                time.sleep(0.05)

        self.assertEqual(subagent._background_subagent_status["t-11"]["status"], "failed")
        # 错误信息也写入 results 供主代理查看
        self.assertEqual(subagent._background_subagent_results["t-11"], "错误：LLM 挂了")

    # ── get_subagent_status 查询运行中/未清理子代理 ─────────────────────────

    def test_get_subagent_status_single_running(self):
        """查询运行中的子代理（单 ID）不得抛 NameError（回归：P2-5 重构误删 now）。"""
        subagent._background_subagent_status["t-12"] = {
            "task": "统计文件数", "started_at": time.time() - 5,
            "status": "running", "action": "Thinking", "conv_id": "conv-x",
        }
        out = subagent.execute_get_subagent_status(id="t-12")
        self.assertIn("t-12", out)
        self.assertIn("running", out)
        self.assertIn("耗时", out)
        self.assertNotIn("Traceback", out)

    def test_get_subagent_status_list_running(self):
        """列出含运行中/已完成未清理子代理的状态列表不得抛 NameError。"""
        now = time.time()
        subagent._background_subagent_status["t-13"] = {
            "task": "任务A", "started_at": now - 10,
            "status": "running", "action": "Tool Call: bash", "conv_id": "conv-x",
        }
        subagent._background_subagent_status["t-14"] = {
            "task": "任务B", "started_at": now - 30, "status": "completed",
            "action": "已完成", "completed_at": now - 5, "conv_id": "conv-x",
        }
        out = subagent.execute_get_subagent_status()
        self.assertIn("t-13", out)
        self.assertIn("t-14", out)
        self.assertIn("耗时", out)
        self.assertNotIn("Traceback", out)

    def test_get_subagent_status_partial_id_match(self):
        """按短 ID（后缀）查询运行中的子代理也应工作（回归：now 引用）。"""
        subagent._background_subagent_status["abc123-7"] = {
            "task": "任务C", "started_at": time.time() - 3,
            "status": "running", "action": "Thinking", "conv_id": "conv-x",
        }
        out = subagent.execute_get_subagent_status(id=7)
        self.assertIn("abc123-7", out)
        self.assertNotIn("Traceback", out)

    # ── 🔴-1 failed 清理 + 🟠-3 clear_completed 清理 ────────────────────────

    def test_cleanup_expired_subagents_removes_failed(self):
        """failed 条目按 failed_at 过期后应被清理（🔴-1）。"""
        now = time.time()
        subagent._background_subagent_status["f1"] = {
            "status": "failed", "failed_at": now - 9999, "action": "失败",
        }
        subagent._background_subagent_results["f1"] = "err"
        subagent._background_subagent_status["f2"] = {
            "status": "failed", "failed_at": now - 10, "action": "失败",
        }
        subagent._background_subagent_results["f2"] = "err2"
        subagent._cleanup_expired_subagents()
        self.assertNotIn("f1", subagent._background_subagent_status)
        self.assertNotIn("f1", subagent._background_subagent_results)
        self.assertIn("f2", subagent._background_subagent_status)  # 未过期保留

    def test_clear_completed_clears_failed_and_results(self):
        """clear_completed 同时清除 failed 与对应 results，running 保留（🟠-3）。"""
        subagent._background_subagent_status["c1"] = {"status": "completed", "completed_at": time.time()}
        subagent._background_subagent_results["c1"] = "r1"
        subagent._background_subagent_status["f1"] = {"status": "failed", "failed_at": time.time()}
        subagent._background_subagent_results["f1"] = "err"
        subagent._background_subagent_status["run"] = {"status": "running", "started_at": time.time()}

        out = subagent.execute_get_subagent_status(clear_completed=True)
        self.assertIn("2", out)  # 清除了 2 条终结记录
        self.assertNotIn("c1", subagent._background_subagent_status)
        self.assertNotIn("c1", subagent._background_subagent_results)
        self.assertNotIn("f1", subagent._background_subagent_status)
        self.assertNotIn("f1", subagent._background_subagent_results)
        self.assertIn("run", subagent._background_subagent_status)  # running 不清

    # ── 🔴-2 _run 兜底异常 ─────────────────────────────────────────────────

    def test_run_background_catches_unexpected_exception(self):
        """_execute_subagent_sync 抛未预料异常时，_run 必须收敛到 failed 并落盘（🔴-2）。"""
        subagent._background_subagent_status["t-15"] = {
            "task": "task", "started_at": time.time(),
            "status": "running", "action": "Thinking", "conv_id": "conv-x",
        }
        result_path = "/tmp/opencode_subagent_t-15_result.txt"
        if os.path.exists(result_path):
            os.remove(result_path)
        with patch("tool_registry.subagent._execute_subagent_sync", side_effect=RuntimeError("boom")):
            subagent._run_subagent_background("task", "general", "t-15")
            deadline = time.time() + 5
            while time.time() < deadline and "t-15" not in subagent._background_subagent_results:
                time.sleep(0.05)
        # 状态收敛到 failed，而非卡在 running / 误标 completed
        self.assertEqual(subagent._background_subagent_status["t-15"]["status"], "failed")
        # 错误信息写入 results 与结果文件
        self.assertIn("boom", subagent._background_subagent_results["t-15"])
        self.assertTrue(os.path.exists(result_path))
        with open(result_path, encoding="utf-8") as f:
            self.assertIn("boom", f.read())

    # ── 子代理默认模型（API Settings 指定） ────────────────────────────────

    def test_get_llm_config_prefers_subagent_default(self):
        """_get_llm_config 优先级：is_subagent_default → is_default → models[0]。"""
        from stores.clipboard_store import LLMModelConfig
        models = [
            LLMModelConfig(alias="普通", base_url="u1", api_key="k1", model_name="m1"),
            LLMModelConfig(alias="全局默认", base_url="u2", api_key="k2", model_name="m2",
                           is_default=True),
            LLMModelConfig(alias="子代理", base_url="u3", api_key="k3", model_name="m3",
                           is_subagent_default=True),
        ]
        # ① 存在子代理默认 → 优先使用
        with patch("stores.clipboard_store.LLMSettingsStore") as MockStore:
            MockStore.return_value.models = models
            cfg = subagent._get_llm_config()
        self.assertEqual(cfg.alias, "子代理")

        # ② 无子代理默认 → 回退全局默认
        models[2].is_subagent_default = False
        with patch("stores.clipboard_store.LLMSettingsStore") as MockStore:
            MockStore.return_value.models = models
            cfg = subagent._get_llm_config()
        self.assertEqual(cfg.alias, "全局默认")

        # ③ 均无标记 → 取第一个
        models[1].is_default = False
        with patch("stores.clipboard_store.LLMSettingsStore") as MockStore:
            MockStore.return_value.models = models
            cfg = subagent._get_llm_config()
        self.assertEqual(cfg.alias, "普通")

    def test_llm_model_config_subagent_default_roundtrip(self):
        """is_subagent_default 标记应随 save_all/load 持久化往返。"""
        import tempfile
        from stores.clipboard_store import LLMSettingsStore, LLMModelConfig
        tmp_path = os.path.join(tempfile.mkdtemp(), "llm_settings.json")
        try:
            with patch("stores.clipboard_store.LLM_SETTINGS_PATH", tmp_path):
                store = LLMSettingsStore()
                store.models = [
                    LLMModelConfig(alias="A", base_url="u", api_key="k", model_name="m",
                                   is_subagent_default=True),
                ]
                store.save_all()
                store2 = LLMSettingsStore()  # 重新加载验证持久化
                self.assertTrue(store2.models[0].is_subagent_default)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    # ── 移除 max_turns 参数（remove-subagent-max-turns 分支） ──────────────

    def test_execute_sub_agent_ignores_stale_max_turns_kwarg(self):
        """旧模型缓存仍携带 max_turns 时，execute_sub_agent 不得 TypeError（**kwargs 容错）。"""
        with patch("tool_registry.subagent._run_subagent_background") as mock_run, \
             patch("tool_registry.subagent._background_subagent_id", 0):
            res = subagent.execute_sub_agent(
                task="任务",
                agent_type="general",
                max_turns=15,  # 模拟旧 schema 缓存的冗余参数
            )
        self.assertIn("子代理已启动", res)
        mock_run.assert_called_once()
        # 轮次参数不再向下传递
        call_args = mock_run.call_args.args
        self.assertEqual(call_args[1], "general")

    def test_execute_subagent_sync_uses_max_tool_iterations_only(self):
        """轮次上限仅由 max_tool_iterations 决定，不再有 max_turns 传入路径。"""
        mock_config = MagicMock(api_key="k", base_url="http://x",
                                model_name="m", max_tokens=10)
        mock_llm = MagicMock()
        # 无限工具调用：耗尽轮次上限后循环必须终止
        mock_llm.sync_chat_completion.return_value = {
            "content": "", "tool_calls": [
                {"id": "c", "function": {"name": "bash", "arguments": "{}"}},
            ]}
        with patch("tool_registry.subagent._get_llm_config", return_value=mock_config), \
             patch("ai_engine.llm_client._LLMHttpClient", return_value=mock_llm), \
             patch("tool_registry.subagent._BashSession"), \
             patch("tool_registry.execute_tool_call", return_value="ok"), \
             patch("stores.clipboard_store.AISettingsStore") as MockSettings:
            MockSettings.return_value.max_tool_iterations = 3
            subagent._execute_subagent_sync("task", "general")
        self.assertEqual(mock_llm.sync_chat_completion.call_count, 3)

    def test_execute_subagent_sync_uses_model_max_tokens(self):
        """子代理 LLM 请求的 max_tokens 来自子代理默认模型配置，主代理不传参。"""
        mock_config = MagicMock(api_key="k", base_url="http://x",
                                model_name="m", max_tokens=777)
        mock_llm = MagicMock()
        mock_llm.sync_chat_completion.return_value = {"content": "done", "tool_calls": None}
        with patch("tool_registry.subagent._get_llm_config", return_value=mock_config), \
             patch("ai_engine.llm_client._LLMHttpClient", return_value=mock_llm), \
             patch("tool_registry.subagent._BashSession"):
            subagent._execute_subagent_sync("task", "general")
        # 发送给 LLM 的请求配置 max_tokens 应为模型配置值（而非主代理传参）
        sub_config = mock_llm.sync_chat_completion.call_args.args[0]
        self.assertEqual(sub_config.max_tokens, 777)

    def test_execute_sub_agent_ignores_stale_max_tokens_kwarg(self):
        """旧模型缓存仍携带 max_tokens 时，execute_sub_agent 不得 TypeError（**kwargs 容错）。"""
        with patch("tool_registry.subagent._run_subagent_background") as mock_run, \
             patch("tool_registry.subagent._background_subagent_id", 0):
            res = subagent.execute_sub_agent(
                task="任务",
                agent_type="general",
                max_turns=15,    # 模拟旧 schema 缓存的冗余参数
                max_tokens=999,  # 同上
            )
        self.assertIn("子代理已启动", res)
        mock_run.assert_called_once()
        # 两个冗余参数均不再向下传递：后台调用仅 task/agent_type/subagent_id
        self.assertEqual(len(mock_run.call_args.args), 3)

    def test_execute_subagent_sync_max_tool_iterations_edge(self):
        """max_tool_iterations 为 0/负数/字符串/None 时不抛 TypeError，至少跑 1 轮（M2）。"""
        mock_config = MagicMock(api_key="k", base_url="http://x",
                                model_name="m", max_tokens=10)
        for bad in (0, -5, "3", None):
            mock_llm = MagicMock()
            mock_llm.sync_chat_completion.return_value = {"content": "done", "tool_calls": None}
            with patch("tool_registry.subagent._get_llm_config", return_value=mock_config), \
                 patch("ai_engine.llm_client._LLMHttpClient", return_value=mock_llm), \
                 patch("tool_registry.subagent._BashSession"), \
                 patch("stores.clipboard_store.AISettingsStore") as MockSettings:
                MockSettings.return_value.max_tool_iterations = bad
                subagent._execute_subagent_sync("task", "general")
            self.assertGreaterEqual(
                mock_llm.sync_chat_completion.call_count, 1,
                f"max_tool_iterations={bad!r} 不应抛 TypeError",
            )

    def test_execute_subagent_sync_config_max_tokens_invalid(self):
        """config.max_tokens 为 None/非数字/0/负数时回退 DEFAULT_MAX_TOKENS=4096（L2）。"""
        for bad in (None, "abc", 0, -1):
            mock_config = MagicMock(api_key="k", base_url="http://x", model_name="m")
            mock_config.max_tokens = bad
            mock_llm = MagicMock()
            mock_llm.sync_chat_completion.return_value = {"content": "done", "tool_calls": None}
            with patch("tool_registry.subagent._get_llm_config", return_value=mock_config), \
                 patch("ai_engine.llm_client._LLMHttpClient", return_value=mock_llm), \
                 patch("tool_registry.subagent._BashSession"):
                subagent._execute_subagent_sync("task", "general")
            sub_config = mock_llm.sync_chat_completion.call_args.args[0]
            self.assertEqual(sub_config.max_tokens, 4096, f"max_tokens={bad!r} 应回退 4096")

    # ── A+B 方案：进度感知（spinner + 轮次/工具计数 + 工具历史浮窗） ────────

    def test_subagent_progress_counters(self):
        """多轮多工具场景：turn / tool_calls_count / tools_history 随执行递增。"""
        subagent._background_subagent_status["p-1"] = {
            "task": "t", "status": "running", "action": "Thinking",
            "turn": 0, "tool_calls_count": 0, "tools_history": [],
        }
        mock_config = MagicMock(api_key="k", base_url="http://x", model_name="m", max_tokens=10)
        mock_llm = MagicMock()
        mock_llm.sync_chat_completion.side_effect = [
            {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "bash", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "get_current_time", "arguments": "{}"}},
            ]},
            {"content": "done", "tool_calls": None},
        ]
        with patch("tool_registry.subagent._get_llm_config", return_value=mock_config), \
             patch("ai_engine.llm_client._LLMHttpClient", return_value=mock_llm), \
             patch("tool_registry.subagent._BashSession"), \
             patch("tool_registry.execute_tool_call", return_value="ok"):
            subagent._execute_subagent_sync("task", "general", subagent_id="p-1")
        info = subagent._background_subagent_status["p-1"]
        self.assertEqual(info["turn"], 2, "两轮请求")
        self.assertEqual(info["tool_calls_count"], 2, "两个工具")
        self.assertEqual(info["tools_history"], ["bash", "get_current_time"])

    def test_tools_history_ring_buffer(self):
        """超过 _MAX_TOOL_HISTORY 个工具时，历史只保留最近 10 个。"""
        subagent._background_subagent_status["p-2"] = {
            "task": "t", "status": "running", "action": "Thinking",
            "turn": 0, "tool_calls_count": 0, "tools_history": [],
        }
        mock_config = MagicMock(api_key="k", base_url="http://x", model_name="m", max_tokens=10)
        mock_llm = MagicMock()
        calls = [{"content": "", "tool_calls": [
            {"id": f"c{i}", "function": {"name": f"tool{i}", "arguments": "{}"}},
        ]} for i in range(12)]  # 12 轮 × 1 工具
        calls.append({"content": "done", "tool_calls": None})
        mock_llm.sync_chat_completion.side_effect = calls
        with patch("tool_registry.subagent._get_llm_config", return_value=mock_config), \
             patch("ai_engine.llm_client._LLMHttpClient", return_value=mock_llm), \
             patch("tool_registry.subagent._BashSession"), \
             patch("tool_registry.execute_tool_call", return_value="ok"), \
             patch("stores.clipboard_store.AISettingsStore") as MockSettings:
            MockSettings.return_value.max_tool_iterations = 20
            subagent._execute_subagent_sync("task", "general", subagent_id="p-2")
        info = subagent._background_subagent_status["p-2"]
        self.assertEqual(len(info["tools_history"]), 10)
        self.assertEqual(info["tools_history"][0], "tool2")  # tool0/tool1 被淘汰
        self.assertEqual(info["tools_history"][-1], "tool11")

    def test_bump_broadcast_ignores_action_dedup(self):
        """连续轮次（action=Thinking）仍因 bump 每轮广播，不被 action 去重吞掉。"""
        events = []

        def listener(sid, info):
            if info:
                events.append((sid, info.get("action"), info.get("turn")))

        subagent.register_subagent_status_listener(listener)
        subagent._background_subagent_status["p-3"] = {
            "task": "t", "status": "running", "action": "Thinking",
            "turn": 0, "tool_calls_count": 0, "tools_history": [],
        }
        mock_config = MagicMock(api_key="k", base_url="http://x", model_name="m", max_tokens=10)
        mock_llm = MagicMock()
        mock_llm.sync_chat_completion.side_effect = [
            {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "bash", "arguments": "{}"}},
            ]},
            {"content": "done", "tool_calls": None},
        ]
        with patch("tool_registry.subagent._get_llm_config", return_value=mock_config), \
             patch("ai_engine.llm_client._LLMHttpClient", return_value=mock_llm), \
             patch("tool_registry.subagent._BashSession"), \
             patch("tool_registry.execute_tool_call", return_value="ok"):
            subagent._execute_subagent_sync("task", "general", subagent_id="p-3")
        _flush_glib_idle()
        turns = [t for (_s, a, t) in events if a == "Thinking"]
        self.assertEqual(turns, [1, 2], "每轮 bump 广播 turn 递增，Thinking 不被去重吞掉")

    def test_completed_keeps_progress_fields(self):
        """completed 写回保留 turn/tool_calls_count/tools_history（浮窗数据不丢）。

        回归：completed 曾用全新 dict 替换，丢弃进度字段 → 浮窗显示"0 轮/暂无工具"。
        """
        subagent._background_subagent_status["c-9"] = {
            "task": "t", "started_at": time.time(),
            "status": "running", "action": "Tool Call: bash", "conv_id": "cv",
            "turn": 3, "tool_calls_count": 5, "tools_history": ["bash", "read_file"],
        }
        with patch("tool_registry.subagent._execute_subagent_sync", return_value="结果正文"):
            subagent._run_subagent_background("task", "general", "c-9")
            deadline = time.time() + 5
            while time.time() < deadline:
                if subagent._background_subagent_status.get("c-9", {}).get("status") == "completed":
                    break
                time.sleep(0.05)
        info = subagent._background_subagent_status["c-9"]
        self.assertEqual(info["status"], "completed")
        self.assertEqual(info["turn"], 3, "轮次保留")
        self.assertEqual(info["tool_calls_count"], 5, "工具计数保留")
        self.assertEqual(info["tools_history"], ["bash", "read_file"], "工具历史保留")


if __name__ == "__main__":
    unittest.main()
