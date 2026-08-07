import unittest
from unittest.mock import MagicMock, patch
from views.ai_chat_panel import AIChatPanel


class TestNewConversationFlickerFix(unittest.TestCase):

    def test_start_new_conversation_cancels_pending_recent_load(self):
        """Verify that start_new_conversation sets _ai_recent_load_pending to False and prevents _load_recent_conversation_deferred from overwriting blank conversation."""
        panel = MagicMock(spec=AIChatPanel)
        panel._ai_recent_load_pending = True
        panel._ai_conversation_id = "old_id"
        panel._ai_messages = []
        panel._ai_running_convs = {}
        panel._ai_html_cache = {}
        panel.separator = MagicMock()
        panel._ai_input_area = MagicMock()
        panel._ai_history_popover = MagicMock()

        # Call start_new_conversation
        AIChatPanel.start_new_conversation(panel)

        # Verify _ai_recent_load_pending is False
        self.assertFalse(panel._ai_recent_load_pending)

        # Execute _load_recent_conversation_deferred and verify it returns False immediately without loading recent conversations
        result = AIChatPanel._load_recent_conversation_deferred(panel)
        self.assertFalse(result)
        panel._get_sorted_conversations.assert_not_called()
        panel._switch_to_conversation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
