import unittest
from unittest.mock import MagicMock, patch
from stores.clipboard_store import ChatMessage
from ai_text_utils.cleanup import _dict_to_chat_message


class TestReasoningBackgroundLoss(unittest.TestCase):
    """Test suite targeting reasoning_content preservation during background streaming and conversation switching."""

    def test_on_llm_api_finished_appends_reasoning_after_tool_role(self):
        """Verify that _on_llm_api_finished appends assistant_msg with reasoning_content even when previous message role is 'tool'."""
        from views.ai_chat_panel import AIChatPanel

        target_messages = [
            {"role": "user", "content": "Run tool test"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1", "name": "bash", "content": "output"}
        ]
        state = {
            "req_id": 100,
            "streaming": True,
            "messages": target_messages,
            "current_assistant_text": "Tool finished answer",
            "current_reasoning_text": "Reasoning before/during tool execution",
            "response_div_added": False,
        }

        panel = MagicMock(spec=AIChatPanel)
        panel._ai_running_convs = {"conv_test": state}
        panel._ai_conversation_id = "other_conv"  # Background execution
        panel._ai_cancelling = False

        # Directly invoke production method on AIChatPanel
        AIChatPanel._on_llm_api_finished(panel, 100)

        # Assert on resulting message history
        self.assertEqual(len(target_messages), 4)
        self.assertEqual(target_messages[-1]["role"], "assistant")
        self.assertEqual(target_messages[-1]["reasoning_content"], "Reasoning before/during tool execution")
        self.assertEqual(target_messages[-1]["content"], "Tool finished answer")
        panel._render_background_conversation.assert_called_once_with("conv_test", target_messages, state)

    def test_dict_to_chat_message_preserves_reasoning_content(self):
        msg_dict = {
            "role": "assistant",
            "content": "Result",
            "reasoning_content": "Deep reasoning content"
        }
        cm = _dict_to_chat_message(msg_dict)
        self.assertEqual(cm.reasoning_content, "Deep reasoning content")


if __name__ == "__main__":
    unittest.main()
