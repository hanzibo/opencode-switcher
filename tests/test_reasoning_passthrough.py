"""reasoning_content 回传修复测试：_build_request 的 assistant 消息 rc 保留语义。"""
import unittest
from types import SimpleNamespace
from unittest import mock

from ai_engine import llm_client as llm_client_module
from ai_engine.llm_client import _LLMHttpClient, LLMRequestConfig, extract_reasoning_content


class TestReasoningContentPassthrough(unittest.TestCase):
    """验证 thinking 模式下 assistant 消息的 reasoning_content 全量回传。"""

    def setUp(self):
        self.client = _LLMHttpClient()
        self.config = LLMRequestConfig(
            base_url="https://example.com/v1",
            api_key="test-key",
            model_name="deepseek-v4-flash",
        )

    def _build_body(self, messages):
        _, _, body = self.client._build_request(self.config, messages, stream=False)
        return body

    def _find_assistant_msg(self, body):
        return [m for m in body["messages"] if m["role"] == "assistant"]

    def test_plain_text_assistant_keeps_reasoning_content(self):
        """纯文本 assistant 轮（无 tool_calls）带 rc → 必须保留（此前会被丢弃）。"""
        body = self._build_body([
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "最终回答", "reasoning_content": "思考过程..."},
        ])
        assistant_msgs = self._find_assistant_msg(body)
        self.assertEqual(len(assistant_msgs), 1)
        self.assertEqual(assistant_msgs[0]["content"], "最终回答")
        self.assertEqual(assistant_msgs[0]["reasoning_content"], "思考过程...")

    def test_tool_call_assistant_keeps_reasoning_content(self):
        """工具调用轮 assistant 带 rc → 保留（回归保护）。"""
        body = self._build_body([
            {"role": "user", "content": "查天气"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "需要调用工具",
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "get_weather", "arguments": "{}"}}],
            },
        ])
        assistant_msgs = self._find_assistant_msg(body)
        self.assertEqual(len(assistant_msgs), 1)
        self.assertEqual(assistant_msgs[0]["reasoning_content"], "需要调用工具")
        self.assertIn("tool_calls", assistant_msgs[0])

    def test_mimo_reasoning_key_mapped(self):
        """MiMo 风格（reasoning 键）→ 统一映射为 reasoning_content。"""
        body = self._build_body([
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "回答", "reasoning": "MiMo 思考"},
        ])
        assistant_msgs = self._find_assistant_msg(body)
        self.assertEqual(assistant_msgs[0]["reasoning_content"], "MiMo 思考")
        self.assertNotIn("reasoning", assistant_msgs[0])

    def test_no_reasoning_content_not_injected(self):
        """无 rc 的 assistant 消息 → 不添加 reasoning_content 字段。"""
        body = self._build_body([
            {"role": "user", "content": "普通问题"},
            {"role": "assistant", "content": "普通回答"},
        ])
        assistant_msgs = self._find_assistant_msg(body)
        self.assertEqual(len(assistant_msgs), 1)
        self.assertNotIn("reasoning_content", assistant_msgs[0])

    def test_subagent_style_tool_round_keeps_rc(self):
        """模拟子代理消息序列（assistant 带 tool_calls + rc → tool 结果 → 下一轮 assistant 纯文本）。"""
        body = self._build_body([
            {"role": "user", "content": "任务"},
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "先执行工具",
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "bash", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "bash", "content": "结果"},
            {"role": "assistant", "content": "任务完成", "reasoning_content": "工具结果已获取"},
        ])
        assistant_msgs = self._find_assistant_msg(body)
        self.assertEqual(len(assistant_msgs), 2)
        self.assertEqual(assistant_msgs[0]["reasoning_content"], "先执行工具")
        self.assertEqual(assistant_msgs[1]["reasoning_content"], "工具结果已获取")


class TestSubagentExecutorRcPassthrough(unittest.TestCase):
    """子代理执行器本身的 rc 附加逻辑（根因现场）回归保护。"""

    def test_subagent_executor_attaches_rc(self):
        """子代理工具轮 assistant 消息必须携带 rc，且下一轮请求中可观测。"""
        from tool_registry import subagent

        fake_resp_tool = {
            "role": "assistant", "content": "",
            "reasoning_content": "需要调用工具",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "bash", "arguments": "{}"}}],
        }
        fake_resp_done = {"role": "assistant", "content": "任务完成"}

        fake_session = mock.MagicMock()  # 替代真实 _BashSession（测试环境不启动 bash 进程）

        with mock.patch.object(subagent, "_get_llm_config",
                               return_value=SimpleNamespace(
                                   base_url="https://example.com/v1", api_key="k",
                                   model_name="m", max_tokens=100)), \
             mock.patch.object(llm_client_module._LLMHttpClient, "sync_chat_completion",
                               side_effect=[fake_resp_tool, fake_resp_done]) as mock_sync, \
             mock.patch.object(subagent, "_BashSession", return_value=fake_session), \
             mock.patch("tool_registry.execute_tool_call", return_value="ok"):
            result = subagent._execute_subagent_sync("test task", "general")

        self.assertIn("任务完成", result)

        # 第二次 LLM 调用（纯文本轮）时，messages 中 assistant 消息必须携带 rc
        second_msgs = mock_sync.call_args_list[1].kwargs["messages"]
        assistant_msgs = [m for m in second_msgs if m.get("role") == "assistant"]
        self.assertEqual(len(assistant_msgs), 1)
        self.assertEqual(assistant_msgs[0]["reasoning_content"], "需要调用工具")
        self.assertIn("tool_calls", assistant_msgs[0])

    def test_subagent_executor_mimo_reasoning_key(self):
        """子代理响应使用 MiMo 风格 reasoning 键 → 同样附加为 reasoning_content。"""
        from tool_registry import subagent

        fake_resp_tool = {
            "role": "assistant", "content": "",
            "reasoning": "MiMo 思考",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "bash", "arguments": "{}"}}],
        }
        fake_resp_done = {"role": "assistant", "content": "完成"}

        fake_session = mock.MagicMock()

        with mock.patch.object(subagent, "_get_llm_config",
                               return_value=SimpleNamespace(
                                   base_url="https://example.com/v1", api_key="k",
                                   model_name="m", max_tokens=100)), \
             mock.patch.object(llm_client_module._LLMHttpClient, "sync_chat_completion",
                               side_effect=[fake_resp_tool, fake_resp_done]) as mock_sync, \
             mock.patch.object(subagent, "_BashSession", return_value=fake_session), \
             mock.patch("tool_registry.execute_tool_call", return_value="ok"):
            subagent._execute_subagent_sync("test task", "general")

        second_msgs = mock_sync.call_args_list[1].kwargs["messages"]
        assistant_msgs = [m for m in second_msgs if m.get("role") == "assistant"]
        self.assertEqual(assistant_msgs[0]["reasoning_content"], "MiMo 思考")
        self.assertNotIn("reasoning", assistant_msgs[0])


class TestExtractReasoningContentHelper(unittest.TestCase):
    """extract_reasoning_content 公共 helper 的键兼容语义。"""

    def test_deepseek_key(self):
        self.assertEqual(extract_reasoning_content({"reasoning_content": "a"}), "a")

    def test_mimo_key(self):
        self.assertEqual(extract_reasoning_content({"reasoning": "b"}), "b")

    def test_deepseek_key_precedence(self):
        self.assertEqual(
            extract_reasoning_content({"reasoning_content": "a", "reasoning": "b"}), "a")

    def test_missing_returns_none(self):
        self.assertIsNone(extract_reasoning_content({"content": "x"}))


if __name__ == "__main__":
    unittest.main()
