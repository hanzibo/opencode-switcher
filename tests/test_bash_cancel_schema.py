import unittest
from unittest.mock import MagicMock
from views.ai_chat_panel import AIChatPanel


class TestBashCancelSchemaSanitization(unittest.TestCase):

    def test_sanitize_tool_calls_schema_inserts_cancelled_response(self):
        """Verify that _sanitize_tool_calls_schema appends missing role='tool' response for tool_calls."""
        panel = MagicMock(spec=AIChatPanel)
        messages = [
            {"role": "user", "content": "run command"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_999",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "sleep 100"}'},
                    }
                ],
            },
        ]

        # Call sanitize method
        modified = AIChatPanel._sanitize_tool_calls_schema(panel, messages)

        self.assertTrue(modified)
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[2]["role"], "tool")
        self.assertEqual(messages[2]["tool_call_id"], "call_999")
        self.assertIn("已取消", messages[2]["content"])

    def test_sanitize_tool_calls_schema_noop_if_already_responded(self):
        """Verify that _sanitize_tool_calls_schema does nothing if tool_calls already has tool response."""
        panel = MagicMock(spec=AIChatPanel)
        messages = [
            {"role": "user", "content": "run command"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_888",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_888",
                "name": "bash",
                "content": "file.txt",
            },
        ]

        modified = AIChatPanel._sanitize_tool_calls_schema(panel, messages)

        self.assertFalse(modified)
        self.assertEqual(len(messages), 3)


if __name__ == "__main__":
    unittest.main()
