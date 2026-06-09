import gi
import subprocess
import threading
import os
gi.require_version("Gtk", "3.0")
gi.require_version("Gio", "2.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GLib, Gio, Pango, GdkPixbuf
from typing import Optional, Callable, List
from clipboard_store import ClipboardItem, CategoryItem, CategoryStore, capture_clipboard_once
from utils import relative_time, is_wayland


CATEGORY_WIDTH = 200
ACTION_WIDTH = 140


def _copy_image_to_clipboard(image_path: str):
    if not os.path.exists(image_path):
        return
    if is_wayland():
        try:
            with open(image_path, "rb") as f:
                p = subprocess.Popen(["wl-copy", "--type", "image/png"], stdin=subprocess.PIPE)
                p.communicate(f.read())
        except Exception:
            pass
    else:
        try:
            subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/png", image_path], stderr=subprocess.DEVNULL)
        except Exception:
            pass


def _copy_to_clipboard(text: str):
    if is_wayland():
        try:
            p = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-8"))
        except FileNotFoundError:
            pass
    else:
        try:
            p = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-8"))
        except FileNotFoundError:
            pass





class ClipboardPanel(Gtk.Box):
    def __init__(self, clip_store, prompt_store, cat_store):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._clip_store = clip_store
        self._cat_store = cat_store
        self._active_category_id = "__clipboard__"
        self._clip_items: List[ClipboardItem] = []
        self._selected_index = 0
        self._filter_query = ""

        self.on_copy_clipboard: Optional[Callable[[str], None]] = None
        self.on_hide_request: Optional[Callable[[], None]] = None
        self.on_dialog_shown: Optional[Callable[[], None]] = None
        self.on_dialog_hidden: Optional[Callable[[], None]] = None
        self.on_menu_shown: Optional[Callable[[], None]] = None
        self.on_menu_hidden: Optional[Callable[[], None]] = None
        self._setup_marker_monitor()

        self._bg_color = Gdk.RGBA()
        self._title_color = Gdk.RGBA()
        self._dir_color = Gdk.RGBA()
        self._snippet_color = Gdk.RGBA()

        self._build_ui()

        self._css_provider = Gtk.CssProvider()
        screen = self.get_screen() or Gdk.Screen.get_default()
        Gtk.StyleContext.add_provider_for_screen(
            screen, self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )

        self._set_theme("dark")

    def _build_ui(self):
        self._cat_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)

        self._cat_toolbar = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 2)
        self._cat_toolbar.set_margin_start(4)
        self._cat_toolbar.set_margin_end(4)
        self._cat_toolbar.set_margin_top(6)
        self._cat_toolbar.set_margin_bottom(2)

        self._btn_new_cat = Gtk.Button.new_with_label("+ New")
        self._btn_new_cat.set_tooltip_text("new")
        self._btn_new_cat.set_relief(Gtk.ReliefStyle.NONE)
        self._btn_new_cat.get_style_context().add_class("cat-tool-btn")

        self._btn_delete_cat = Gtk.Button.new_with_label("\u232b Delete")
        self._btn_delete_cat.set_tooltip_text("delete")
        self._btn_delete_cat.set_relief(Gtk.ReliefStyle.NONE)
        self._btn_delete_cat.get_style_context().add_class("cat-tool-btn")

        self._btn_rename_cat = Gtk.Button.new_with_label("\u270e Rename")
        self._btn_rename_cat.set_tooltip_text("rename")
        self._btn_rename_cat.set_relief(Gtk.ReliefStyle.NONE)
        self._btn_rename_cat.get_style_context().add_class("cat-tool-btn")

        self._btn_new_cat.connect("clicked", self._on_new_category_clicked)
        self._btn_delete_cat.connect("clicked", self._on_delete_category_clicked)
        self._btn_rename_cat.connect("clicked", self._on_rename_category_clicked)

        self._cat_list = Gtk.ListBox.new()
        self._cat_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._cat_list.set_size_request(CATEGORY_WIDTH, -1)
        self._cat_list.connect("row-selected", self._on_category_selected)
        self._cat_list.connect("button-press-event", self._on_category_button)

        self._rebuild_category_list()

        self._cat_toolbar.pack_start(self._btn_new_cat, False, False, 0)
        self._cat_toolbar.pack_start(self._btn_delete_cat, False, False, 0)
        self._cat_toolbar.pack_start(self._btn_rename_cat, False, False, 0)

        self._cat_vbox.pack_start(self._cat_toolbar, False, False, 0)
        self._cat_vbox.pack_start(self._cat_list, True, True, 0)

        self._cat_sep = Gtk.DrawingArea.new()
        self._cat_sep.set_size_request(1, -1)
        self._cat_sep.connect("draw", self._on_sep_draw)

        self._content_scrolled = Gtk.ScrolledWindow.new()
        self._content_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._content_scrolled.set_hexpand(True)
        self._content_scrolled.set_vexpand(True)

        self._content_list = Gtk.ListBox.new()
        self._content_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._content_list.set_activate_on_single_click(False)
        self._content_list.connect("row-activated", self._on_content_activated)
        self._content_list.connect("button-press-event", self._on_content_button)
        self._content_list.connect("key-press-event", self._on_content_key)
        self._content_scrolled.add(self._content_list)

        self._action_sep = Gtk.DrawingArea.new()
        self._action_sep.set_size_request(1, -1)
        self._action_sep.connect("draw", self._on_sep_draw)

        self._action_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        self._action_box.set_size_request(ACTION_WIDTH, -1)
        self._action_box.set_valign(Gtk.Align.START)
        self._action_box.set_margin_start(12)
        self._action_box.set_margin_top(12)

        self._btn_delete = Gtk.Button.new_with_label("Delete")
        self._btn_delete.connect("clicked", self._on_delete_clicked)
        self._btn_delete_all = Gtk.Button.new_with_label("Delete All")
        self._btn_delete_all.connect("clicked", self._on_delete_all_clicked)
        self._btn_create = Gtk.Button.new_with_label("Create")
        self._btn_create.connect("clicked", self._on_create_clicked)
        self._btn_edit = Gtk.Button.new_with_label("Edit")
        self._btn_edit.connect("clicked", self._on_edit_clicked)

        self.pack_start(self._cat_vbox, False, True, 0)
        self.pack_start(self._cat_sep, False, False, 0)
        self.pack_start(self._content_scrolled, True, True, 0)
        self.pack_start(self._action_sep, False, False, 0)
        self.pack_start(self._action_box, False, False, 0)

    def _get_active_category(self):
        """Return the CustomCategory object for the active category, or None if Clipboard."""
        if self._active_category_id == "__clipboard__":
            return None
        return self._cat_store.get(self._active_category_id)

    def _make_category_label(self, name: str, cat_id: str) -> Gtk.Label:
        lbl = Gtk.Label.new(name)
        lbl.set_name("catLabel")
        lbl.set_xalign(0)
        lbl.set_margin_start(16)
        lbl.set_margin_top(12)
        lbl.set_margin_bottom(12)
        if cat_id == "__clipboard__":
            lbl.set_markup(f"<b>{name}</b>")
        return lbl

    def _rebuild_category_list(self):
        """Rebuild the sidebar category list from CategoryStore."""
        for child in self._cat_list.get_children():
            self._cat_list.remove(child)
        for cat in self._cat_store.get_all():
            row = Gtk.ListBoxRow.new()
            row.get_style_context().add_class("cat-row")
            row.cat_id = cat.id
            lbl = self._make_category_label(cat.name, cat.id)
            row.add(lbl)
            self._cat_list.add(row)
        self._cat_list.show_all()
        for row in self._cat_list.get_children():
            if hasattr(row, 'cat_id') and row.cat_id == self._active_category_id:
                self._cat_list.select_row(row)
                break

    def _on_sep_draw(self, widget, cr):
        alloc = widget.get_allocation()
        cr.set_source_rgba(*self._sep_rgba)
        cr.rectangle(alloc.x, alloc.y, alloc.width, alloc.height)
        cr.fill()
        return True

    def _set_theme(self, name: str):
        self._theme = name
        if name == "dark":
            self._bg_color = Gdk.RGBA(0.075, 0.078, 0.090, 1.0)
            self._title_color = Gdk.RGBA(0.96, 0.96, 0.97, 1.0)
            self._dir_color = Gdk.RGBA(0.58, 0.64, 0.72, 1.0)
            self._snippet_color = Gdk.RGBA(0.58, 0.64, 0.72, 0.70)
            self._sep_rgba = (1, 1, 1, 0.08)
            vals = dict(
                text_fg="rgba(255,255,255,0.95)",
                text_secondary="rgba(255,255,255,0.55)",
                hover_bg="rgba(255,255,255,0.04)",
                sel_bg="rgba(99,102,241,0.12)",
                sel_border="#6366f1",
                cat_hover="rgba(255,255,255,0.03)",
                cat_sel="rgba(99,102,241,0.10)",
                cat_sel_border="#6366f1",
                btn_bg="rgba(255,255,255,0.05)",
                btn_border="rgba(255,255,255,0.08)",
                btn_hover="rgba(255,255,255,0.09)",
                btn_active="rgba(255,255,255,0.14)",
            )
        else:
            self._bg_color = Gdk.RGBA(0.965, 0.973, 0.980, 1.0)
            self._title_color = Gdk.RGBA(0.09, 0.09, 0.11, 1.0)
            self._dir_color = Gdk.RGBA(0.39, 0.45, 0.55, 1.0)
            self._snippet_color = Gdk.RGBA(0.39, 0.45, 0.55, 0.70)
            self._sep_rgba = (0, 0, 0, 0.08)
            vals = dict(
                text_fg="rgba(15,23,42,0.92)",
                text_secondary="rgba(15,23,42,0.55)",
                hover_bg="rgba(0,0,0,0.03)",
                sel_bg="rgba(79,70,229,0.08)",
                sel_border="#4f46e5",
                cat_hover="rgba(0,0,0,0.02)",
                cat_sel="rgba(79,70,229,0.06)",
                cat_sel_border="#4f46e5",
                btn_bg="rgba(0,0,0,0.04)",
                btn_border="rgba(0,0,0,0.08)",
                btn_hover="rgba(0,0,0,0.06)",
                btn_active="rgba(0,0,0,0.10)",
            )
        css = (
            ".cat-row { padding: 12px 18px; border-radius: 6px; margin: 2px 8px; border-left: 4px solid transparent; }"
            ".cat-row:hover { background: %(cat_hover)s; }"
            ".cat-row:selected { background: %(cat_sel)s; border-left: 4px solid %(cat_sel_border)s; }"
            ".cat-row #catLabel { color: %(text_secondary)s; }"
            ".cat-row:selected #catLabel { color: %(text_fg)s; }"
            ".row { padding: 12px 18px; border-radius: 6px; margin: 2px 8px; border-left: 4px solid transparent; }"
            ".row:hover { background: %(hover_bg)s; }"
            ".row:selected { background: %(sel_bg)s; border-left: 4px solid %(sel_border)s; }"
            "#catLabel { font-size: 16px; font-weight: 500; padding: 0 8px; }"
            "#clipTitle { font-family: \"JetBrains Mono\",\"monospace\"; font-size: 16px; padding: 0; }"
            "#clipText { font-size: 14px; padding: 0; }"
            "#clipTime { font-size: 12px; padding: 0; }"
            "#promptTitle { font-family: \"JetBrains Mono\",\"monospace\"; font-size: 16px; font-weight: bold; padding: 0; }"
            "#promptText { font-size: 14px; padding: 0; }"
            "button { color: %(text_fg)s; background: %(btn_bg)s;"
            " border: 1px solid %(btn_border)s; border-radius: 6px; padding: 8px 16px; font-size: 14px; font-weight: 500; }"
            "button:hover { background: %(btn_hover)s; border-color: %(sel_border)s; }"
            "button:active { background: %(btn_active)s; }"
            ".cat-tool-btn { font-size: 12px; padding: 4px 6px; border: none; border-radius: 4px; }"
            ".cat-tool-btn:hover { background: %(btn_hover)s; }"
            ".cat-tool-btn:active { background: %(btn_active)s; }"
        ) % vals
        self._css_provider.load_from_data(css.encode("utf-8"))
        for w in (self, self._cat_list, self._content_scrolled, self._content_list):
            w.override_background_color(Gtk.StateFlags.NORMAL, self._bg_color)

    def set_theme(self, name: str):
        self._set_theme(name)

    def set_filter(self, query: str):
        self._filter_query = query.strip().lower()
        self._rebuild()

    def reset_filter(self):
        """Public API: Clear filter query without triggering rebuild."""
        self._filter_query = ""

    def load_cached(self):
        self._clip_store.reload()
        self._clip_items = list(self._clip_store.get_all())
        self._clip_items.reverse()
        self._rebuild()

    def load_data(self):
        capture_clipboard_once(self._clip_store)
        GLib.idle_add(self._finish_load)

    def _finish_load(self):
        self._clip_store.reload()
        self._clip_items = list(self._clip_store.get_all())
        self._clip_items.reverse()
        self._rebuild()

    def _rebuild(self):
        for child in self._content_list.get_children():
            self._content_list.remove(child)
        import gc
        gc.collect()

        if self._active_category_id == "__clipboard__":
            items = self._clip_items
            if self._filter_query:
                items = [i for i in items if self._filter_query in i.text.lower()]
        else:
            cat = self._cat_store.get(self._active_category_id)
            items = cat.items if cat else []
            if self._filter_query:
                items = [i for i in items
                         if self._filter_query in i.title.lower()
                         or self._filter_query in i.text.lower()]

        self._update_actions()

        if not items:
            row = Gtk.ListBoxRow.new()
            row.set_sensitive(False)
            lbl = Gtk.Label.new("No items")
            lbl.set_name("clipText")
            lbl.set_halign(Gtk.Align.CENTER)
            lbl.set_margin_top(30)
            lbl.set_margin_bottom(30)
            lbl.override_color(Gtk.StateFlags.NORMAL, self._snippet_color)
            row.add(lbl)
            self._content_list.add(row)
            self._content_list.show_all()
            return

        for idx, item in enumerate(items):
            row = Gtk.ListBoxRow.new()
            row.get_style_context().add_class("row")
            row.item_index = idx
            row.store_item = item

            if self._active_category_id == "__clipboard__":
                self._build_clip_row(row, item)
            else:
                self._build_prompt_row(row, item)

            self._content_list.add(row)

        self._content_list.show_all()
        if self._content_list.get_children():
            self._content_list.select_row(self._content_list.get_row_at_index(0))

    def _build_clip_row(self, row, item: ClipboardItem):
        hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 12)
        hbox.set_margin_start(16)
        hbox.set_margin_end(16)
        hbox.set_margin_top(10)
        hbox.set_margin_bottom(10)

        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 2)
        vbox.set_hexpand(True)

        if hasattr(item, "type") and item.type == "image" and item.image_path:
            try:
                if not hasattr(item, '_thumb_pixbuf') or item._thumb_pixbuf is None:
                    raw_pixbuf = GdkPixbuf.Pixbuf.new_from_file(item.image_path)
                    h = raw_pixbuf.get_height()
                    w = raw_pixbuf.get_width()
                    target_h = 40
                    target_w = int(w * (target_h / h))
                    if target_w > 120:
                        target_w = 120
                        target_h = int(h * (target_w / w))
                    item._thumb_pixbuf = raw_pixbuf.scale_simple(target_w, target_h, GdkPixbuf.InterpType.BILINEAR)
                img = Gtk.Image.new_from_pixbuf(item._thumb_pixbuf)
                preview_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 10)
                lbl = Gtk.Label.new("[Image]")
                lbl.override_color(Gtk.StateFlags.NORMAL, self._title_color)
                preview_box.pack_start(lbl, False, False, 0)
                preview_box.pack_start(img, False, False, 0)
                vbox.pack_start(preview_box, False, False, 0)
            except Exception:
                text_label = Gtk.Label.new("[Image - Failed to load]")
                text_label.override_color(Gtk.StateFlags.NORMAL, self._title_color)
                vbox.pack_start(text_label, False, False, 0)
        else:
            text_label = Gtk.Label.new()
            text_label.set_name("clipText")
            one_line = " ".join(item.text.split())[:200]
            text_label.set_text(one_line)
            text_label.set_halign(Gtk.Align.START)
            text_label.set_xalign(0)
            text_label.set_ellipsize(Pango.EllipsizeMode.END)
            text_label.override_color(Gtk.StateFlags.NORMAL, self._title_color)
            vbox.pack_start(text_label, False, False, 0)

        hbox.pack_start(vbox, True, True, 0)

        time_label = Gtk.Label.new()
        time_label.set_name("clipTime")
        time_label.set_text(relative_time(item.timestamp))
        time_label.set_valign(Gtk.Align.START)
        time_label.set_margin_top(2)
        time_label.override_color(Gtk.StateFlags.NORMAL, self._snippet_color)
        hbox.pack_start(time_label, False, False, 0)

        row.add(hbox)

    def _build_prompt_row(self, row, item: CategoryItem):
        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 4)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(10)
        vbox.set_margin_bottom(10)

        title_label = Gtk.Label.new()
        title_label.set_name("promptTitle")
        title_label.set_text(item.title)
        title_label.set_halign(Gtk.Align.START)
        title_label.set_xalign(0)
        title_label.override_color(Gtk.StateFlags.NORMAL, self._title_color)
        vbox.pack_start(title_label, False, False, 0)

        text_preview = " ".join(item.text.split())[:300]
        text_label = Gtk.Label.new()
        text_label.set_name("promptText")
        text_label.set_text(text_preview)
        text_label.set_halign(Gtk.Align.START)
        text_label.set_xalign(0)
        text_label.set_ellipsize(Pango.EllipsizeMode.END)
        text_label.override_color(Gtk.StateFlags.NORMAL, self._snippet_color)
        vbox.pack_start(text_label, False, False, 0)

        row.add(vbox)

    def _update_actions(self):
        for child in self._action_box.get_children():
            self._action_box.remove(child)

        if self._active_category_id == "__clipboard__":
            self._action_box.pack_start(self._btn_delete, False, False, 0)
            self._action_box.pack_start(self._btn_delete_all, False, False, 0)
        else:
            self._action_box.pack_start(self._btn_create, False, False, 0)
            self._action_box.pack_start(self._btn_edit, False, False, 0)
            self._action_box.pack_start(self._btn_delete, False, False, 0)

        self._action_box.show_all()

        is_clipboard = self._active_category_id == "__clipboard__"
        has_custom_cats = any(c.id != "__clipboard__" for c in self._cat_store.get_all())
        self._btn_delete_cat.set_sensitive(not is_clipboard and has_custom_cats)
        self._btn_rename_cat.set_sensitive(not is_clipboard)

    def _on_category_selected(self, _listbox, row):
        if row is None:
            return
        self._active_category_id = row.cat_id
        self._selected_index = 0
        self._rebuild()

    def _on_category_button(self, _listbox, event):
        if event.button != 3:
            return False
        row = self._cat_list.get_row_at_y(int(event.y))
        if row is None or not hasattr(row, 'cat_id'):
            return False
        cat_id = row.cat_id
        if cat_id == "__clipboard__":
            return False

        self._cat_list.select_row(row)

        cat = self._cat_store.get(cat_id)
        if cat is None:
            return False

        menu = Gtk.Menu.new()
        if cat.pinned:
            item = Gtk.MenuItem.new_with_label("Remove from Top")
        else:
            item = Gtk.MenuItem.new_with_label("Show at Top")
        item.connect("activate", lambda *_: self._toggle_pin(cat_id, not cat.pinned))
        menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _toggle_pin(self, cat_id: str, pinned: bool):
        try:
            self._cat_store.set_pinned(cat_id, pinned)
            self._rebuild_category_list()
        except ValueError:
            pass

    def _on_content_activated(self, _listbox, row):
        if hasattr(row, "store_item"):
            self._activate_item(row.store_item)

    def _on_content_button(self, _listbox, event):
        if event.button != 3:
            return False
        row = self._listbox_at_y(event.y)
        if not row or not hasattr(row, "store_item"):
            return False
        self._content_list.select_row(row)

        menu = Gtk.Menu.new()
        item = row.store_item
        if self._active_category_id == "__clipboard__":
            copy_item = Gtk.MenuItem.new_with_label("Copy")
            copy_item.connect("activate", lambda *_: self._activate_item(item))
            menu.append(copy_item)
            del_item = Gtk.MenuItem.new_with_label("Delete")
            del_item.connect("activate", lambda *_: self._delete_item(item))
            menu.append(del_item)
        else:
            copy_item = Gtk.MenuItem.new_with_label("Copy")
            copy_item.connect("activate", lambda *_: self._activate_item(item))
            menu.append(copy_item)
            edit_item = Gtk.MenuItem.new_with_label("Edit")
            edit_item.connect("activate", lambda *_: self._edit_prompt(item))
            menu.append(edit_item)
            del_item = Gtk.MenuItem.new_with_label("Delete")
            del_item.connect("activate", lambda *_: self._delete_item(item))
            menu.append(del_item)
        if self.on_menu_shown:
            self.on_menu_shown()
        menu.connect("deactivate", lambda *_: GLib.timeout_add(300, self._on_menu_deactivated))
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _on_menu_deactivated(self):
        if self.on_menu_hidden:
            self.on_menu_hidden()
        return False

    def _on_content_key(self, _widget, event):
        keyname = Gdk.keyval_name(event.keyval)
        if keyname in ("Down", "KP_Down"):
            self._move_selection(1)
            return True
        elif keyname in ("Up", "KP_Up"):
            self._move_selection(-1)
            return True
        elif keyname in ("Delete", "KP_Delete"):
            self._delete_selected()
            return True
        return False

    def _listbox_at_y(self, y):
        return self._content_list.get_row_at_y(int(y))

    def _move_selection(self, direction: int):
        rows = self._content_list.get_children()
        sel = self._content_list.get_selected_row()
        if sel is None or sel not in rows:
            idx = 0 if direction > 0 else len(rows) - 1
        else:
            idx = list(rows).index(sel)
            idx = max(0, min(len(rows) - 1, idx + direction))
        target = rows[idx]
        self._content_list.select_row(target)
        target.grab_focus()

    def _delete_selected(self):
        row = self._content_list.get_selected_row()
        if row and hasattr(row, "store_item"):
            self._delete_item(row.store_item)

    def _delete_item(self, item):
        if self._active_category_id == "__clipboard__":
            idx = next(
                (i for i, ci in enumerate(self._clip_store.get_all()) if ci.hash == item.hash),
                None,
            )
            if idx is not None:
                self._clip_store.delete(idx)
        else:
            cat = self._cat_store.get(self._active_category_id)
            if cat:
                idx = next(
                    (i for i, ci in enumerate(cat.items)
                     if ci.title == item.title and ci.text == item.text),
                    None,
                )
                if idx is not None:
                    self._cat_store.delete_item(self._active_category_id, idx)
        self.load_cached()

    def activate_selected(self):
        """Public API: activate (copy) the currently selected item."""
        row = self._content_list.get_selected_row()
        if row and hasattr(row, "store_item"):
            self._activate_item(row.store_item)

    def move_selection(self, direction: int):
        """Public API: move content list selection by *direction* rows."""
        self._move_selection(direction)

    def delete_selected(self):
        """Public API: delete the currently selected item."""
        self._delete_selected()

    def _activate_item(self, item):
        if self._active_category_id == "__clipboard__" and isinstance(item, ClipboardItem):
            if hasattr(item, "type") and item.type == "image" and item.image_path:
                _copy_image_to_clipboard(item.image_path)
                if self.on_copy_clipboard:
                    self.on_copy_clipboard("[Image]", item.hash)
                if self.on_hide_request:
                    self.on_hide_request()
                return
            text = item.text
        elif isinstance(item, CategoryItem):
            text = item.text
        else:
            return
        _copy_to_clipboard(text)
        if self.on_copy_clipboard:
            self.on_copy_clipboard(text, item.hash if isinstance(item, ClipboardItem) else None)
        if self.on_hide_request:
            self.on_hide_request()

    def _on_delete_clicked(self, _btn):
        row = self._content_list.get_selected_row()
        if row and hasattr(row, "store_item"):
            if self.on_dialog_shown:
                self.on_dialog_shown()
            self._delete_item(row.store_item)
            if self.on_dialog_hidden:
                self.on_dialog_hidden()

    def _on_delete_all_clicked(self, _btn):
        if self._active_category_id != "__clipboard__":
            return
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Clear clipboard history?",
        )
        dialog.format_secondary_text("All clipboard history items will be permanently deleted.")
        def _on_response(dlg, resp):
            dlg.destroy()
            if resp == Gtk.ResponseType.YES:
                self._clip_store.clear_all()
                self.load_cached()
            if self.on_dialog_hidden:
                self.on_dialog_hidden()
        dialog.connect("response", _on_response)
        if self.on_dialog_shown:
            self.on_dialog_shown()
        dialog.show_all()

    def _on_create_clicked(self, _btn):
        self._show_prompt_dialog(create=True)

    def _on_edit_clicked(self, _btn):
        row = self._content_list.get_selected_row()
        if not row or not hasattr(row, "store_item"):
            return
        item = row.store_item
        if isinstance(item, CategoryItem):
            self._show_prompt_dialog(create=False, existing=item)

    def _edit_prompt(self, item: CategoryItem):
        self._show_prompt_dialog(create=False, existing=item)

    def _show_prompt_dialog(self, create: bool, existing: Optional[CategoryItem] = None):
        dialog = Gtk.Dialog(
            title="Create prompt" if create else "Edit prompt",
            transient_for=self.get_toplevel(),
            modal=True,
        )
        dialog.set_default_size(520, 420)
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Save", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)

        content = dialog.get_content_area()
        content.set_spacing(8)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_margin_top(16)
        content.set_margin_bottom(16)

        content.add(Gtk.Label.new("Title:"))
        title_entry = Gtk.Entry.new()
        if existing:
            title_entry.set_text(existing.title)
        title_entry.set_activates_default(True)
        content.add(title_entry)

        content.add(Gtk.Label.new("Text:"))
        text_view = Gtk.TextView.new()
        text_view.set_wrap_mode(Gtk.WrapMode.WORD)
        sw = Gtk.ScrolledWindow.new()
        sw.set_min_content_height(240)
        sw.set_min_content_width(460)
        sw.add(text_view)
        content.add(sw)

        if existing:
            buf = text_view.get_buffer()
            buf.set_text(existing.text)

        if self.on_dialog_shown:
            self.on_dialog_shown()
        dialog.show_all()

        def on_response(dlg, response):
            title = title_entry.get_text().strip()
            buf = text_view.get_buffer()
            start, end = buf.get_bounds()
            text = buf.get_text(start, end, False).strip()
            dlg.destroy()
            if response != Gtk.ResponseType.ACCEPT or not title or not text:
                if self.on_dialog_hidden:
                    self.on_dialog_hidden()
                return
            if create:
                self._cat_store.add_item(self._active_category_id, title, text)
            else:
                cat = self._cat_store.get(self._active_category_id)
                if cat:
                    idx = next(
                        (i for i, ci in enumerate(cat.items)
                         if ci.timestamp == existing.timestamp),
                        None,
                    )
                    if idx is not None:
                        self._cat_store.update_item(self._active_category_id, idx, title, text)
            self.load_cached()
            if self.on_dialog_hidden:
                self.on_dialog_hidden()

        dialog.connect("response", on_response)

    def _setup_marker_monitor(self):
        marker_dir = os.path.expanduser("~/.cache/opencode-switcher")
        marker_path = os.path.join(marker_dir, "clipboard.updated")
        try:
            os.makedirs(marker_dir, exist_ok=True)
            if not os.path.isfile(marker_path):
                with open(marker_path, "w") as f:
                    f.write("0")
            gfile = Gio.File.new_for_path(marker_path)
            self._monitor = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._monitor.connect("changed", self._on_marker_changed)
        except Exception:
            pass

    def _on_marker_changed(self, monitor, gfile, other_file, event):
        if event == Gio.FileMonitorEvent.CHANGES_DONE_HINT:
            return
        if self._clip_items is not None:
            marker_path = os.path.expanduser("~/.cache/opencode-switcher/clipboard.updated")
            is_image = False
            try:
                if os.path.exists(marker_path):
                    with open(marker_path, "r") as f:
                        content = f.read().strip()
                    if content.startswith("image:"):
                        is_image = True
            except Exception:
                pass

            if is_image:
                capture_clipboard_once(self._clip_store)
                GLib.idle_add(self._finish_load)
            else:
                self._finish_load()

    def _on_new_category_clicked(self, _btn):
        dialog = Gtk.Dialog(
            title="New Category",
            transient_for=self.get_toplevel(),
            modal=True,
        )
        dialog.set_default_size(350, 150)
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Create", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)

        content = dialog.get_content_area()
        content.set_spacing(8)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_margin_top(16)
        content.set_margin_bottom(16)

        content.add(Gtk.Label.new("Category Name:"))
        name_entry = Gtk.Entry.new()
        name_entry.set_activates_default(True)
        content.add(name_entry)


        if self.on_dialog_shown:
            self.on_dialog_shown()
        dialog.show_all()

        def on_response(dlg, response):
            name = name_entry.get_text().strip()
            dlg.destroy()
            if response != Gtk.ResponseType.ACCEPT:
                if self.on_dialog_hidden:
                    self.on_dialog_hidden()
                return
            if not name:
                if self.on_dialog_hidden:
                    self.on_dialog_hidden()
                return
            try:
                new_id = self._cat_store.create(name)
                self._rebuild_category_list()
                # Select the newly created category
                for row in self._cat_list.get_children():
                    if hasattr(row, 'cat_id') and row.cat_id == new_id:
                        self._cat_list.select_row(row)
                        break
            except ValueError as e:
                # Name conflict — silently ignore (could show error_label but dialog is gone)
                pass
            if self.on_dialog_hidden:
                self.on_dialog_hidden()

        dialog.connect("response", on_response)

    def _on_delete_category_clicked(self, _btn):
        cat_id = self._active_category_id
        if cat_id == "__clipboard__":
            return
        cat = self._cat_store.get(cat_id)
        if cat is None:
            return
        if self.on_dialog_shown:
            self.on_dialog_shown()
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f'Delete "{cat.name}"?',
        )
        dialog.format_secondary_text("All items in this category will be permanently deleted.")

        def on_response(dlg, resp):
            dlg.destroy()
            if resp == Gtk.ResponseType.YES:
                try:
                    self._cat_store.delete(cat_id)
                except ValueError:
                    pass
                self._active_category_id = "__clipboard__"
                self._rebuild_category_list()
                self._rebuild()
            if self.on_dialog_hidden:
                self.on_dialog_hidden()

        dialog.connect("response", on_response)
        dialog.show_all()

    def _on_rename_category_clicked(self, _btn):
        cat_id = self._active_category_id
        if cat_id == "__clipboard__":
            return
        cat = self._cat_store.get(cat_id)
        if cat is None:
            return

        selected_row = self._cat_list.get_selected_row()
        if selected_row is None:
            return

        label = selected_row.get_child()
        if label is None or not isinstance(label, Gtk.Label):
            return

        old_name = label.get_text()

        entry = Gtk.Entry.new()
        entry.set_text(old_name)
        entry.select_region(0, -1)
        selected_row.remove(label)
        selected_row.add(entry)
        entry.show()
        entry.grab_focus()

        def make_lbl(text):
            return self._make_category_label(text, cat_id)

        def revert():
            if entry.get_parent() == selected_row:
                selected_row.remove(entry)
                lbl = make_lbl(old_name)
                selected_row.add(lbl)
                lbl.show()

        def on_activate(ent):
            new_name = ent.get_text().strip()
            if new_name and new_name != old_name:
                try:
                    self._cat_store.rename(cat_id, new_name)
                    self._rebuild_category_list()
                except ValueError:
                    revert()
            else:
                revert()

        def on_focus_out(ent, ev):
            revert()
            return False

        entry.connect("activate", on_activate)
        entry.connect("focus-out-event", on_focus_out)
