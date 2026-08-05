"""Unit tests for the startup permission sweep and private write modes.

Covers: legacy 0644 sensitive files tightened to 0600, private data dirs
becoming 0700, safe no-ops for missing paths / symlinks / permission errors,
and newly written clipboard/conversation/memory/todo data being 0600.
"""

import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from system.utils import (
    harden_dir_private,
    harden_file_private,
    harden_json_files_in_dir,
    sweep_sensitive_permissions,
)


class TestHardenFilePrivate(unittest.TestCase):
    """harden_file_private: 0600 tightening without symlink following."""

    def setUp(self):
        self._old_umask = os.umask(0o022)
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        os.umask(self._old_umask)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_path_is_safe_noop(self):
        self.assertFalse(harden_file_private(os.path.join(self.tmp, "nope.json")))

    def test_legacy_0644_becomes_0600(self):
        path = os.path.join(self.tmp, "clipboard_history.json")
        with open(path, "w") as f:
            f.write("{}")
        os.chmod(path, 0o644)
        self.assertTrue(harden_file_private(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_symlink_target_never_chmodded(self):
        target = os.path.join(self.tmp, "secret.txt")
        with open(target, "w") as f:
            f.write("do not touch")
        os.chmod(target, 0o644)
        link = os.path.join(self.tmp, "clipboard_history.json")
        os.symlink(target, link)
        harden_file_private(link)
        # Only the link itself may change; the target must keep its mode.
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o644)

    def test_permission_error_is_safe(self):
        path = os.path.join(self.tmp, "data.json")
        with open(path, "w") as f:
            f.write("{}")
        with patch("os.chmod", side_effect=OSError(13, "Permission denied")):
            self.assertFalse(harden_file_private(path))
        self.assertTrue(os.path.isfile(path))


class TestHardenDirPrivate(unittest.TestCase):
    """harden_dir_private: 0700 creation/tightening, symlinked dirs skipped."""

    def setUp(self):
        self._old_umask = os.umask(0o022)
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        os.umask(self._old_umask)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_dir_created_0700(self):
        path = os.path.join(self.tmp, "sub", "conversations")
        self.assertTrue(harden_dir_private(path))
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)

    def test_existing_0755_dir_tightened_to_0700(self):
        path = os.path.join(self.tmp, "config")
        os.makedirs(path, exist_ok=True)
        os.chmod(path, 0o755)
        self.assertTrue(harden_dir_private(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)

    def test_symlinked_dir_skipped(self):
        real = os.path.join(self.tmp, "real")
        os.makedirs(real)
        os.chmod(real, 0o755)
        link = os.path.join(self.tmp, "config")
        os.symlink(real, link)
        self.assertFalse(harden_dir_private(link))
        self.assertEqual(stat.S_IMODE(os.stat(real).st_mode), 0o755)


class TestHardenJsonFilesInDir(unittest.TestCase):
    """harden_json_files_in_dir: only direct *.json children are touched."""

    def setUp(self):
        self._old_umask = os.umask(0o022)
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        os.umask(self._old_umask)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_only_json_children_chmodded(self):
        d = os.path.join(self.tmp, "conversations")
        os.makedirs(d)
        a = os.path.join(d, "a.json")
        b = os.path.join(d, "b.txt")
        nested = os.path.join(d, "sub")
        os.makedirs(nested)
        for p in (a, b):
            with open(p, "w") as f:
                f.write("x")
        os.chmod(a, 0o644)
        os.chmod(b, 0o644)
        self.assertEqual(harden_json_files_in_dir(d), 1)
        self.assertEqual(stat.S_IMODE(os.stat(a).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(b).st_mode), 0o644)

    def test_missing_dir_returns_zero(self):
        self.assertEqual(harden_json_files_in_dir(os.path.join(self.tmp, "nope")), 0)


class TestSweepSensitivePermissions(unittest.TestCase):
    """End-to-end sweep with store paths patched to temp directories."""

    def setUp(self):
        self._old_umask = os.umask(0o022)
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "config")
        self.conv_dir = os.path.join(self.tmp, "cache", "conversations")
        self.gmail_dir = os.path.join(self.cfg, "gmail_credentials")
        for d in (self.cfg, self.conv_dir, self.gmail_dir):
            os.makedirs(d)
            os.chmod(d, 0o755)

        def _p(name):
            return os.path.join(self.cfg, name)

        self.patchers = [
            patch("stores.clipboard_store.CONFIG_DIR", self.cfg),
            patch("stores.clipboard_store.CLIPBOARD_PATH", _p("clipboard_history.json")),
            patch("stores.clipboard_store.MEMORY_PATH", _p("agent_memory.json")),
            patch("stores.clipboard_store.LLM_SETTINGS_PATH", _p("llm_settings.json")),
            patch("stores.clipboard_store.QQ_MAIL_CREDENTIALS_PATH", _p("qq_mail_credentials.json")),
            patch("stores.clipboard_store.AI_SETTINGS_PATH", _p("ai_settings.json")),
            patch("stores.clipboard_store.CATEGORIES_PATH", _p("categories.json")),
            patch("stores.clipboard_store.CUSTOM_PROMPTS_PATH", _p("custom_prompts.json")),
            patch("stores.clipboard_store.GMAIL_CREDENTIALS_DIR", self.gmail_dir),
            patch("system.utils.CONVERSATIONS_DIR", self.conv_dir),
            patch("tool_registry.todo._TODO_PATH", _p("todos.json")),
        ]
        for p in self.patchers:
            p.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for p in self.patchers:
            p.stop()
        os.umask(self._old_umask)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_legacy(self, rel, mode=0o644):
        path = os.path.join(self.cfg, rel)
        with open(path, "w") as f:
            f.write("{}")
        os.chmod(path, mode)
        return path

    def test_legacy_0644_files_become_0600(self):
        legacy = [
            self._write_legacy("clipboard_history.json"),
            self._write_legacy("clipboard_history.json.backup"),
            self._write_legacy("agent_memory.json"),
            self._write_legacy("todos.json"),
            self._write_legacy("llm_settings.json"),
            self._write_legacy("ai_settings.json"),
        ]
        conv = os.path.join(self.conv_dir, "abc123.json")
        with open(conv, "w") as f:
            f.write("{}")
        os.chmod(conv, 0o644)
        idx = os.path.join(self.conv_dir, ".conversations.index")
        with open(idx, "w") as f:
            f.write("{}")
        os.chmod(idx, 0o644)

        sweep_sensitive_permissions()

        for path in legacy + [conv, idx]:
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)

    def test_private_dirs_become_0700(self):
        sweep_sensitive_permissions()
        for d in (self.cfg, self.conv_dir, self.gmail_dir):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700, d)

    def test_missing_paths_are_safe_noop(self):
        sweep_sensitive_permissions()
        self.assertFalse(os.path.exists(os.path.join(self.cfg, "clipboard_history.json")))
        self.assertFalse(os.path.exists(os.path.join(self.cfg, "todos.json")))

    def test_unrelated_files_untouched(self):
        unrelated = os.path.join(self.tmp, "unrelated.txt")
        with open(unrelated, "w") as f:
            f.write("project file")
        os.chmod(unrelated, 0o644)
        stray = os.path.join(self.conv_dir, "notes.txt")
        with open(stray, "w") as f:
            f.write("not json")
        os.chmod(stray, 0o644)

        sweep_sensitive_permissions()

        self.assertEqual(stat.S_IMODE(os.stat(unrelated).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(stray).st_mode), 0o644)

    def test_symlinked_sensitive_file_not_followed(self):
        target = os.path.join(self.cfg, "real_secret.json")
        with open(target, "w") as f:
            f.write("{}")
        os.chmod(target, 0o644)
        os.symlink(target, os.path.join(self.cfg, "clipboard_history.json"))

        sweep_sensitive_permissions()

        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o644)


class TestPrivateWriteModes(unittest.TestCase):
    """Newly written clipboard/conversation/memory/todo data must be 0o600."""

    def setUp(self):
        self._old_umask = os.umask(0o022)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        os.umask(self._old_umask)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clipboard_atomic_write_is_0600(self):
        from stores.clipboard_store import ClipboardItem, ClipboardStore

        path = os.path.join(self.tmp, "clipboard_history.json")
        with patch("stores.clipboard_store.CLIPBOARD_PATH", path):
            with patch("stores.clipboard_store.CONFIG_DIR", self.tmp):
                store = ClipboardStore()
                item = ClipboardItem(text="secret", timestamp=1, hash="h1")
                ClipboardStore._write_items_to(path, [item])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            self.assertEqual(json.load(f)[0]["text"], "secret")

    def test_conversation_save_and_index_are_0600(self):
        from stores.clipboard_store import ConversationStore

        store = ConversationStore()
        store._dir = self.tmp
        store._index = None
        conv = store.create_conversation(title="T")
        conv_path = os.path.join(self.tmp, conv.id + ".json")
        self.assertEqual(stat.S_IMODE(os.stat(conv_path).st_mode), 0o600)
        idx_path = os.path.join(self.tmp, store._INDEX_FILENAME)
        self.assertTrue(os.path.isfile(idx_path))
        self.assertEqual(stat.S_IMODE(os.stat(idx_path).st_mode), 0o600)

    def test_memory_save_is_0600(self):
        from stores.clipboard_store import MemStore

        mem_path = os.path.join(self.tmp, "agent_memory.json")
        with patch("stores.clipboard_store.MEMORY_PATH", mem_path):
            store = MemStore()
            store.put("key", "value")
            store.save()
        self.assertEqual(stat.S_IMODE(os.stat(mem_path).st_mode), 0o600)

    def test_todo_save_is_0600(self):
        from tool_registry.todo import _save_todos

        todo_path = os.path.join(self.tmp, "todos.json")
        with patch("tool_registry.todo._TODO_PATH", todo_path):
            _save_todos({"version": 1, "todos": [], "next_id": 1})
        self.assertEqual(stat.S_IMODE(os.stat(todo_path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
