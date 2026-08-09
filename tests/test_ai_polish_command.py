import unittest
from unittest.mock import MagicMock, patch
from stores.clipboard_store import LLMSettingsStore, LLMModelConfig
from views.ai_chat_panel import AIChatPanel


class TestAIPolishCommand(unittest.TestCase):

    def test_get_polish_model_priority(self):
        """Test LLMSettingsStore.get_polish_model priority hierarchy."""
        store = LLMSettingsStore.__new__(LLMSettingsStore)
        m1 = LLMModelConfig(alias="Model1", base_url="http://a", api_key="k1", model_name="m1", is_default=True, is_polish_default=False)
        m2 = LLMModelConfig(alias="Model2", base_url="http://b", api_key="k2", model_name="m2", is_default=False, is_polish_default=True)
        store.models = [m1, m2]

        # 1. When a model has is_polish_default=True, it should be returned
        polish_model = store.get_polish_model()
        self.assertEqual(polish_model.alias, "Model2")

        # 2. When no model has is_polish_default=True, fallback to is_default
        m2.is_polish_default = False
        polish_model = store.get_polish_model()
        self.assertEqual(polish_model.alias, "Model1")

        # 3. When no model is default, fallback to first model
        m1.is_default = False
        polish_model = store.get_polish_model()
        self.assertEqual(polish_model.alias, "Model1")

    def test_handle_ai_polish_prompt_construction_with_history(self):
        """Test prompt construction when previous assistant message exists."""
        panel = MagicMock(spec=AIChatPanel)
        panel._ai_messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "This is the previous assistant answer."}
        ]
        panel._ai_entry = MagicMock()
        panel._update_send_button = MagicMock()
        panel._llm_settings_store = MagicMock()
        panel._llm_settings_store.get_polish_model.return_value = LLMModelConfig(
            alias="PolishModel", base_url="http://test", api_key="key", model_name="m"
        )
        panel._llm_client = MagicMock()

        with patch("threading.Thread") as mock_thread:
            AIChatPanel._handle_ai_polish_command(panel, "如何优化代码性能？")
            mock_thread.assert_called_once()

    def test_handle_ai_polish_prompt_construction_first_message(self):
        """Test prompt construction on brand new conversation (no previous assistant message)."""
        panel = MagicMock(spec=AIChatPanel)
        panel._ai_messages = []
        panel._ai_entry = MagicMock()
        panel._update_send_button = MagicMock()
        panel._llm_settings_store = MagicMock()
        panel._llm_settings_store.get_polish_model.return_value = LLMModelConfig(
            alias="PolishModel", base_url="http://test", api_key="key", model_name="m"
        )
        panel._llm_client = MagicMock()

        with patch("threading.Thread") as mock_thread:
            AIChatPanel._handle_ai_polish_command(panel, "帮我写一段 Python 脚本")
            mock_thread.assert_called_once()


if __name__ == "__main__":
    unittest.main()
