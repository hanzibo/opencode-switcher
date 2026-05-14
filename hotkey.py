from pynput.keyboard import Listener, Key
from typing import Optional, Callable


class HotkeyManager:
    HOTKEY_MODIFIERS = {Key.ctrl, Key.ctrl_l, Key.ctrl_r}
    HOTKEY_SHIFT = {Key.shift, Key.shift_l, Key.shift_r}

    def __init__(self):
        self._listener: Optional[Listener] = None
        self._pressed: set = set()
        self.on_trigger: Optional[Callable[[], None]] = None

    def start(self):
        self._listener = Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()

    def stop(self):
        if self._listener:
            self._listener.stop()
            self._listener = None

    def _on_press(self, key):
        self._pressed.add(key)
        is_space = key == Key.space or getattr(key, 'char', None) == ' '
        has_ctrl = bool(self._pressed & self.HOTKEY_MODIFIERS)
        has_shift = bool(self._pressed & self.HOTKEY_SHIFT)
        if is_space and has_ctrl and has_shift and self.on_trigger:
            self.on_trigger()

    def _on_release(self, key):
        self._pressed.discard(key)
