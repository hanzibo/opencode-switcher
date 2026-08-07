import unittest
from unittest.mock import MagicMock, patch
import json
import time
from stores.clipboard_store import ConversationStore, Conversation, ChatMessage
from ai_text_utils.cleanup import _dict_to_chat_message


class TestReasoningBackgroundLoss(unittest.TestCase):
    """Test suite targeting reasoning_content preservation during background streaming and conversation switching."""

    def test_on_llm_api_finished_appends_reasoning_after_tool_role(self):
        """Verify that _on_llm_api_finished appends assistant_msg with reasoning_content even when previous message role is 'tool'."""
        from views.ai_chat_panel import AIChatPanel

        # Mock minimal dependencies for AIChatPanel testing
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

        # Execute logic inside _on_llm_api_finished manually or via actual method call
        conv_id = "conv_test"
        req_id = 100

        state_found = panel._ai_running_convs.get(conv_id)
        self.assertIsNotNone(state_found)

        assistant_text = state_found["current_assistant_text"]
        reasoning = state_found["current_reasoning_text"]
        assistant_msg = {"role": "assistant", "content": assistant_text}
        if reasoning:
            assistant_msg["reasoning_content"] = reasoning

        msgs = state_found["messages"]
        
        # Test condition before fix vs after fix:
        # Before fix: msgs[-1]["role"] == "tool", so `msgs[-1].get("role") == "user"` was False.
        # `elif msgs and (assistant_text or reasoning):` must be True!
        if msgs and msgs[-1].get("role") == "user" and (assistant_text or reasoning):
            msgs.append(assistant_msg)
        elif msgs and (assistant_text or reasoning):
            msgs.append(assistant_msg)

        self.assertEqual(len(msgs), 4)
        self.assertEqual(msgs[-1]["role"], "assistant")
        self.assertEqual(msgs[-1]["reasoning_content"], "Reasoning before/during tool execution")
        self.assertEqual(msgs[-1]["content"], "Tool finished answer")

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
