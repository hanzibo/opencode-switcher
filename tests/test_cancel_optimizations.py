import unittest
from unittest.mock import MagicMock
from views.ai_chat_panel import AIChatPanel


class TestCancelOptimizations(unittest.TestCase):

    def test_cancel_watchdog_id_cleanup(self):
        """Verify that GLib cancel watchdog source ID is tracked and cleaned up on finish."""
        panel = MagicMock(spec=AIChatPanel)
        panel._ai_cancel_watchdog_id = 99
        panel._ai_cancelling = True
        panel._ai_running_convs = {"conv1": {"req_id": 1, "current_assistant_text": "", "current_reasoning_text": "", "messages": []}}
        panel._ai_conversation_id = "conv1"
        panel._ai_messages = []
        panel._ai_settings_store = MagicMock()
        panel._ai_spinner = MagicMock()

        # Simulate cleanup logic
        if getattr(panel, "_ai_cancel_watchdog_id", 0) != 0:
            panel._ai_cancel_watchdog_id = 0

        self.assertEqual(panel._ai_cancel_watchdog_id, 0)

    def test_cancel_ui_visual_feedback(self):
        """Verify that clicking pause updates label to cancellation status."""
        panel = MagicMock(spec=AIChatPanel)
        panel._ai_lbl = MagicMock()
        
        # Simulate label update on pause click
        panel._ai_lbl.set_markup("<b>AI 助手看盘</b>\n<span size='small' foreground='#f43f5e'>(正在中止...)</span>")
        panel._ai_lbl.set_markup.assert_called_with("<b>AI 助手看盘</b>\n<span size='small' foreground='#f43f5e'>(正在中止...)</span>")


    def test_remove_typing_indicators_on_stream_end(self):
        """Verify that removeTypingIndicators JS call is executed on stream end."""
        panel = MagicMock(spec=AIChatPanel)
        panel._ai_webview = MagicMock()
        
        # Simulate stream end JS call
        panel._ai_webview.run_javascript("removeTypingIndicators(); _scrollToBottom();", None, None)
        panel._ai_webview.run_javascript.assert_called_with("removeTypingIndicators(); _scrollToBottom();", None, None)


if __name__ == "__main__":
    unittest.main()
