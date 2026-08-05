"""Regression tests for off-main-thread clipboard image capture.

Covers the non-GTK worker/store behavior behind the marker-driven image
capture in views/clipboard_panel.py: the capture runs on a daemon worker
thread, the store update is thread-safe, and the completion callback is
marshalled back via GLib.idle_add. No display required.
"""

import os
import shutil
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from gi.repository import GLib

from stores.clipboard_store import ClipboardStore
from views.clipboard_panel import _capture_image_worker, _spawn_image_capture_thread


def _drain_glib(timeout_s=5.0):
    """Pump the default GLib main context so queued idle callbacks run."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not ctx.iteration(False):
            break
        time.sleep(0.001)


class TestCaptureImageWorker(unittest.TestCase):
    """The capture helper must not run on the caller thread and must update
    the store safely while keeping the completion callback on the main loop."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.clip_path = os.path.join(self.test_dir, "clipboard_history.json")
        self.patcher = patch("stores.clipboard_store.CLIPBOARD_PATH", self.clip_path)
        self.patcher.start()
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

    def _png(self):
        return b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 64

    @contextmanager
    def _image_env(self, png, capture_tids=None):
        def fake_capture_image():
            if capture_tids is not None:
                capture_tids.append(threading.get_ident())
            return png

        with patch(
            "stores.clipboard_store.subprocess.check_output", return_value=b"text/plain\nimage/png\n"
        ) as types_patch, patch(
            "stores.clipboard_store._capture_image", side_effect=fake_capture_image
        ) as capture_patch:
            yield types_patch, capture_patch

    def test_image_capture_runs_off_caller_thread_and_reloads_via_idle(self):
        store = ClipboardStore()
        capture_tids = []
        on_done_tids = []
        main_tid = threading.get_ident()
        png = self._png()

        with self._image_env(png, capture_tids) as (_types, _capture):
            worker = threading.Thread(
                target=_capture_image_worker,
                args=(store, lambda: on_done_tids.append(threading.get_ident())),
            )
            worker.start()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

        _drain_glib()

        items = store.get_all()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].type, "image")
        self.assertTrue(os.path.isfile(items[0].image_path))
        self.assertTrue(capture_tids)
        self.assertNotIn(main_tid, capture_tids)
        self.assertEqual(on_done_tids, [main_tid])

    def test_worker_is_daemonized(self):
        store = ClipboardStore()
        with self._image_env(self._png()) as (_types, _capture):
            thread = _spawn_image_capture_thread(store, None)
        self.assertTrue(thread.daemon)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_text_fallback_through_worker_still_captures(self):
        store = ClipboardStore()
        with self._image_env(None) as (_types, _capture), patch(
            "stores.clipboard_store.subprocess.run",
            return_value=type("R", (), {"returncode": 0, "stdout": b"hello from wl-paste"})(),
        ):
            worker = threading.Thread(target=_capture_image_worker, args=(store, None))
            worker.start()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

        items = store.get_all()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "hello from wl-paste")

    def test_repeated_concurrent_captures_deduplicate(self):
        store = ClipboardStore()
        png = self._png()

        def run():
            _capture_image_worker(store, None)

        with self._image_env(png) as (_types, _capture):
            threads = [threading.Thread(target=run) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        images = [i for i in store.get_all() if i.type == "image"]
        self.assertEqual(len(images), 1)
        self.assertTrue(all(t.is_alive() is False for t in threads))


if __name__ == "__main__":
    unittest.main()
