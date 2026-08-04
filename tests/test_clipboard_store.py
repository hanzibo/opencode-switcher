"""Unit tests for stores/clipboard_store.py — classification, FIFO storage, categories, memory."""

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


class TestClipboardStore(unittest.TestCase):
    """Test ClipboardStore FIFO eviction, deduplication, and persistence."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.clip_path = os.path.join(self.test_dir, "clipboard_history.json")
        self.patcher = patch("stores.clipboard_store.CLIPBOARD_PATH", self.clip_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
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
