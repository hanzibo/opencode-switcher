#!/usr/bin/python3
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gtk, GLib, AyatanaAppIndicator3

from hotkey import HotkeyManager
from panel import SearchPanel
from session_store import get_sessions, delete_session
from launcher import launch_session, launch_new_session


class App:
    def __init__(self):
        self._panel = SearchPanel()
        self._hotkey = HotkeyManager()
        self._indicator = self._build_indicator()

        self._panel.on_select = self._on_session_selected
        self._panel.on_open = self._on_panel_opened
        self._panel.on_delete_session = self._on_delete_session
        self._hotkey.on_trigger = lambda: self._on_hotkey()

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
        menu.append(Gtk.SeparatorMenuItem.new())
        quit_item = Gtk.MenuItem.new_with_label("Quit")
        quit_item.connect("activate", lambda *_: GLib.idle_add(self._confirm_quit))
        menu.append(quit_item)
        menu.show_all()

        ind.set_menu(menu)
        return ind

    def _confirm_quit(self):
        dialog = Gtk.MessageDialog(
            transient_for=None,
            flags=Gtk.DialogFlags.MODAL,
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

    def _on_hotkey(self):
        GLib.idle_add(self._panel.toggle)

    def run(self):
        self._hotkey.start()
        Gtk.main()

    def stop(self):
        self._hotkey.stop()
        self._indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.PASSIVE)
        self._indicator = None
        Gtk.main_quit()

    def _on_panel_opened(self):
        sessions = get_sessions()
        self._panel.load_sessions(sessions)

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

    def _show_error(self, msg: str):
        dialog = Gtk.MessageDialog(
            transient_for=None,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text="Failed to launch session",
        )
        dialog.format_secondary_text(msg)
        dialog.run()
        dialog.destroy()

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
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f'Delete "{session.title}"?',
        )
        dialog.format_secondary_text("This session will be archived and hidden from the list.")
        dialog.connect("response", on_confirm)
        dialog.show_all()


if __name__ == "__main__":
    import sys, traceback
    app = App()
    try:
        app.run()
    except KeyboardInterrupt:
        app.stop()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
