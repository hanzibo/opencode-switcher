"""Unit tests for tool_registry/filesystem.py — safe path checks, read/write, list_dir."""

import os
import shutil
import tempfile
import unittest

from tool_registry.filesystem import (
    execute_read_file,
    execute_write_file,
    execute_list_directory,
    execute_file_info,
)


class TestFilesystemTools(unittest.TestCase):
    """Test filesystem AI tool executors and boundary guards."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.file1 = os.path.join(self.test_dir, "hello.py")
        with open(self.file1, "w", encoding="utf-8") as f:
            f.write("def foo():\n    print('Hello World')\n\ndef bar():\n    return 42\n")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_read_file(self):
        res = execute_read_file(path=self.file1)
        self.assertIn("def foo():", res)
        self.assertIn("Hello World", res)

    def test_read_file_slice(self):
        res = execute_read_file(path=self.file1, start_line=1, max_chars=20)
        self.assertIn("def foo():", res)

    def test_write_file(self):
        new_file = os.path.join(self.test_dir, "sub", "test.txt")
        res = execute_write_file(
            path=new_file,
            content="Created content",
            force=True,
        )
        self.assertTrue(os.path.isfile(new_file))

    def test_list_directory(self):
        res = execute_list_directory(path=self.test_dir)
        self.assertIn("hello.py", res)

    def test_file_info(self):
        res = execute_file_info(path=self.file1)
        self.assertIn("hello.py", res)


if __name__ == "__main__":
    unittest.main()
