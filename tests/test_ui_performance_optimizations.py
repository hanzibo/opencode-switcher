import unittest
from unittest.mock import MagicMock
from stores.clipboard_store import ClipboardItem, ClipboardStore
from views.clipboard_panel import ClipboardPanel


class TestUIPerformanceOptimizations(unittest.TestCase):

    def test_clipboard_filter_does_not_call_classify_text_dynamically(self):
        """Verify that _item_matches_filter uses pre-classified item.type and does not invoke classify_text."""
        panel = MagicMock(spec=ClipboardPanel)
        panel._active_category_id = "__clipboard__"
        panel._active_tab_type = "code"
        panel._filter_query = ""
        panel._clip_store = MagicMock(spec=ClipboardStore)

        item = ClipboardItem(text="hello world", timestamp=1000, hash="abc", type="text")
        
        # Invoke actual filter method
        result = ClipboardPanel._item_matches_filter(panel, item)
        
        # Item type is 'text', active tab is 'code', so it should return False
        self.assertFalse(result)
        # Verify classify_text was never called on _clip_store
        panel._clip_store.classify_text.assert_not_called()

    def test_switch_to_conversation_updates_token_display_and_cached_html(self):
        """Verify that _switch_to_conversation updates last_rendered_html and calls _update_token_display when cached_html exists."""
        from views.ai_chat_panel import AIChatPanel

        panel = MagicMock(spec=AIChatPanel)
        panel._ai_running_convs = {}
        panel._ai_html_cache = {"conv_cached": "<div>Cached HTML Content</div>"}
        panel._ai_messages = []
        panel._ai_conversation_id = None
        panel._ai_active_model_info = None
        panel._webview_ready = True
        panel._webview_suspended = False
        panel._conversation_store = MagicMock()
        panel._ai_spinner = MagicMock()
        panel._ai_entry = MagicMock()
        panel._ai_lbl = MagicMock()
        panel.separator = MagicMock()
        panel._ai_input_area = MagicMock()
        panel._ai_history_popover = MagicMock()
        panel._ai_webview = MagicMock()

        mock_conv = MagicMock()
        mock_conv.id = "conv_cached"
        mock_conv.messages = []
        mock_conv.created_at = 1000
        mock_conv.summary = ""
        mock_conv.system_prompt = ""
        panel._conversation_store.load_conversation.return_value = mock_conv
        panel._read_model_config.return_value = ("http://api", "key", "model", "Model Alias", 0.7, 4000, 1.0, False, "high")

        # Call _switch_to_conversation
        AIChatPanel._switch_to_conversation(panel, "conv_cached", save_current=False)

        # Assert token display was updated and cached html set
        panel._update_token_display.assert_called()
        self.assertEqual(panel._last_rendered_html, "<div>Cached HTML Content</div>")


if __name__ == "__main__":
    unittest.main()
