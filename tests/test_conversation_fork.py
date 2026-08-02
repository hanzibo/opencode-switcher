import os
import tempfile
import unittest
from unittest.mock import patch
from stores.clipboard_store import ConversationStore, Conversation, ChatMessage


class TestConversationFork(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = ConversationStore()
        self.store._dir = self.temp_dir.name

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_fork_conversation_default_title(self):
        # Create original conversation
        conv = self.store.create_conversation(
            title="Original Topic",
            system_prompt="You are a helpful coding assistant.",
            model_config={"model_name": "deepseek-chat", "alias": "DeepSeek V3"}
        )
        conv.messages = [
            ChatMessage(role="user", content="Hello world"),
            ChatMessage(role="assistant", content="Hi there!", reasoning_content="Thinking..."),
            ChatMessage(role="user", content="Execute tool"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[{"id": "call_1", "function": {"name": "bash", "arguments": '{"cmd":"ls"}'}}]
            ),
            ChatMessage(role="tool", content="file.txt", tool_call_id="call_1", name="bash")
        ]
        self.store.save_conversation(conv)

        # Fork conversation
        forked = self.store.fork_conversation(conv.id)

        self.assertIsNotNone(forked)
        self.assertNotEqual(forked.id, conv.id)
        self.assertEqual(forked.title, "Original Topic (Fork)")
        self.assertEqual(forked.system_prompt, conv.system_prompt)
        self.assertEqual(len(forked.messages), 5)
        self.assertEqual(forked.messages[0].content, "Hello world")
        self.assertEqual(forked.messages[1].reasoning_content, "Thinking...")
        self.assertEqual(forked.messages[3].tool_calls, [{"id": "call_1", "function": {"name": "bash", "arguments": '{"cmd":"ls"}'}}])
        self.assertEqual(forked.messages[4].tool_call_id, "call_1")
        self.assertEqual(forked.model_config_snapshot, {"model_name": "deepseek-chat", "alias": "DeepSeek V3"})

        # Verify disk persistence for both files
        src_path = self.store._path(conv.id)
        fork_path = self.store._path(forked.id)
        self.assertTrue(os.path.isfile(src_path))
        self.assertTrue(os.path.isfile(fork_path))

    def test_fork_conversation_custom_title(self):
        conv = self.store.create_conversation(title="Base Conversation")
        conv.messages = [ChatMessage(role="user", content="Test prompt")]
        self.store.save_conversation(conv)

        forked = self.store.fork_conversation(conv.id, new_title="Custom Branch Title")

        self.assertIsNotNone(forked)
        self.assertEqual(forked.title, "Custom Branch Title")

    def test_fork_conversation_nested_model_config_deepcopy(self):
        conv = self.store.create_conversation(
            title="Nested Model Config Test",
            model_config={"nested": {"param": 123}}
        )
        self.store.save_conversation(conv)

        forked = self.store.fork_conversation(conv.id)
        self.assertIsNotNone(forked)
        
        # Modify forked nested dict, verify original remains unchanged
        forked.model_config_snapshot["nested"]["param"] = 999
        self.assertEqual(conv.model_config_snapshot["nested"]["param"], 123)

    def test_fork_conversation_save_disk_error(self):
        conv = self.store.create_conversation(title="Disk Error Test")
        self.store.save_conversation(conv)

        with patch.object(self.store, "save_conversation", side_effect=OSError("Disk full")):
            forked = self.store.fork_conversation(conv.id)
            self.assertIsNone(forked)

    def test_fork_nonexistent_conversation(self):
        forked = self.store.fork_conversation("non_existent_id")
        self.assertIsNone(forked)


if __name__ == "__main__":
    unittest.main()
