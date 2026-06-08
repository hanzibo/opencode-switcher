import gc
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango
try:
    from gi.repository import GdkX11
except ImportError:
    GdkX11 = None
from typing import Optional, Callable, List, Dict
from session_store import Session
from clipboard_panel import ClipboardPanel
import difflib
import os
import time
from utils import relative_time, is_wayland


def _get_active_monitor_geometry():
    display = Gdk.Display.get_default()
    if not display:
        return 0, 0, 1280, 720
    try:
        seat = display.get_default_seat()
        pointer = seat.get_pointer()
        ptr_x, ptr_y = pointer.get_position()
        monitor = display.get_monitor_at_point(ptr_x, ptr_y)
    except Exception:
        monitor = None
    if not monitor:
        monitor = display.get_primary_monitor()
    if not monitor:
        monitor = display.get_monitor(0)
    if not monitor:
        return 0, 0, 1280, 720
    geo = monitor.get_geometry()
    return (geo.x, geo.y, geo.width, geo.height)


def _fuzzy_score(query: str, text: str) -> float:
    if not query:
        return 1.0
    query_lower = query.lower()
    text_lower = text.lower()
    if query_lower in text_lower:
        return 0.9 + (len(query) / len(text)) * 0.1
    ratio = difflib.SequenceMatcher(None, query_lower, text_lower).ratio()
    return ratio





class SearchPanel:
    PANEL_WIDTH = 1320
    SIDEBAR_WIDTH = 330
    MAX_VISIBLE = 10
    ROW_HEIGHT = 96

    def __init__(self):
        self.on_select: Optional[Callable[[Session], None]] = None
        self.on_open: Optional[Callable[[], None]] = None
        self.on_delete_session: Optional[Callable[[Session], None]] = None
        self.on_rename_session: Optional[Callable[[str, str], None]] = None
        self.on_launch_pure: Optional[Callable[[Session], None]] = None
        self._sessions: List[Session] = []
        self._filtered: List[Session] = []
        self._selected_index = 0
        self._directories: List[str] = []
        self._selected_directory: Optional[str] = None
        self._menu_active = False
        self._delete_in_progress = False
        self._dialog_active = False
        self._show_time = 0.0
        self._active_tab = 0
        self._clipboard_panel: Optional[ClipboardPanel] = None
        self._tab_bar = None

        self._bg_color = Gdk.RGBA()
        self._title_color = Gdk.RGBA()
        self._dir_color = Gdk.RGBA()
        self._snippet_color = Gdk.RGBA()

        self._window = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
        self._window.set_title("OpenCode Switcher")
        self._window.set_default_size(self.PANEL_WIDTH, self.MAX_VISIBLE * self.ROW_HEIGHT + 60)
        self._window.set_resizable(False)
        self._window.set_decorated(False)
        self._window.set_keep_above(True)
        self._window.set_skip_taskbar_hint(True)
        self._window.set_skip_pager_hint(True)
        self._window.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self._window.set_position(Gtk.WindowPosition.NONE)
        self._window.set_accept_focus(True)
        self._window.set_app_paintable(True)
        self._window.connect("draw", self._on_window_draw)

        screen = self._window.get_screen()
        self._css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            screen, self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )

        self._main_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        self._main_vbox.override_background_color(Gtk.StateFlags.NORMAL, self._bg_color)
        self._main_vbox.set_hexpand(True)
        self._main_vbox.set_vexpand(True)

        self._tab_bar = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
        self._tab_bar.set_can_focus(False)
        self._tab_labels = []
        for title in ["Opencode Sessions", "Clipboard"]:
            eb = Gtk.EventBox.new()
            eb.set_can_focus(False)
            lbl = Gtk.Label.new(title)
            lbl.set_can_focus(False)
            lbl.set_name("tabLabel")
            lbl.set_margin_start(16)
            lbl.set_margin_end(16)
            lbl.set_margin_top(8)
            lbl.set_margin_bottom(8)
            eb.add(lbl)
            self._tab_labels.append(lbl)
            self._tab_bar.pack_start(eb, True, True, 0)
        self._tab_bar.get_children()[0].connect("button-press-event", lambda *_: self._switch_tab(0))
        self._tab_bar.get_children()[1].connect("button-press-event", lambda *_: self._switch_tab(1))
        self._tab_bar.get_children()[0].get_style_context().add_class("tab-active")
        self._tab_bar.get_children()[1].get_style_context().add_class("tab-inactive")
        self._main_vbox.pack_start(self._tab_bar, False, False, 0)

        self._search_entry = Gtk.SearchEntry.new()
        self._search_entry.set_name("searchEntry")
        self._search_entry.set_placeholder_text("Search sessions…")
        self._search_entry.override_color(Gtk.StateFlags.NORMAL, None)
        self._search_changed_id = self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_entry.connect("activate", self._on_activate)
        self._search_entry.connect("key-press-event", self._on_key_press)
        self._main_vbox.pack_start(self._search_entry, False, False, 0)

        middle_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
        middle_hbox.set_hexpand(True)
        middle_hbox.set_vexpand(True)

        self._dir_scrolled = Gtk.ScrolledWindow.new()
        self._dir_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._dir_scrolled.set_size_request(self.SIDEBAR_WIDTH, -1)
        self._dir_scrolled.override_background_color(Gtk.StateFlags.NORMAL, self._bg_color)

        self._dir_listbox = Gtk.ListBox.new()
        self._dir_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._dir_listbox.set_can_focus(True)
        self._dir_listbox.override_background_color(Gtk.StateFlags.NORMAL, self._bg_color)
        self._dir_listbox.connect("row-selected", self._on_dir_selected)
        self._dir_listbox.connect("key-press-event", self._on_dir_key_press)
        self._dir_listbox.connect("focus-in-event", self._on_dir_focus_in)
        self._dir_listbox.connect("focus-out-event", lambda *_: self._update_separator_focus(False))
        self._dir_scrolled.add(self._dir_listbox)

        middle_hbox.pack_start(self._dir_scrolled, False, True, 0)

        self._separator = Gtk.DrawingArea.new()
        self._separator.set_size_request(1, -1)
        self._separator.connect("draw", self._on_separator_draw)
        middle_hbox.pack_start(self._separator, False, False, 0)

        self._session_scrolled = Gtk.ScrolledWindow.new()
        self._session_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._session_scrolled.override_background_color(Gtk.StateFlags.NORMAL, self._bg_color)

        self._listbox = Gtk.ListBox.new()
        self._listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._listbox.override_background_color(Gtk.StateFlags.NORMAL, self._bg_color)
        self._listbox.connect("row-activated", self._on_row_activated)
        self._listbox.set_activate_on_single_click(False)
        self._listbox.connect("key-press-event", self._on_session_key_press)
        self._listbox.connect("button-press-event", self._on_session_button)
        self._listbox.connect("focus-in-event", self._on_session_focus_in)
        self._listbox.connect("focus-out-event", lambda *_: self._update_separator_focus(False))
        self._session_scrolled.add(self._listbox)

        middle_hbox.pack_start(self._session_scrolled, True, True, 0)

        self._content_stack = Gtk.Stack.new()
        self._content_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self._content_stack.add_named(middle_hbox, "sessions")
        self._main_vbox.pack_start(self._content_stack, True, True, 0)

        self._window.add(self._main_vbox)
        self._window.connect("key-press-event", self._on_window_key)
        self._window.connect("focus-out-event", self._on_focus_out)

        self._set_theme("dark")

    def set_theme(self, name: str):
        self._set_theme(name)

    def _set_theme(self, name: str):
        self._theme = name
        bg, fg1, fg2, fg3 = Gdk.RGBA, Gdk.RGBA, Gdk.RGBA, Gdk.RGBA
        sep = self._separator_rgba if hasattr(self, "_separator_rgba") else (1, 1, 1, 1.0)
        if name == "dark":
            self._bg_color = bg(0.075, 0.078, 0.090, 1.0)
            self._title_color = bg(0.96, 0.96, 0.97, 1.0)
            self._dir_color = bg(0.58, 0.64, 0.72, 1.0)
            self._snippet_color = bg(0.58, 0.64, 0.72, 0.70)
            self._separator_rgba = (1, 1, 1, 0.08)
            self._default_separator_rgba = (1, 1, 1, 0.08)
            self._dot_live = (0.063, 0.725, 0.506, 0.9)
            self._dot_recent = (0.960, 0.620, 0.043, 0.8)
            self._dot_closed = (0.392, 0.455, 0.545, 0.5)
            vals = dict(
                window_border="rgba(255,255,255,0.06)", hover_bg="rgba(255,255,255,0.04)",
                sel_bg="rgba(99,102,241,0.12)", sel_border="#6366f1", search_bg="#1c1d21",
                search_fg="#f5f5f7", caret="#6366f1", input_border="rgba(255,255,255,0.08)",
                tab_fg="rgba(255,255,255,0.5)", tab_active_fg="#ffffff",
            )
        else:
            self._bg_color = bg(0.965, 0.973, 0.980, 1.0)
            self._title_color = bg(0.09, 0.09, 0.11, 1.0)
            self._dir_color = bg(0.39, 0.45, 0.55, 1.0)
            self._snippet_color = bg(0.39, 0.45, 0.55, 0.70)
            self._separator_rgba = (0, 0, 0, 0.08)
            self._default_separator_rgba = (0, 0, 0, 0.08)
            self._dot_live = (0.020, 0.588, 0.412, 0.85)
            self._dot_recent = (0.850, 0.467, 0.024, 0.75)
            self._dot_closed = (0.580, 0.639, 0.722, 0.4)
            vals = dict(
                window_border="rgba(0,0,0,0.06)", hover_bg="rgba(0,0,0,0.03)",
                sel_bg="rgba(79,70,229,0.08)", sel_border="#4f46e5", search_bg="#ffffff",
                search_fg="#0f172a", caret="#4f46e5", input_border="rgba(0,0,0,0.08)",
                tab_fg="rgba(15,23,42,0.55)", tab_active_fg="#0f172a",
            )
        css = (
            "window { border: 1px solid %(window_border)s; }"
            "#searchEntry { font-size: 24px; padding: 12px 16px; background: %(search_bg)s;"
            " color: %(search_fg)s; border: 1px solid %(input_border)s; border-radius: 8px;"
            " caret-color: %(caret)s; margin: 16px 20px 10px 20px; }"
            "#searchEntry:focus { border-color: %(sel_border)s; }"
            "#resultLabel { font-family: \"JetBrains Mono\",\"monospace\"; font-size: 20px; padding: 0; }"
            "#dirLabel { font-size: 16px; padding: 0; }"
            "#snippetLabel { font-size: 16px; padding: 0; }"
            ".row { padding: 12px 18px; border-radius: 6px; margin: 2px 10px; border-left: 4px solid transparent; }"
            ".row:hover { background: %(hover_bg)s; }"
            ".row:selected { background: %(sel_bg)s; border-left: 4px solid %(sel_border)s; }"
            "#emptyLabel { font-size: 20px; padding: 0; }"
            "#sideLabel { font-size: 17px; padding: 12px 18px; }"
            "#tabLabel { font-size: 16px; font-weight: bold; padding: 12px 24px; color: %(tab_fg)s; }"
            ".tab-active { background: transparent; border-bottom: 3px solid %(sel_border)s; }"
            ".tab-active #tabLabel { color: %(tab_active_fg)s; }"
            ".tab-inactive { background: transparent; border-bottom: 3px solid transparent; }"
            ".tab-inactive:hover { background: %(hover_bg)s; }"
        ) % vals
        self._css_provider.load_from_data(css.encode("utf-8"))
        for w in (self._main_vbox, self._dir_scrolled, self._dir_listbox,
                  self._session_scrolled, self._listbox):
            w.override_background_color(Gtk.StateFlags.NORMAL, self._bg_color)
        if self._tab_bar:
            for child in self._tab_bar.get_children():
                child.override_background_color(Gtk.StateFlags.NORMAL, self._bg_color)
        self._separator.queue_draw()
        if self._clipboard_panel:
            self._clipboard_panel.set_theme(name)
        if self._window.is_visible():
            self._build_all()

    def set_clipboard_panel(self, panel: ClipboardPanel, clip_store, prompt_store):
        self._clipboard_panel = panel
        self._clip_store = clip_store
        self._prompt_store = prompt_store
        self._content_stack.add_named(panel, "clipboard")
        if self._theme:
            panel.set_theme(self._theme)
        panel.on_dialog_shown = self._on_clip_dialog_shown
        panel.on_dialog_hidden = self._on_clip_dialog_hidden
        panel.on_menu_shown = self._on_clip_menu_shown
        panel.on_menu_hidden = self._on_clip_menu_hidden

    def _on_clip_dialog_shown(self):
        self._dialog_active = True
        self._menu_active = False

    def _on_clip_dialog_hidden(self):
        self._dialog_active = True
        GLib.timeout_add(3000, lambda: setattr(self, '_dialog_active', False) or False)

    def _on_clip_menu_shown(self):
        self._menu_active = True

    def _on_clip_menu_hidden(self):
        self._menu_active = False

    def _switch_tab(self, index: int):
        if index == self._active_tab:
            return
        self._active_tab = index
        for i, child in enumerate(self._tab_bar.get_children()):
            ctx = child.get_style_context()
            if i == index:
                ctx.add_class("tab-active")
                ctx.remove_class("tab-inactive")
            else:
                ctx.remove_class("tab-active")
                ctx.add_class("tab-inactive")
        self._show_time = time.time()

        # Temporarily block search-changed signal to avoid intermediate filtering/rendering
        self._search_entry.handler_block(self._search_changed_id)
        try:
            if self._search_entry.get_text() != "":
                self._search_entry.set_text("")

            if index == 0:
                self._search_entry.set_placeholder_text("Search sessions…")
                self._build_all()
                self._content_stack.set_visible_child_name("sessions")
            else:
                self._search_entry.set_placeholder_text("Filter clipboard…")
                if self._clipboard_panel:
                    self._clipboard_panel.reset_filter()
                    self._clipboard_panel.load_cached()
                self._content_stack.set_visible_child_name("clipboard")
                if self._clipboard_panel and not is_wayland():
                    self._clipboard_panel.load_data()
        finally:
            self._search_entry.handler_unblock(self._search_changed_id)

        self._search_entry.grab_focus()

    def toggle(self):
        if self._window.is_visible():
            self.hide()
        else:
            self.show()

    def show(self):
        self._show_time = time.time()
        if self._active_tab == 0:
            if self.on_open:
                self.on_open()
            self._build_all()
        elif self._clipboard_panel:
            if is_wayland():
                self._clipboard_panel.load_cached()
            else:
                self._clipboard_panel.load_data()

        mx, my, mw, mh = _get_active_monitor_geometry()
        x = mx + (mw - self.PANEL_WIDTH) // 2
        y = my + int(mh * 0.18)
        self._window.move(x, y)
        self._window.show_all()
        self._window.set_focus(self._search_entry)
        display = Gdk.Display.get_default()
        if GdkX11 is not None and isinstance(display, GdkX11.X11Display):
            win = self._window.get_window()
            if win is not None:
                self._window.present_with_time(GdkX11.x11_get_server_time(win))
            else:
                self._window.present()
        else:
            self._window.present()
        self._search_entry.grab_focus()

    def is_visible(self) -> bool:
        return self._window.is_visible()

    def hide(self):
        self._window.hide()
        self._search_entry.set_text("")
        gc.collect()

    def _on_window_draw(self, widget, cr):
        cr.set_source_rgba(
            self._bg_color.red,
            self._bg_color.green,
            self._bg_color.blue,
            self._bg_color.alpha,
        )
        cr.paint()
        return False

    def _on_separator_draw(self, widget, cr):
        alloc = widget.get_allocation()
        cr.set_source_rgba(*self._separator_rgba)
        cr.rectangle(alloc.x, alloc.y, alloc.width, alloc.height)
        cr.fill()
        return True

    def _update_separator_focus(self, focused: bool):
        if focused:
            self._separator_rgba = (0.67, 1.0, 0.86, 0.9)
        else:
            self._separator_rgba = self._default_separator_rgba
        self._separator.queue_draw()

    def _on_dir_focus_in(self, *args):
        self._update_separator_focus(True)
        return False

    def _on_session_focus_in(self, *args):
        self._update_separator_focus(True)
        return False

    def _on_focus_out(self, *_args):
        if self._menu_active or self._delete_in_progress or self._dialog_active:
            return
        if time.time() - self._show_time < 0.5:
            return
        self.hide()

    def load_sessions(self, sessions: List[Session]):
        self._sessions = sessions
        dirs: Dict[str, int] = {}
        for s in sessions:
            d = s.directory
            if d:
                dirs[d] = dirs.get(d, 0) + 1
        self._directories = sorted(dirs.keys())
        self._selected_directory = None
        self._filtered = sessions[:]
        self._selected_index = 0
        if self._window.is_visible():
            self._build_all()

    def _update_all_count(self):
        first = self._dir_listbox.get_row_at_index(0)
        if first and first.dir_path is None:
            label = first.get_child()
            if label:
                label.set_text(f"All ({len(self._filtered)})")

    def _build_directory_rows(self):
        idx = {}

        all_row = Gtk.ListBoxRow.new()
        all_row.get_style_context().add_class("row")
        all_row.dir_path = None
        all_label = Gtk.Label.new(f"All ({len(self._filtered)})")
        all_label.set_name("sideLabel")
        all_label.set_halign(Gtk.Align.START)
        all_label.set_xalign(0)
        all_label.override_color(Gtk.StateFlags.NORMAL, self._title_color)
        all_row.add(all_label)
        idx[None] = all_row

        for d in self._directories:
            row = Gtk.ListBoxRow.new()
            row.get_style_context().add_class("row")
            row.dir_path = d
            basename = os.path.basename(d) or d
            count = sum(1 for s in self._sessions if s.directory == d)
            label = Gtk.Label.new(f"{basename} ({count})")
            label.set_name("sideLabel")
            label.set_halign(Gtk.Align.START)
            label.set_xalign(0)
            label.override_color(Gtk.StateFlags.NORMAL, self._dir_color)
            label.set_tooltip_text(d)
            row.add(label)
            idx[d] = row

        for child in self._dir_listbox.get_children():
            self._dir_listbox.remove(child)

        ordered = [all_row]
        for d in self._directories:
            ordered.append(idx[d])
        for r in ordered:
            self._dir_listbox.add(r)

        self._dir_listbox.show_all()

        target = self._selected_directory
        select_row = idx.get(target, all_row)
        self._dir_listbox.select_row(select_row)

    def _on_dir_selected(self, _listbox, row):
        if hasattr(row, "dir_path"):
            self._selected_directory = row.dir_path
            self._selected_index = 0
            if row.dir_path:
                basename = os.path.basename(row.dir_path) or row.dir_path
                self._search_entry.set_placeholder_text(f"Search in {basename}…")
            else:
                self._search_entry.set_placeholder_text("Search sessions…")
            self._apply_filters()

    _COMMANDS = [
        ("/new", "Start a new OpenCode session"),
        ("/open", "Select a directory and start a new session"),
        ("/gm", "Ask Gemini: /gm <query>"),
        ("/google", "Google AI Search: /google <query>"),
    ]

    def _apply_filters(self):
        query = self._search_entry.get_text().strip()

        if self._selected_directory:
            base = [s for s in self._sessions if s.directory == self._selected_directory]
        else:
            base = self._sessions[:]

        if query.startswith("/"):
            self._filtered = []

            is_new_intent = query == "/new" or query.startswith("/new ")
            is_open_intent = query == "/open" or query.startswith("/open ")
            is_gm_intent = query == "/gm" or query.startswith("/gm ")
            is_google_intent = query == "/google" or query.startswith("/google ")

            if is_new_intent and "/open" not in query:
                target_dir = self._sessions[0].directory if self._sessions else os.path.expanduser("~")
                project = os.path.basename(target_dir) or "project"
                self._filtered = [Session(
                    id="new-opencode", title="Start new OpenCode session",
                    directory=target_dir, project_name=project, status="new",
                    snippet=f"\u2192 {target_dir}", started_at=0, updated_at=0,
                )]
            elif is_open_intent:
                self._filtered = [Session(
                    id="open-folder", title="Open folder\u2026",
                    directory="", project_name="", status="new",
                    snippet="\u2192 Select a directory to start a new OpenCode session",
                    started_at=0, updated_at=0,
                )]
            elif is_gm_intent:
                prompt_text = query[4:].strip()
                snippet_text = f"Ask Gemini: {prompt_text}" if prompt_text else "Ask Gemini: /gm <query>"
                self._filtered = [Session(
                    id="gemini-query", title="/gm",
                    directory="", project_name="", status="new",
                    snippet=snippet_text, started_at=0, updated_at=0,
                )]
            elif is_google_intent:
                prompt_text = query[8:].strip()
                snippet_text = f"Google AI Search: {prompt_text}" if prompt_text else "Google AI Search: /google <query>"
                self._filtered = [Session(
                    id="google-query", title="/google",
                    directory="", project_name="", status="new",
                    snippet=snippet_text, started_at=0, updated_at=0,
                )]
            else:
                for cmd, desc in self._COMMANDS:
                    if query == "/" or cmd.startswith(query):
                        if cmd == "/new":
                            cmd_id = "new-opencode"
                        elif cmd == "/open":
                            cmd_id = "open-folder"
                        elif cmd == "/gm":
                            cmd_id = "gemini-query"
                        else:
                            cmd_id = "google-query"
                        self._filtered.append(Session(
                            id=cmd_id, title=cmd,
                            directory="", project_name="", status="new",
                            snippet=desc, started_at=0, updated_at=0,
                        ))
        elif not query:
            self._filtered = base[:]
        else:
            scored = []
            for s in base:
                title_score = _fuzzy_score(query, s.title)
                dir_score = _fuzzy_score(query, s.directory)
                proj_score = _fuzzy_score(query, s.project_name)
                best = max(title_score, dir_score, proj_score)
                if best > 0.5:
                    scored.append((best, s))
            scored.sort(key=lambda x: -x[0])
            self._filtered = [s for _, s in scored]

        self._selected_index = 0
        self._render()

    def _on_search_changed(self, entry):
        if self._active_tab == 1 and self._clipboard_panel:
            self._clipboard_panel.set_filter(entry.get_text())
        else:
            self._apply_filters()

    def _build_all(self):
        self._build_directory_rows()
        self._render()

    def _render(self):
        for child in self._listbox.get_children():
            self._listbox.remove(child)
        gc.collect()

        self._update_all_count()

        rows_to_show = self._filtered[:self.MAX_VISIBLE]

        if not rows_to_show:
            row = Gtk.ListBoxRow.new()
            row.get_style_context().add_class("row")
            row.set_sensitive(False)
            label = Gtk.Label.new("No matching sessions")
            label.set_name("emptyLabel")
            label.set_halign(Gtk.Align.CENTER)
            label.set_margin_top(30)
            label.set_margin_bottom(30)
            label.override_color(Gtk.StateFlags.NORMAL, self._snippet_color)
            row.add(label)
            self._listbox.add(row)
            self._listbox.show_all()
            return

        for session in rows_to_show:
            row = Gtk.ListBoxRow.new()
            row.get_style_context().add_class("row")
            row.session = session

            hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 15)
            hbox.set_margin_start(21)
            hbox.set_margin_end(21)
            hbox.set_margin_top(12)
            hbox.set_margin_bottom(12)

            dot = Gtk.DrawingArea.new()
            dot.set_size_request(12, 12)
            dot.set_valign(Gtk.Align.CENTER)
            status = session.status
            dot_live = self._dot_live
            dot_recent = self._dot_recent
            dot_closed = self._dot_closed
            def on_dot_draw(w, cr, st=status, dl=dot_live, dr=dot_recent, dc=dot_closed):
                row = w.get_parent().get_parent()
                is_sel = isinstance(row, Gtk.ListBoxRow) and bool(row.get_state_flags() & Gtk.StateFlags.SELECTED)
                if st == "live":
                    cr.set_source_rgba(dl[0], dl[1], dl[2], 1.0 if is_sel else dl[3])
                elif st == "recent":
                    cr.set_source_rgba(dr[0], dr[1], dr[2], 0.9 if is_sel else dr[3])
                else:
                    cr.set_source_rgba(dc[0], dc[1], dc[2], 0.6 if is_sel else dc[3])
                cr.arc(6, 6, 5.25, 0, 6.2832)
                cr.fill()
                return True
            dot.connect("draw", on_dot_draw)
            hbox.pack_start(dot, False, False, 0)

            vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 4)
            vbox.set_hexpand(True)

            title_label = Gtk.Label.new()
            title_label.set_name("resultLabel")
            title_label.set_text(session.title)
            title_label.set_halign(Gtk.Align.START)
            title_label.set_xalign(0)
            title_label.override_color(Gtk.StateFlags.NORMAL, self._title_color)
            vbox.pack_start(title_label, False, False, 0)

            dir_label = Gtk.Label.new()
            dir_label.set_name("dirLabel")
            dir_label.set_text(session.directory)
            dir_label.set_halign(Gtk.Align.START)
            dir_label.set_xalign(0)
            dir_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            dir_label.override_color(Gtk.StateFlags.NORMAL, self._dir_color)
            vbox.pack_start(dir_label, False, False, 0)

            if session.snippet:
                snip_label = Gtk.Label.new()
                snip_label.set_name("snippetLabel")
                snip_label.set_text(session.snippet)
                snip_label.set_halign(Gtk.Align.START)
                snip_label.set_xalign(0)
                snip_label.set_ellipsize(Pango.EllipsizeMode.END)
                snip_label.override_color(Gtk.StateFlags.NORMAL, self._snippet_color)
                vbox.pack_start(snip_label, False, False, 0)

            hbox.pack_start(vbox, True, True, 0)

            time_label = Gtk.Label.new()
            time_label.set_name("snippetLabel")
            time_label.set_text(relative_time(session.updated_at))
            time_label.set_valign(Gtk.Align.START)
            time_label.set_margin_top(3)
            time_label.override_color(Gtk.StateFlags.NORMAL, self._snippet_color)
            hbox.pack_start(time_label, False, False, 0)

            row.add(hbox)
            self._listbox.add(row)

        self._listbox.show_all()
        if self._filtered:
            self._listbox.select_row(self._listbox.get_row_at_index(0))
            adj = self._session_scrolled.get_vadjustment()
            if adj:
                adj.set_value(0)

    def _on_activate(self, _entry):
        if self._active_tab == 1 and self._clipboard_panel:
            self._on_clipboard_activate()
        else:
            self._confirm_selection()

    def _on_clipboard_activate(self):
        if not self._clipboard_panel:
            return
        self._clipboard_panel.activate_selected()

    def _on_session_button(self, _listbox, event):
        if event.button != 3:
            return False
        row = self._listbox.get_row_at_y(int(event.y))
        if not row or not hasattr(row, "session"):
            return False
        self._listbox.select_row(row)
        session = row.session
        if session.id in ("new-opencode", "open-folder", "gemini-query", "google-query"):
            return False
        self._menu_active = True
        menu = Gtk.Menu.new()
        rename_item = Gtk.MenuItem.new_with_label("Rename session")
        rename_item.connect("activate", lambda *_: self._on_rename(session))
        menu.append(rename_item)
        pure_item = Gtk.MenuItem.new_with_label("Start without plugins")
        pure_item.connect("activate", lambda *_: self._on_launch_pure(session))
        menu.append(pure_item)
        delete_item = Gtk.MenuItem.new_with_label("Delete session")
        delete_item.connect("activate", lambda *_: self._on_delete(session))
        menu.append(delete_item)
        menu.connect("deactivate", lambda *_: GLib.timeout_add(300, self._clear_menu))
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _clear_menu(self):
        self._menu_active = False
        return False

    def _on_delete(self, session):
        self._delete_in_progress = True
        GLib.timeout_add(60000, lambda: setattr(self, '_delete_in_progress', False) or False)
        if self.on_delete_session:
            self.on_delete_session(session)

    def _on_launch_pure(self, session):
        self.hide()
        if self.on_launch_pure:
            self.on_launch_pure(session)

    def _on_rename(self, session):
        self._delete_in_progress = True
        dialog = Gtk.Dialog(
            title="Rename session",
            transient_for=self._window,
            modal=True,
        )
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Rename", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)

        content = dialog.get_content_area()
        content.set_spacing(10)
        content.set_margin_start(20)
        content.set_margin_end(20)
        content.set_margin_top(20)
        content.set_margin_bottom(20)

        content.add(Gtk.Label.new("New title:"))
        entry = Gtk.Entry.new()
        entry.set_text(session.title)
        entry.set_activates_default(True)
        content.add(entry)
        dialog.show_all()

        def on_response(dlg, response):
            new_title = entry.get_text().strip() if response == Gtk.ResponseType.ACCEPT else ""
            dlg.destroy()
            self._delete_in_progress = False
            if response == Gtk.ResponseType.ACCEPT and new_title and self.on_rename_session:
                self.on_rename_session(session.id, new_title)

        dialog.connect("response", on_response)
        entry.grab_focus()

    def _do_select(self, session):
        if session.id == "open-folder":
            dialog = Gtk.FileChooserDialog(
                title="Select directory",
                transient_for=self._window,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
            dialog.add_button("_Open", Gtk.ResponseType.ACCEPT)
            def _on_dialog_response(dlg, response):
                if response == Gtk.ResponseType.ACCEPT:
                    chosen = dlg.get_filename()
                    dlg.destroy()
                    self.hide()
                    if self.on_select:
                        self.on_select(Session(
                            id="new-opencode",
                            title=os.path.basename(chosen),
                            directory=chosen,
                            project_name=os.path.basename(chosen),
                            status="new",
                            snippet=f"→ {chosen}",
                            started_at=0,
                            updated_at=0,
                        ))
                else:
                    dlg.destroy()
            dialog.connect("response", _on_dialog_response)
            dialog.show()
            return
        if session.id == "gemini-query":
            query = self._search_entry.get_text().strip()
            prompt_text = ""
            if query.startswith("/gm ") or query == "/gm":
                prompt_text = query[4:].strip()
            self._handle_gm_command(prompt_text)
            return
        if session.id == "google-query":
            query = self._search_entry.get_text().strip()
            prompt_text = ""
            if query.startswith("/google ") or query == "/google":
                prompt_text = query[8:].strip()
            self._handle_google_command(prompt_text)
            return
        self.hide()
        if self.on_select:
            self.on_select(session)

    def _handle_google_command(self, prompt_text: str):
        import subprocess
        import urllib.parse
        encoded = urllib.parse.quote(prompt_text)
        url = f"https://www.google.com/search?udm=50&q={encoded}"
        try:
            subprocess.Popen(["firefox", url])
        except Exception as e:
            print(f"Error launching Firefox for Google AI search: {e}", flush=True)
        GLib.idle_add(self.hide)

    def _handle_gm_command(self, prompt_text: str):
        import subprocess
        import threading

        # 1. Copy prompt to clipboard
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(prompt_text, -1)
        clipboard.store()
        if hasattr(self, "_clip_store") and self._clip_store:
            self._clip_store.mark_written(prompt_text)

        # 2. Check if Firefox is already running to decide on a short/long delay
        firefox_running = False
        try:
            subprocess.check_output(["pgrep", "-f", "firefox"])
            firefox_running = True
        except Exception:
            pass

        # 3. Launch Firefox first (while switcher panel is still active/focused)
        try:
            subprocess.Popen(["firefox", "https://gemini.google.com/app"])
        except Exception as e:
            print(f"Error launching Firefox: {e}", flush=True)
            self.hide()
            return

        # 4. Hide panel after launching
        GLib.idle_add(self.hide)

        # 5. Auto-type in background thread
        def automate_typing():
            # Wait for browser and page to load (much shorter delay if already running)
            delay = 1.2 if firefox_running else 4.0
            time.sleep(delay)


            # Inject Ctrl+V and Enter
            try:
                # Try evdev (uinput) first for Wayland/X11 hardware-level emulation
                try:
                    from evdev import UInput, ecodes as ec
                    events = {
                        ec.EV_KEY: [ec.KEY_LEFTCTRL, ec.KEY_V, ec.KEY_ENTER]
                    }
                    ui = UInput(events)
                    
                    # Press Ctrl+V
                    ui.write(ec.EV_KEY, ec.KEY_LEFTCTRL, 1)
                    ui.write(ec.EV_KEY, ec.KEY_V, 1)
                    ui.syn()
                    
                    time.sleep(0.05)
                    
                    # Release Ctrl+V
                    ui.write(ec.EV_KEY, ec.KEY_V, 0)
                    ui.write(ec.EV_KEY, ec.KEY_LEFTCTRL, 0)
                    ui.syn()
                    
                    time.sleep(0.3)
                    
                    # Press Enter
                    ui.write(ec.EV_KEY, ec.KEY_ENTER, 1)
                    ui.syn()
                    
                    time.sleep(0.05)
                    
                    # Release Enter
                    ui.write(ec.EV_KEY, ec.KEY_ENTER, 0)
                    ui.syn()
                    
                    ui.close()
                    print("Automated typing successfully via evdev.UInput", flush=True)
                    return
                except Exception as evdev_err:
                    print(f"evdev.UInput failed or not available ({evdev_err}), falling back to pynput...", flush=True)

                # Fallback to pynput
                from pynput.keyboard import Controller, Key
                keyboard = Controller()
                
                # Press Ctrl+V
                with keyboard.pressed(Key.ctrl):
                    keyboard.press('v')
                    keyboard.release('v')
                
                # Sleep briefly before Enter
                time.sleep(0.3)
                
                # Press Enter
                keyboard.press(Key.enter)
                keyboard.release(Key.enter)
                print("Automated typing successfully via pynput", flush=True)
            except Exception as e:
                print(f"Error simulating keyboard input: {e}", flush=True)

        threading.Thread(target=automate_typing, daemon=True).start()


    def _on_row_activated(self, _listbox, row):
        if hasattr(row, "session"):
            self._do_select(row.session)

    def _confirm_selection(self):
        if self._filtered and self._selected_index < len(self._filtered):
            session = self._filtered[self._selected_index]
            self._do_select(session)

    def _on_key_press(self, _widget, event):
        keyname = Gdk.keyval_name(event.keyval)

        if keyname in ("Return", "KP_Enter"):
            query = self._search_entry.get_text().strip()
            if query.startswith("/gm ") or query == "/gm":
                prompt_text = query[4:].strip()
                self._handle_gm_command(prompt_text)
                return True
            elif query.startswith("/google ") or query == "/google":
                prompt_text = query[8:].strip()
                self._handle_google_command(prompt_text)
                return True

        if self._active_tab == 1 and self._clipboard_panel:
            if keyname in ("Down", "KP_Down", "Up", "KP_Up"):
                self._clipboard_panel.move_selection(1 if "Down" in keyname else -1)
                return True
            elif keyname in ("Delete", "KP_Delete"):
                self._clipboard_panel.delete_selected()
                return True
            elif keyname in ("Return", "KP_Enter"):
                self._on_clipboard_activate()
                return True
            return False

        if keyname == "Tab":
            self._window.child_focus(Gtk.DirectionType.TAB_FORWARD)
            GLib.idle_add(self._focus_dir_listbox)
            return True
        if keyname in ("Down", "KP_Down"):
            self._move_selection(1)
            return True
        elif keyname in ("Up", "KP_Up"):
            self._move_selection(-1)
            return True
        elif keyname in ("Home", "KP_Home"):
            self._selected_index = 0
            self._update_selection()
            return True
        elif keyname in ("End", "KP_End"):
            self._selected_index = len(self._filtered) - 1 if self._filtered else 0
            self._update_selection()
            return True
        elif keyname in ("Page_Up", "KP_Page_Up"):
            self._move_selection(-self.MAX_VISIBLE)
            return True
        elif keyname in ("Page_Down", "KP_Page_Down"):
            self._move_selection(self.MAX_VISIBLE)
            return True
        elif keyname in ("Delete", "KP_Delete"):
            if self._filtered and self._selected_index < len(self._filtered):
                session = self._filtered[self._selected_index]
                if session.id not in ("new-opencode", "open-folder", "gemini-query", "google-query"):
                    self._on_delete(session)
            return True
        elif event.keyval == Gdk.KEY_r and (event.state & Gdk.ModifierType.CONTROL_MASK):
            if self._filtered and self._selected_index < len(self._filtered):
                session = self._filtered[self._selected_index]
                if session.id not in ("new-opencode", "open-folder", "gemini-query", "google-query"):
                    self._on_rename(session)
            return True
        return False

    def _focus_dir_listbox(self):
        self._dir_listbox.grab_focus()
        selected = self._dir_listbox.get_selected_row()
        if selected is None:
            first = self._dir_listbox.get_row_at_index(0)
            if first:
                self._dir_listbox.select_row(first)
                first.grab_focus()
        else:
            selected.grab_focus()
        return False

    def _on_dir_key_press(self, _widget, event):
        keyname = Gdk.keyval_name(event.keyval)
        if keyname == "Tab":
            self._search_entry.grab_focus()
            return True
        elif keyname in ("Down", "KP_Down"):
            self._move_dir_selection(1)
            return True
        elif keyname in ("Up", "KP_Up"):
            self._move_dir_selection(-1)
            return True
        return False

    def _move_dir_selection(self, direction):
        rows = self._dir_listbox.get_children()
        if not rows:
            return
        current = self._dir_listbox.get_selected_row()
        if current is None or current not in rows:
            target = rows[0] if direction > 0 else rows[-1]
        else:
            idx = list(rows).index(current)
            new_idx = max(0, min(len(rows) - 1, idx + direction))
            target = rows[new_idx]
        self._dir_listbox.select_row(target)
        target.grab_focus()
        GLib.idle_add(self._scroll_to_dir_row, target)

    def _scroll_to_dir_row(self, row):
        adj = self._dir_scrolled.get_vadjustment()
        if adj is None:
            return False
        pos = row.translate_coordinates(self._dir_listbox, 0, 0)
        if pos is None:
            return False
        top = pos[1]
        bottom = top + row.get_allocation().height
        visible_top = adj.get_value()
        visible_bottom = visible_top + adj.get_page_size()
        if top < visible_top:
            adj.set_value(top)
        elif bottom > visible_bottom:
            adj.set_value(bottom - adj.get_page_size())
        return False

    def _on_session_key_press(self, _widget, event):
        keyname = Gdk.keyval_name(event.keyval)
        if keyname == "Tab":
            self._search_entry.grab_focus()
            return True
        elif keyname in ("Delete", "KP_Delete"):
            selected = self._listbox.get_selected_row()
            if selected and hasattr(selected, "session"):
                session = selected.session
                if session.id not in ("new-opencode", "open-folder", "gemini-query", "google-query"):
                    self._on_delete(session)
            return True
        elif event.keyval == Gdk.KEY_r and (event.state & Gdk.ModifierType.CONTROL_MASK):
            selected = self._listbox.get_selected_row()
            if selected and hasattr(selected, "session"):
                session = selected.session
                if session.id not in ("new-opencode", "open-folder", "gemini-query", "google-query"):
                    self._on_rename(session)
            return True
        return False

    def _on_window_key(self, _widget, event):
        keyname = Gdk.keyval_name(event.keyval)
        if keyname == "Escape":
            self.hide()
            return True
        if event.keyval == Gdk.KEY_1 and (event.state & Gdk.ModifierType.CONTROL_MASK):
            self._switch_tab(0)
            return True
        if event.keyval == Gdk.KEY_2 and (event.state & Gdk.ModifierType.CONTROL_MASK):
            self._switch_tab(1)
            return True
        return False

    def _scroll_to_row(self, row):
        adj = self._session_scrolled.get_vadjustment()
        if adj is None:
            return
        pos = row.translate_coordinates(self._listbox, 0, 0)
        if pos is None:
            return
        top = pos[1]
        bottom = top + row.get_allocation().height
        visible_top = adj.get_value()
        visible_bottom = visible_top + adj.get_page_size()
        if top < visible_top:
            adj.set_value(top)
        elif bottom > visible_bottom:
            adj.set_value(bottom - adj.get_page_size())

    def _update_selection(self):
        n = len(self._filtered)
        if n == 0:
            return
        visible_limit = min(n, self.MAX_VISIBLE)
        self._selected_index = max(0, min(visible_limit - 1, self._selected_index))
        row = self._listbox.get_row_at_index(self._selected_index)
        if row:
            self._listbox.select_row(row)
            row.grab_focus()
            GLib.idle_add(self._scroll_to_row, row)

    def _move_selection(self, direction: int):
        n = len(self._filtered)
        if n == 0:
            return
        visible_limit = min(n, self.MAX_VISIBLE)
        self._selected_index = max(0, min(visible_limit - 1, self._selected_index + direction))
        self._update_selection()