import gi
import subprocess
import threading
import os
import re
import html
import tool_registry
gi.require_version("Gtk", "3.0")
gi.require_version("Gio", "2.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("WebKit2", "4.1")
import sys
import hashlib
import mimetypes
import urllib.parse
from gi.repository import Gtk, Gdk, GLib, Gio, Pango, GdkPixbuf, PangoCairo, WebKit2
from typing import Optional, Callable, List, Dict, Any, Tuple, Set
from copy import deepcopy
from uuid import uuid4
from clipboard_store import ClipboardItem, CategoryItem, CategoryStore, CustomCategory, capture_clipboard_once, CustomPrompt, CustomPromptsStore, LLMSettingsStore, LLMModelConfig, ConversationStore, ChatMessage, Conversation, DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS, DEFAULT_TOP_P, CONFIG_DIR
import time
import requests
import json
import base64
from utils import relative_time, is_wayland, request_window_focus
from urllib.parse import urlparse, parse_qs
from ai_text_utils import (
    _dict_to_chat_message, _extract_after_header, _escape_math,
    _unescape_math, _markdown_to_html_safe, _ensure_list_blankline,
    _ensure_table_blankline, _close_unclosed_code_blocks, _fix_latex,
    _clean_history_title, _extract_local_title, _rebuild_markdown_from_messages,
    _vision_content_to_markdown, _resolve_vision_image_src,
    _vision_content_to_text, _image_hash_path, _image_to_data_uri, _cached_image_to_data_uri,
    _model_supports_vision
)

# Regex to match placeholders: ${index[:prompt][=default]}
# - Group 1: index (\d+)
# - Group 2: optional prompt, allowing escaped colons (\:) and equals (\=)
# - Group 3: optional default value, matched if the leading '=' is not escaped (?<!\\)
TEMPLATE_REGEX = re.compile(r"\$\{(\d+)(?::((?:[^}=]|\\:|\\=)+))?(?<!\\)(?:=([^}]*))?\}")
PROMPT_PLACEHOLDER_RE = re.compile(r'\\\\|\\(\${&})|(\${&})')


from ai_html_template import get_html_template, _get_pygments_css
from dynamic_copy_dialog import show_dynamic_copy_dialog
from sort_dialog import show_sort_dialog
from recycle_bin_dialog import show_recycle_bin_dialog
from sort_cats_dialog import show_sort_cats_dialog
from prompt_dialog import show_prompt_dialog
from prompts_config_dialog import show_prompts_config_dialog
from ai_popovers import AICommandPopover, HistoryPopover
from ai_tool_loop import run_llm_react_loop
from ai_chat_panel import AIChatPanel

AI_MESSAGES_SOFT_LIMIT = 200
AI_MESSAGES_TRIM_TARGET = 100
AI_BTN_LABEL_SEND = "发送"
AI_BTN_LABEL_STOP = "暂停"
MAX_TOOL_ITERATIONS = 25  # ReAct loop safety limit

# LaTeX commands that LLMs commonly double-escape (\\frac → \frac, etc.)
_LATEX_COMMANDS = frozenset({
    "frac", "sqrt", "sum", "int", "prod", "lim", "sin", "cos", "log", "ln",
    "det", "begin", "end", "left", "right", "text", "mathrm", "mathbf",
    "mathit", "mathtt", "mathcal", "mathbb", "mathfrak", "displaystyle",
    "partial", "nabla", "infty", "alpha", "beta", "gamma", "delta", "epsilon",
    "theta", "lambda", "pi", "sigma", "omega", "varphi", "rightarrow", "leftarrow",
    "Rightarrow", "Leftarrow", "mapsto", "implies", "iff", "cdot", "times",
    "approx", "equiv", "neq", "leq", "geq", "subset", "supset", "cup", "cap",
})

CATEGORY_WIDTH = 200
ACTION_WIDTH = 140
PANEL_WIDTH = 1320
# ponytail: removed fixed AI_PANEL_WIDTH — now uses equal expand with content area


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


_DIV_CLOSE_LEN = 6  # len('</div>')





def _textview_draw_placeholder(widget, cr):
    """Draw placeholder text on a Gtk.TextView when its buffer is empty.

    Connect via: textview.connect_after("draw", _textview_draw_placeholder)
    Set placeholder via: textview.placeholder_text = "..."
    """
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



class ClipboardPanel(Gtk.Box):
    # Slash commands available in the AI chat input box (command, description)
    _AI_COMMANDS = [
        ("/new", "新对话"),
        ("/delete", "删除并新建"),
        ("/retry", "回滚到上一轮"),
        ("/rollback", "回滚到任意轮"),
        ("/title", "设置/生成标题"),
        ("/model", "切换模型"),
    ]

    def __init__(self, clip_store, cat_store):
        # ponytail: removed unused prompt_store parameter
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._clip_store = clip_store
        self._cat_store = cat_store
        self._custom_prompts_store = CustomPromptsStore()
        self._llm_settings_store = LLMSettingsStore()
        self._active_category_id = "__clipboard__"
        self._clip_items: List[ClipboardItem] = []
        self._selected_index = 0
        self._filter_query = ""
        self._active_tab_type = "all"
        self._tab_buttons = {}
        self._in_category_button = False

        self.on_copy_clipboard: Optional[Callable[[str], None]] = None
        self._on_hide_request: Optional[Callable[[], None]] = None
        self._on_dialog_shown: Optional[Callable[[], None]] = None
        self._on_dialog_hidden: Optional[Callable[[], None]] = None
        self._on_ai_copy_started: Optional[Callable[[], None]] = None
        self._on_ai_copy_finished: Optional[Callable[[], None]] = None
        self._on_menu_shown: Optional[Callable[[], None]] = None
        self._on_menu_hidden: Optional[Callable[[], None]] = None
        self._on_clipboard_to_ai_request: Optional[Callable[[], None]] = None
        self._on_combo_popup_shown: Optional[Callable[[], None]] = None
        self._on_combo_popup_hidden: Optional[Callable[[], None]] = None
        self._editing_rename_row = None
        self._editing_rename_entry = None
        self._editing_rename_old_name = None
        self._editing_rename_cat_id = None
        self._rename_activate_id = 0
        self._rename_focus_out_id = 0
        self._setup_marker_monitor()
        self._last_rendered_category_id = None
        self._last_rendered_item_ids = None
        self._loading_data = False
        self._conversation_store = ConversationStore()
        self._pygments_css_cache: Dict[str, str] = {}
        self._sidebar_collapsed = False

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
        self._cat_list.connect("row-selected", self._on_category_selected)
        self._cat_list.connect("button-press-event", self._on_category_button)

        # Wrap in ScrolledWindow to keep window height fixed and allow list scrolling
        self._cat_scrolled = Gtk.ScrolledWindow.new()
        self._cat_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._cat_scrolled.set_size_request(CATEGORY_WIDTH, -1)
        self._cat_scrolled.add(self._cat_list)

        self._cat_toolbar.pack_start(self._btn_new_cat, False, False, 0)
        self._cat_toolbar.pack_start(self._btn_delete_cat, False, False, 0)
        self._cat_toolbar.pack_start(self._btn_rename_cat, False, False, 0)

        self._cat_vbox.pack_start(self._cat_toolbar, False, False, 0)
        self._cat_vbox.pack_start(self._cat_scrolled, True, True, 0)

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
            ("all", "📋", "全部"),
            ("text", "📝", "文本"),
            ("image", "🖼️", "图片"),
            ("link", "🔗", "链接"),
            ("code", "💻", "代码")
        ]
        for t_type, t_icon, t_tip in tab_spec:
            btn = Gtk.Button.new_with_label(t_icon)
            btn.set_tooltip_text(t_tip)
            btn.get_style_context().add_class("filter-tab")
            if t_type == "all":
                btn.get_style_context().add_class("filter-tab-active")
            btn.connect("clicked", self._on_filter_tab_clicked, t_type)
            self._filter_tabs_box.pack_start(btn, True, True, 0)
            self._tab_buttons[t_type] = btn
            btn.show()

        self._filter_gear_btn = Gtk.Button.new_with_label("\u2699")
        self._filter_gear_btn.set_tooltip_text("更多操作")
        self._filter_gear_btn.get_style_context().add_class("filter-gear")
        self._filter_gear_btn.set_relief(Gtk.ReliefStyle.NONE)
        self._filter_gear_btn.set_can_focus(False)
        self._filter_gear_btn.connect("clicked", self._on_filter_gear_clicked)
        self._filter_tabs_box.pack_end(self._filter_gear_btn, False, False, 0)
        self._filter_gear_btn.show()

        self._content_vbox.pack_start(self._filter_tabs_box, False, False, 0)
        self._content_vbox.pack_start(self._content_scrolled, True, True, 0)

        self._sidebar_toggle = Gtk.ToggleButton.new_with_label("\u25c0")
        self._sidebar_toggle.set_relief(Gtk.ReliefStyle.NONE)
        self._sidebar_toggle.set_tooltip_text("折叠侧边栏")
        self._sidebar_toggle.get_style_context().add_class("sidebar-toggle")
        self._sidebar_toggle.set_size_request(20, -1)
        self._sidebar_toggle.set_can_focus(False)
        self._sidebar_toggle.connect("toggled", self._on_sidebar_toggled)

        self.pack_start(self._cat_vbox, False, True, 0)
        self.pack_start(self._sidebar_toggle, False, False, 0)
        self.pack_start(self._cat_sep, False, False, 0)

        self._content_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self._content_paned.pack1(self._content_vbox, resize=True, shrink=False)
        self.pack_start(self._content_paned, True, True, 0)

        # AI Chat Panel
        self._ai_chat_panel = AIChatPanel(
            conversation_store=self._conversation_store,
            llm_settings_store=self._llm_settings_store,
            theme="dark",
            ai_commands=self._AI_COMMANDS,
            pygments_css_cache=self._pygments_css_cache
        )
        # Wire up callback hooks
        self._ai_chat_panel.on_dialog_shown = self.on_dialog_shown
        self._ai_chat_panel.on_dialog_hidden = self.on_dialog_hidden
        self._ai_chat_panel.on_ai_copy_started = self.on_ai_copy_started
        self._ai_chat_panel.on_ai_copy_finished = self.on_ai_copy_finished
        self._ai_chat_panel.on_hide_request = self.on_hide_request
        self._ai_chat_panel.on_menu_shown = self.on_menu_shown
        self._ai_chat_panel.on_menu_hidden = self.on_menu_hidden
        self._ai_chat_panel.on_combo_popup_shown = self.on_combo_popup_shown
        self._ai_chat_panel.on_combo_popup_hidden = self.on_combo_popup_hidden
        self._ai_chat_panel.on_clipboard_to_ai_request = self.on_clipboard_to_ai_request

        self._content_paned.pack2(self._ai_chat_panel, resize=False, shrink=False)
        self._content_paned.set_position(int(PANEL_WIDTH * 0.5))

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

    def _on_sidebar_toggled(self, btn):
        self._sidebar_collapsed = btn.get_active()
        if self._sidebar_collapsed:
            self._cat_vbox.set_no_show_all(True)
            self._cat_vbox.hide()
            self._cat_sep.hide()
            btn.set_label("\u25b6")
            btn.set_tooltip_text("展开侧边栏")
        else:
            self._cat_vbox.set_no_show_all(False)
            self._cat_vbox.show()
            self._cat_sep.show()
            btn.set_label("\u25c0")
            btn.set_tooltip_text("折叠侧边栏")
        self.queue_resize()

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
            ".row { padding: 12px 18px; border-radius: 6px; margin: 4px 8px; border-left: 4px solid transparent; }"
            ".row:hover { background: %(hover_bg)s; }"
            ".row:selected { background: %(sel_bg)s; border-left: 4px solid %(sel_border)s; }"
            "#catLabel { font-size: 16px; font-weight: 500; padding: 0 8px; }"
            "#clipTitle { font-family: \"JetBrains Mono\",\"monospace\"; font-size: 16px; padding: 0; }"
            "#clipText { font-size: 14px; padding: 0; }"
            "#clipTime { font-size: 12px; padding: 0; }"
            "#promptTitle { font-family: \"JetBrains Mono\",\"monospace\"; font-size: 16px; font-weight: bold; padding: 0; }"
            "#promptText { font-size: 14px; padding: 0; }"
            "button { color: %(text_fg)s; background: %(btn_bg)s; background-image: none;"
            " border: 1px solid %(btn_border)s; border-radius: 6px; padding: 8px 16px; font-size: 14px; font-weight: 500; box-shadow: none; }"
            "button:hover { background: %(btn_hover)s; background-image: none; border-color: %(sel_border)s; }"
            "button:active { background: %(btn_active)s; background-image: none; }"

            ".cat-tool-btn { font-size: 12px; padding: 4px 6px; border: none; border-radius: 4px; }"
            ".cat-tool-btn:hover { background: %(btn_hover)s; }"
            ".cat-tool-btn:active { background: %(btn_active)s; }"
            ".filter-tab { padding: 2px 8px; border-radius: 20px; border: 1px solid %(btn_border)s; background: %(btn_bg)s; background-image: none; box-shadow: none; font-size: 15px; color: %(text_secondary)s; }"
            ".filter-tab:hover { background: %(btn_hover)s; background-image: none; color: %(text_fg)s; }"
            ".filter-tab-active { background: %(sel_bg)s; background-image: none; border-color: %(sel_border)s; color: %(text_fg)s; }"
            ".sidebar-toggle { font-size: 10px; padding: 0; min-width: 18px; min-height: 18px;"
            " border: none; background: transparent; color: %(text_secondary)s;"
            " border-radius: 2px; margin: 0; }"
            ".sidebar-toggle:hover { background: %(btn_hover)s; color: %(text_fg)s; }"
            ".filter-gear { font-size: 16px; padding: 0 4px; min-width: 24px; min-height: 22px;"
            " border: none; background: transparent; color: %(text_secondary)s;"
            " border-radius: 4px; }"
            ".filter-gear:hover { background: %(btn_hover)s; color: %(text_fg)s; }"
            ".row-more-btn { font-size: 18px; padding: 0 4px; min-width: 26px; min-height: 26px;"
            " border: none; background: transparent; color: %(text_secondary)s;"
            " border-radius: 4px; }"
            ".row-more-btn:hover { background: %(btn_hover)s; color: %(text_fg)s; }"
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
            "background-color: %(btn_bg)s; background-image: none; color: %(text_fg)s; "
            "border: 1px solid %(btn_border)s; border-radius: 6px; "
            "padding: 8px 16px; font-size: 14px; font-weight: 500; "
            "box-shadow: none; text-shadow: none; }"
            "dialog button:hover, messagedialog button:hover, .custom-dialog button:hover { background-color: %(btn_hover)s; background-image: none; border-color: %(sel_border)s; }"
            "dialog button:active, messagedialog button:active, .custom-dialog button:active { background-color: %(btn_active)s; background-image: none; }"
            "dialog button.suggested-action, messagedialog button.suggested-action, .custom-dialog button.suggested-action { "
            "background-color: %(sel_border)s; background-image: none; "
            "color: #ffffff; border: 1px solid %(sel_border)s; "
            "box-shadow: none; text-shadow: none; }"
            "dialog button.suggested-action:hover, messagedialog button.suggested-action:hover, .custom-dialog button.suggested-action:hover { "
            "background-color: %(btn_hover)s; border-color: %(sel_border)s; }"
            "dialog headerbar, messagedialog headerbar, .custom-dialog headerbar, "
            "dialog headerbar.titlebar, messagedialog headerbar.titlebar, .custom-dialog headerbar.titlebar { "
            "background-color: %(dialog_bg)s; background-image: none; box-shadow: none; border-style: none; border-color: %(dialog_bg)s; color: %(text_fg)s; }"
            "dialog headerbar *, messagedialog headerbar *, .custom-dialog headerbar * { "
            "background-color: transparent; background-image: none; box-shadow: none; color: %(text_fg)s; }"
            "dialog headerbar button, messagedialog headerbar button, .custom-dialog headerbar button { "
            "background-color: transparent; background-image: none; border: none; box-shadow: none; color: %(text_fg)s; }"
            "dialog headerbar button:hover, messagedialog headerbar button:hover, .custom-dialog headerbar button:hover { background-color: %(btn_hover)s; }"
            "entry, textview, textview text { background-color: %(input_bg)s; color: %(input_fg)s; border: 1px solid %(input_border)s; border-radius: 6px; caret-color: %(sel_border)s; }"
            "spinbutton { background-color: %(input_bg)s; color: %(input_fg)s; border: 1px solid %(input_border)s; border-radius: 6px; }"
            "spinbutton entry { background-color: %(input_bg)s; color: %(input_fg)s; border: none; border-radius: 0; }"
            "spinbutton button { background-color: %(input_bg)s; color: %(text_fg)s; border: none; border-radius: 0; min-width: 24px; min-height: 24px; }"
            "spinbutton button:hover { background-color: %(btn_hover)s; }"
            "textview { padding: 4px; }"
            "menu, menuitem { background-color: %(dialog_bg)s; background-image: none; }"
            "menu { border: 1px solid %(input_border)s; border-radius: 6px; padding: 4px 0; }"
            "menuitem { color: %(text_fg)s; padding: 6px 16px; }"
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
            ".custom-dialog frame, .custom-dialog GtkFrame { background-color: %(dialog_bg)s; border: 1px solid %(input_border)s; border-radius: 8px; padding: 4px; }"
            ".custom-dialog frame > label { color: %(text_fg)s; font-weight: bold; padding-left: 8px; }"
            ".custom-dialog frame > border { border-color: %(input_border)s; }"

            # ── AI Chat Panel / Popover / WebView Styles ──
            ".history-dropdown-btn { font-size: 13px; padding: 2px 8px; min-height: 28px; border-radius: 6px; color: %(text_fg)s; background: %(btn_bg)s; background-image: none; }"
            ".history-dropdown-btn label { font-size: 13px; }"
            ".history-dropdown-btn:hover { background: %(btn_hover)s; border-color: %(sel_border)s; }"
            ".clear-all-btn { color: %(text_secondary)s; padding: 6px 12px; margin-top: 2px; font-size: 13px; font-weight: bold; border: none; background: transparent; }"
            ".clear-all-btn:hover { background-color: rgba(239, 68, 68, 0.1); color: #ef4444; border-radius: 4px; }"
            ".edit-mode-btn { font-size: 12px; padding: 4px 8px; margin: 2px; border-radius: 4px; background: %(btn_bg)s; background-image: none; border: 1px solid %(btn_border)s; color: %(text_fg)s; }"
            ".edit-mode-btn:hover { background: %(btn_hover)s; border-color: %(sel_border)s; }"
            ".delete-sel-btn { font-size: 12px; padding: 4px 8px; margin: 2px; border-radius: 4px; background: rgba(239, 68, 68, 0.08); background-image: none; border: 1px solid rgba(239, 68, 68, 0.2); color: #ef4444; font-weight: bold; }"
            ".delete-sel-btn:hover { background: rgba(239, 68, 68, 0.15); border-color: #ef4444; }"
            ".delete-sel-btn:disabled { color: %(text_secondary)s; background: transparent; border-color: %(btn_border)s; }"
            ".ai-history-popover { background-color: %(dialog_bg)s; border: 1px solid %(input_border)s; border-radius: 8px; padding: 4px; background-image: none; box-shadow: none; }"
            ".ai-history-popover box { background-color: transparent; background-image: none; }"
            ".ai-history-popover scrolledwindow { background-color: transparent; border: none; box-shadow: none; outline: none; }"
            ".ai-history-popover viewport { background-color: transparent; border: none; }"
            ".ai-history-popover list, .ai-history-popover listbox { background-color: transparent; border: none; }"
            ".ai-history-popover row, .ai-history-popover listboxrow { padding: 0; border: none; background: transparent; border-radius: 4px; }"
            ".ai-history-popover row:hover, .ai-history-popover listboxrow:hover { background-color: %(btn_hover)s; }"
            ".ai-history-popover row:selected, .ai-history-popover listboxrow:selected { background-color: %(sel_bg)s; color: %(text_fg)s; }"
            ".ai-history-popover row label, .ai-history-popover listboxrow label { color: %(text_fg)s; font-size: 13px; }"
            ".ai-history-popover separator { background: %(input_border)s; min-height: 1px; margin: 2px 0; }"
            ".ai-history-popover checkbutton { background: transparent; color: %(text_fg)s; }"
            ".ai-history-popover check { background-color: %(input_bg)s; "
             "border: 1px solid %(input_border)s; border-radius: 3px; "
             "min-width: 16px; min-height: 16px; "
             "-gtk-icon-source: none; background-image: none; }"
            ".ai-history-popover check:checked { "
             "background-color: %(sel_border)s; border-color: %(sel_border)s; "
             "-gtk-icon-source: -gtk-icontheme('object-select-symbolic'); "
             "background-image: none; }"
            "#aiScrolled, #aiWebView { background-color: transparent; border: none; box-shadow: none; padding: 0; }"
            ".model-selector-popover { border-radius: 6px; background-color: %(dialog_bg)s; background-image: none; box-shadow: none; }"
            ".model-selector-popover > decoration { border-radius: 6px; }"
            ".model-selector-list { background-color: transparent; }"
            ".model-selector-list row { border: none; border-bottom: 1px solid %(input_border)s; background-color: transparent; background-image: none; color: %(text_fg)s; }"
            ".model-selector-list row:last-child { border-bottom: none; }"
            ".model-selector-list row:hover { background-color: %(hover_bg)s; color: %(text_fg)s; }"
            ".model-selector-list row:selected { background-color: %(sel_bg)s; color: %(text_fg)s; }"
            ".model-default-tag { color: %(sel_border)s; }"
            ".command-autocomplete-popover { border-radius: 6px; background-color: %(dialog_bg)s; background-image: none; box-shadow: none; }"
            ".command-autocomplete-popover > decoration { border-radius: 6px; }"
            ".command-autocomplete-list { background-color: transparent; }"
            ".command-autocomplete-list row { border: none; border-bottom: 1px solid %(input_border)s; padding: 2px 0; background-color: transparent; background-image: none; color: %(text_fg)s; }"
            ".command-autocomplete-list row:last-child { border-bottom: none; }"
            ".command-autocomplete-list row:hover { background-color: %(hover_bg)s; }"
            ".command-autocomplete-list row:selected { background-color: %(sel_bg)s; color: %(text_fg)s; }"
        ) % vals
        self._css_provider.load_from_data(css.encode("utf-8"))
        for w in (self, self._cat_list, self._content_scrolled, self._content_list):
            w.override_background_color(Gtk.StateFlags.NORMAL, self._bg_color)

        if hasattr(self, '_ai_chat_panel') and self._ai_chat_panel:
            self._ai_chat_panel.set_theme(name)

    def set_theme(self, name: str):
        self._last_rendered_category_id = None
        self._last_rendered_item_ids = None
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
        if getattr(self, "_loading_data", False):
            return
        self._loading_data = True
        def worker():
            try:
                capture_clipboard_once(self._clip_store)
            finally:
                GLib.idle_add(self._finish_load_and_reset)
        threading.Thread(target=worker, daemon=True).start()

    def _finish_load_and_reset(self):
        self._loading_data = False
        self._finish_load()

    def _finish_load(self):
        self._clip_store.reload()
        self._clip_items = list(self._clip_store.get_all())
        self._clip_items.reverse()
        self._rebuild()

    def _rebuild(self):
        if not hasattr(self, '_content_list'):
            return

        if self._active_category_id == "__clipboard__":
            items = self._clip_items
        else:
            cat = self._cat_store.get(self._active_category_id)
            items = cat.items if cat else []

        # Check if we can skip rebuild because category and items haven't changed
        current_item_ids = []
        for item in items:
            item_id = getattr(item, "hash", None) or getattr(item, "title", None) or getattr(item, "text", "")
            item_ts = getattr(item, "timestamp", 0) if hasattr(item, "timestamp") else 0
            current_item_ids.append((item_id, item_ts))

        if self._last_rendered_category_id == self._active_category_id and self._last_rendered_item_ids == current_item_ids:
            return  # Skip expensive rebuild

        self._last_rendered_category_id = self._active_category_id
        self._last_rendered_item_ids = current_item_ids

        for child in self._content_list.get_children():
            self._content_list.remove(child)

        if self._active_category_id == "__clipboard__":
            self._filter_tabs_box.show()
        else:
            self._filter_tabs_box.hide()

        # Build regular item rows
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

    def _on_filter_gear_clicked(self, btn):
        menu = Gtk.Menu.new()
        is_clipboard = self._active_category_id == "__clipboard__"

        if is_clipboard:
            del_all = Gtk.MenuItem.new_with_label("Delete All")
            del_all.connect("activate", lambda *_: self._on_delete_all_clicked(None))
            menu.append(del_all)

        prompts = Gtk.MenuItem.new_with_label("Prompts Config")
        prompts.connect("activate", lambda *_: self._on_prompts_config_clicked(None))
        menu.append(prompts)

        sort_cats = Gtk.MenuItem.new_with_label("Sort Categories")
        sort_cats.connect("activate", lambda *_: self._on_sort_cats_clicked(None))
        menu.append(sort_cats)

        if not is_clipboard:
            cat = self._cat_store.get(self._active_category_id) if self._active_category_id else None
            if cat and len(cat.items) > 1:
                sort_items = Gtk.MenuItem.new_with_label("Sort Items")
                sort_items.connect("activate", lambda *_: self._show_sort_dialog())
                menu.append(sort_items)

        menu.append(Gtk.SeparatorMenuItem.new())

        backup = Gtk.MenuItem.new_with_label("Backup")
        backup.connect("activate", lambda *_: self._on_backup_clicked(None))
        menu.append(backup)

        restore = Gtk.MenuItem.new_with_label("Restore")
        restore.connect("activate", lambda *_: self._on_restore_clicked(None))
        menu.append(restore)

        recycle = Gtk.MenuItem.new_with_label("Recycle Bin")
        recycle.connect("activate", lambda *_: self._on_recycle_bin_clicked(None))
        menu.append(recycle)

        if self.on_menu_shown:
            self.on_menu_shown()
        menu.connect("deactivate", lambda *_: GLib.timeout_add(300, self._on_menu_deactivated))
        menu.show_all()
        menu.popup_at_widget(btn, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)

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

        more_btn = Gtk.Button.new_with_label("\u22ef")
        more_btn.set_relief(Gtk.ReliefStyle.NONE)
        more_btn.set_valign(Gtk.Align.START)
        more_btn.set_margin_top(2)
        more_btn.get_style_context().add_class("row-more-btn")
        more_btn.connect("clicked", self._on_row_more_clicked, row, item)
        hbox.pack_start(more_btn, False, False, 0)

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

        more_btn = Gtk.Button.new_with_label("\u22ef")
        more_btn.set_relief(Gtk.ReliefStyle.NONE)
        more_btn.set_valign(Gtk.Align.START)
        more_btn.set_margin_top(2)
        more_btn.get_style_context().add_class("row-more-btn")
        more_btn.connect("clicked", self._on_row_more_clicked, row, item)
        hbox.pack_start(more_btn, False, False, 0)

        row.add(hbox)

    def _on_sort_clicked(self, _btn):
        self._show_sort_dialog()

    def _show_sort_dialog(self):
        show_sort_dialog(
            cat_store=self._cat_store,
            category_id=self._active_category_id,
            parent_window=self.get_toplevel(),
            on_dialog_shown=self.on_dialog_shown,
            on_dialog_hidden=self.on_dialog_hidden,
            rebuild_cb=self._rebuild
        )

    def _on_category_selected(self, _listbox, row):
        if row is None or not hasattr(row, 'cat_id'):
            return

        # Before switching, if we were on clipboard, save visibility state and hide AI panel
        if self._active_category_id == "__clipboard__":
            if hasattr(self, '_ai_chat_panel'):
                self._ai_panel_visible_saved = self._ai_chat_panel.is_visible()
                if self._ai_panel_visible_saved:
                    self._ai_chat_panel.hide_panel()

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

        # After switching, if we are back on clipboard, restore AI panel if it was visible before
        if self._active_category_id == "__clipboard__":
            if getattr(self, "_ai_panel_visible_saved", False) and hasattr(self, '_ai_chat_panel'):
                self._ai_chat_panel.show_panel()

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

    def _on_row_more_clicked(self, btn, row, item):
        self._content_list.select_row(row)

        menu = Gtk.Menu.new()
        if self._active_category_id == "__clipboard__":
            copy_item = Gtk.MenuItem.new_with_label("Copy")
            copy_item.connect("activate", lambda *_: self._activate_item(item))
            menu.append(copy_item)
            del_item = Gtk.MenuItem.new_with_label("Delete")
            del_item.connect("activate", lambda *_: self._delete_item(item))
            menu.append(del_item)

            item_type = getattr(item, "type", "text")
            if item_type == "image":
                send_ai_item = Gtk.MenuItem.new_with_label("\U0001f5bc\ufe0f \u53d1\u9001\u5230 AI \u770b\u76d8")
                send_ai_item.connect("activate", lambda *_: self._send_image_to_ai(item))
                menu.append(send_ai_item)

            custom_prompts = self._custom_prompts_store.get_all()
            if custom_prompts:
                applicable_prompts = []
                for p in custom_prompts:
                    p_categories = getattr(p, "categories", None) or ["text"]
                    if item_type in p_categories:
                        applicable_prompts.append(p)
                if applicable_prompts:
                    menu.append(Gtk.SeparatorMenuItem.new())
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
        menu.popup_at_widget(btn, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)

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

            item_type = getattr(item, "type", "text")
            if item_type == "image":
                send_ai_item = Gtk.MenuItem.new_with_label("🖼️ 发送到 AI 看盘")
                send_ai_item.connect("activate", lambda *_: self._send_image_to_ai(item))
                menu.append(send_ai_item)

            custom_prompts = self._custom_prompts_store.get_all()
            if custom_prompts:
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
                suffix = "\n\n" + unescaped_prompt
            else:
                suffix = ""
            final_query = original_content + suffix

        # Check action type
        act_type = getattr(prompt_obj, "action_type", "web")
        if act_type == "api":
            self._ai_chat_panel.ask_llm_api(final_query, prompt_obj)
            return

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

    def _send_image_to_ai(self, item: ClipboardItem):
        image_path = getattr(item, "image_path", None)
        if not image_path or not os.path.isfile(image_path):
            return

        def do_background_send():
            try:
                data_uri = _image_to_data_uri(image_path)
                if not data_uri:
                    return
                h = getattr(item, "hash", "") or hashlib.sha256(
                    open(image_path, "rb").read()
                ).hexdigest()[:16]
                def show_with_pending(h, image_path, data_uri):
                    self._ai_chat_panel.set_pending_image(h, image_path, data_uri)
                    self._ai_chat_panel.show_panel()
                    self._ai_chat_panel.grab_entry_focus()
                GLib.idle_add(show_with_pending, h, image_path, data_uri)
            except Exception:
                pass

        threading.Thread(target=do_background_send, daemon=True).start()

    def _set_pending_image_and_show_panel(self, h, image_path, data_uri):
        self._ai_chat_panel.set_pending_image(h, image_path, data_uri)
        self._ai_chat_panel.show_panel()
        self._ai_chat_panel.grab_entry_focus()

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

    def is_history_popup_shown(self) -> bool:
        return self._ai_chat_panel.is_popup_shown()

    def hide_ai_panel(self):
        self._ai_chat_panel.hide_panel()

    def is_ai_panel_visible(self) -> bool:
        return self._ai_chat_panel.is_visible()

    def start_new_conversation(self):
        self._ai_chat_panel.start_new_conversation()

    def open_ai_and_load_recent(self):
        self._ai_chat_panel.open_ai_and_load_recent()

    def navigate_conversation(self, direction: int):
        self._ai_chat_panel.navigate_conversation(direction)

    def _on_prompts_config_clicked(self, _btn):
        show_prompts_config_dialog(
            parent_window=self.get_toplevel(),
            custom_prompts_store=self._custom_prompts_store,
            llm_settings_store=self._llm_settings_store,
            on_dialog_shown=self.on_dialog_shown,
            on_dialog_hidden=self.on_dialog_hidden
        )

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
                if self.on_copy_clipboard:
                    self.on_copy_clipboard("[Image]", item.hash)
                _copy_image_to_clipboard(item.image_path)
                if self.on_hide_request:
                    self.on_hide_request()
                return
            text = item.text
        elif isinstance(item, CategoryItem):
            text = item.text
            text = self._process_template_text(text)
        else:
            return
        if self.on_copy_clipboard:
            self.on_copy_clipboard(text, item.hash if isinstance(item, ClipboardItem) else None)
        _copy_to_clipboard(text)
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
        show_prompt_dialog(
            parent_window=self.get_toplevel(),
            create=True,
            existing=None,
            active_category_id=self._active_category_id,
            cat_store=self._cat_store,
            on_dialog_shown=self.on_dialog_shown,
            on_dialog_hidden=self.on_dialog_hidden,
            load_cached_callback=self.load_cached
        )

    def _on_edit_clicked(self, _btn):
        row = self._content_list.get_selected_row()
        if not row or not hasattr(row, "store_item"):
            return
        item = row.store_item
        if isinstance(item, CategoryItem):
            show_prompt_dialog(
                parent_window=self.get_toplevel(),
                create=False,
                existing=item,
                active_category_id=self._active_category_id,
                cat_store=self._cat_store,
                on_dialog_shown=self.on_dialog_shown,
                on_dialog_hidden=self.on_dialog_hidden,
                load_cached_callback=self.load_cached
            )

    def _edit_prompt(self, item: CategoryItem):
        show_prompt_dialog(
            parent_window=self.get_toplevel(),
            create=False,
            existing=item,
            active_category_id=self._active_category_id,
            cat_store=self._cat_store,
            on_dialog_shown=self.on_dialog_shown,
            on_dialog_hidden=self.on_dialog_hidden,
            load_cached_callback=self.load_cached
        )

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
                GLib.idle_add(self._finish_load)

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

            # Run backup in background thread to avoid blocking the GTK main loop
            def worker():
                err = "Unknown error"
                path_or_msg = ""
                try:
                    err, path_or_msg = _backup_config(selected_path)
                finally:
                    GLib.idle_add(self._on_backup_finished, err, path_or_msg)

            threading.Thread(target=worker, daemon=True).start()

        dialog.connect("response", on_response)
        dialog.show_all()

    def _on_backup_finished(self, err, path_or_msg):
        """Called on the main thread after background backup completes."""
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

            # Run restore in background thread to avoid blocking the GTK main loop
            def worker():
                err = "Unknown error"
                try:
                    err = _restore_config(archive_path)
                finally:
                    GLib.idle_add(self._on_restore_finished, err)

            threading.Thread(target=worker, daemon=True).start()

        dialog.connect("response", on_resp)
        dialog.show_all()

    def _on_restore_finished(self, err):
        """Called on the main thread after background restore completes."""

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

    def _on_recycle_bin_clicked(self, _btn):
        self._show_recycle_bin_dialog()

    def _show_recycle_bin_dialog(self):
        show_recycle_bin_dialog(
            cat_store=self._cat_store,
            parent_window=self.get_toplevel(),
            snippet_color=self._snippet_color,
            on_dialog_shown=self.on_dialog_shown,
            on_dialog_hidden=self.on_dialog_hidden,
            rebuild_category_list_cb=self._rebuild_category_list,
            rebuild_cb=self._rebuild
        )

    def _on_sort_cats_clicked(self, _btn):
        self._show_sort_cats_dialog()

    def _show_sort_cats_dialog(self):
        show_sort_cats_dialog(
            cat_store=self._cat_store,
            parent_window=self.get_toplevel(),
            on_dialog_shown=self.on_dialog_shown,
            on_dialog_hidden=self.on_dialog_hidden,
            rebuild_category_list_cb=self._rebuild_category_list,
            rebuild_cb=self._rebuild
        )

    def _show_dynamic_copy_dialog(self, item):
        show_dynamic_copy_dialog(
            item=item,
            parent_window=self.get_toplevel(),
            copy_to_clipboard_func=_copy_to_clipboard,
            on_copy_clipboard=self.on_copy_clipboard,
            on_hide_request=self.on_hide_request,
            on_dialog_shown=self.on_dialog_shown,
            on_dialog_hidden=self.on_dialog_hidden
        )

    @property
    def on_dialog_shown(self):
        return self._on_dialog_shown

    @on_dialog_shown.setter
    def on_dialog_shown(self, value):
        self._on_dialog_shown = value
        if hasattr(self, "_ai_chat_panel") and self._ai_chat_panel:
            self._ai_chat_panel.on_dialog_shown = value

    @property
    def on_dialog_hidden(self):
        return self._on_dialog_hidden

    @on_dialog_hidden.setter
    def on_dialog_hidden(self, value):
        self._on_dialog_hidden = value
        if hasattr(self, "_ai_chat_panel") and self._ai_chat_panel:
            self._ai_chat_panel.on_dialog_hidden = value

    @property
    def on_ai_copy_started(self):
        return self._on_ai_copy_started

    @on_ai_copy_started.setter
    def on_ai_copy_started(self, value):
        self._on_ai_copy_started = value
        if hasattr(self, "_ai_chat_panel") and self._ai_chat_panel:
            self._ai_chat_panel.on_ai_copy_started = value

    @property
    def on_ai_copy_finished(self):
        return self._on_ai_copy_finished

    @on_ai_copy_finished.setter
    def on_ai_copy_finished(self, value):
        self._on_ai_copy_finished = value
        if hasattr(self, "_ai_chat_panel") and self._ai_chat_panel:
            self._ai_chat_panel.on_ai_copy_finished = value

    @property
    def on_menu_shown(self):
        return self._on_menu_shown

    @on_menu_shown.setter
    def on_menu_shown(self, value):
        self._on_menu_shown = value
        if hasattr(self, "_ai_chat_panel") and self._ai_chat_panel:
            self._ai_chat_panel.on_menu_shown = value

    @property
    def on_menu_hidden(self):
        return self._on_menu_hidden

    @on_menu_hidden.setter
    def on_menu_hidden(self, value):
        self._on_menu_hidden = value
        if hasattr(self, "_ai_chat_panel") and self._ai_chat_panel:
            self._ai_chat_panel.on_menu_hidden = value

    @property
    def on_combo_popup_shown(self):
        return self._on_combo_popup_shown

    @on_combo_popup_shown.setter
    def on_combo_popup_shown(self, value):
        self._on_combo_popup_shown = value
        if hasattr(self, "_ai_chat_panel") and self._ai_chat_panel:
            self._ai_chat_panel.on_combo_popup_shown = value

    @property
    def on_combo_popup_hidden(self):
        return self._on_combo_popup_hidden

    @on_combo_popup_hidden.setter
    def on_combo_popup_hidden(self, value):
        self._on_combo_popup_hidden = value
        if hasattr(self, "_ai_chat_panel") and self._ai_chat_panel:
            self._ai_chat_panel.on_combo_popup_hidden = value

    @property
    def on_clipboard_to_ai_request(self):
        return self._on_clipboard_to_ai_request

    @on_clipboard_to_ai_request.setter
    def on_clipboard_to_ai_request(self, value):
        self._on_clipboard_to_ai_request = value
        if hasattr(self, "_ai_chat_panel") and self._ai_chat_panel:
            self._ai_chat_panel.on_clipboard_to_ai_request = value

    @property
    def on_hide_request(self):
        return self._on_hide_request

    @on_hide_request.setter
    def on_hide_request(self, value):
        self._on_hide_request = value
        if hasattr(self, "_ai_chat_panel") and self._ai_chat_panel:
            self._ai_chat_panel.on_hide_request = value





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

    Automatically creates a pre-restore snapshot of the current config directory
    so that if the restore fails, the previous state is rolled back.
    The snapshot is deleted on success.

    Returns None on success, or an error message string on failure.
    On rollback, the error message includes a note about automatic recovery.
    """
    import tarfile
    import shutil
    import tempfile
    config_dir = os.path.expanduser("~/.config/opencode-switcher")
    config_parent = os.path.dirname(config_dir)
    temp_backup = None
    try:
        # 1. Pre-restore snapshot: copy current config to temp dir
        temp_backup = tempfile.mkdtemp(prefix="opencode-switcher-pre-restore-")
        snapshot_path = os.path.join(temp_backup, "opencode-switcher")
        shutil.copytree(config_dir, snapshot_path)

        # 2. Perform restore
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(config_parent)

        # 3. Verify the extracted directory exists
        if not os.path.isdir(config_dir):
            raise Exception("Archive did not produce an 'opencode-switcher' directory")

        # 4. Success — clean up temp snapshot
        shutil.rmtree(temp_backup, ignore_errors=True)
        return None

    except Exception as e:
        # 5. Failure — rollback to pre-restore snapshot
        if temp_backup is not None and os.path.isdir(temp_backup):
            try:
                if os.path.isdir(config_dir):
                    shutil.rmtree(config_dir, ignore_errors=True)
                if os.path.isdir(snapshot_path):
                    shutil.copytree(snapshot_path, config_dir)
                shutil.rmtree(temp_backup, ignore_errors=True)
                return f"{e}. Your previous configuration has been restored from an automatic backup."
            except Exception as rollback_err:
                return (f"{e}. Additionally, automatic rollback failed: {rollback_err}. "
                        f"A manual backup may exist at: {temp_backup}")
        return str(e)
