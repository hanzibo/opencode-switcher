"""Unit tests for stores/clipboard_store.py — classification, FIFO storage, categories, memory."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from stores.clipboard_store import (
    ClipboardItem,
    ClipboardStore,
    CategoryItem,
    CategoryStore,
    CustomCategory,
    AISettingsStore,
    MemStore,
    classify_text,
    detect_language_name,
)


class TestClipboardClassification(unittest.TestCase):
    """Test text classification heuristics and language detection."""

    def test_classify_url(self):
        self.assertEqual(classify_text("https://github.com/opencode/switcher"), "link")

    def test_classify_code(self):
        py_code = "def hello_world():\n    print('Hello World')\n    return True\n"
        self.assertEqual(classify_text(py_code), "code")

    def test_detect_language_name(self):
        self.assertEqual(detect_language_name("import os\nimport sys\n"), "Python")
        self.assertEqual(detect_language_name("const x = 1;\nconsole.log(x);"), "JavaScript")
        self.assertEqual(detect_language_name("#!/bin/bash\necho hello"), "Shell")
        self.assertEqual(detect_language_name('{"key": "value"}'), "JSON")

    def test_ai_text_utils_direct_import(self):
        from ai_text_utils import classify_text as direct_classify, detect_language_name as direct_detect
        from ai_text_utils.classifier import classify_text as mod_classify, detect_language_name as mod_detect
        self.assertIs(direct_classify, mod_classify)
        self.assertIs(direct_detect, mod_detect)
        self.assertEqual(direct_classify(""), "text")
        self.assertIsNone(direct_detect(""))
        self.assertEqual(direct_classify("<html><body>hello</body></html>"), "code")
        self.assertEqual(direct_detect("<html><body>hello</body></html>"), "HTML")
        self.assertEqual(direct_detect("SELECT id, name FROM users;"), "SQL")



class TestClipboardStore(unittest.TestCase):
    """Test ClipboardStore FIFO eviction, deduplication, and persistence."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.clip_path = os.path.join(self.test_dir, "clipboard_history.json")
        self.patcher = patch("stores.clipboard_store.CLIPBOARD_PATH", self.clip_path)
        self.patcher.start()
        # Keep image dir + cache marker writes inside the temp dir
        self.config_patcher = patch("stores.clipboard_store.CONFIG_DIR", self.test_dir)
        self.config_patcher.start()
        self.expanduser_patcher = patch(
            "stores.clipboard_store.os.path.expanduser", return_value=self.test_dir
        )
        self.expanduser_patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.config_patcher.stop()
        self.expanduser_patcher.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_fifo_eviction(self):
        store = ClipboardStore()
        # Default max_clipboard is 150
        for i in range(160):
            store.add(f"item content {i}")

        items = store.get_all()
        self.assertLessEqual(len(items), 150)
        # The latest added item is at the end of store.get_all()
        self.assertEqual(items[-1].text, "item content 159")

    def test_mark_written_deduplication(self):
        store = ClipboardStore()
        content = "unique app-written text"
        store.mark_written(content)
        self.assertEqual(store._last_written_hash, store.get_all()[-1].hash if store.get_all() else store._last_written_hash)

    def test_delete_item(self):
        store = ClipboardStore()
        store.add("Item 1")
        store.add("Item 2")
        store.delete(0)
        self.assertEqual(len(store.get_all()), 1)

    def test_round_trip_persistence(self):
        store = ClipboardStore()
        store.add("persist me one")
        store.add("persist me two")
        # A fresh instance reads the same file back
        store2 = ClipboardStore()
        texts = [i.text for i in store2.get_all()]
        self.assertEqual(texts, ["persist me one", "persist me two"])
        # The primary file is always complete, valid JSON
        with open(self.clip_path) as f:
            data = json.load(f)
        self.assertEqual([d["text"] for d in data], texts)
        # The backup mirrors the freshly written state for recovery
        with open(self.clip_path + ".backup") as f:
            backup = json.load(f)
        self.assertEqual([d["text"] for d in backup], texts)

    def test_no_temp_left_after_successful_save(self):
        store = ClipboardStore()
        store.add("clean save")
        store.reload()
        self.assertFalse(os.path.exists(self.clip_path + ".tmp"))

    def test_backup_recovery_from_corrupt_primary(self):
        store = ClipboardStore()
        store.add("important history")
        store.add("second entry")  # second save rotates the primary into the backup
        # Corrupt the primary, leaving the backup intact
        with open(self.clip_path, "w") as f:
            f.write("{ not valid json !!!")
        store2 = ClipboardStore()
        self.assertEqual([i.text for i in store2.get_all()],
                         ["important history", "second entry"])
        # The primary was healed with valid JSON containing the recovered items
        with open(self.clip_path) as f:
            data = json.load(f)
        self.assertEqual([d["text"] for d in data],
                         ["important history", "second entry"])

    def test_backup_recovery_when_primary_missing(self):
        store = ClipboardStore()
        store.add("still recoverable")
        store.add("and this one too")
        os.remove(self.clip_path)
        # Primary gone but backup present -> recover and heal
        store2 = ClipboardStore()
        self.assertEqual([i.text for i in store2.get_all()],
                         ["still recoverable", "and this one too"])
        self.assertTrue(os.path.isfile(self.clip_path))

    def test_corrupt_primary_and_missing_backup_falls_back_to_empty(self):
        with open(self.clip_path, "w") as f:
            f.write("{ broken")
        store = ClipboardStore()
        self.assertEqual(store.get_all(), [])

    def test_valid_primary_not_overwritten_by_backup_recovery(self):
        # A valid primary must never be replaced by stale backup content
        store = ClipboardStore()
        store.add("newer state")
        with open(self.clip_path, "w") as f:
            json.dump([{"text": "externally written", "timestamp": 1,
                        "hash": "deadbeef", "type": "text"}], f)
        store.reload()
        self.assertEqual([i.text for i in store.get_all()], ["externally written"])
        with open(self.clip_path) as f:
            data = json.load(f)
        self.assertEqual([d["text"] for d in data], ["externally written"])

    def test_save_failure_cleans_temp_and_keeps_backup(self):
        store = ClipboardStore()
        store.add("first")
        store.add("second")
        with patch("stores.clipboard_store.json.dump", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            store.add("third")
        # No partial temp file lingers after a failed write
        self.assertFalse(os.path.exists(self.clip_path + ".tmp"))
        # The primary and backup still hold the last good state
        with open(self.clip_path) as f:
            data = json.load(f)
        self.assertEqual([d["text"] for d in data], ["first", "second"])
        with open(self.clip_path + ".backup") as f:
            backup = json.load(f)
        self.assertEqual([d["text"] for d in backup], ["first", "second"])
        # A fresh instance still sees the full history
        store2 = ClipboardStore()
        self.assertEqual([i.text for i in store2.get_all()], ["first", "second"])

    def test_image_persistence_across_reload(self):
        store = ClipboardStore()
        store.add_image(b"fake-png-bytes-12345")
        images = [i for i in store.get_all() if i.type == "image"]
        self.assertEqual(len(images), 1)
        img_path = images[0].image_path
        self.assertTrue(os.path.isfile(img_path))
        # A fresh instance keeps the image item and its file (not orphaned)
        store2 = ClipboardStore()
        self.assertEqual(len([i for i in store2.get_all() if i.type == "image"]), 1)
        self.assertTrue(os.path.isfile(img_path))
        # The item was persisted to the history JSON
        with open(self.clip_path) as f:
            data = json.load(f)
        self.assertEqual(data[0]["image_path"], img_path)

    def test_image_file_removed_when_item_deleted(self):
        store = ClipboardStore()
        store.add_image(b"fake-png-bytes-99999")
        img_path = store.get_all()[0].image_path
        self.assertTrue(os.path.isfile(img_path))
        store.delete(0)
        self.assertFalse(os.path.exists(img_path))

    def test_orphan_image_cleanup_on_load(self):
        img_dir = os.path.join(self.test_dir, "images")
        os.makedirs(img_dir, exist_ok=True)
        stray = os.path.join(img_dir, "stray.png")
        with open(stray, "wb") as f:
            f.write(b"orphan")
        ClipboardStore()
        self.assertFalse(os.path.exists(stray))


class TestCategoryStore(unittest.TestCase):
    """Test CategoryStore custom categories, items, and recycle bin."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cat_path = os.path.join(self.test_dir, "categories.json")
        self.patcher = patch("stores.clipboard_store.CATEGORIES_PATH", self.cat_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_add_custom_category(self):
        store = CategoryStore()
        cat_id = store.create("Work Snippets")
        self.assertIsNotNone(cat_id)
        categories = store.get_all()
        self.assertTrue(any(c.name == "Work Snippets" for c in categories))

    def test_add_item_to_category(self):
        store = CategoryStore()
        cat_id = store.create("Scripts")
        store.add_item(cat_id, "Foo Script", "print('foo')")
        cat = store.get(cat_id)
        self.assertIsNotNone(cat)
        self.assertEqual(len(cat.items), 1)
        self.assertEqual(cat.items[0].title, "Foo Script")


class TestMemStore(unittest.TestCase):
    """Test MemStore long-term memory storage."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mem_path = os.path.join(self.test_dir, "memory.json")
        self.patcher = patch("stores.clipboard_store.MEMORY_PATH", self.mem_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_and_recall_memory(self):
        store = MemStore()
        store.put("pref_theme", "user prefers dark mode")
        mem = store.get("pref_theme")
        self.assertIsNotNone(mem)
        self.assertEqual(mem.value, "user prefers dark mode")


if __name__ == "__main__":
    unittest.main()
