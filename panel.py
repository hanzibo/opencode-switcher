import gc
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango, GdkX11
from typing import Optional, Callable, List, Dict
from session_store import Session
import difflib
import os
import time


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


def _relative_time(ts_ms: int) -> str:
    if not ts_ms:
        return ""
    delta = time.time() * 1000 - ts_ms
    if delta < 0:
        return "now"
    secs = delta / 1000
    if secs < 60:
        return "now"
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)}m ago"
    hours = mins / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 7:
        return f"{int(days)}d ago"
    weeks = days / 7
    return f"{int(weeks)}w ago"


class SearchPanel:
    PANEL_WIDTH = 1320
    SIDEBAR_WIDTH = 330
    MAX_VISIBLE = 10
    ROW_HEIGHT = 96

    def __init__(self):
        self.on_select: Optional[Callable[[Session], None]] = None
        self.on_open: Optional[Callable[[], None]] = None
        self.on_delete_session: Optional[Callable[[Session], None]] = None
        self._sessions: List[Session] = []
        self._filtered: List[Session] = []
        self._selected_index = 0
        self._directories: List[str] = []
        self._selected_directory: Optional[str] = None
        self._menu_active = False

        self._bg_color = Gdk.RGBA()
        self._title_color = Gdk.RGBA()
        self._dir_color = Gdk.RGBA()
        self._snippet_color = Gdk.RGBA()

        self._window = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
        self._window.set_title("OpenCode Switcher")
        self._window.set_default_size(self.PANEL_WIDTH, 1)
        self._window.set_resizable(False)
        self._window.set_decorated(False)
        self._window.set_keep_above(True)
        self._window.set_skip_taskbar_hint(True)
        self._window.set_skip_pager_hint(True)
        # self._window.set_type_hint(Gdk.WindowTypeHint.UTILITY)
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

        self._search_entry = Gtk.SearchEntry.new()
        self._search_entry.set_name("searchEntry")
        self._search_entry.set_placeholder_text("Search sessions…")
        self._search_entry.override_color(Gtk.StateFlags.NORMAL, None)
        self._search_entry.connect("search-changed", self._on_search_changed)
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
        self._dir_listbox.override_background_color(Gtk.StateFlags.NORMAL, self._bg_color)
        self._dir_listbox.connect("row-activated", self._on_dir_selected)
        self._dir_listbox.connect("key-press-event", self._on_dir_key_press)
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
        self._session_scrolled.add(self._listbox)

        middle_hbox.pack_start(self._session_scrolled, True, True, 0)

        self._main_vbox.pack_start(middle_hbox, True, True, 0)

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
            self._bg_color = bg(0.102, 0.106, 0.118, 1.0)
            self._title_color = bg(0.91, 0.91, 0.91, 1.0)
            self._dir_color = bg(1.0, 1.0, 1.0, 0.55)
            self._snippet_color = bg(1.0, 1.0, 1.0, 0.40)
            self._separator_rgba = (1, 1, 1, 1.0)
            self._dot_live = (0.67, 1.0, 0.86, 0.9)
            self._dot_recent = (1.0, 0.78, 0.28, 0.7)
            self._dot_closed = (0.5, 0.5, 0.5, 0.3)
            vals = dict(
                window_border="rgba(255,255,255,0.08)", hover_bg="rgba(255,255,255,0.04)",
                sel_bg="rgba(170,255,220,0.18)", sel_border="rgba(170,255,220,0.5)", search_bg="#c0c0c0",
                search_fg="#000000", caret="#000000", input_border="rgba(255,255,255,0.10)",
            )
        else:
            self._bg_color = bg(0.95, 0.95, 0.95, 1.0)
            self._title_color = bg(0.1, 0.1, 0.1, 1.0)
            self._dir_color = bg(0.0, 0.0, 0.0, 0.55)
            self._snippet_color = bg(0.0, 0.0, 0.0, 0.40)
            self._separator_rgba = (0, 0, 0, 0.20)
            self._dot_live = (0.0, 0.55, 0.30, 0.85)
            self._dot_recent = (0.80, 0.50, 0.0, 0.75)
            self._dot_closed = (0.55, 0.55, 0.55, 0.4)
            vals = dict(
                window_border="rgba(0,0,0,0.08)", hover_bg="rgba(0,0,0,0.04)",
                sel_bg="rgba(170,255,220,0.28)", sel_border="rgba(170,255,220,0.6)", search_bg="#e0e0e0",
                search_fg="#000000", caret="#000000", input_border="rgba(0,0,0,0.10)",
            )
        css = (
            "window { border: 1px solid %(window_border)s; }"
            "#searchEntry { font-size: 39px; padding: 21px 27px; background: %(search_bg)s;"
            " color: %(search_fg)s; border: none; border-bottom: 1px solid %(input_border)s;"
            " caret-color: %(caret)s; }"
            "#searchEntry:focus { outline: none; }"
            "#resultLabel { font-family: \"JetBrains Mono\",\"monospace\"; font-size: 22px; padding: 0; }"
            "#dirLabel { font-size: 19px; padding: 0; }"
            "#snippetLabel { font-size: 19px; padding: 0; }"
            ".row { padding: 15px 24px; }"
            ".row:hover { background: %(hover_bg)s; }"
            ".row:selected { background: %(sel_bg)s; border-left: 3px solid %(sel_border)s; }"
            "#emptyLabel { font-size: 22px; padding: 0; }"
            "#sideLabel { font-size: 19px; padding: 15px 21px; }"
        ) % vals
        self._css_provider.load_from_data(css.encode("utf-8"))
        for w in (self._main_vbox, self._dir_scrolled, self._dir_listbox,
                  self._session_scrolled, self._listbox):
            w.override_background_color(Gtk.StateFlags.NORMAL, self._bg_color)
        self._separator.queue_draw()
        if self._window.is_visible():
            self._build_all()

    def toggle(self):
        if self._window.is_visible():
            self.hide()
        else:
            self.show()

    def show(self):
        if self.on_open:
            self.on_open()

        mx, my, mw, mh = _get_active_monitor_geometry()
        x = mx + (mw - self.PANEL_WIDTH) // 2
        y = my + int(mh * 0.18)
        self._window.move(x, y)
        self._build_all()
        self._window.show_all()
        self._window.set_focus(self._search_entry)
        display = Gdk.Display.get_default()
        if isinstance(display, GdkX11.X11Display):
            self._window.present_with_time(display.get_user_time())
        else:
            self._window.present()
        self._search_entry.grab_focus()

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

    def _on_focus_out(self, *_args):
        if not self._menu_active:
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

    def _apply_filters(self):
        query = self._search_entry.get_text().strip()

        if self._selected_directory:
            base = [s for s in self._sessions if s.directory == self._selected_directory]
        else:
            base = self._sessions[:]

        if not query:
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

        is_new_intent = query == "/new" or query.startswith("/new ")
        if is_new_intent:
            target_dir = self._sessions[0].directory if self._sessions else os.path.expanduser("~")
            project = os.path.basename(target_dir) or "project"
            new_session = Session(
                id="new-opencode",
                title=f"Start new OpenCode session",
                directory=target_dir,
                project_name=project,
                status="new",
                snippet=f"→ {target_dir}",
                started_at=0,
                updated_at=0,
            )
            self._filtered = [new_session] + self._filtered

        self._selected_index = 0
        self._render()

    def _on_search_changed(self, entry):
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
        self._window.resize(self.PANEL_WIDTH, self.MAX_VISIBLE * self.ROW_HEIGHT + 60)

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
            time_label.set_text(_relative_time(session.updated_at))
            time_label.set_valign(Gtk.Align.START)
            time_label.set_margin_top(3)
            time_label.override_color(Gtk.StateFlags.NORMAL, self._snippet_color)
            hbox.pack_start(time_label, False, False, 0)

            row.add(hbox)
            self._listbox.add(row)

        self._listbox.show_all()
        if self._filtered:
            self._listbox.select_row(self._listbox.get_row_at_index(0))

    def _on_activate(self, _entry):
        self._confirm_selection()

    def _on_session_button(self, _listbox, event):
        if event.button != 3:
            return False
        row = self._listbox.get_row_at_y(int(event.y))
        if not row or not hasattr(row, "session"):
            return False
        self._listbox.select_row(row)
        self._menu_active = True
        menu = Gtk.Menu.new()
        delete_item = Gtk.MenuItem.new_with_label("Delete session")
        session = row.session
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
        if self.on_delete_session:
            self.on_delete_session(session)

    def _on_row_activated(self, _listbox, row):
        if hasattr(row, "session") and self.on_select:
            self.hide()
            self.on_select(row.session)

    def _confirm_selection(self):
        if self._filtered and self._selected_index < len(self._filtered):
            session = self._filtered[self._selected_index]
            self.hide()
            if self.on_select:
                self.on_select(session)

    def _on_key_press(self, _widget, event):
        keyname = Gdk.keyval_name(event.keyval)
        if keyname == "Tab":
            self._dir_listbox.grab_focus()
            if self._dir_listbox.get_row_at_index(0):
                self._dir_listbox.select_row(self._dir_listbox.get_row_at_index(0))
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
        return False

    def _on_dir_key_press(self, _widget, event):
        keyname = Gdk.keyval_name(event.keyval)
        if keyname == "Tab":
            self._search_entry.grab_focus()
            return True
        return False

    def _on_session_key_press(self, _widget, event):
        keyname = Gdk.keyval_name(event.keyval)
        if keyname == "Tab":
            self._search_entry.grab_focus()
            return True
        return False

    def _on_window_key(self, _widget, event):
        keyname = Gdk.keyval_name(event.keyval)
        if keyname == "Escape":
            self.hide()
            return True
        return False

    def _update_selection(self):
        n = len(self._filtered)
        if n == 0:
            return
        self._selected_index = max(0, min(n - 1, self._selected_index))
        row = self._listbox.get_row_at_index(self._selected_index)
        if row:
            self._listbox.select_row(row)

    def _move_selection(self, direction: int):
        n = len(self._filtered)
        if n == 0:
            return
        self._selected_index = max(0, min(n - 1, self._selected_index + direction))
        self._update_selection()
