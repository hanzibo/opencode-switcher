"""Unit tests for ai_engine/llm_client.py clean_messages_for_llm function."""

import unittest
from ai_engine.llm_client import clean_messages_for_llm


class TestLLMCleaner(unittest.TestCase):
    """Test message cleaning and validation rules for OpenAI/DeepSeek API calls."""

    def test_clean_valid_conversation(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi! How can I help you?"},
        ]
        cleaned = clean_messages_for_llm(messages)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[0]["role"], "user")
        self.assertEqual(cleaned[1]["role"], "assistant")

    def test_clean_assistant_markup(self):
        messages = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": 'Sure! <details class="thinking-details">thinking...</details>\n<div class="assistant-header">Header</div>\nHere is the answer.'},
        ]
        cleaned = clean_messages_for_llm(messages)
        self.assertNotIn("thinking...", cleaned[1]["content"])
        self.assertNotIn("assistant-header", cleaned[1]["content"])
        self.assertIn("Here is the answer.", cleaned[1]["content"])

    def test_clean_system_prompt_handling(self):
        messages = [
            {"role": "system", "content": "System instruction 1"},
            {"role": "user", "content": "Hi"},
        ]
        cleaned = clean_messages_for_llm(messages)
        self.assertEqual(cleaned[0]["role"], "system")
        self.assertEqual(cleaned[0]["content"], "System instruction 1")


if __name__ == "__main__":
    unittest.main()
