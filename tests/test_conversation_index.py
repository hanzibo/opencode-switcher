"""Unit tests for ConversationStore's durable metadata index.

Covers index maintenance on save/create/fork/delete, index-served listing
without full message parsing on a warm index, rebuild on corrupt/missing
index, reconciliation of missing/unindexed/mtime-changed files, ordering,
and no data loss.
"""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from stores.clipboard_store import ConversationStore, ChatMessage


class TestConversationIndex(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = ConversationStore()
        self.store._dir = self.temp_dir.name
        self.store._index = None
        self.index_path = os.path.join(self.store._dir, self.store._INDEX_FILENAME)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _make_conv(self, title="Conv", n_messages=3, summary=None):
        conv = self.store.create_conversation(title=title)
        conv.messages = [ChatMessage(role="user", content=f"msg {i}") for i in range(n_messages)]
        if summary is not None:
            conv.summary = summary
        self.store.save_conversation(conv)
        return conv

    def _index_on_disk(self):
        with open(self.index_path) as f:
            return json.load(f)

    # ── maintenance on CRUD ─────────────────────────────────────────────────

    def test_save_creates_and_updates_index_entry(self):
        conv = self._make_conv(title="Alpha", n_messages=4, summary="sum alpha")
        self.assertTrue(os.path.isfile(self.index_path))
        disk = self._index_on_disk()["conversations"]
        self.assertIn(conv.id, disk)
        self.assertEqual(disk[conv.id]["title"], "Alpha")
        self.assertEqual(disk[conv.id]["summary"], "sum alpha")
        self.assertEqual(disk[conv.id]["message_count"], 4)
        self.assertEqual(disk[conv.id]["updated_at"], conv.updated_at)

        conv.messages.append(ChatMessage(role="assistant", content="more"))
        conv.title = "Alpha v2"
        self.store.save_conversation(conv)
        disk = self._index_on_disk()["conversations"]
        self.assertEqual(disk[conv.id]["title"], "Alpha v2")
        self.assertEqual(disk[conv.id]["message_count"], 5)

    def test_delete_removes_index_entry(self):
        conv = self._make_conv()
        self.store.delete_conversation(conv.id)
        self.assertNotIn(conv.id, self._index_on_disk()["conversations"])
        self.assertNotIn(conv.id, [s["id"] for s in self.store.list_conversations()])

    def test_fork_adds_new_index_entry(self):
        conv = self._make_conv(title="Base", n_messages=2, summary="orig sum")
        forked = self.store.fork_conversation(conv.id)
        self.assertIsNotNone(forked)
        disk = self._index_on_disk()["conversations"]
        self.assertIn(conv.id, disk)
        self.assertIn(forked.id, disk)
        self.assertEqual(disk[forked.id]["title"], "Base (Fork)")
        self.assertEqual(disk[forked.id]["message_count"], 2)
        self.assertEqual(disk[forked.id]["summary"], "orig sum")

    # ── warm index: no full message parsing ────────────────────────────────

    def test_warm_list_does_not_parse_conversation_files(self):
        for i in range(5):
            self._make_conv(title=f"Conv {i}", n_messages=10)
        # Forget the in-memory index: the on-disk index alone must be enough.
        self.store._index = None

        json_opens = []
        real_open = open  # capture before patching builtins.open

        def counting_open(path, *args, **kwargs):
            if isinstance(path, str) and path.endswith(".json"):
                json_opens.append(path)
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=counting_open):
            summaries = self.store.list_conversations()
        self.assertEqual(len(summaries), 5)
        # No conversation JSON was opened — only the index file.
        self.assertEqual(json_opens, [])

    def test_warm_list_returns_correct_metadata(self):
        convs = [self._make_conv(title=f"T{i}", n_messages=i + 1, summary=f"S{i}")
                 for i in range(3)]
        self.store._index = None  # reload from disk
        by_id = {s["id"]: s for s in self.store.list_conversations()}
        for conv in convs:
            entry = by_id[conv.id]
            self.assertEqual(entry["title"], conv.title)
            self.assertEqual(entry["summary"], conv.summary)
            self.assertEqual(entry["message_count"], len(conv.messages))
            self.assertEqual(entry["updated_at"], conv.updated_at)

    # ── corrupt / missing index rebuild ─────────────────────────────────────

    def test_missing_index_rebuilds_from_full_scan(self):
        self._make_conv(title="A", n_messages=2)
        self._make_conv(title="B", n_messages=5)
        os.remove(self.index_path)
        self.store._index = None

        summaries = self.store.list_conversations()
        self.assertEqual(len(summaries), 2)
        self.assertEqual({s["title"] for s in summaries}, {"A", "B"})
        # Index file was recreated with both entries.
        self.assertEqual(len(self._index_on_disk()["conversations"]), 2)

    def test_corrupt_index_rebuilds_from_full_scan(self):
        self._make_conv(title="A", n_messages=3)
        with open(self.index_path, "w") as f:
            f.write("{not valid json!!!")
        self.store._index = None

        summaries = self.store.list_conversations()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["title"], "A")
        self.assertEqual(summaries[0]["message_count"], 3)
        # Index file healed.
        self.assertEqual(len(self._index_on_disk()["conversations"]), 1)

    def test_partially_corrupt_index_entry_healed(self):
        conv = self._make_conv(title="Good", n_messages=7, summary="kept")
        self.store._index = None
        # Hand-craft a valid index with one stale entry (wrong mtime + wrong metadata).
        entry = self._index_on_disk()["conversations"][conv.id]
        entry["mtime"] = entry["mtime"] - 1000.0
        entry["title"] = "stale-title"
        entry["message_count"] = 0
        with open(self.index_path, "w") as f:
            json.dump({"version": 1, "conversations": {conv.id: entry}}, f)
        self.store._index = None

        summaries = self.store.list_conversations()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["title"], "Good")
        self.assertEqual(summaries[0]["message_count"], 7)
        self.assertEqual(self._index_on_disk()["conversations"][conv.id]["title"], "Good")

    # ── reconciliation with the directory ───────────────────────────────────

    def test_missing_conversation_file_dropped(self):
        conv = self._make_conv()
        os.remove(self.store._path(conv.id))  # external delete

        summaries = self.store.list_conversations()
        self.assertNotIn(conv.id, [s["id"] for s in summaries])
        self.assertNotIn(conv.id, self._index_on_disk()["conversations"])

    def test_unindexed_external_file_indexed(self):
        self._make_conv(title="Known", n_messages=1)
        ext_id = "ext123"
        with open(self.store._path(ext_id), "w") as f:
            json.dump({
                "id": ext_id,
                "title": "External",
                "summary": "came from outside",
                "messages": [{"role": "user", "content": "hi"},
                             {"role": "assistant", "content": "yo"}],
                "created_at": 1,
                "updated_at": 9999999999999,
            }, f)
        self.store._index = None  # external file also means a fresh process

        summaries = self.store.list_conversations()
        by_id = {s["id"]: s for s in summaries}
        self.assertEqual(len(by_id), 2)
        self.assertEqual(by_id[ext_id]["title"], "External")
        self.assertEqual(by_id[ext_id]["message_count"], 2)
        self.assertEqual(by_id[ext_id]["summary"], "came from outside")
        self.assertIn(ext_id, self._index_on_disk()["conversations"])

    # ── ordering ────────────────────────────────────────────────────────────

    def test_ordering_by_updated_at_descending(self):
        for i, title in enumerate(["Old", "Mid", "New"]):
            conv = self._make_conv(title=title, n_messages=1)
            conv.updated_at = 1000 + i * 1000  # Old=1000, Mid=2000, New=3000
            self.store.save_conversation(conv, bump_updated_at=False)
        titles = [s["title"] for s in self.store.list_conversations()]
        self.assertEqual(titles, ["New", "Mid", "Old"])

    # ── no data loss / compatibility ────────────────────────────────────────

    def test_no_data_loss_after_index_ops(self):
        convs = [self._make_conv(title=f"Conv {i}", n_messages=i + 1,
                                 summary=f"summary {i}") for i in range(4)]
        forked = self.store.fork_conversation(convs[0].id)
        self.store.delete_conversation(convs[2].id)
        self.store._index = None  # force a fresh process view

        summaries = self.store.list_conversations()
        by_id = {s["id"]: s for s in summaries}
        remaining = [c for i, c in enumerate(convs) if i != 2]
        self.assertEqual(len(by_id), len(remaining) + 1)  # + fork
        for conv in remaining:
            loaded = self.store.load_conversation(conv.id)
            self.assertEqual(loaded.title, conv.title)
            self.assertEqual(len(loaded.messages), len(conv.messages))
            self.assertEqual(loaded.summary, conv.summary)
            self.assertEqual(by_id[conv.id]["message_count"], len(conv.messages))
        self.assertIsNotNone(self.store.load_conversation(forked.id))
        self.assertIsNone(self.store.load_conversation(convs[2].id))

    def test_legacy_files_without_summary_indexed(self):
        conv = self._make_conv(title="Legacy", n_messages=3, summary="s")
        legacy_id = "legacy1"
        # Old-format file: no "summary" key, no "model_config_snapshot".
        with open(self.store._path(legacy_id), "w") as f:
            json.dump({
                "id": legacy_id,
                "title": "Legacy Conv",
                "messages": [{"role": "user", "content": "q"},
                             {"role": "assistant", "content": "a"},
                             {"role": "user", "content": "q2"}],
                "created_at": 5,
                "updated_at": 6,
            }, f)
        self.store._index = None

        summaries = self.store.list_conversations()
        by_id = {s["id"]: s for s in summaries}
        self.assertEqual(by_id[legacy_id]["title"], "Legacy Conv")
        self.assertEqual(by_id[legacy_id]["message_count"], 3)
        self.assertEqual(by_id[legacy_id]["summary"], "")
        self.assertEqual(by_id[legacy_id]["updated_at"], 6)
        # Existing conversation untouched.
        self.assertEqual(self.store.load_conversation(conv.id).title, "Legacy")
        self.assertNotEqual(conv.id, legacy_id)

    def test_index_write_is_atomic_no_tmp_leftover(self):
        conv = self._make_conv()
        forked = self.store.fork_conversation(conv.id)
        self.store.delete_conversation(conv.id)
        self.assertFalse(os.path.exists(self.index_path + ".tmp"))
        self.assertTrue(os.path.isfile(self.index_path))
        self.assertEqual(len(self._index_on_disk()["conversations"]), 1)

    def test_empty_store_creates_no_index(self):
        self.assertFalse(os.path.exists(self.index_path))
        self.assertEqual(self.store.list_conversations(), [])
        self.assertFalse(os.path.exists(self.index_path))


if __name__ == "__main__":
    unittest.main()
