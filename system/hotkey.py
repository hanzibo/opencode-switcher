import os
import socket
import threading
from typing import Optional, Callable

SOCKET_DIR = os.path.expanduser("~/.cache/opencode-switcher")
SOCKET_PATH = os.path.join(SOCKET_DIR, "toggle.sock")


class HotkeyManager:
    def __init__(self):
        self._listener: Optional[threading.Thread] = None
        self._socket: Optional[socket.socket] = None
        self._running = False
        self.on_trigger: Optional[Callable[[], None]] = None
        self.on_trigger_ai: Optional[Callable[[], None]] = None

    def start(self):
        self._start_socket_listener()

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
                if data:
                    msg = data.strip()
                    if msg == b"toggle" and self.on_trigger:
                        self.on_trigger()
                    elif msg == b"toggle_ai" and self.on_trigger_ai:
                        self.on_trigger_ai()
            except socket.timeout:
                continue
            except OSError:
                break

    def stop(self):
        self._running = False
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
