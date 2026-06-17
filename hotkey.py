import os
import socket
import threading
from typing import Optional, Callable
from utils import is_wayland

SOCKET_DIR = os.path.expanduser("~/.cache/opencode-switcher")
SOCKET_PATH = os.path.join(SOCKET_DIR, "toggle.sock")


class HotkeyManager:
    def __init__(self):
        self._listener: Optional[threading.Thread] = None
        self._socket: Optional[socket.socket] = None
        self._running = False
        self._use_pynput = False
        self.on_trigger: Optional[Callable[[], None]] = None

    def start(self):
        self._use_pynput = not is_wayland()
        if self._use_pynput:
            self._start_pynput()
        else:
            self._start_socket_listener()

    def _start_pynput(self):
        from pynput.keyboard import Listener, Key

        # ponytail: removed unused self._pynput_keys assignment
        self._pynput_pressed: set = set()

        def on_press(key):
            self._pynput_pressed.add(key)
            is_space = key == Key.space or getattr(key, 'char', None) == ' '
            has_ctrl = bool(self._pynput_pressed & {Key.ctrl, Key.ctrl_l, Key.ctrl_r})
            has_shift = bool(self._pynput_pressed & {Key.shift, Key.shift_l, Key.shift_r})
            if is_space and has_ctrl and has_shift and self.on_trigger:
                self.on_trigger()

        def on_release(key):
            self._pynput_pressed.discard(key)

        self._listener = Listener(on_press=on_press, on_release=on_release)
        self._listener.start()

    def _start_socket_listener(self):
        self._running = True
        os.makedirs(SOCKET_DIR, exist_ok=True)
        try:
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)
        except OSError:
            pass
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.bind(SOCKET_PATH)
        self._socket.listen(1)
        self._socket.settimeout(1)
        self._listener = threading.Thread(target=self._socket_loop, daemon=True)
        self._listener.start()

    def _socket_loop(self):
        while self._running:
            try:
                conn, _ = self._socket.accept()
                data = conn.recv(1024)
                conn.close()
                if data and data.strip() == b"toggle" and self.on_trigger:
                    self.on_trigger()
            except socket.timeout:
                continue
            except OSError:
                break

    def stop(self):
        self._running = False
        if self._use_pynput and self._listener is not None:
            self._listener.stop()
        self._listener = None
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        try:
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)
        except OSError:
            pass
