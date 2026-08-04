import unittest
from unittest.mock import MagicMock, patch
from tool_registry import subagent
from stores.clipboard_store import AISettingsStore


def _flush_glib_idle():
    """在无 GTK 主循环的测试环境中，迭代 GLib 主上下文以执行 idle_add 排队的回调。

    _notify_subagent_status_change 在非主线程（含 pytest 主线程，未运行 GTK 主循环）
    时通过 GLib.idle_add 投递回调；测试需手动 flush 才能收到事件。
    """
    from gi.repository import GLib
    ctx = GLib.main_context_default()
    guard = 0
    while ctx.pending() and guard < 200:
        ctx.iteration(False)
        guard += 1


class TestSubagentMonitoring(unittest.TestCase):
    def setUp(self):
        subagent._background_subagent_status.clear()
        subagent._background_subagent_results.clear()
        subagent._subagent_status_listeners.clear()

    def test_subagent_action_state_notifications(self):
        events = []

        def listener(sid, info):
            if info:
                events.append((sid, info.get("status"), info.get("action")))

        subagent.register_subagent_status_listener(listener)

        mock_config = MagicMock()
        mock_config.api_key = "test-key"
        mock_config.base_url = "https://api.test/v1"
        mock_config.model_name = "test-model"
        mock_config.max_tokens = 1000

        mock_llm = MagicMock()
        mock_llm.sync_chat_completion.side_effect = [
            {
                "content": "",
                "tool_calls": [{"id": "call_1", "function": {"name": "bash", "arguments": "{}"}}]
            },
            {
                "content": "Done task",
                "tool_calls": None
            }
        ]

        with patch("tool_registry.subagent._get_llm_config", return_value=mock_config), \
             patch("ai_engine.llm_client._LLMHttpClient", return_value=mock_llm), \
             patch("tool_registry.execute_tool_call", return_value="success"), \
             patch("tool_registry.subagent._BashSession"):

            subagent._background_subagent_status["sa_test"] = {
                "task": "test",
                "status": "running",
                "action": "Thinking",
            }
            res = subagent._execute_subagent_sync(
                task="test task",
                agent_type="general",
                subagent_id="sa_test"
            )

        self.assertEqual(res, "Done task")
        _flush_glib_idle()
        actions = [e[2] for e in events]
        # 进度事件语义（A+B 方案）：_bump_turn 每轮广播一次 Thinking（turn0/turn1），
        # 不再由 _update_action 单独广播；Tool Call: bash = action 1 次 + 工具完成 bump 1 次
        self.assertEqual(actions.count("Thinking"), 2, "turn0/turn1 各广播一次 Thinking")
        self.assertEqual(actions.count("Tool Call: bash"), 2, "action 1 次 + tool bump 1 次")
        self.assertEqual(actions.count("Answering"), 1)

    def test_subagent_failed_status_propagation(self):
        events = []

        def listener(sid, info):
            if info:
                events.append((sid, info.get("status"), info.get("action")))

        subagent.register_subagent_status_listener(listener)

        subagent._background_subagent_status["sa_fail"] = {
            "task": "fail test",
            "status": "running",
            "action": "Thinking",
        }

        with patch("tool_registry.subagent._get_llm_config", side_effect=RuntimeError("未配置 API Key")), \
             patch("tool_registry.subagent._BashSession"):
            
            res = subagent._execute_subagent_sync(
                task="fail task",
                agent_type="general",
                subagent_id="sa_fail"
            )

        self.assertTrue(res.startswith("错误："))
        self.assertEqual(subagent._background_subagent_status["sa_fail"]["status"], "failed")
        _flush_glib_idle()
        statuses = [e[1] for e in events]
        self.assertIn("failed", statuses)

    def test_subagent_max_iterations_multi_turn(self):
        mock_config = MagicMock()
        mock_config.api_key = "test-key"
        mock_config.base_url = "https://api.test/v1"
        mock_config.model_name = "test-model"
        mock_config.max_tokens = 1000

        mock_llm = MagicMock()
        # Infinite tool calls: 耗尽轮次上限后循环必须终止
        mock_llm.sync_chat_completion.return_value = {
            "content": "",
            "tool_calls": [{"id": "call_x", "function": {"name": "get_current_time", "arguments": "{}"}}]
        }

        with patch("tool_registry.subagent._get_llm_config", return_value=mock_config), \
             patch("ai_engine.llm_client._LLMHttpClient", return_value=mock_llm), \
             patch("tool_registry.execute_tool_call", return_value="12:00"), \
             patch("tool_registry.subagent._BashSession"), \
             patch("stores.clipboard_store.AISettingsStore") as MockSettings:
            # 轮次上限仅由用户设置 max_tool_iterations 决定：设为 10 后，
            # 子代理不再接收 max_turns，最多跑到 10 轮
            MockSettings.return_value.max_tool_iterations = 10
            subagent._execute_subagent_sync(
                task="test task",
                agent_type="general"
            )
            self.assertEqual(mock_llm.sync_chat_completion.call_count, 10)

    def test_subagent_block_tuple_unpacking(self):
        mock_panel = MagicMock()
        mock_panel._ai_subagent_blocks = {}
        mock_panel._ai_selected_subagents = set()

        # Test tuple structure (child, event_box, box, spinner) — 当前实现为 4 元素（A+B 方案）
        mock_child = MagicMock()
        mock_box = MagicMock()
        mock_spinner = MagicMock()
        mock_panel._ai_subagent_blocks["sa_1"] = (mock_child, mock_child, mock_box, mock_spinner)

        # Ensure 4-element tuple unpacking succeeds without ValueError
        from views.ai_chat_panel import AIChatPanel
        
        with patch("tool_registry.get_subagent_status_map", return_value={"sa_1": {"status": "completed"}}):
            AIChatPanel._on_subagent_block_click(mock_panel, "sa_1")
            self.assertIn("sa_1", mock_panel._ai_selected_subagents)
            
            AIChatPanel._remove_subagent_block(mock_panel, "sa_1")
            self.assertNotIn("sa_1", mock_panel._ai_subagent_blocks)


if __name__ == "__main__":
    unittest.main()
