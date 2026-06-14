#!/usr/bin/python3
import json
import os
import fcntl
import subprocess
import sys
import threading
import time
from typing import Optional
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gtk, GLib, AyatanaAppIndicator3

from hotkey import HotkeyManager
from panel import SearchPanel
from session_store import get_sessions, delete_session, rename_session
from launcher import launch_session, launch_new_session, launch_session_pure
from clipboard_store import ClipboardStore, PromptStore, CategoryStore, capture_clipboard_once
from clipboard_panel import ClipboardPanel
from utils import is_wayland

CONFIG_DIR = os.path.expanduser("~/.config/opencode-switcher")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {"theme": "dark"}


def _save_config(config: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f)


class App:
    def __init__(self):
        config = _load_config()
        self._theme = config.get("theme", "dark")
        self._clip_store = ClipboardStore()
        self._prompt_store = PromptStore()
        self._cat_store = CategoryStore()
        self._panel = SearchPanel()
        self._panel.set_theme(self._theme)

        clip_panel = ClipboardPanel(self._clip_store, self._prompt_store, self._cat_store)
        clip_panel.on_copy_clipboard = self._on_clipboard_copied
        clip_panel.on_hide_request = lambda: GLib.idle_add(self._panel.hide)
        self._panel.set_clipboard_panel(clip_panel, self._clip_store, self._prompt_store, self._cat_store)

        self._hotkey = HotkeyManager()
        self._running = True
        self._restart_requested = False
        self._indicator = self._build_indicator()

        self._panel.on_select = self._on_session_selected
        self._panel.on_open = self._on_panel_opened
        self._panel.on_delete_session = self._on_delete_session
        self._panel.on_rename_session = self._on_rename_session
        self._panel.on_launch_pure = self._on_session_launch_pure
        self._hotkey.on_trigger = lambda: self._on_hotkey()

    def _clipboard_loop(self):
        """Background daemon thread: initial capture + periodic polling on X11."""
        capture_clipboard_once(self._clip_store)
        while self._running:
            if not is_wayland():
                capture_clipboard_once(self._clip_store)
            time.sleep(3)

    def _build_indicator(self):
        ind = AyatanaAppIndicator3.Indicator.new(
            "opencode-switcher",
            "utilities-terminal",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        ind.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)

        menu = Gtk.Menu.new()
        show_item = Gtk.MenuItem.new_with_label("Show / Hide")
        show_item.connect("activate", lambda *_: GLib.idle_add(self._panel.toggle))
        menu.append(show_item)

        theme_menu = Gtk.Menu.new()
        dark_item = Gtk.RadioMenuItem.new_with_label(None, "Dark")
        light_item = Gtk.RadioMenuItem.new_with_label_from_widget(dark_item, "Light")
        if self._theme == "light":
            light_item.set_active(True)
        dark_item.connect("toggled", lambda item: self._on_theme_changed("dark") if item.get_active() else None)
        light_item.connect("toggled", lambda item: self._on_theme_changed("light") if item.get_active() else None)
        theme_menu.append(dark_item)
        theme_menu.append(light_item)
        theme_menu_item = Gtk.MenuItem.new_with_label("Theme")
        theme_menu_item.set_submenu(theme_menu)
        menu.append(theme_menu_item)

        menu.append(Gtk.SeparatorMenuItem.new())
        restart_item = Gtk.MenuItem.new_with_label("Restart")
        restart_item.connect("activate", lambda *_: GLib.idle_add(self._on_restart))
        menu.append(restart_item)
        quit_item = Gtk.MenuItem.new_with_label("Quit")
        quit_item.connect("activate", lambda *_: GLib.idle_add(self._confirm_quit))
        menu.append(quit_item)
        menu.show_all()

        ind.set_menu(menu)
        return ind

    def _on_theme_changed(self, theme: str):
        self._theme = theme
        self._panel.set_theme(theme)
        config = _load_config()
        config["theme"] = theme
        _save_config(config)

    def _confirm_quit(self):
        dialog = Gtk.MessageDialog(
            transient_for=None,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Quit OpenCode Switcher?",
        )
        dialog.format_secondary_text("The app will stop and the hotkey will be unregistered.")
        dialog.connect("response", self._on_quit_response)
        dialog.show_all()

    def _on_quit_response(self, dialog, response):
        dialog.destroy()
        if response == Gtk.ResponseType.YES:
            self.stop()

    def _on_restart(self):
        self._restart_requested = True
        self.stop()

    def _on_clipboard_copied(self, text: str, item_hash: Optional[str] = None):
        self._clip_store.mark_written(text, item_hash)

    def _on_hotkey(self):
        GLib.idle_add(self._panel.toggle)

    def run(self):
        self._hotkey.start()
        threading.Thread(target=self._clipboard_loop, daemon=True).start()
        Gtk.main()

    def stop(self):
        self._running = False
        self._hotkey.stop()
        self._indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.PASSIVE)
        self._indicator = None
        Gtk.main_quit()

    def _on_panel_opened(self):
        self._session_load_seq = getattr(self, "_session_load_seq", 0) + 1
        seq = self._session_load_seq

        def _bg_load(seq_val):
            try:
                sessions = get_sessions()
                def _apply():
                    if seq_val == self._session_load_seq:
                        self._panel.load_sessions(sessions)
                    return False
                GLib.idle_add(_apply)
            except Exception as e:
                print(f"Error loading sessions in background: {e}", flush=True)

        threading.Thread(target=_bg_load, args=(seq,), daemon=True).start()

    def _on_session_selected(self, session):
        try:
            if session.id == "new-opencode":
                err = launch_new_session(session.directory)
            else:
                err = launch_session(session.id, session.directory)
            if err:
                print(f"opencode-switcher: {err}", flush=True)
                GLib.idle_add(self._show_error, err)
        except Exception as e:
            print(f"opencode-switcher: Crash: {e}", flush=True)
            import traceback
            traceback.print_exc()

    def _on_session_launch_pure(self, session):
        try:
            err = launch_session_pure(session.id, session.directory)
            if err:
                print(f"opencode-switcher: {err}", flush=True)
                GLib.idle_add(self._show_error, err)
        except Exception as e:
            print(f"opencode-switcher: Crash: {e}", flush=True)
            import traceback
            traceback.print_exc()

    def _show_error(self, msg: str):
        dialog = Gtk.MessageDialog(
            transient_for=None,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text="Failed to launch session",
        )
        dialog.format_secondary_text(msg)
        dialog.connect("response", lambda dlg, _: dlg.destroy())
        dialog.show_all()

    def _on_delete_session(self, session):
        def on_confirm(dialog, response):
            dialog.destroy()
            if response != Gtk.ResponseType.YES:
                return
            err = delete_session(session.id)
            if err:
                GLib.idle_add(lambda: self._show_error(err))
            else:
                sessions = get_sessions()
                self._panel.load_sessions(sessions)
        dialog = Gtk.MessageDialog(
            transient_for=None,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f'Delete "{session.title}"?',
        )
        dialog.format_secondary_text("This session will be archived and hidden from the list.")
        dialog.connect("response", on_confirm)
        dialog.show_all()

    def _on_rename_session(self, session_id: str, new_title: str):
        err = rename_session(session_id, new_title)
        if err:
            GLib.idle_add(lambda: self._show_error(err))
        else:
            sessions = get_sessions()
            self._panel.load_sessions(sessions)


if __name__ == "__main__":
    import traceback

    # Single-instance lock
    LOCK_PATH = os.path.join(CONFIG_DIR, "lock")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        lock_fd = open(LOCK_PATH, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        print("opencode-switcher: another instance is already running", flush=True)
        sys.exit(0)

    app = App()
    try:
        app.run()
    except KeyboardInterrupt:
        app.stop()
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    if app._restart_requested:
        lock_fd.close()
        subprocess.Popen([sys.executable] + sys.argv, stderr=subprocess.DEVNULL)
