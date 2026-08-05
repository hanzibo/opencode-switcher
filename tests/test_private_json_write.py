"""Unit tests for system.utils.write_json_private — user-private JSON writes (0o600)."""

import json
import os
import shutil
import stat
import tempfile
import unittest

from system.utils import write_json_private


class TestWriteJsonPrivate(unittest.TestCase):
    """Test that JSON writes create/preserve 0o600 regardless of umask."""

    def setUp(self):
        # Force a permissive umask so a plain open("w") would produce 0644 —
        # proves the helper overrides umask instead of depending on it.
        self._old_umask = os.umask(0o022)
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "sub", "data.json")

    def tearDown(self):
        os.umask(self._old_umask)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_file_with_0600(self):
        write_json_private(self.path, {"version": 1, "todos": [], "next_id": 1})
        self.assertTrue(os.path.isfile(self.path))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_roundtrip_content(self):
        data = {"version": 1, "todos": [{"id": "todo_abc", "title": "hi"}], "next_id": 2}
        write_json_private(self.path, data)
        with open(self.path, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), data)

    def test_existing_world_readable_file_tightened_to_0600(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"leaky": True}, f)
        os.chmod(self.path, 0o644)
        write_json_private(self.path, {"secure": True})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        with open(self.path, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"secure": True})


if __name__ == "__main__":
    unittest.main()
