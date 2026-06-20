import gi
import subprocess
import threading
import os
import re
gi.require_version("Gtk", "3.0")
gi.require_version("Gio", "2.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GLib, Gio, Pango, GdkPixbuf, PangoCairo
from typing import Optional, Callable, List
from uuid import uuid4
from clipboard_store import ClipboardItem, CategoryItem, CategoryStore, CustomCategory, capture_clipboard_once, CustomPrompt, CustomPromptsStore
import time
from utils import relative_time, is_wayland, request_window_focus

# Regex to match placeholders: ${index[:prompt][=default]}
# - Group 1: index (\d+)
# - Group 2: optional prompt, allowing escaped colons (\:) and equals (\=)
# - Group 3: optional default value, matched if the leading '=' is not escaped (?<!\\)
TEMPLATE_REGEX = re.compile(r"\$\{(\d+)(?::((?:[^}=]|\\:|\\=)+))?(?<!\\)(?:=([^}]*))?\}")
PROMPT_PLACEHOLDER_RE = re.compile(r'\\\\|\\(\${&})|(\${&})')


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
    def __init__(self, clip_store, cat_store):
        # ponytail: removed unused prompt_store parameter
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._clip_store = clip_store
        self._cat_store = cat_store
        self._custom_prompts_store = CustomPromptsStore()
        self._active_category_id = "__clipboard__"
        self._clip_items: List[ClipboardItem] = []
        self._selected_index = 0
        self._filter_query = ""
        self._active_tab_type = "all"
        self._tab_buttons = {}
        self._in_category_button = False

        self.on_copy_clipboard: Optional[Callable[[str], None]] = None
        self.on_hide_request: Optional[Callable[[], None]] = None
        self.on_dialog_shown: Optional[Callable[[], None]] = None
        self.on_dialog_hidden: Optional[Callable[[], None]] = None
        self.on_menu_shown: Optional[Callable[[], None]] = None
        self.on_menu_hidden: Optional[Callable[[], None]] = None
        self._editing_rename_row = None
        self._editing_rename_entry = None
        self._editing_rename_old_name = None
        self._editing_rename_cat_id = None
        self._rename_activate_id = 0
        self._rename_focus_out_id = 0
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
        self._content_list.set_filter_func(self._list_filter_func, None)
        self._content_list.connect("row-activated", self._on_content_activated)
        self._content_list.connect("button-press-event", self._on_content_button)
        self._content_list.connect("key-press-event", self._on_content_key)
        self._content_scrolled.add(self._content_list)
 
        # Create content vbox to house the filter tabs and the scrolled list window
        self._content_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        self._content_vbox.set_hexpand(True)
        self._content_vbox.set_vexpand(True)

        self._filter_tabs_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
        self._filter_tabs_box.set_no_show_all(True)
        self._filter_tabs_box.set_margin_start(12)
        self._filter_tabs_box.set_margin_end(12)
        self._filter_tabs_box.set_margin_top(8)
        self._filter_tabs_box.set_margin_bottom(8)

        tab_spec = [
            ("all", "全部"),
            ("text", "文本"),
            ("image", "图片"),
            ("link", "链接"),
            ("code", "代码")
        ]
        for t_type, t_label in tab_spec:
            btn = Gtk.Button.new_with_label(t_label)
            btn.get_style_context().add_class("filter-tab")
            if t_type == "all":
                btn.get_style_context().add_class("filter-tab-active")
            btn.connect("clicked", self._on_filter_tab_clicked, t_type)
            self._filter_tabs_box.pack_start(btn, True, True, 0)
            self._tab_buttons[t_type] = btn
            btn.show()

        self._content_vbox.pack_start(self._filter_tabs_box, False, False, 0)
        self._content_vbox.pack_start(self._content_scrolled, True, True, 0)

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
        self._btn_prompts_config = Gtk.Button.new_with_label("Prompts Config")
        self._btn_prompts_config.connect("clicked", self._on_prompts_config_clicked)
        self._btn_create = Gtk.Button.new_with_label("Create")
        self._btn_create.connect("clicked", self._on_create_clicked)
        self._btn_edit = Gtk.Button.new_with_label("Edit")
        self._btn_edit.connect("clicked", self._on_edit_clicked)
 
        self._btn_sort = Gtk.Button.new_with_label("Sort")
        self._btn_sort.connect("clicked", self._on_sort_clicked)
 
        self._action_sep2 = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        self._action_sep2.set_margin_top(8)
        self._action_sep2.set_margin_bottom(8)
 
        self._btn_backup = Gtk.Button.new_with_label("Backup")
        self._btn_backup.connect("clicked", self._on_backup_clicked)
 
        self._btn_restore = Gtk.Button.new_with_label("Restore")
        self._btn_restore.connect("clicked", self._on_restore_clicked)
 
        self._btn_recycle_bin = Gtk.Button.new_with_label("Recycle Bin")
        self._btn_recycle_bin.connect("clicked", self._on_recycle_bin_clicked)
 
        self._btn_sort_cats = Gtk.Button.new_with_label("Sort Categories")
        self._btn_sort_cats.connect("clicked", self._on_sort_cats_clicked)
 
        self.pack_start(self._cat_vbox, False, True, 0)
        self.pack_start(self._cat_sep, False, False, 0)
        self.pack_start(self._content_vbox, True, True, 0)
        self.pack_start(self._action_sep, False, False, 0)
        self.pack_start(self._action_box, False, False, 0)

        # Rebuild category list after all UI components (especially _content_list) are initialized
        self._rebuild_category_list()

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
        cats = self._cat_store.get_all()
        last_pinned = True
        for cat in cats:
            if last_pinned and not cat.pinned:
                # Insert a clear horizontal separator row between pinned and unpinned categories
                sep_row = Gtk.ListBoxRow.new()
                sep_row.set_selectable(False)
                sep_row.set_activatable(False)
                sep_row.set_can_focus(False)
                sep_row.get_style_context().add_class("cat-sep-row")

                sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
                sep.set_margin_start(12)
                sep.set_margin_end(12)
                sep.set_margin_top(8)
                sep.set_margin_bottom(8)
                sep_row.add(sep)
                self._cat_list.add(sep_row)

            row = Gtk.ListBoxRow.new()
            row.get_style_context().add_class("cat-row")
            row.cat_id = cat.id
            lbl = self._make_category_label(cat.name, cat.id)
            row.add(lbl)
            self._cat_list.add(row)
            last_pinned = cat.pinned

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
            self._bg_color = Gdk.RGBA(0.039, 0.043, 0.063, 1.0)
            self._title_color = Gdk.RGBA(0.95, 0.96, 0.98, 1.0)
            self._dir_color = Gdk.RGBA(0.39, 0.45, 0.55, 1.0)
            self._snippet_color = Gdk.RGBA(0.28, 0.33, 0.41, 1.0)
            self._sep_rgba = (1, 1, 1, 0.05)
            vals = dict(
                text_fg="rgba(255,255,255,0.95)",
                text_secondary="rgba(255,255,255,0.45)",
                hover_bg="rgba(255,255,255,0.03)",
                sel_bg="rgba(129,140,248,0.10)",
                sel_border="#818cf8",
                cat_hover="rgba(255,255,255,0.03)",
                cat_sel="rgba(129,140,248,0.10)",
                cat_sel_border="#818cf8",
                btn_bg="rgba(255,255,255,0.04)",
                btn_border="rgba(255,255,255,0.06)",
                btn_hover="rgba(129,140,248,0.12)",
                btn_active="rgba(129,140,248,0.18)",
                cat_sep_color="rgba(129,140,248,0.25)",
                dialog_bg="#0a0b10",
                input_bg="#12131a",
                input_fg="#f1f5f9",
                input_border="rgba(255,255,255,0.06)",
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
                cat_sep_color="rgba(0,0,0,0.08)",
                dialog_bg="#ffffff",
                input_bg="#ffffff",
                input_fg="#0f172a",
                input_border="rgba(0,0,0,0.08)",
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
            ".filter-tab { padding: 4px 12px; border-radius: 20px; border: 1px solid %(btn_border)s; background: %(btn_bg)s; background-image: none; box-shadow: none; font-size: 13px; color: %(text_secondary)s; }"
            ".filter-tab:hover { background: %(btn_hover)s; background-image: none; color: %(text_fg)s; }"
            ".filter-tab-active { background: %(sel_bg)s; background-image: none; border-color: %(sel_border)s; color: %(text_fg)s; }"
            ".cat-sep-row separator { background: %(cat_sep_color)s; min-height: 1px; }"
            "dialog, messagedialog, GtkDialog, GtkMessageDialog, .custom-dialog, "
            "dialog box, messagedialog box, dialog grid, messagedialog grid, .custom-dialog box, "
            ".dialog-vbox, .dialog-action-area, .dialog-content-area { "
            "background-color: %(dialog_bg)s; color: %(text_fg)s; border: none; box-shadow: none; }"
            "dialog scrolledwindow, messagedialog scrolledwindow, .custom-dialog scrolledwindow, "
            "dialog viewport, messagedialog viewport, .custom-dialog viewport { "
            "background-color: transparent; border: none; }"
            "dialog label, messagedialog label, .custom-dialog label { color: %(text_fg)s; background-color: transparent; }"
            "dialog entry, messagedialog entry, .custom-dialog entry, "
            "dialog textview, messagedialog textview, .custom-dialog textview, "
            "dialog textview text, messagedialog textview text, .custom-dialog textview text { "
            "background-color: %(input_bg)s; color: %(input_fg)s; "
            "border: 1px solid %(input_border)s; border-radius: 6px; "
            "caret-color: %(sel_border)s; }"
            "dialog entry:focus, messagedialog entry:focus, .custom-dialog entry:focus, "
            "dialog textview:focus, messagedialog textview:focus, .custom-dialog textview:focus { "
            "border-color: %(sel_border)s; }"
            "dialog button, messagedialog button, .custom-dialog button { "
            "background-color: %(btn_bg)s; color: %(text_fg)s; "
            "border: 1px solid %(btn_border)s; border-radius: 6px; "
            "padding: 8px 16px; font-size: 14px; font-weight: 500; "
            "box-shadow: none; text-shadow: none; }"
            "dialog button:hover, messagedialog button:hover, .custom-dialog button:hover { background-color: %(btn_hover)s; border-color: %(sel_border)s; }"
            "dialog button:active, messagedialog button:active, .custom-dialog button:active { background-color: %(btn_active)s; }"
            "dialog headerbar, messagedialog headerbar, .custom-dialog headerbar, "
            "dialog headerbar.titlebar, messagedialog headerbar.titlebar, .custom-dialog headerbar.titlebar { "
            "background-color: %(dialog_bg)s; background-image: none; box-shadow: none; border-style: none; border-color: %(dialog_bg)s; color: %(text_fg)s; }"
            "dialog headerbar *, messagedialog headerbar *, .custom-dialog headerbar * { "
            "background-color: transparent; background-image: none; box-shadow: none; color: %(text_fg)s; }"
            "dialog headerbar button, messagedialog headerbar button, .custom-dialog headerbar button { "
            "background-color: transparent; background-image: none; border: none; box-shadow: none; color: %(text_fg)s; }"
            "dialog headerbar button:hover, messagedialog headerbar button:hover, .custom-dialog headerbar button:hover { background-color: %(btn_hover)s; }"
            "entry, textview, textview text { background-color: %(input_bg)s; color: %(input_fg)s; border: 1px solid %(input_border)s; border-radius: 6px; }"
            "textview { padding: 4px; }"
            "menu, menuitem { background-color: %(dialog_bg)s; background-image: none; }"
            "menu { border: 1px solid %(input_border)s; border-radius: 6px; padding: 4px 0; }"
            "menuitem { color: %(text_fg)s; padding: 4px 16px; }"
            "menuitem:hover, menuitem:selected { background-color: %(btn_hover)s; color: %(text_fg)s; }"
            "menuitem label { color: %(text_fg)s; }"
            "menuitem:hover label, menuitem:selected label { color: %(text_fg)s; }"
            ".custom-dialog separator { background: %(input_border)s; min-height: 1px; }"
            ".custom-dialog list { background-color: %(input_bg)s; border: 1px solid %(input_border)s; border-radius: 6px; padding: 4px 0; }"
            ".custom-dialog row, .custom-dialog listrow, .custom-dialog .sort-row { background-color: transparent; color: %(text_fg)s; border-bottom: 1px solid %(input_border)s; }"
            ".custom-dialog row:last-child, .custom-dialog listrow:last-child, .custom-dialog .sort-row:last-child { border-bottom: none; }"
            ".custom-dialog row:hover, .custom-dialog listrow:hover, .custom-dialog .sort-row:hover { background-color: %(hover_bg)s; }"
            ".custom-dialog row:selected, .custom-dialog listrow:selected, .custom-dialog .sort-row:selected { background-color: %(sel_bg)s; color: %(text_fg)s; }"
            ".dynamic-copy-tag { color: #2ecc71; font-size: 12px; font-weight: bold; }"
            ".code-lang-tag { color: %(sel_border)s; font-size: 10px; font-weight: bold; margin-bottom: 2px; }"
            ".custom-dialog notebook, .custom-dialog notebook > stack { border: none; background-color: transparent; }"
        ) % vals
        self._css_provider.load_from_data(css.encode("utf-8"))
        for w in (self, self._cat_list, self._content_scrolled, self._content_list):
            w.override_background_color(Gtk.StateFlags.NORMAL, self._bg_color)

    def set_theme(self, name: str):
        self._set_theme(name)

    def set_filter(self, query: str):
        self._filter_query = query.strip().lower()
        self._content_list.invalidate_filter()
        GLib.idle_add(self._select_first_visible_row)

    def reset_filter(self):
        """Public API: Clear filter query without triggering rebuild."""
        self._filter_query = ""
        self._active_tab_type = "all"
        for t_type, btn in self._tab_buttons.items():
            context = btn.get_style_context()
            if t_type == "all":
                context.add_class("filter-tab-active")
            else:
                context.remove_class("filter-tab-active")

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
        if not hasattr(self, '_content_list'):
            return
        for child in self._content_list.get_children():
            self._content_list.remove(child)

        if self._active_category_id == "__clipboard__":
            self._filter_tabs_box.show()
            items = self._clip_items
        else:
            self._filter_tabs_box.hide()
            cat = self._cat_store.get(self._active_category_id)
            items = cat.items if cat else []

        self._update_actions()

        # 1. Build regular item rows
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

        # 2. Build the "No items" placeholder row
        placeholder_row = Gtk.ListBoxRow.new()
        placeholder_row.set_sensitive(False)
        placeholder_row.is_placeholder = True

        lbl = Gtk.Label.new("No items")
        lbl.set_name("clipText")
        lbl.set_halign(Gtk.Align.CENTER)
        lbl.set_margin_top(30)
        lbl.set_margin_bottom(30)
        lbl.override_color(Gtk.StateFlags.NORMAL, self._snippet_color)
        placeholder_row.add(lbl)
        self._content_list.add(placeholder_row)

        self._content_list.show_all()
        self._content_list.invalidate_filter()
        GLib.idle_add(self._select_first_visible_row)

    def _item_matches_filter(self, item) -> bool:
        if not item:
            return False
            
        # 1. Apply Search Query Filter
        if self._filter_query:
            query = self._filter_query
            if self._active_category_id == "__clipboard__":
                if query not in item.text.lower():
                    return False
            else:
                title = getattr(item, "title", "") or ""
                text = getattr(item, "text", "") or ""
                if query not in title.lower() and query not in text.lower():
                    return False
                    
        # 2. Apply Tab Filter (only if active category is __clipboard__)
        if self._active_category_id == "__clipboard__" and self._active_tab_type != "all":
            # Determine type of this item
            item_type = getattr(item, "type", None)
            if item_type is None:
                # Classify ClipboardItem text on the fly if type is missing
                item_text = getattr(item, "text", "") or ""
                item_type = self._clip_store.classify_text(item_text)
                
            if item_type != self._active_tab_type:
                return False
                
        return True

    def _on_filter_tab_clicked(self, _btn, tab_type):
        if self._active_tab_type == tab_type:
            return
            
        # Update active tab button styling
        for t_type, btn in self._tab_buttons.items():
            context = btn.get_style_context()
            if t_type == tab_type:
                context.add_class("filter-tab-active")
            else:
                context.remove_class("filter-tab-active")
                
        self._active_tab_type = tab_type
        
        # Invalidate filter to trigger Gtk ListBox filter func
        self._content_list.invalidate_filter()
        GLib.idle_add(self._select_first_visible_row)

    def _list_filter_func(self, row, user_data):
        if getattr(row, "is_placeholder", False):
            if self._active_category_id == "__clipboard__":
                items = self._clip_items
            else:
                cat = self._cat_store.get(self._active_category_id)
                items = cat.items if cat else []
            
            if not items:
                return True
                
            # If any item matches the filter, hide placeholder (return False)
            any_match = any(self._item_matches_filter(i) for i in items)
            return not any_match

        item = getattr(row, "store_item", None)
        if not item:
            return True
            
        return self._item_matches_filter(item)

    def _select_first_visible_row(self):
        first_visible = None
        for row in self._content_list.get_children():
            if row.is_visible() and getattr(row, "store_item", None) is not None:
                first_visible = row
                break
        if first_visible:
            self._content_list.select_row(first_visible)
        else:
            self._content_list.select_row(None)

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

        right_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 2)
        right_vbox.set_valign(Gtk.Align.START)

        # Show programming language tag above time label if it is a code item
        if hasattr(item, "type") and item.type == "code":
            lang = getattr(item, "language", None)
            if not lang:
                lang = self._clip_store.detect_language_name(item.text) if hasattr(self._clip_store, "detect_language_name") else "Code"
                # Cache detected language back on the item so it is saved later
                try:
                    item.language = lang
                except Exception:
                    pass
            lang_display = lang.upper() if lang else "CODE"
            lang_label = Gtk.Label.new(lang_display)
            lang_label.get_style_context().add_class("code-lang-tag")
            lang_label.set_halign(Gtk.Align.END)
            right_vbox.pack_start(lang_label, False, False, 0)

        time_label = Gtk.Label.new()
        time_label.set_name("clipTime")
        time_label.set_text(relative_time(item.timestamp))
        time_label.set_valign(Gtk.Align.START)
        time_label.set_halign(Gtk.Align.END)
        time_label.override_color(Gtk.StateFlags.NORMAL, self._snippet_color)
        right_vbox.pack_start(time_label, False, False, 0)

        hbox.pack_start(right_vbox, False, False, 0)

        row.add(hbox)

    def _build_prompt_row(self, row, item: CategoryItem):
        hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 12)
        hbox.set_margin_start(16)
        hbox.set_margin_end(16)
        hbox.set_margin_top(10)
        hbox.set_margin_bottom(10)

        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 4)
        vbox.set_hexpand(True)

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

        hbox.pack_start(vbox, True, True, 0)

        has_placeholders = len(TEMPLATE_REGEX.findall(item.text)) > 0
        if has_placeholders:
            tag_label = Gtk.Label.new("Dynamic Copy")
            tag_label.get_style_context().add_class("dynamic-copy-tag")
            tag_label.set_halign(Gtk.Align.END)
            tag_label.set_valign(Gtk.Align.CENTER)
            hbox.pack_start(tag_label, False, False, 0)

        row.add(hbox)

    def _update_actions(self):
        for child in self._action_box.get_children():
            self._action_box.remove(child)

        if self._active_category_id == "__clipboard__":
            self._action_box.pack_start(self._btn_delete, False, False, 0)
            self._action_box.pack_start(self._btn_delete_all, False, False, 0)
            self._action_box.pack_start(self._btn_prompts_config, False, False, 0)
        else:
            self._action_box.pack_start(self._btn_create, False, False, 0)
            self._action_box.pack_start(self._btn_edit, False, False, 0)
            self._action_box.pack_start(self._btn_delete, False, False, 0)
            self._action_box.pack_start(self._btn_sort, False, False, 0)

        self._action_box.pack_start(self._action_sep2, False, False, 0)
        self._action_box.pack_start(self._btn_sort_cats, False, False, 0)
        self._action_box.pack_start(self._btn_backup, False, False, 0)
        self._action_box.pack_start(self._btn_restore, False, False, 0)
        self._action_box.pack_start(self._btn_recycle_bin, False, False, 0)

        self._action_box.show_all()

        is_clipboard = self._active_category_id == "__clipboard__"
        has_custom_cats = any(c.id != "__clipboard__" for c in self._cat_store.get_all())
        self._btn_delete_cat.set_sensitive(not is_clipboard and has_custom_cats)
        self._btn_rename_cat.set_sensitive(not is_clipboard)

        # Sort button sensitivity: only for custom categories with 2+ items
        if not is_clipboard:
            cat = self._cat_store.get(self._active_category_id)
            self._btn_sort.set_sensitive(len(cat.items) > 1 if cat else False)

    def _on_sort_clicked(self, _btn):
        self._show_sort_dialog()

    def _show_sort_dialog(self):
        cat = self._cat_store.get(self._active_category_id)
        if not cat or len(cat.items) <= 1:
            return

        items = list(cat.items)  # local working copy
        dialog = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
        dialog.get_style_context().add_class("custom-dialog")
        dialog.set_title("Sort: {}".format(cat.name))
        dialog.set_modal(True)
        dialog.set_default_size(500, 400)
        dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        dialog.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        dialog.set_resizable(True)
        dialog.set_transient_for(self.get_toplevel())

        # Main vertical box
        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        dialog.add(vbox)

        title_label = Gtk.Label.new("Drag items to reorder: {}".format(cat.name))
        title_label.set_xalign(0)
        title_label.set_margin_start(12)
        title_label.set_margin_top(8)
        title_label.set_margin_bottom(8)
        vbox.pack_start(title_label, False, False, 0)

        # Separator after top bar
        sep1 = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(sep1, False, False, 0)

        # ===== Scrollable ListBox =====
        scrolled = Gtk.ScrolledWindow.new()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        listbox = Gtk.ListBox.new()
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        scrolled.add(listbox)
        vbox.pack_start(scrolled, True, True, 0)

        # ===== Drag & Drop setup =====
        target_entry = Gtk.TargetEntry.new("text/plain", Gtk.TargetFlags.SAME_APP, 0)

        # Apply CSS for visual feedback borders with constant size
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            .sort-row {
                border-top: 3px solid transparent;
                border-bottom: 3px solid transparent;
            }
            .sort-row.drag-hover-top {
                border-top-color: #3584e4;
            }
            .sort-row.drag-hover-bottom {
                border-bottom-color: #3584e4;
            }
        """)
        listbox.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_USER)

        # Track currently highlighted row
        _current_hover_row = None
        _current_hover_dir = None

        # Set DnD DEST on Gtk.ListBox itself (omit HIGHLIGHT to prevent green border)
        listbox.drag_dest_set(Gtk.DestDefaults.MOTION | Gtk.DestDefaults.DROP, [target_entry], Gdk.DragAction.MOVE)

        def clear_hover_styling():
            nonlocal _current_hover_row, _current_hover_dir
            if _current_hover_row:
                try:
                    ctx = _current_hover_row.get_style_context()
                    ctx.remove_class("drag-hover-top")
                    ctx.remove_class("drag-hover-bottom")
                except Exception:
                    pass
                _current_hover_row = None
                _current_hover_dir = None

        def on_drag_data_get(widget, context, sel_data, info, time):
            sel_data.set_text(str(widget.item_index), -1)

        def on_drag_end(widget, context):
            clear_hover_styling()

        def on_drag_motion(lb, context, x, y, time):
            nonlocal _current_hover_row, _current_hover_dir
            vadj = scrolled.get_vadjustment()
            if vadj:
                visible_top = vadj.get_value()
                visible_height = vadj.get_page_size()
                clamped_y = max(visible_top, min(y, visible_top + visible_height))
            else:
                clamped_y = y

            row = lb.get_row_at_y(clamped_y)
            if row is None:
                clear_hover_styling()
                Gdk.drag_status(context, Gdk.DragAction.MOVE, time)
                return True

            alloc = row.get_allocation()
            row_y = clamped_y - alloc.y
            below = row_y > alloc.height / 2
            direction = 'bottom' if below else 'top'

            if _current_hover_row != row or _current_hover_dir != direction:
                clear_hover_styling()
                _current_hover_row = row
                _current_hover_dir = direction
                ctx = row.get_style_context()
                if direction == 'top':
                    ctx.add_class("drag-hover-top")
                else:
                    ctx.add_class("drag-hover-bottom")

            Gdk.drag_status(context, Gdk.DragAction.MOVE, time)
            return True

        def on_drag_leave(lb, context, time):
            clear_hover_styling()

        def on_drag_data_received(lb, context, x, y, sel_data, info, time):
            src_text = sel_data.get_text()
            if src_text is None:
                Gtk.drag_finish(context, False, False, time)
                return

            try:
                src_idx = int(src_text)
            except (ValueError, TypeError):
                Gtk.drag_finish(context, False, False, time)
                return

            # Clamp y to the visible viewport bounds of the ScrolledWindow
            vadj = scrolled.get_vadjustment()
            if vadj:
                visible_top = vadj.get_value()
                visible_height = vadj.get_page_size()
                clamped_y = max(visible_top, min(y, visible_top + visible_height))
            else:
                clamped_y = y

            row = lb.get_row_at_y(clamped_y)
            clear_hover_styling()

            if row is None:
                dst_idx = len(items)
            else:
                alloc = row.get_allocation()
                row_y = clamped_y - alloc.y
                below = row_y > alloc.height / 2
                dst_idx = getattr(row, 'item_index', 0)
                if below:
                    dst_idx += 1

            if src_idx == dst_idx or src_idx == dst_idx - 1:
                Gtk.drag_finish(context, False, False, time)
                return

            item = items.pop(src_idx)
            if dst_idx > src_idx:
                dst_idx -= 1
            items.insert(dst_idx, item)

            build_rows()

            children = lb.get_children()
            if 0 <= dst_idx < len(children):
                lb.select_row(children[dst_idx])

            Gtk.drag_finish(context, True, False, time)

        listbox.connect("drag-motion", on_drag_motion)
        listbox.connect("drag-leave", on_drag_leave)
        listbox.connect("drag-data-received", on_drag_data_received)

        # ===== Fill listbox with rows =====
        def build_rows():
            for child in listbox.get_children():
                listbox.remove(child)
            for idx, item in enumerate(items):
                row = Gtk.ListBoxRow.new()
                row.item_index = idx
                row.get_style_context().add_class("sort-row")
                row.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_USER)
                row.set_size_request(-1, 36)

                evbox = Gtk.EventBox.new()
                evbox.item_index = idx

                lbl = Gtk.Label.new(item.title if item.title else "(untitled)")
                lbl.set_xalign(0)
                lbl.set_margin_start(16)
                lbl.set_margin_top(6)
                lbl.set_margin_bottom(6)
                evbox.add(lbl)
                row.add(evbox)

                evbox.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [target_entry], Gdk.DragAction.MOVE)
                evbox.connect("drag-data-get", on_drag_data_get)
                evbox.connect("drag-end", on_drag_end)

                listbox.add(row)
            listbox.show_all()

        # ===== Bottom buttons =====
        bottom_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        bottom_box.set_margin_top(8)
        bottom_box.set_margin_bottom(8)
        bottom_box.set_margin_end(12)

        cancel_btn = Gtk.Button.new_with_label("Cancel")
        cancel_btn.connect("clicked", lambda _: dialog.destroy())

        confirm_btn = Gtk.Button.new_with_label("Confirm")
        confirm_btn.get_style_context().add_class("suggested-action")

        def on_confirm(_btn):
            self._cat_store.reorder_items(self._active_category_id, items)
            self._rebuild()
            dialog.destroy()

        confirm_btn.connect("clicked", on_confirm)

        bottom_box.pack_end(confirm_btn, False, False, 0)
        bottom_box.pack_end(cancel_btn, False, False, 0)
        vbox.pack_start(bottom_box, False, False, 0)

        sep2 = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(sep2, False, False, 0)
        # Move bottom_box after sep2 in the vbox order
        vbox.reorder_child(bottom_box, -1)

        # ===== Build rows initially =====
        build_rows()

        # ===== Wire focus guards =====
        dialog.connect("show", lambda *_: self.on_dialog_shown and self.on_dialog_shown())
        dialog.connect("destroy", lambda *_: self.on_dialog_hidden and self.on_dialog_hidden())

        dialog.show_all()

    def _on_category_selected(self, _listbox, row):
        if row is None or not hasattr(row, 'cat_id'):
            return
        self._active_category_id = row.cat_id
        self._selected_index = 0
        
        # Reset the tab type to "all" when switching categories
        self._active_tab_type = "all"
        for t_type, btn in self._tab_buttons.items():
            context = btn.get_style_context()
            if t_type == "all":
                context.add_class("filter-tab-active")
            else:
                context.remove_class("filter-tab-active")
                
        if not self._in_category_button:
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

        self._in_category_button = True
        self._cat_list.select_row(row)
        self._in_category_button = False

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

        menu.append(Gtk.SeparatorMenuItem.new())

        rename_item = Gtk.MenuItem.new_with_label("Rename")
        rename_item.connect("activate", lambda *_: self._on_rename_category_clicked(None))
        menu.append(rename_item)

        delete_item = Gtk.MenuItem.new_with_label("Delete")
        delete_item.connect("activate", lambda *_: self._on_delete_category_clicked(None))
        menu.append(delete_item)

        if self.on_menu_shown:
            self.on_menu_shown()
        menu.connect("deactivate", lambda *_: GLib.timeout_add(300, self._on_menu_deactivated))
        menu.show_all()
        menu.popup(None, None, None, None, event.button, event.time)
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

            custom_prompts = self._custom_prompts_store.get_all()
            if custom_prompts:
                item_type = getattr(item, "type", "text")
                if item_type != "image":
                    applicable_prompts = []
                    for p in custom_prompts:
                        p_categories = getattr(p, "categories", None) or ["text"]
                        if item_type in p_categories:
                            applicable_prompts.append(p)
                    
                    if applicable_prompts:
                        sep = Gtk.SeparatorMenuItem.new()
                        menu.append(sep)
                        for p in applicable_prompts:
                            prompt_item = Gtk.MenuItem.new_with_label(p.name)
                            prompt_item.connect("activate", lambda *_, p_obj=p: self._ask_custom_prompt(item, p_obj))
                            menu.append(prompt_item)
        else:
            copy_item = Gtk.MenuItem.new_with_label("Copy")
            copy_item.connect("activate", lambda *_: self._activate_item(item))
            menu.append(copy_item)

            dynamic_copy_item = Gtk.MenuItem.new_with_label("Dynamic Copy")
            has_placeholders = len(TEMPLATE_REGEX.findall(item.text)) > 0
            dynamic_copy_item.set_sensitive(has_placeholders)
            dynamic_copy_item.connect("activate", lambda *_: self._show_dynamic_copy_dialog(item))
            menu.append(dynamic_copy_item)

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

    def _ask_custom_prompt(self, item: ClipboardItem, prompt_obj: CustomPrompt):
        original_content = item.text.rstrip()
        custom_prompt = prompt_obj.prompt.strip()

        # Interpolate ${&} placeholder
        has_placeholder = False
        pattern = PROMPT_PLACEHOLDER_RE
        def replace(match):
            nonlocal has_placeholder
            matched_str = match.group(0)
            if matched_str == '\\\\':
                return '\\\\'
            elif match.group(1):
                return match.group(1)
            else:
                has_placeholder = True
                return original_content

        interpolated = pattern.sub(replace, custom_prompt)

        if has_placeholder:
            final_query = interpolated.replace('\\\\', '\\')
            suffix = ""
        else:
            unescaped_prompt = interpolated.replace('\\\\', '\\')
            if unescaped_prompt:
                suffix = " " + unescaped_prompt
            else:
                suffix = ""
            final_query = original_content + suffix

        if len(final_query) > 2000:
            if has_placeholder:
                final_query = final_query[:2000]
            else:
                max_len = 2000 - len(suffix)
                truncated_original = original_content[:max_len]
                final_query = truncated_original + suffix

            dialog = Gtk.MessageDialog(
                transient_for=self.get_toplevel(),
                modal=True,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.OK,
                text="查询内容过长",
            )
            dialog.format_secondary_text("选中的剪切板内容过长，已自动截断至 2000 个字符进行 Google 搜索。")

            def _on_response(dlg, resp):
                dlg.destroy()
                if self.on_dialog_hidden:
                    self.on_dialog_hidden()
                self._open_google_search(final_query)
                if self.on_hide_request:
                    self.on_hide_request()

            dialog.connect("response", _on_response)
            if self.on_dialog_shown:
                self.on_dialog_shown()
            dialog.show_all()
        else:
            self._open_google_search(final_query)
            if self.on_hide_request:
                self.on_hide_request()

    def _open_google_search(self, query: str):
        import urllib.parse
        encoded = urllib.parse.quote(query)
        url = f"https://www.google.com/search?udm=50&q={encoded}"
        try:
            subprocess.Popen(["firefox", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            request_window_focus("firefox")
        except Exception:
            try:
                Gtk.show_uri_on_window(self.get_toplevel(), url, Gdk.CURRENT_TIME)
                request_window_focus("firefox")
            except Exception as e:
                print(f"Error launching Google search: {e}", flush=True)

    def _on_prompts_config_clicked(self, _btn):
        self._show_prompts_config_dialog()

    def _show_prompts_config_dialog(self):
        prompts = self._custom_prompts_store.get_all()
        self._dialog_active_idx = 0 if prompts else -1
        tab_buttons = {}

        dialog = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
        dialog.get_style_context().add_class("custom-dialog")
        dialog.set_title("Prompts Config")
        dialog.set_modal(True)
        dialog.set_default_size(600, 450)
        dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        dialog.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        dialog.set_resizable(True)
        dialog.set_transient_for(self.get_toplevel())

        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        dialog.add(vbox)

        title_label = Gtk.Label.new("Prompts Config")
        title_label.set_xalign(0)
        title_label.set_margin_start(12)
        title_label.set_margin_top(8)
        title_label.set_margin_bottom(8)
        vbox.pack_start(title_label, False, False, 0)

        sep1 = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(sep1, False, False, 0)

        # Tab bar (scrolled box)
        top_bar = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
        top_bar.set_margin_start(12)
        top_bar.set_margin_end(12)
        top_bar.set_margin_top(8)
        top_bar.set_margin_bottom(8)

        tab_scrolled = Gtk.ScrolledWindow.new()
        tab_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        tab_scrolled.set_hexpand(True)

        tab_bar_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
        tab_scrolled.add(tab_bar_box)
        top_bar.pack_start(tab_scrolled, True, True, 0)

        add_btn = Gtk.Button.new_with_label("➕")
        add_btn.set_tooltip_text("Add new prompt")
        top_bar.pack_start(add_btn, False, False, 0)
        vbox.pack_start(top_bar, False, False, 0)

        # Content edit area
        mid_vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 6)
        mid_vbox.set_margin_start(12)
        mid_vbox.set_margin_end(12)
        mid_vbox.set_margin_top(8)
        mid_vbox.set_margin_bottom(8)

        name_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        name_label = Gtk.Label.new("菜单显示名称:")
        name_label.set_xalign(0)
        name_entry = Gtk.Entry.new()
        name_entry.set_hexpand(True)
        name_hbox.pack_start(name_label, False, False, 0)
        name_hbox.pack_start(name_entry, True, True, 0)
        mid_vbox.pack_start(name_hbox, False, False, 0)

        prompt_label_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
        prompt_label = Gtk.Label.new("追加提示词:")
        prompt_label.set_xalign(0)
        prompt_label_hbox.pack_start(prompt_label, True, True, 0)

        insert_btn = Gtk.Button.new_with_label("+ ${&}")
        insert_btn.set_tooltip_text("插入剪切板内容占位符")
        insert_btn.get_style_context().add_class("flat")

        def on_insert_clicked(_btn):
            buffer = prompt_textview.get_buffer()
            buffer.insert_at_cursor("${&}")
            prompt_textview.grab_focus()

        insert_btn.connect("clicked", on_insert_clicked)
        prompt_label_hbox.pack_end(insert_btn, False, False, 0)
        mid_vbox.pack_start(prompt_label_hbox, False, False, 0)

        prompt_scrolled = Gtk.ScrolledWindow.new()
        prompt_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        prompt_scrolled.set_vexpand(True)

        prompt_textview = Gtk.TextView.new()
        prompt_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        prompt_scrolled.add(prompt_textview)
        mid_vbox.pack_start(prompt_scrolled, True, True, 0)

        # Checkboxes for categories
        applicability_hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 12)
        applicability_hbox.set_margin_top(8)
        applicability_hbox.set_margin_bottom(8)

        app_label = Gtk.Label.new("适用类别:")
        app_label.set_xalign(0)
        applicability_hbox.pack_start(app_label, False, False, 0)

        select_all_check = Gtk.CheckButton.new_with_label("全选")
        text_check = Gtk.CheckButton.new_with_label("文本")
        link_check = Gtk.CheckButton.new_with_label("链接")
        code_check = Gtk.CheckButton.new_with_label("代码")

        applicability_hbox.pack_start(select_all_check, False, False, 0)
        applicability_hbox.pack_start(text_check, False, False, 0)
        applicability_hbox.pack_start(link_check, False, False, 0)
        applicability_hbox.pack_start(code_check, False, False, 0)

        mid_vbox.pack_start(applicability_hbox, False, False, 0)
        vbox.pack_start(mid_vbox, True, True, 0)

        updating_checks = [False]

        def update_select_all_state():
            if updating_checks[0]:
                return
            updating_checks[0] = True
            all_checked = text_check.get_active() and link_check.get_active() and code_check.get_active()
            select_all_check.set_active(all_checked)
            updating_checks[0] = False

        def on_select_all_toggled(widget):
            if updating_checks[0]:
                return
            updating_checks[0] = True
            active = widget.get_active()
            text_check.set_active(active)
            link_check.set_active(active)
            code_check.set_active(active)
            updating_checks[0] = False

        def on_check_toggled(widget):
            update_select_all_state()

        select_all_check.connect("toggled", on_select_all_toggled)
        text_check.connect("toggled", on_check_toggled)
        link_check.connect("toggled", on_check_toggled)
        code_check.connect("toggled", on_check_toggled)

        def get_selected_categories():
            cats = []
            if text_check.get_active():
                cats.append("text")
            if link_check.get_active():
                cats.append("link")
            if code_check.get_active():
                cats.append("code")
            return cats

        # Bottom buttons
        bottom_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        bottom_box.set_margin_top(8)
        bottom_box.set_margin_bottom(8)
        bottom_box.set_margin_start(12)
        bottom_box.set_margin_end(12)

        delete_btn = Gtk.Button.new_with_label("🗑️ Delete")
        cancel_btn = Gtk.Button.new_with_label("Cancel")
        confirm_btn = Gtk.Button.new_with_label("Confirm")
        confirm_btn.get_style_context().add_class("suggested-action")

        bottom_box.pack_start(delete_btn, False, False, 0)
        bottom_box.pack_end(confirm_btn, False, False, 0)
        bottom_box.pack_end(cancel_btn, False, False, 0)
        vbox.pack_start(bottom_box, False, False, 0)

        sep2 = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(sep2, False, False, 0)
        vbox.reorder_child(bottom_box, -1)

        def save_current_active_prompt():
            if 0 <= self._dialog_active_idx < len(prompts):
                name = name_entry.get_text().strip()
                prompts[self._dialog_active_idx].name = name if name else "New Prompt"

                buffer = prompt_textview.get_buffer()
                start, end = buffer.get_bounds()
                prompt_text = buffer.get_text(start, end, True)
                prompts[self._dialog_active_idx].prompt = prompt_text

                # Save categories
                prompts[self._dialog_active_idx].categories = get_selected_categories()

        def load_prompt_to_fields(idx):
            if 0 <= idx < len(prompts):
                updating_checks[0] = True
                # Temporarily block the name_entry changed handler to prevent self-triggering cycle
                name_entry.handler_block(changed_handler_id)
                name_entry.set_text(prompts[idx].name)
                name_entry.handler_unblock(changed_handler_id)

                prompt_textview.get_buffer().set_text(prompts[idx].prompt)

                # Load categories
                cats = getattr(prompts[idx], "categories", None) or ["text"]
                text_check.set_active("text" in cats)
                link_check.set_active("link" in cats)
                code_check.set_active("code" in cats)

                all_checked = "text" in cats and "link" in cats and "code" in cats
                select_all_check.set_active(all_checked)
                updating_checks[0] = False

                name_entry.set_sensitive(True)
                prompt_textview.set_sensitive(True)
                insert_btn.set_sensitive(True)
                text_check.set_sensitive(True)
                link_check.set_sensitive(True)
                code_check.set_sensitive(True)
                select_all_check.set_sensitive(True)
                delete_btn.set_sensitive(True)
            else:
                updating_checks[0] = True
                name_entry.handler_block(changed_handler_id)
                name_entry.set_text("")
                name_entry.handler_unblock(changed_handler_id)

                prompt_textview.get_buffer().set_text("")
                text_check.set_active(False)
                link_check.set_active(False)
                code_check.set_active(False)
                select_all_check.set_active(False)
                updating_checks[0] = False

                name_entry.set_sensitive(False)
                prompt_textview.set_sensitive(False)
                insert_btn.set_sensitive(False)
                text_check.set_sensitive(False)
                link_check.set_sensitive(False)
                code_check.set_sensitive(False)
                select_all_check.set_sensitive(False)
                delete_btn.set_sensitive(False)

        def rebuild_tabs():
            for child in tab_bar_box.get_children():
                tab_bar_box.remove(child)
            tab_buttons.clear()

            for idx, p in enumerate(prompts):
                btn = Gtk.Button.new_with_label(p.name)
                btn.idx = idx
                if idx == self._dialog_active_idx:
                    btn.get_style_context().add_class("suggested-action")

                def on_tab_clicked(b):
                    save_current_active_prompt()
                    self._dialog_active_idx = b.idx
                    rebuild_tabs()
                    load_prompt_to_fields(b.idx)

                btn.connect("clicked", on_tab_clicked)
                tab_bar_box.pack_start(btn, False, False, 0)
                tab_buttons[idx] = btn
            tab_bar_box.show_all()

        def on_add_clicked(_btn):
            save_current_active_prompt()
            new_p = CustomPrompt(
                id=str(uuid4()),
                name="New Prompt",
                prompt="",
                categories=["text"]
            )
            prompts.append(new_p)
            self._dialog_active_idx = len(prompts) - 1
            rebuild_tabs()
            load_prompt_to_fields(self._dialog_active_idx)
            name_entry.grab_focus()

        def on_delete_clicked(_btn):
            if not (0 <= self._dialog_active_idx < len(prompts)):
                return

            confirm = Gtk.MessageDialog(
                transient_for=dialog,
                modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="确定要删除该提示词配置吗？",
            )

            def on_confirm_resp(dlg, resp):
                dlg.destroy()
                if resp == Gtk.ResponseType.YES:
                    prompts.pop(self._dialog_active_idx)
                    if not prompts:
                        self._dialog_active_idx = -1
                    else:
                        self._dialog_active_idx = max(0, self._dialog_active_idx - 1)
                    rebuild_tabs()
                    load_prompt_to_fields(self._dialog_active_idx)

            confirm.connect("response", on_confirm_resp)
            confirm.show_all()

        def on_confirm_clicked(_btn):
            # Validate categories
            cats = get_selected_categories()

            if 0 <= self._dialog_active_idx < len(prompts) and not cats:
                warning = Gtk.MessageDialog(
                    transient_for=dialog,
                    modal=True,
                    message_type=Gtk.MessageType.WARNING,
                    buttons=Gtk.ButtonsType.OK,
                    text="配置无效",
                )
                warning.format_secondary_text("请至少勾选一个适用类别（文本、链接、代码）。")

                def on_warn_resp(dlg, resp):
                    dlg.destroy()
                warning.connect("response", on_warn_resp)
                warning.show_all()
                return

            save_current_active_prompt()
            for p in prompts:
                if not p.name.strip():
                    p.name = "New Prompt"
                # Safe fallback
                if not getattr(p, "categories", None):
                    p.categories = ["text"]
            self._custom_prompts_store.save_all(prompts)
            dialog.destroy()

        def on_name_changed(entry):
            idx = self._dialog_active_idx
            if 0 <= idx < len(prompts):
                new_text = entry.get_text().strip()
                display_name = new_text if new_text else "New Prompt"
                prompts[idx].name = display_name
                if idx in tab_buttons:
                    tab_buttons[idx].set_label(display_name)

        changed_handler_id = name_entry.connect("changed", on_name_changed)

        add_btn.connect("clicked", on_add_clicked)
        delete_btn.connect("clicked", on_delete_clicked)
        cancel_btn.connect("clicked", lambda _: dialog.destroy())
        confirm_btn.connect("clicked", on_confirm_clicked)

        rebuild_tabs()
        load_prompt_to_fields(self._dialog_active_idx)

        dialog.connect("show", lambda *_: self.on_dialog_shown and self.on_dialog_shown())
        dialog.connect("destroy", lambda *_: self.on_dialog_hidden and self.on_dialog_hidden())

        dialog.show_all()

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
        visible_rows = [
            r for r in self._content_list.get_children()
            if r.is_visible() and getattr(r, "store_item", None) is not None
        ]
        if not visible_rows:
            return

        sel = self._content_list.get_selected_row()
        if sel is None or sel not in visible_rows:
            idx = 0 if direction > 0 else len(visible_rows) - 1
        else:
            idx = visible_rows.index(sel)
            idx = max(0, min(len(visible_rows) - 1, idx + direction))

        target = visible_rows[idx]
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
            text = self._process_template_text(text)
        else:
            return
        _copy_to_clipboard(text)
        if self.on_copy_clipboard:
            self.on_copy_clipboard(text, item.hash if isinstance(item, ClipboardItem) else None)
        if self.on_hide_request:
            self.on_hide_request()

    def _process_template_text(self, text: str) -> str:
        """Process custom templates during direct copy.

        Replaces placeholders containing default values (e.g., ${1=default}) with their
        actual defaults (unescaped), and replaces placeholders without default values
        (e.g., ${1:prompt} or ${1}) with an empty string.
        """
        def repl(match):
            default_text = match.group(3)
            if default_text is not None:
                return self._unescape_template_field(default_text)
            return ""
        return TEMPLATE_REGEX.sub(repl, text)

    def _unescape_template_field(self, val: Optional[str]) -> Optional[str]:
        r"""Unescape backslash-escaped colons (\:) and equals (\=) inside a template field."""
        if val is None:
            return None
        return val.replace("\\:", ":").replace("\\=", "=")

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
        content.pack_start(sw, True, True, 0)

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

    def _show_message_dialog(self, msg_type: Gtk.MessageType, title: str, text: str):
        """Show a modal message dialog with focus guard (OK button)."""
        if self.on_dialog_shown:
            self.on_dialog_shown()
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            modal=True,
            message_type=msg_type,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dialog.format_secondary_text(text)
        dialog.connect("response", lambda dlg, _: dlg.destroy() or (self.on_dialog_hidden() if self.on_dialog_hidden else None))
        dialog.show_all()

    def _on_delete_category_clicked(self, _btn):
        cat_id = self._active_category_id
        if cat_id == "__clipboard__":
            return
        cat = self._cat_store.get(cat_id)
        if cat is None:
            return

        if cat.pinned:
            self._show_message_dialog(
                Gtk.MessageType.WARNING,
                "Cannot delete pinned category",
                "This category is pinned at the top. Please remove it from the top before deleting."
            )
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

    def cancel_rename(self):
        if self._editing_rename_entry:
            entry = self._editing_rename_entry
            self._editing_rename_entry = None

            # Disconnect handlers by ID
            if self._rename_focus_out_id:
                try:
                    entry.disconnect(self._rename_focus_out_id)
                except Exception:
                    pass
                self._rename_focus_out_id = 0
            if self._rename_activate_id:
                try:
                    entry.disconnect(self._rename_activate_id)
                except Exception:
                    pass
                self._rename_activate_id = 0

            # Shift focus away from the entry to avoid GTK focus destruction crashes
            toplevel = self.get_toplevel()
            if toplevel and hasattr(toplevel, 'set_focus'):
                try:
                    toplevel.set_focus(None)
                except Exception:
                    pass

            self._editing_rename_row = None
            self._editing_rename_old_name = None
            self._editing_rename_cat_id = None
            self._rebuild_category_list()
        return False

    def _on_rename_activate(self, ent):
        old_name = self._editing_rename_old_name
        cat_id = self._editing_rename_cat_id
        new_name = ent.get_text().strip()
        if new_name and new_name != old_name and cat_id:
            try:
                self._cat_store.rename(cat_id, new_name)
            except ValueError:
                pass
        self.cancel_rename()

    def _on_rename_focus_out(self, ent, ev):
        GLib.idle_add(self.cancel_rename)
        return False

    def _on_rename_category_clicked(self, _btn):
        self.cancel_rename()

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

        self._editing_rename_row = selected_row
        self._editing_rename_entry = entry
        self._editing_rename_old_name = old_name
        self._editing_rename_cat_id = cat_id

        self._rename_activate_id = entry.connect("activate", self._on_rename_activate)
        self._rename_focus_out_id = entry.connect("focus-out-event", self._on_rename_focus_out)

    def _on_backup_clicked(self, _btn):
        if self.on_dialog_shown:
            self.on_dialog_shown()
        dialog = Gtk.FileChooserDialog(
            title="Select backup destination",
            transient_for=self.get_toplevel(),
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Backup", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)

        def on_response(dlg, resp):
            selected_path = dlg.get_filename()
            dlg.destroy()
            if resp != Gtk.ResponseType.ACCEPT or not selected_path:
                if self.on_dialog_hidden:
                    self.on_dialog_hidden()
                return

            err, path_or_msg = _backup_config(selected_path)
            if err is None:
                self._show_message_dialog(
                    Gtk.MessageType.INFO,
                    "Backup Complete",
                    "Backup saved to:\n" + path_or_msg,
                )
            else:
                self._show_message_dialog(
                    Gtk.MessageType.WARNING,
                    "Backup Failed",
                    "Could not create backup:\n" + err,
                )
            if self.on_dialog_hidden:
                self.on_dialog_hidden()

        dialog.connect("response", on_response)
        dialog.show_all()

    def _on_restore_clicked(self, _btn):
        if self.on_dialog_shown:
            self.on_dialog_shown()
        dialog = Gtk.FileChooserDialog(
            title="Select backup archive to restore",
            transient_for=self.get_toplevel(),
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Restore", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)

        filt = Gtk.FileFilter.new()
        filt.set_name("Backup archives (*.tar.gz)")
        filt.add_pattern("*.tar.gz")
        dialog.add_filter(filt)

        def on_response(dlg, resp):
            archive_path = dlg.get_filename()
            dlg.destroy()
            if resp != Gtk.ResponseType.ACCEPT or not archive_path:
                if self.on_dialog_hidden:
                    self.on_dialog_hidden()
                return

            self._confirm_restore(archive_path)

        dialog.connect("response", on_response)
        dialog.show_all()

    def _confirm_restore(self, archive_path: str):
        if self.on_dialog_shown:
            self.on_dialog_shown()
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Restore backup?",
        )
        dialog.format_secondary_text(
            "This will overwrite all current clipboard history, categories, "
            "and settings with the data from the backup archive.\n\n"
            "This action cannot be undone."
        )

        def on_resp(dlg, resp):
            dlg.destroy()
            if resp != Gtk.ResponseType.YES:
                if self.on_dialog_hidden:
                    self.on_dialog_hidden()
                return

            err = _restore_config(archive_path)
            if err is None:
                self._show_message_dialog(
                    Gtk.MessageType.INFO,
                    "Restore Complete",
                    "Restore completed. Refreshing data...",
                )
                self.load_cached()
            else:
                self._show_message_dialog(
                    Gtk.MessageType.WARNING,
                    "Restore Failed",
                    "Could not restore backup:\n" + err,
                )
            if self.on_dialog_hidden:
                self.on_dialog_hidden()

        dialog.connect("response", on_resp)
        dialog.show_all()

    def _on_recycle_bin_clicked(self, _btn):
        self._show_recycle_bin_dialog()

    def _show_recycle_bin_dialog(self):
        if self.on_dialog_shown:
            self.on_dialog_shown()

        from copy import deepcopy
        from uuid import uuid4

        dialog = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
        dialog.get_style_context().add_class("custom-dialog")
        dialog.set_title("Recycle Bin")
        dialog.set_modal(True)
        dialog.set_default_size(500, 400)
        dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        dialog.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        dialog.set_resizable(True)
        dialog.set_transient_for(self.get_toplevel())

        # Transaction copies of the recycle bin and categories
        temp_recycle_bin = deepcopy(self._cat_store._recycle_bin)
        temp_categories = deepcopy(self._cat_store._categories)

        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)
        dialog.add(vbox)

        # Title Label
        title_lbl = Gtk.Label.new("Deleted Templates:")
        title_lbl.set_xalign(0)
        title_lbl.set_halign(Gtk.Align.START)
        vbox.pack_start(title_lbl, False, False, 0)

        # List Area
        scrolled = Gtk.ScrolledWindow.new()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        listbox = Gtk.ListBox.new()
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        scrolled.add(listbox)
        vbox.pack_start(scrolled, True, True, 0)

        # Middle bottom box: Restore button
        middle_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
        restore_btn = Gtk.Button.new_with_label("Restore")
        middle_box.pack_start(restore_btn, True, False, 0)
        vbox.pack_start(middle_box, False, False, 0)

        # Separator before bottom bar
        sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(sep, False, False, 0)

        # Bottom box: Permanently Delete (left) | Cancel & Confirm (right)
        bottom_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        
        perm_delete_btn = Gtk.Button.new_with_label("Permanently Delete All")
        perm_delete_btn.get_style_context().add_class("destructive-action")
        bottom_box.pack_start(perm_delete_btn, False, False, 0)

        cancel_btn = Gtk.Button.new_with_label("Cancel")
        confirm_btn = Gtk.Button.new_with_label("Confirm")
        confirm_btn.get_style_context().add_class("suggested-action")
        
        bottom_box.pack_end(confirm_btn, False, False, 0)
        bottom_box.pack_end(cancel_btn, False, False, 0)
        vbox.pack_start(bottom_box, False, False, 0)

        def build_rows():
            for child in listbox.get_children():
                listbox.remove(child)
            for entry in temp_recycle_bin:
                row = Gtk.ListBoxRow.new()
                row.entry = entry

                hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
                
                title = entry["item"]["title"]
                if not title:
                    title = "(untitled)"
                title_lbl = Gtk.Label.new(title)
                title_lbl.set_halign(Gtk.Align.START)
                title_lbl.set_xalign(0)
                title_lbl.set_margin_start(12)
                title_lbl.set_margin_top(8)
                title_lbl.set_margin_bottom(8)

                cat_name = entry["original_cat_name"]
                cat_lbl = Gtk.Label.new(f"From category: {cat_name}")
                cat_lbl.set_halign(Gtk.Align.END)
                cat_lbl.set_xalign(1)
                cat_lbl.set_margin_end(12)
                cat_lbl.override_color(Gtk.StateFlags.NORMAL, self._snippet_color)

                hbox.pack_start(title_lbl, True, True, 0)
                hbox.pack_end(cat_lbl, False, False, 0)
                row.add(hbox)
                listbox.add(row)
            listbox.show_all()

        build_rows()

        # Restore action
        def on_restore_clicked(_btn):
            row = listbox.get_selected_row()
            if not row or not hasattr(row, "entry"):
                return
            entry = row.entry
            
            # Perform restore in temp
            temp_recycle_bin.remove(entry)
            
            orig_id = entry["original_cat_id"]
            orig_name = entry["original_cat_name"]
            item_data = entry["item"]
            item = CategoryItem(
                title=item_data["title"],
                text=item_data["text"],
                timestamp=item_data["timestamp"]
            )
            
            target_cat = None
            for c in temp_categories:
                if c.id == orig_id:
                    target_cat = c
                    break
            if not target_cat:
                for c in temp_categories:
                    if c.name == orig_name:
                        target_cat = c
                        break
            if not target_cat:
                # To prevent naming conflicts in temp_categories
                new_cat_id = uuid4().hex[:12]
                target_cat = CustomCategory(
                    id=new_cat_id,
                    name=orig_name,
                    items=[],
                    pinned=False,
                    created_at=int(time.time() * 1000)
                )
                temp_categories.append(target_cat)
            
            target_cat.items.append(item)
            build_rows()

        restore_btn.connect("clicked", on_restore_clicked)

        # Permanently Delete All action
        def on_perm_delete_clicked(_btn):
            if not temp_recycle_bin:
                return

            # Double confirmation dialog
            confirm = Gtk.MessageDialog(
                transient_for=dialog,
                modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Permanently delete all templates?",
            )
            confirm.format_secondary_text("This action cannot be undone and all templates in the Recycle Bin will be lost forever.")
            
            def on_confirm_response(dlg, resp):
                dlg.destroy()
                if resp == Gtk.ResponseType.YES:
                    temp_recycle_bin.clear()
                    build_rows()

            confirm.connect("response", on_confirm_response)
            confirm.show_all()

        perm_delete_btn.connect("clicked", on_perm_delete_clicked)

        # Cancel action
        def on_cancel_clicked(_btn):
            dialog.destroy()

        cancel_btn.connect("clicked", on_cancel_clicked)

        # Confirm action
        def on_confirm_clicked(_btn):
            self._cat_store._recycle_bin = temp_recycle_bin
            self._cat_store._categories = temp_categories
            self._cat_store._save()
            self._rebuild_category_list()
            self._rebuild()
            dialog.destroy()

        confirm_btn.connect("clicked", on_confirm_clicked)

        # Focus guards connection
        dialog.connect("show", lambda *_: self.on_dialog_shown and self.on_dialog_shown())
        dialog.connect("destroy", lambda *_: self.on_dialog_hidden and self.on_dialog_hidden())

        dialog.show_all()

    def _on_sort_cats_clicked(self, _btn):
        self._show_sort_cats_dialog()

    def _show_sort_cats_dialog(self):
        if self.on_dialog_shown:
            self.on_dialog_shown()

        from copy import deepcopy

        dialog = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
        dialog.get_style_context().add_class("custom-dialog")
        dialog.set_title("Sort Categories")
        dialog.set_modal(True)
        dialog.set_default_size(500, 400)
        dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        dialog.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        dialog.set_resizable(True)
        dialog.set_transient_for(self.get_toplevel())

        # Exclude __clipboard__ as it's the system immutable category at top
        all_cats = [c for c in self._cat_store.get_all() if c.id != "__clipboard__"]
        temp_pinned = [c for c in all_cats if c.pinned]
        temp_normal = [c for c in all_cats if not c.pinned]

        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)
        dialog.add(vbox)

        # Tab layout using Notebook
        notebook = Gtk.Notebook.new()
        notebook.set_show_border(False)
        vbox.pack_start(notebook, True, True, 0)

        # Tab 1: Pinned Categories
        page1 = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        scrolled1 = Gtk.ScrolledWindow.new()
        scrolled1.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled1.set_vexpand(True)
        listbox1 = Gtk.ListBox.new()
        listbox1.set_selection_mode(Gtk.SelectionMode.SINGLE)
        scrolled1.add(listbox1)
        page1.pack_start(scrolled1, True, True, 0)
        notebook.append_page(page1, Gtk.Label.new("Pinned Categories"))

        # Tab 2: Normal Categories
        page2 = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        scrolled2 = Gtk.ScrolledWindow.new()
        scrolled2.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled2.set_vexpand(True)
        listbox2 = Gtk.ListBox.new()
        listbox2.set_selection_mode(Gtk.SelectionMode.SINGLE)
        scrolled2.add(listbox2)
        page2.pack_start(scrolled2, True, True, 0)
        notebook.append_page(page2, Gtk.Label.new("Normal Categories"))

        # ===== Drag & Drop setup =====
        target_entry = Gtk.TargetEntry.new("text/plain", Gtk.TargetFlags.SAME_APP, 0)

        css = Gtk.CssProvider()
        css.load_from_data(b"""
            .sort-row {
                border-top: 3px solid transparent;
                border-bottom: 3px solid transparent;
            }
            .sort-row.drag-hover-top {
                border-top-color: #3584e4;
            }
            .sort-row.drag-hover-bottom {
                border-bottom-color: #3584e4;
            }
        """)
        listbox1.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_USER)
        listbox2.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_USER)

        def setup_dnd(lb, scr, items, rebuild_func):
            _current_hover_row = None
            _current_hover_dir = None

            def clear_hover_styling():
                nonlocal _current_hover_row, _current_hover_dir
                if _current_hover_row:
                    try:
                        ctx = _current_hover_row.get_style_context()
                        ctx.remove_class("drag-hover-top")
                        ctx.remove_class("drag-hover-bottom")
                    except Exception:
                        pass
                    _current_hover_row = None
                    _current_hover_dir = None

            def on_drag_data_get(widget, context, sel_data, info, time):
                sel_data.set_text(str(widget.item_index), -1)

            def on_drag_end(widget, context):
                clear_hover_styling()

            def on_drag_motion(lb_widget, context, x, y, time):
                nonlocal _current_hover_row, _current_hover_dir
                vadj = scr.get_vadjustment()
                if vadj:
                    visible_top = vadj.get_value()
                    visible_height = vadj.get_page_size()
                    clamped_y = max(visible_top, min(y, visible_top + visible_height))
                else:
                    clamped_y = y

                row = lb_widget.get_row_at_y(clamped_y)
                if row is None:
                    clear_hover_styling()
                    Gdk.drag_status(context, Gdk.DragAction.MOVE, time)
                    return True

                alloc = row.get_allocation()
                row_y = clamped_y - alloc.y
                below = row_y > alloc.height / 2
                direction = 'bottom' if below else 'top'

                if _current_hover_row != row or _current_hover_dir != direction:
                    clear_hover_styling()
                    _current_hover_row = row
                    _current_hover_dir = direction
                    ctx = row.get_style_context()
                    if direction == 'top':
                        ctx.add_class("drag-hover-top")
                    else:
                        ctx.add_class("drag-hover-bottom")

                Gdk.drag_status(context, Gdk.DragAction.MOVE, time)
                return True

            def on_drag_leave(lb_widget, context, time):
                clear_hover_styling()

            def on_drag_data_received(lb_widget, context, x, y, sel_data, info, time):
                src_text = sel_data.get_text()
                if src_text is None:
                    Gtk.drag_finish(context, False, False, time)
                    return

                try:
                    src_idx = int(src_text)
                except (ValueError, TypeError):
                    Gtk.drag_finish(context, False, False, time)
                    return

                vadj = scr.get_vadjustment()
                if vadj:
                    visible_top = vadj.get_value()
                    visible_height = vadj.get_page_size()
                    clamped_y = max(visible_top, min(y, visible_top + visible_height))
                else:
                    clamped_y = y

                row = lb_widget.get_row_at_y(clamped_y)
                clear_hover_styling()

                if row is None:
                    dst_idx = len(items)
                else:
                    alloc = row.get_allocation()
                    row_y = clamped_y - alloc.y
                    below = row_y > alloc.height / 2
                    dst_idx = getattr(row, 'item_index', 0)
                    if below:
                        dst_idx += 1

                if src_idx == dst_idx or src_idx == dst_idx - 1:
                    Gtk.drag_finish(context, False, False, time)
                    return

                item = items.pop(src_idx)
                if dst_idx > src_idx:
                    dst_idx -= 1
                items.insert(dst_idx, item)

                rebuild_func()

                children = lb_widget.get_children()
                if 0 <= dst_idx < len(children):
                    lb_widget.select_row(children[dst_idx])

                Gtk.drag_finish(context, True, False, time)

            lb.drag_dest_set(Gtk.DestDefaults.MOTION | Gtk.DestDefaults.DROP, [target_entry], Gdk.DragAction.MOVE)
            lb.connect("drag-motion", on_drag_motion)
            lb.connect("drag-leave", on_drag_leave)
            lb.connect("drag-data-received", on_drag_data_received)

            return on_drag_data_get, on_drag_end

        # ===== Pinned Categories build_rows =====
        def build_pinned():
            for child in listbox1.get_children():
                listbox1.remove(child)
            for idx, cat in enumerate(temp_pinned):
                row = Gtk.ListBoxRow.new()
                row.item_index = idx
                row.get_style_context().add_class("sort-row")
                row.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_USER)
                row.set_size_request(-1, 36)

                evbox = Gtk.EventBox.new()
                evbox.item_index = idx

                lbl = Gtk.Label.new(cat.name if cat.name else "(untitled)")
                lbl.set_xalign(0)
                lbl.set_margin_start(16)
                lbl.set_margin_top(6)
                lbl.set_margin_bottom(6)
                evbox.add(lbl)
                row.add(evbox)

                evbox.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [target_entry], Gdk.DragAction.MOVE)
                evbox.connect("drag-data-get", drag_get1)
                evbox.connect("drag-end", drag_end1)

                listbox1.add(row)
            listbox1.show_all()

        # ===== Normal Categories build_rows =====
        def build_normal():
            for child in listbox2.get_children():
                listbox2.remove(child)
            for idx, cat in enumerate(temp_normal):
                row = Gtk.ListBoxRow.new()
                row.item_index = idx
                row.get_style_context().add_class("sort-row")
                row.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_USER)
                row.set_size_request(-1, 36)

                evbox = Gtk.EventBox.new()
                evbox.item_index = idx

                lbl = Gtk.Label.new(cat.name if cat.name else "(untitled)")
                lbl.set_xalign(0)
                lbl.set_margin_start(16)
                lbl.set_margin_top(6)
                lbl.set_margin_bottom(6)
                evbox.add(lbl)
                row.add(evbox)

                evbox.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [target_entry], Gdk.DragAction.MOVE)
                evbox.connect("drag-data-get", drag_get2)
                evbox.connect("drag-end", drag_end2)

                listbox2.add(row)
            listbox2.show_all()

        # Connect drag handlers
        drag_get1, drag_end1 = setup_dnd(listbox1, scrolled1, temp_pinned, build_pinned)
        drag_get2, drag_end2 = setup_dnd(listbox2, scrolled2, temp_normal, build_normal)

        # Build initially
        build_pinned()
        build_normal()

        # Separator before bottom bar
        sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(sep, False, False, 0)

        # Bottom buttons box (right-aligned)
        bottom_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        bottom_box.set_margin_top(8)
        bottom_box.set_margin_bottom(8)
        bottom_box.set_margin_end(12)

        cancel_btn = Gtk.Button.new_with_label("Cancel")
        cancel_btn.connect("clicked", lambda _: dialog.destroy())

        confirm_btn = Gtk.Button.new_with_label("Confirm")
        confirm_btn.get_style_context().add_class("suggested-action")

        def on_confirm(_btn):
            # Combine the ordered pinned and normal categories back
            self._cat_store.reorder_categories(temp_pinned + temp_normal)
            self._rebuild_category_list()
            self._rebuild()
            dialog.destroy()

        confirm_btn.connect("clicked", on_confirm)

        bottom_box.pack_end(confirm_btn, False, False, 0)
        bottom_box.pack_end(cancel_btn, False, False, 0)
        vbox.pack_start(bottom_box, False, False, 0)

        # Focus guards connection
        dialog.connect("show", lambda *_: self.on_dialog_shown and self.on_dialog_shown())
        dialog.connect("destroy", lambda *_: self.on_dialog_hidden and self.on_dialog_hidden())

        dialog.show_all()

    def _show_dynamic_copy_dialog(self, item):
        placeholders = {}
        defaults = {}
        for match in TEMPLATE_REGEX.finditer(item.text):
            num = int(match.group(1))
            prompt_text = match.group(2)
            default_text = match.group(3)
            if prompt_text:
                if num not in placeholders:
                    placeholders[num] = self._unescape_template_field(prompt_text)
            if default_text:
                if num not in defaults:
                    defaults[num] = self._unescape_template_field(default_text)

        matches = [m[0] for m in TEMPLATE_REGEX.findall(item.text)]
        if not matches:
            return

        nums = sorted(list(set(int(m) for m in matches)))

        dialog = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
        dialog.get_style_context().add_class("custom-dialog")
        dialog.set_title("Dynamic Copy - {}".format(item.title if item.title else "Template"))
        dialog.set_modal(True)
        dialog.set_default_size(800, 500)
        dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        dialog.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        dialog.set_resizable(True)
        dialog.set_transient_for(self.get_toplevel())

        # Main vertical container
        vbox_main = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
        vbox_main.set_margin_top(12)
        vbox_main.set_margin_bottom(12)
        vbox_main.set_margin_start(12)
        vbox_main.set_margin_end(12)
        dialog.add(vbox_main)

        # Left/Right columns container
        hbox_cols = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 12)
        vbox_main.pack_start(hbox_cols, True, True, 0)

        # --- Left Column: Parameter Input Panel ---
        vbox_input = Gtk.Box.new(Gtk.Orientation.VERTICAL, 6)

        lbl_input = Gtk.Label.new("Parameter Input:")
        lbl_input.set_xalign(0)
        vbox_input.pack_start(lbl_input, False, False, 0)

        notebook = Gtk.Notebook.new()
        notebook.set_show_border(False)
        vbox_input.pack_start(notebook, True, True, 0)
        hbox_cols.pack_start(vbox_input, True, True, 0)

        # --- Right Column: Real-time Preview & Edit Panel ---
        vbox_preview = Gtk.Box.new(Gtk.Orientation.VERTICAL, 6)

        lbl_preview = Gtk.Label.new("Real-time Preview:")
        lbl_preview.set_xalign(0)
        vbox_preview.pack_start(lbl_preview, False, False, 0)

        scrolled_preview = Gtk.ScrolledWindow.new()
        scrolled_preview.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled_preview.set_vexpand(True)

        preview_tv = Gtk.TextView.new()
        preview_tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        preview_tv.set_left_margin(8)
        preview_tv.set_right_margin(8)
        preview_tv.set_top_margin(8)
        preview_tv.set_bottom_margin(8)
        preview_buffer = preview_tv.get_buffer()
        scrolled_preview.add(preview_tv)
        vbox_preview.pack_start(scrolled_preview, True, True, 0)

        chk_edit = Gtk.CheckButton.new_with_label("允许在预览框直接编辑")
        chk_edit.set_active(True)
        preview_tv.set_editable(True)

        def on_chk_edit_toggled(btn):
            preview_tv.set_editable(btn.get_active())

        chk_edit.connect("toggled", on_chk_edit_toggled)
        vbox_preview.pack_start(chk_edit, False, False, 0)
        hbox_cols.pack_start(vbox_preview, True, True, 0)

        # --- Data Binding and Key Event logic ---
        input_buffers = {}
        input_textviews = {}
        is_updating_preview = False

        def update_preview():
            nonlocal is_updating_preview
            if is_updating_preview:
                return
            is_updating_preview = True
            try:
                text = item.text
                for num, buf in input_buffers.items():
                    start_iter = buf.get_start_iter()
                    end_iter = buf.get_end_iter()
                    val = buf.get_text(start_iter, end_iter, True)
                    if not val:
                        replacement = ""
                    else:
                        replacement = val
                    pattern = r"\$\{" + str(num) + r"(?:[:=][^}]+)?\}"
                    text = re.sub(pattern, lambda m: replacement, text)
                preview_buffer.set_text(text)
            finally:
                is_updating_preview = False

        def on_key_press(widget, event, num_val):
            is_shift = (event.state & Gdk.ModifierType.SHIFT_MASK) != 0

            # Shift+Tab
            if event.keyval == Gdk.KEY_ISO_Left_Tab or (event.keyval == Gdk.KEY_Tab and is_shift):
                current_page = notebook.get_current_page()
                if current_page > 0:
                    notebook.set_current_page(current_page - 1)
                    prev_num = nums[current_page - 1]
                    input_textviews[prev_num].grab_focus()
                return True

            # Tab
            if event.keyval == Gdk.KEY_Tab:
                current_page = notebook.get_current_page()
                n_pages = notebook.get_n_pages()
                if current_page < n_pages - 1:
                    notebook.set_current_page(current_page + 1)
                    next_num = nums[current_page + 1]
                    input_textviews[next_num].grab_focus()
                else:
                    confirm_btn.grab_focus()
                return True

            # Ctrl/Cmd+Enter
            is_enter = event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
            has_modifier = (event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD4_MASK | Gdk.ModifierType.META_MASK)) != 0
            if is_enter and has_modifier:
                on_confirm(None)
                return True

            return False

        def on_textview_draw(widget, cr):
            buf = widget.get_buffer()
            if buf.get_char_count() == 0:
                placeholder = getattr(widget, "placeholder_text", "")
                if placeholder:
                    text_window = widget.get_window(Gtk.TextWindowType.TEXT)
                    if text_window and Gtk.cairo_should_draw_window(cr, text_window):
                        cr.save()
                        start_iter = buf.get_start_iter()
                        rect = widget.get_iter_location(start_iter)
                        left, top = widget.buffer_to_window_coords(Gtk.TextWindowType.TEXT, rect.x, rect.y)
                        cr.translate(left, top)
                        
                        layout = widget.create_pango_layout(placeholder)
                        context = widget.get_style_context()
                        font_desc = context.get_property("font", Gtk.StateFlags.NORMAL)
                        layout.set_font_description(font_desc)
                        
                        color = context.get_color(Gtk.StateFlags.NORMAL)
                        cr.set_source_rgba(color.red, color.green, color.blue, 0.45)
                        
                        PangoCairo.show_layout(cr, layout)
                        cr.restore()
            return False

        for num in nums:
            scr_in = Gtk.ScrolledWindow.new()
            scr_in.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            scr_in.set_margin_top(6)
            scr_in.set_margin_bottom(6)
            scr_in.set_margin_start(6)
            scr_in.set_margin_end(6)

            tv_in = Gtk.TextView.new()
            tv_in.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            tv_in.set_left_margin(8)
            tv_in.set_right_margin(8)
            tv_in.set_top_margin(8)
            tv_in.set_bottom_margin(8)
            buf_in = tv_in.get_buffer()
            scr_in.add(tv_in)

            tab_lbl = Gtk.Label.new(f"${{{num}}}")
            notebook.append_page(scr_in, tab_lbl)

            input_buffers[num] = buf_in
            input_textviews[num] = tv_in

            # Set placeholder if defined
            if num in placeholders:
                tv_in.placeholder_text = placeholders[num]
                tv_in.connect_after("draw", on_textview_draw)

            # Set default text if defined
            if num in defaults:
                buf_in.set_text(defaults[num])

            # Select all text on focus
            def on_focus_in(widget, event):
                buf = widget.get_buffer()
                start, end = buf.get_bounds()
                buf.select_range(start, end)
                return False
            tv_in.connect("focus-in-event", on_focus_in)

            buf_in.connect("changed", lambda *_: update_preview())
            buf_in.connect("changed", lambda w, tv=tv_in: tv.queue_draw())
            tv_in.connect("key-press-event", on_key_press, num)

        def on_preview_key_press(widget, event):
            is_enter = event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
            has_modifier = (event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD4_MASK | Gdk.ModifierType.META_MASK)) != 0
            if is_enter and has_modifier:
                on_confirm(None)
                return True
            return False

        preview_tv.connect("key-press-event", on_preview_key_press)

        # Initialize preview text
        update_preview()

        # Separator before buttons
        sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        vbox_main.pack_start(sep, False, False, 0)

        # --- Bottom Buttons Box ---
        bottom_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
        bottom_box.set_margin_top(8)
        bottom_box.set_margin_bottom(8)
        bottom_box.set_margin_end(12)

        cancel_btn = Gtk.Button.new_with_label("Cancel")
        cancel_btn.connect("clicked", lambda _: dialog.destroy())

        confirm_btn = Gtk.Button.new_with_label("Copy")
        confirm_btn.get_style_context().add_class("suggested-action")

        def on_confirm(_btn):
            start_iter = preview_buffer.get_start_iter()
            end_iter = preview_buffer.get_end_iter()
            text = preview_buffer.get_text(start_iter, end_iter, True)

            _copy_to_clipboard(text)
            if self.on_copy_clipboard:
                self.on_copy_clipboard(text, None)

            dialog.destroy()

            if self.on_hide_request:
                self.on_hide_request()

        confirm_btn.connect("clicked", on_confirm)

        bottom_box.pack_end(confirm_btn, False, False, 0)
        bottom_box.pack_end(cancel_btn, False, False, 0)
        vbox_main.pack_start(bottom_box, False, False, 0)

        # Focus guards
        dialog.connect("show", lambda *_: self.on_dialog_shown and self.on_dialog_shown())
        dialog.connect("destroy", lambda *_: self.on_dialog_hidden and self.on_dialog_hidden())

        # Grab focus on the first input textview initially
        if nums:
            input_textviews[nums[0]].grab_focus()

        dialog.show_all()





def _backup_config(target_dir: str) -> tuple[Optional[str], str]:
    """Backup ~/.config/opencode-switcher/ to a .tar.gz archive.

    Returns (error, path_or_msg). On success, error=None and path_or_msg is the archive path.
    On failure, error is the error string and path_or_msg is empty.
    """
    import tarfile
    config_dir = os.path.expanduser("~/.config/opencode-switcher")
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    archive_name = f"opencode-switcher-backup-{timestamp}.tar.gz"
    archive_path = os.path.join(target_dir, archive_name)
    try:
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(config_dir, arcname="opencode-switcher")
        return (None, archive_path)
    except Exception as e:
        return (str(e), "")


def _restore_config(archive_path: str) -> Optional[str]:
    """Restore opencode-switcher config from a .tar.gz archive.

    Returns None on success, or an error message string on failure.
    """
    import tarfile
    config_parent = os.path.dirname(os.path.expanduser("~/.config/opencode-switcher"))
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(config_parent)
        return None
    except Exception as e:
        return str(e)
