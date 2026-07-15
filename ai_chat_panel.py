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
from clipboard_store import ClipboardItem, CategoryItem, CategoryStore, CustomCategory, capture_clipboard_once, CustomPrompt, CustomPromptsStore, LLMSettingsStore, LLMModelConfig, ConversationStore, ChatMessage, Conversation, AISettingsStore, DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS, DEFAULT_TOP_P, CONFIG_DIR
import time
import requests
import json
import base64
from utils import relative_time, request_window_focus
from urllib.parse import urlparse, parse_qs
from ai_text_utils import (
    _dict_to_chat_message, _extract_after_header, _escape_math,
    _unescape_math, _markdown_to_html_safe, _ensure_list_blankline,
    _ensure_table_blankline, _close_unclosed_code_blocks, _fix_latex,
    _clean_history_title, _extract_local_title, _rebuild_markdown_from_messages,
    _vision_content_to_markdown, _resolve_vision_image_src,
    _vision_content_to_text, _image_hash_path, _image_to_data_uri, _cached_image_to_data_uri,
    _model_supports_vision, USER_AVATAR_HTML, ASSISTANT_AVATAR_HTML,
    _strip_ai_markup,
    _preserve_newlines,
)
from render_pipeline import render_turn, TurnRenderInput, build_update_js

# Regex to match placeholders: ${index[:prompt][=default]}
# - Group 1: index (\d+)
# - Group 2: optional prompt, allowing escaped colons (\:) and equals (\=)
# - Group 3: optional default value, matched if the leading '=' is not escaped (?<\\!)
TEMPLATE_REGEX = re.compile(r"\$\{(\d+)(?::((?:[^}=]|\\:|\\=)+))?(?<!\\)(?:=([^}]*))?\}")
PROMPT_PLACEHOLDER_RE = re.compile(r'\\\\|\\(\${&})|(\${&})')

# WebView memory pressure settings — applied at WebContext construction time
_MPS_MEMORY_LIMIT = 300
_MPS_POLL_INTERVAL = 5
_MPS_CONSERVATIVE = 0.2
_MPS_STRICT = 0.4


from ai_html_template import get_html_template, _get_pygments_css
from dynamic_copy_dialog import show_dynamic_copy_dialog
from sort_dialog import show_sort_dialog
from recycle_bin_dialog import show_recycle_bin_dialog
from sort_cats_dialog import show_sort_cats_dialog
from llm_client import _LLMHttpClient, _LLMHttpError
from prompt_dialog import show_prompt_dialog
from prompts_config_dialog import show_prompts_config_dialog
from ai_popovers import AICommandPopover, HistoryPopover
from ai_tool_loop import run_llm_react_loop

AI_BTN_LABEL_SEND = "发送"
AI_BTN_LABEL_STOP = "暂停"


def _to_chat_messages(msgs: List[Dict]) -> List[ChatMessage]:
    from clipboard_store import ChatMessage
    return [ChatMessage(role=m["role"], content=m["content"], 
                        tool_call_id=m.get("tool_call_id"),
                        name=m.get("name"),
                        tool_calls=m.get("tool_calls"),
                        reasoning_content=m.get("reasoning_content")) for m in msgs]


class AIChatPanel(Gtk.Box):
    # Slash commands available in the AI chat input box (command, description)
    _AI_COMMANDS = [
        ("/new", "新对话"),
        ("/delete", "删除并新建"),
        ("/retry", "回滚到上一轮"),
        ("/rollback", "回滚到任意轮"),
        ("/title", "设置/生成标题"),
        ("/model", "切换模型"),
        ("/cd", "切换 bash 工作路径"),
    ]
    _SUSPEND_DELAY_SECONDS = 5
    # ── Streaming v2: Token batching ──
    _BATCH_FLUSH_MS = 60                    # 批处理窗口（ms）
    _STREAM_MODE_OFF = "off"                # 特性开关：关闭 v2
    _STREAM_MODE_TEXT = "text"              # 特性开关：仅纯文本流式
    _STREAM_MODE_FULL = "full"             # 特性开关：含工具调用兼容
    _ENABLE_STREAMING_V2 = "text_only"      # 当前模式：off / text_only / full
    _MPS = None
    _STREAM_PERF_LOG = False

    def __init__(self, conversation_store, llm_settings_store, ai_settings_store=None, theme="dark", ai_commands=None, pygments_css_cache=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._conversation_store = conversation_store
        self._llm_settings_store = llm_settings_store
        self._ai_settings_store = ai_settings_store
        self._theme = theme
        self._ai_commands = ai_commands
        self._pygments_css_cache = pygments_css_cache or {}

        # AI streaming & conversation state
        self._ai_streaming = False
        self._ai_pending_image_hash = None
        self._ai_pending_image_path = None
        self._ai_pending_image_data_uri = None
        self._ai_cancel_event = threading.Event()
        self._ai_messages = []
        self._ai_conversation_id = uuid4().hex[:12]
        self._ai_history_popover = None
        self._ai_history_btn = None
        self._ai_history_btn_label = None
        self._ai_history_listbox = None
        self._ai_history_switching = False
        self._ai_history_edit_mode = False
        self._ai_history_selected_ids = set()
        self._ai_history_edit_btn = None
        self._ai_history_delete_sel_btn = None
        self._ai_history_select_all_btn = None
        self._ai_history_done_btn = None
        self._ai_request_id = 0
        self._ai_current_assistant_text = ""
        self._ai_response_div_added = False
        self._ai_assistant_html_base = ""
        self._ai_markdown_text = ""
        self._ai_assistant_buffer = ""
        self._ai_last_prompt_obj = None
        self._ai_active_model_info = None
        self._ai_html_cache = {}
        self._ai_running_convs = {}
        self._last_rendered_html = ""
        self._ai_conversation_created_at = 0
        self._ai_title_generated = False
        self._ai_history_queries = []
        self._ai_history_index = -1
        self._ai_current_draft = ""
        self._llm_client = _LLMHttpClient()
        self._ai_panel_visible_saved = False
        self._ai_cmd_popover = None
        self._ai_cmd_listbox = None
        self._ai_cmd_popover_visible = False
        self._ai_cmd_suppress_rebuild = False
        self._ai_tool_iteration = 0
        self._ai_render_timeout_id = 0
        self._ai_ask_user_state = None
        self._ai_selected_subagents: Set[str] = set()
        self._ai_subagent_blocks: Dict[str, tuple] = {}
        self._ai_current_reasoning_text = ""
        self._ai_summary: str = ""
        self._ai_summary_generating: bool = False
        self._ai_pending_title_notification = False
        self._webview_suspended = False
        self._suspend_timeout_id = 0

        # ── Streaming v2: Token batching state ──
        self._token_buffer = ""
        self._flush_scheduled = False
        self._reasoning_buffer = ""
        self._reasoning_flush_scheduled = False
        self._last_flushed_len = 0
        self._streaming_mode = self._STREAM_MODE_OFF
        self._streaming_container_created = False

        # Callback hooks
        self.on_dialog_shown = None
        self.on_dialog_hidden = None
        self.on_ai_copy_started = None
        self.on_ai_copy_finished = None
        self.on_hide_request = None
        self.on_menu_shown = None
        self.on_menu_hidden = None
        self.on_combo_popup_shown = None
        self.on_combo_popup_hidden = None
        self.on_clipboard_to_ai_request = None

        # Separator (packed by parent ClipboardPanel)
        self.separator = Gtk.Separator.new(Gtk.Orientation.VERTICAL)
        self.separator.set_no_show_all(True)

        # Margins & visibility (matches old _ai_vbox)
        self.set_margin_start(8)
        self.set_margin_end(8)
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_no_show_all(True)

        self._build_ui()

    def _build_ui(self):
        # Local import to avoid circular dependency (clipboard_panel imports AIChatPanel)
        from clipboard_panel import _textview_draw_placeholder, _copy_to_clipboard

        # Title / Header
        ai_hdr = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        self._ai_lbl = Gtk.Label.new()
        self._ai_lbl.set_markup("<b>AI 助手看盘</b>")
        self._ai_lbl.set_xalign(0)
        ai_hdr.pack_start(self._ai_lbl, True, True, 0)

        self._ai_spinner = Gtk.Spinner.new()
        self._ai_spinner.set_no_show_all(True)
        ai_hdr.pack_start(self._ai_spinner, False, False, 0)

        # Conversation history dropdown button (inserted before copy button)
        self._ai_history_btn = Gtk.Button.new()
        self._ai_history_btn.set_size_request(160, -1)
        self._ai_history_btn.set_no_show_all(True)
        self._ai_history_btn.set_tooltip_text("切换对话历史")
        self._ai_history_btn.set_sensitive(False)
        self._ai_history_btn.get_style_context().add_class("history-dropdown-btn")

        btn_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
        self._ai_history_btn_label = Gtk.Label.new("历史对话")
        self._ai_history_btn_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._ai_history_btn_label.set_max_width_chars(15)
        self._ai_history_btn_label.set_xalign(0)
        arrow = Gtk.Label.new("▾")

        btn_box.pack_start(self._ai_history_btn_label, True, True, 0)
        btn_box.pack_start(arrow, False, False, 0)
        self._ai_history_btn.add(btn_box)

        ai_hdr.pack_start(self._ai_history_btn, False, False, 0)

        # Create Popover for history selection
        self._ai_history_popover = HistoryPopover(
            relative_to_widget=self._ai_history_btn,
            history_btn=self._ai_history_btn,
            history_btn_label=self._ai_history_btn_label,
            conversation_store=self._conversation_store,
            get_current_conv_id_fn=lambda: self._ai_conversation_id,
            get_sorted_conversations_fn=self._get_sorted_conversations,
            on_conversation_selected=self._switch_to_conversation,
            on_clear_all_deleted_reset_fn=self._reset_ai_panel_silent,
            on_dialog_shown=lambda: self.on_dialog_shown() if self.on_dialog_shown else None,
            on_dialog_hidden=lambda: self.on_dialog_hidden() if self.on_dialog_hidden else None,
            on_popover_shown=lambda: self.on_combo_popup_shown() if self.on_combo_popup_shown else None,
            on_popover_closed=lambda: self.on_combo_popup_hidden() if self.on_combo_popup_hidden else None
        )


        # Close button
        ai_close = Gtk.Button.new_with_label("\u274c")
        ai_close.set_tooltip_text("关闭AI面板")
        ai_close.get_style_context().add_class("flat")

        def on_ai_close_clicked(_btn):
            self.set_no_show_all(True)
            self.hide()
            self.separator.set_no_show_all(True)
            self.separator.hide()
            self._ai_panel_visible_saved = False
            self.queue_resize()

        ai_close.connect("clicked", on_ai_close_clicked)
        ai_hdr.pack_start(ai_close, False, False, 0)

        self.pack_start(ai_hdr, False, False, 0)

        # Separator
        ai_sep_line = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        self.pack_start(ai_sep_line, False, False, 0)

        # Scrolled Text view
        ai_scrolled = Gtk.ScrolledWindow.new()
        ai_scrolled.set_name("aiScrolled")
        ai_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        ai_scrolled.set_vexpand(True)

        if AIChatPanel._MPS is None:
            _mps = WebKit2.MemoryPressureSettings.new()
            _mps.set_memory_limit(_MPS_MEMORY_LIMIT)
            _mps.set_poll_interval(_MPS_POLL_INTERVAL)
            _mps.set_conservative_threshold(_MPS_CONSERVATIVE)
            _mps.set_strict_threshold(_MPS_STRICT)
            AIChatPanel._MPS = _mps
            self._ai_web_context = WebKit2.WebContext(memory_pressure_settings=AIChatPanel._MPS)
            self._ai_web_context.set_cache_model(WebKit2.CacheModel.DOCUMENT_VIEWER)
            self._ai_webview = WebKit2.WebView.new_with_context(self._ai_web_context)
        self._ai_webview.set_name("aiWebView")

        # Minimize WebKit resource footprint
        settings = self._ai_webview.get_settings()

        # Media & audio
        settings.set_enable_media(False)
        settings.set_enable_media_stream(False)
        settings.set_enable_webrtc(False)
        settings.set_enable_webaudio(False)
        settings.set_enable_encrypted_media(False)

        # Graphics
        settings.set_enable_webgl(False)
        settings.set_enable_accelerated_2d_canvas(False)
        settings.set_hardware_acceleration_policy(
            WebKit2.HardwareAccelerationPolicy.NEVER
        )

        # Storage & cache
        settings.set_enable_html5_database(False)
        settings.set_enable_html5_local_storage(False)
        settings.set_enable_offline_web_application_cache(False)
        settings.set_enable_page_cache(False)

        # Navigation & features
        settings.set_enable_fullscreen(False)
        settings.set_enable_plugins(False)
        settings.set_enable_hyperlink_auditing(False)
        settings.set_enable_back_forward_navigation_gestures(False)
        settings.set_enable_dns_prefetching(False)
        settings.set_enable_caret_browsing(False)
        settings.set_enable_smooth_scrolling(False)

        # Allow file:// page to load file:// subresources (KaTeX CSS/JS/fonts)
        settings.set_allow_file_access_from_file_urls(True)

        self._ai_webview.load_html(self.get_html_template("dark"), "file:///")

        self._ai_webview.connect("decide-policy", self._on_decide_policy)
        self._ai_webview.connect("context-menu", lambda *_: True)
        self._ai_webview.connect("web-process-terminated", self._on_webview_crashed)
        ai_scrolled.add(self._ai_webview)

        # Synchronize background colors to prevent Wayland resize flickering/leaks
        if self._theme == "dark":
            bg_rgba = Gdk.RGBA(0.039, 0.043, 0.063, 1.0)  # #0a0b10
        else:
            bg_rgba = Gdk.RGBA(1.0, 1.0, 1.0, 1.0)  # #ffffff

        self.override_background_color(Gtk.StateFlags.NORMAL, bg_rgba)
        ai_scrolled.override_background_color(Gtk.StateFlags.NORMAL, bg_rgba)
        self._ai_webview.set_background_color(bg_rgba)

        self.pack_start(ai_scrolled, True, True, 0)

        # Multi-turn conversation input area (hidden until first response)
        self._ai_input_area = Gtk.Box.new(Gtk.Orientation.VERTICAL, 2)
        self._ai_input_area.set_no_show_all(True)
        self._ai_input_area.set_margin_top(4)

        # Sub-agent status bar (shown when background sub-agents exist)
        self._ai_subagent_bar = Gtk.FlowBox.new()
        self._ai_subagent_bar.set_max_children_per_line(100)
        self._ai_subagent_bar.set_min_children_per_line(1)
        self._ai_subagent_bar.set_selection_mode(Gtk.SelectionMode.NONE)
        self._ai_subagent_bar.set_column_spacing(6)
        self._ai_subagent_bar.set_row_spacing(0)
        self._ai_subagent_bar.set_margin_bottom(2)
        self._ai_subagent_bar.set_margin_start(4)
        self._ai_subagent_bar.set_margin_end(4)
        self._ai_subagent_bar.hide()
        self._ai_subagent_bar.get_style_context().add_class("subagent-status-bar")
        self._ai_subagent_bar.connect("child-activated", self._on_subagent_child_activated)
        self._ai_input_area.pack_start(self._ai_subagent_bar, False, False, 0)

        self._ai_entry = Gtk.TextView.new()
        self._ai_entry.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._ai_entry.set_hexpand(True)
        self._ai_entry.set_left_margin(6)
        self._ai_entry.set_right_margin(6)
        self._ai_entry.set_top_margin(4)
        self._ai_entry.set_bottom_margin(4)
        self._ai_entry.set_accepts_tab(False)
        self._ai_entry.get_buffer().connect("changed", lambda *_: self._adjust_ai_entry_height())
        self._ai_entry.get_buffer().connect("changed", lambda *_: self._on_ai_entry_changed())
        self._ai_entry.placeholder_text = "输入后续问题..."
        self._ai_entry.connect_after("draw", _textview_draw_placeholder)
        self._ai_entry.connect("key-press-event", self._on_ai_entry_key_press)
        self._ai_entry.connect("button-press-event", self._on_ai_entry_button_press)
        self._ai_entry.connect("paste-clipboard", self._on_ai_entry_paste_clipboard)

        # Drag and Drop support for files
        self._ai_entry.drag_dest_set(
            Gtk.DestDefaults.ALL,
            [],
            Gdk.DragAction.COPY
        )
        self._ai_entry.drag_dest_add_uri_targets()
        self._ai_entry.connect("drag-data-received", self._on_ai_entry_drag_data_received)

        self._ai_entry_sw = Gtk.ScrolledWindow.new()
        self._ai_entry_sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._ai_entry_sw.add(self._ai_entry)

        self._ai_send_btn = Gtk.Button.new_with_label("发送")
        self._ai_send_btn.connect("clicked", self._on_send_clicked)

        self._ai_new_btn = Gtk.Button.new_with_label("+")
        self._ai_new_btn.set_tooltip_text("新对话 (Ctrl+Shift+N)")
        self._ai_new_btn.set_size_request(32, -1)
        self._ai_new_btn.get_style_context().add_class("flat")
        self._ai_new_btn.connect("clicked", lambda *_: self.start_new_conversation())

        self._ai_attach_btn = Gtk.Button.new_with_label("\U0001f4ce")
        self._ai_attach_btn.set_tooltip_text("添加图片附件")
        self._ai_attach_btn.set_size_request(32, -1)
        self._ai_attach_btn.get_style_context().add_class("flat")
        self._ai_attach_btn.connect("clicked", self._on_ai_attach_btn_clicked)

        self._ai_input_row = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
        self._ai_input_row.pack_start(self._ai_new_btn, False, False, 0)
        self._ai_input_row.pack_start(self._ai_entry_sw, True, True, 0)
        self._ai_input_row.pack_start(self._ai_attach_btn, False, False, 0)
        self._ai_input_row.pack_start(self._ai_send_btn, False, False, 0)
        self._ai_input_area.pack_start(self._ai_input_row, False, False, 0)

        # Attachment bar for pending image
        self._ai_attachment_bar = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        self._ai_attachment_bar.set_no_show_all(True)
        self._ai_attachment_bar.set_margin_bottom(4)
        self._ai_attachment_bar.set_margin_start(4)
        self._ai_attach_thumb = Gtk.Image.new()
        self._ai_attach_label = Gtk.Label.new("")
        self._ai_attach_label.set_opacity(0.7)
        self._ai_attach_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._ai_attach_remove_btn = Gtk.Button.new_with_label("\u00d7")
        self._ai_attach_remove_btn.set_relief(Gtk.ReliefStyle.NONE)
        self._ai_attach_remove_btn.set_size_request(24, 24)
        self._ai_attach_remove_btn.connect("clicked", lambda *_: self._remove_pending_image())
        self._ai_attachment_bar.pack_start(self._ai_attach_thumb, False, False, 0)
        self._ai_attachment_bar.pack_start(self._ai_attach_label, True, True, 0)
        self._ai_attachment_bar.pack_start(self._ai_attach_remove_btn, False, False, 0)
        self._ai_input_area.pack_start(self._ai_attachment_bar, False, False, 0)

        self._ai_model_popover = Gtk.Popover.new(self._ai_entry)
        self._ai_model_popover.set_position(Gtk.PositionType.TOP)
        self._ai_model_popover.get_style_context().add_class("model-selector-popover")

        model_sw = Gtk.ScrolledWindow.new()
        model_sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        model_sw.set_min_content_height(200)
        model_sw.set_max_content_height(440)
        model_sw.set_size_request(400, 200)

        self._ai_model_listbox = Gtk.ListBox.new()
        self._ai_model_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._ai_model_listbox.set_activate_on_single_click(True)
        self._ai_model_listbox.get_style_context().add_class("model-selector-list")
        self._ai_model_listbox.connect("row-activated", self._on_model_selector_activated)

        model_sw.add(self._ai_model_listbox)
        self._ai_model_popover.add(model_sw)
        self._ai_model_popover.connect("closed", self._on_model_popover_closed)

        cmd_hints = "  |  ".join(f"/{cmd[1:]} {desc}"
                                 for cmd, desc in self._AI_COMMANDS)
        hint_text = f"Shift+Enter \u21b5 \u00b7 Enter \u53d1\u9001  |  {cmd_hints}"
        self._ai_hint_label = Gtk.Label.new(hint_text)
        self._ai_hint_label.set_xalign(1)
        self._ai_hint_label.get_style_context().add_class("dim-label")
        self._ai_hint_label.set_margin_end(4)
        self._ai_hint_label.set_opacity(0.6)
        self._ai_hint_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._ai_hint_label.set_max_width_chars(55)
        self._ai_hint_label.set_tooltip_text(hint_text)
        self._ai_input_area.pack_start(self._ai_hint_label, False, False, 0)

        self._ai_cmd_popover = AICommandPopover(self._ai_entry, self._AI_COMMANDS)

        self.pack_start(self._ai_input_area, False, False, 0)

        try:
            _subagent_css = b"""
                .subagent-status-bar { margin: 4px 8px 2px 8px; min-height: 28px; background-color: #1a1d2e; border-radius: 6px; padding: 4px 6px; border: 1px solid #2a2d3e; }
                .subagent-block-running { background-color: #3b82f6; color: #ffffff; border-radius: 4px; font-size: 12px; border: 2px solid transparent; }
                .subagent-block-done { background-color: #22c55e; color: #ffffff; border-radius: 4px; font-size: 12px; border: 2px solid transparent; }
                .subagent-block-done:hover { background-color: #16a34a; }
                .subagent-block-failed { background-color: #ef4444; color: #ffffff; border-radius: 4px; font-size: 12px; border: 2px solid transparent; }
                .subagent-block-selected { border-color: #ffffff; }
                flowboxchild:focus { outline: none; box-shadow: none; }
            """
            _css_provider = Gtk.CssProvider()
            _css_provider.load_from_data(_subagent_css)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), _css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        except Exception as e:
            print(f"[opencode-switcher] CSS load error: {e}")

        self._refresh_subagent_bar()
        self.connect("destroy", self._on_destroy)
        from tool_registry import register_subagent_status_listener
        register_subagent_status_listener(self._on_subagent_status_changed)

    def _read_model_config(self, prompt_obj: Optional[CustomPrompt] = None, model_info: Optional[Dict] = None):
        bound_alias = None
        if model_info:
            bound_alias = model_info.get("alias")
        elif prompt_obj:
            bound_alias = getattr(prompt_obj, "bound_model_alias", None)

        model_config = None
        if bound_alias:
            model_config = next((m for m in self._llm_settings_store.models if m.alias == bound_alias), None)

        # Try matching by base_url and model_name if alias match didn't resolve a valid model
        if not model_config and model_info:
            base_url_info = model_info.get("base_url", "").strip()
            model_name_info = model_info.get("model_name", "").strip()
            model_config = next(
                (m for m in self._llm_settings_store.models 
                 if m.base_url.strip() == base_url_info and m.model_name.strip() == model_name_info),
                None
            )

        if not model_config:
            model_config = next((m for m in self._llm_settings_store.models if m.is_default), None)
        if not model_config and self._llm_settings_store.models:
            model_config = self._llm_settings_store.models[0]

        if model_config:
            base_url = model_config.base_url.strip()
            api_key = model_config.api_key.strip()
            model_name = model_config.model_name.strip()
            temperature = model_config.temperature
            max_tokens = model_config.max_tokens
            top_p = model_config.top_p
        else:
            base_url = ""
            api_key = ""
            model_name = ""
            temperature = DEFAULT_TEMPERATURE
            max_tokens = DEFAULT_MAX_TOKENS
            top_p = DEFAULT_TOP_P

        if not api_key:
            api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            api_key = os.environ.get("OPENAI_API_KEY", "").strip()

        if not base_url:
            base_url = os.environ.get("DEEPSEEK_BASE_URL", "").strip()
        if not base_url:
            base_url = os.environ.get("OPENAI_BASE_URL", "").strip()

        if not model_name:
            model_name = os.environ.get("DEEPSEEK_MODEL_NAME", "").strip()
        if not model_name:
            model_name = os.environ.get("OPENAI_MODEL_NAME", "").strip()

        # Override inference params from model_info (conversation snapshot) if present
        if model_info:
            if "temperature" in model_info:
                temperature = model_info["temperature"]
            if "max_tokens" in model_info:
                max_tokens = model_info["max_tokens"]
            if "top_p" in model_info:
                top_p = model_info["top_p"]

        display_name = f"{model_config.alias} ({model_name})" if model_config else model_name
        return base_url, api_key, model_name, display_name, temperature, max_tokens, top_p

    def _get_title_model_config(self):
        """Return (base_url, api_key, model_name, temperature, max_tokens, top_p)
        for the model marked as title-generation model, or None if not set."""
        model = next((m for m in self._llm_settings_store.models if m.is_title_model), None)
        if not model:
            return None
        return (model.base_url.strip(), model.api_key.strip(), model.model_name.strip(),
                model.temperature, model.max_tokens, model.top_p)

    def _start_new_conversation(self, prompt_text: str):
        self._ai_messages = [{"role": "user", "content": prompt_text}]
        self._ai_conversation_id = uuid4().hex[:12]
        self._ai_assistant_buffer = ""
        self._ai_current_assistant_text = ""
        self._ai_response_div_added = False
        self._ai_assistant_html_base = ""
        rendered_prompt = _close_unclosed_code_blocks(prompt_text)
        self._ai_markdown_text = (
            f'<div class="msg-row user" markdown="1">\n'
            f'{USER_AVATAR_HTML}\n'
            f'<div class="msg-bubble user" markdown="1">\n'
            f'{rendered_prompt}\n'
            f'<copy-marker data-msg-index="0" class="user-copy-marker"></copy-marker>\n'
            f'</div>\n'
            f'</div>\n\n'
        )
        self._ai_title_generated = False
        self._ai_summary = ""
        user_html = _markdown_to_html_safe(
            self._ai_markdown_text,
            fallback_content=(
                f'<div class="msg-row user" markdown="1">\n'
                f'{USER_AVATAR_HTML}\n'
                f'<div class="msg-bubble user" markdown="1">\n'
                f'<p>{prompt_text}</p>\n'
                f'</div>\n'
                f'</div>'
            )
        )
        self._ai_webview.load_html(self.get_html_template(self._theme, user_html), "file:///")

    def _build_llm_messages(self) -> tuple:
        """构建发送给 LLM 的消息列表和额外 system 消息。

        Returns:
            tuple: (messages_list, extra_system_messages)
            - messages_list: 纯对话消息列表（不含摘要）
            - extra_system_messages: 摘要 system 消息列表（如有），
              仅在 HTTP 请求层注入，不污染 self._ai_messages
        """
        extra = []
        if self._ai_summary:
            extra.append({
                "role": "system",
                "content": f"【历史摘要】\n{self._ai_summary}"
            })
        return list(self._ai_messages), extra

    # ── Streaming v2: Token Batching ────────────────────────────────────

    def _init_streaming_state(self):
        """在每轮对话开始时初始化流式状态。"""
        self._token_buffer = ""
        self._flush_scheduled = False
        self._reasoning_buffer = ""
        self._reasoning_flush_scheduled = False
        self._last_flushed_len = 0
        self._streaming_container_created = False

        if self._ENABLE_STREAMING_V2 == self._STREAM_MODE_OFF:
            self._streaming_mode = self._STREAM_MODE_OFF
        elif self._ENABLE_STREAMING_V2 == "full":
            self._streaming_mode = self._STREAM_MODE_FULL
        else:
            self._streaming_mode = self._STREAM_MODE_TEXT

    def _ensure_streaming_container(self) -> bool:
        """确保流式消息容器已创建，若未创建则发送 appendMessageContainer JS。"""
        if not self._streaming_container_created and hasattr(self, "_ai_webview") and self._ai_webview:
            req_id = getattr(self, "_ai_request_id", 0)
            msg_id = f"msg-{req_id}"
            self._ai_webview.run_javascript(f"appendMessageContainer('{msg_id}');", None, None)
            self._streaming_container_created = True
            return True
        return self._streaming_container_created

    def _on_token_delta(self, text: str):
        """收到 LLM 文本增量，累积到 buffer 并安排 60ms flush（主线程调用）。"""
        if self._STREAM_PERF_LOG:
            print(f"[perf] token_delta: +{len(text)}ch, buffer={len(self._token_buffer)}ch", flush=True)
        if self._streaming_mode == self._STREAM_MODE_OFF:
            return

        self._token_buffer += text

        if not self._flush_scheduled:
            self._flush_scheduled = True
            GLib.timeout_add(self._BATCH_FLUSH_MS, self._flush_token_buffer)

    def _flush_token_buffer(self) -> bool:
        """60ms 定时器回调：将累积的 token 文本批量 flush 到 WebView。"""
        if self._STREAM_PERF_LOG:
            print(f"[perf] flush_token: {len(self._token_buffer)}ch → JS", flush=True)
        self._flush_scheduled = False

        if self._streaming_mode == self._STREAM_MODE_OFF:
            self._token_buffer = ""
            return False

        if not self._token_buffer:
            return False

        self._ensure_streaming_container()

        js_code = f"appendStreamToken({json.dumps(self._token_buffer)});"
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(js_code, None, None)

        self._token_buffer = ""
        return False

    # ── Reasoning delta batching (与 token batching 对称) ──

    def _on_reasoning_delta(self, text: str):
        """收到 LLM 推理增量，累积到 buffer 并安排 60ms flush。"""
        if self._STREAM_PERF_LOG:
            print(f"[perf] reasoning_delta: +{len(text)}ch, buffer={len(self._reasoning_buffer)}ch", flush=True)
        if self._streaming_mode == self._STREAM_MODE_OFF:
            return
        self._reasoning_buffer += text
        if not self._reasoning_flush_scheduled:
            self._reasoning_flush_scheduled = True
            GLib.timeout_add(self._BATCH_FLUSH_MS, self._flush_reasoning_buffer)

    def _flush_reasoning_buffer(self) -> bool:
        """60ms 定时器回调：将累积的推理文本批量 flush 到 WebView。"""
        self._reasoning_flush_scheduled = False
        if self._streaming_mode == self._STREAM_MODE_OFF:
            self._reasoning_buffer = ""
            return False
        if not self._reasoning_buffer:
            return False

        self._ensure_streaming_container()

        js_code = f"appendStreamReasoning({json.dumps(self._reasoning_buffer)});"
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(js_code, None, None)
        self._reasoning_buffer = ""
        return False

    def _flush_all_buffers(self):
        """flush 所有累积 buffer（在流结束/模式切换时调用）。"""
        if self._reasoning_buffer:
            self._flush_reasoning_buffer()
        if self._token_buffer:
            self._flush_token_buffer()

    def _switch_to_html_mode(self, req_id: int):
        """工具调用阶段：结束纯文本追加，切换到 HTML 全量渲染模式。"""
        if self._STREAM_PERF_LOG:
            print(f"[perf] switch_to_html_mode: req={req_id}", flush=True)
        if self._streaming_mode != self._STREAM_MODE_TEXT:
            return

        # 1. 先 flush 所有累积 buffer
        self._flush_all_buffers()

        # 2. 构建完整渲染
        msg_id = f"msg-{req_id}"
        turn_msgs = self._get_turn_messages()
        output = render_turn(TurnRenderInput(
            turn_messages=turn_msgs,
            all_messages=self._ai_messages,
            streaming_reasoning=self._ai_current_reasoning_text,
            streaming_content=self._ai_current_assistant_text,
            is_streaming=False,
        ))

        # 3. 发送 reasoning + answer 的最终 HTML
        js_code = f"finalizeStreamingContent({json.dumps(output.answer_html)}, {json.dumps(output.reasoning_html)});"
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(js_code, None, None)

        self._streaming_mode = self._STREAM_MODE_FULL

    # ────────────────────────────────────────────────────────────────────

    def _send_user_message(self, text: str):
        self._init_streaming_state()
        # Build message content with or without pending image
        if self._ai_pending_image_hash:
            content = [
                {"type": "text", "text": text},
                {
                    "type": "image_url",
                    "image_url": {
                        "hash": self._ai_pending_image_hash,
                        "detail": "high",
                    },
                },
            ]
        else:
            content = text

        self._ai_messages.append({"role": "user", "content": content})
        self._ai_request_id += 1
        current_req_id = self._ai_request_id
        self._ai_streaming = True
        self._ai_current_assistant_text = ""
        self._ai_response_div_added = False
        self._ai_assistant_html_base = ""

        if getattr(self, "_ai_render_timeout_id", 0) != 0:
            GLib.source_remove(self._ai_render_timeout_id)
            self._ai_render_timeout_id = 0

        # Build markdown for rendering — extract text part for code-block check
        if isinstance(content, list):
            text_part = next(
                (p["text"] for p in content if isinstance(p, dict) and p.get("type") == "text"),
                text
            )
            img_src = _resolve_vision_image_src(content)
            rendered_text = _close_unclosed_code_blocks(text_part)
            if img_src:
                rendered_text += f'\n\n<img src="{img_src}" class="chat-image" onclick="showLightbox(this.src)">'
        else:
            rendered_text = _close_unclosed_code_blocks(content)
            if rendered_text:
                rendered_text = _preserve_newlines(rendered_text)
        user_msg_idx = len(self._ai_messages) - 1
        self._ai_markdown_text += (
            f'\n\n<div class="msg-row user" markdown="1">\n'
            f'{USER_AVATAR_HTML}\n'
            f'<div class="msg-bubble user" markdown="1">\n'
            f'{rendered_text}\n'
            f'<copy-marker data-msg-index="{user_msg_idx}" class="user-copy-marker"></copy-marker>\n'
            f'</div>\n'
            f'</div>\n\n'
        )
        # 重置 JS 自动滚动标志，确保新消息提交后滚动到最底端并跟随流式输出
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript("_autoScroll = true;", None, None)
        self._render_markdown(self._ai_markdown_text)

        self._ai_spinner.show()
        self._ai_spinner.start()

        base_url, api_key, model_name, _, temperature, max_tokens, top_p = self._read_model_config(
            self._ai_last_prompt_obj,
            getattr(self, "_ai_active_model_info", None)
        )

        if not base_url or not model_name or not api_key:
            self._ai_streaming = False
            self._ai_spinner.stop()
            self._ai_spinner.hide()
            self._ai_send_btn.set_sensitive(True)
            self._ai_entry.placeholder_text = ""
            error_msg = (
                "❌ [错误] 模型配置不完整。\n\n"
                "请检查 **Prompts Config → ⚙️ API Settings** 中的模型配置，\n"
                "或在环境变量中设置 DEEPSEEK/OPENAI 的 BASE_URL、API_KEY、MODEL_NAME。"
            )
            self._ai_markdown_text += f'\n\n{error_msg}\n\n'
            self._render_markdown(self._ai_markdown_text)
            return

        self._ai_cancel_event.clear()
        self._update_send_button(True)
        self._ai_entry.placeholder_text = "等待回复中..."
        msgs_for_llm, extra_sys = self._build_llm_messages()
        threading.Thread(
            target=self._run_llm_api_request,
            args=(base_url, api_key, model_name, msgs_for_llm, current_req_id,
                  temperature, max_tokens, top_p, self._ai_markdown_text,
                  self._ai_conversation_id, extra_sys),
            daemon=True
        ).start()
    def _retry_response(self, assistant_index: int):
        """删除指定的 assistant 回复并重新请求 LLM（丢弃该回复之后的所有消息）。"""
        if self._ai_streaming:
            active_state = self._ai_running_convs.get(self._ai_conversation_id)
            if active_state:
                active_state["cancel_event"].set()
                self._ai_running_convs.pop(self._ai_conversation_id, None)
            self._llm_client.cancel_active_request()
            self._update_send_button(False)
            self._ai_streaming = False
            self._ai_spinner.stop()
            self._ai_spinner.hide()

        msgs = self._ai_messages
        if not (0 <= assistant_index < len(msgs)) or msgs[assistant_index].get("role") != "assistant":
            return

        # 逆向寻找到触发该回复的最后一个 user 消息节点
        user_index = assistant_index
        while user_index >= 0 and msgs[user_index].get("role") != "user":
            user_index -= 1

        if user_index < 0:
            return

        # 丢弃该轮交互产生的所有中间状态（包括工具调用、结果、当前回答等）
        self._ai_messages = msgs[:user_index + 1]

        self._ai_markdown_text = self._rebuild_markdown_from_messages(self._ai_messages)
        # 重置 JS 自动滚动标志，确保重试后滚动到最底端
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript("_autoScroll = true;", None, None)
        self._render_markdown(self._ai_markdown_text)

        self._init_streaming_state()
        self._ai_request_id += 1
        current_req_id = self._ai_request_id
        self._ai_streaming = True
        self._ai_current_assistant_text = ""
        self._ai_response_div_added = False
        self._ai_assistant_html_base = ""

        self._ai_spinner.show()
        self._ai_spinner.start()
        self._update_send_button(True)
        self._ai_entry.placeholder_text = "等待回复中..."

        base_url, api_key, model_name, _, temperature, max_tokens, top_p = self._read_model_config(
            self._ai_last_prompt_obj,
            getattr(self, "_ai_active_model_info", None)
        )
        self._ai_cancel_event.clear()
        msgs_for_llm, extra_sys = self._build_llm_messages()
        threading.Thread(
            target=self._run_llm_api_request,
            args=(base_url, api_key, model_name, msgs_for_llm, current_req_id,
                  temperature, max_tokens, top_p, self._ai_markdown_text,
                  self._ai_conversation_id, extra_sys),
            daemon=True
        ).start()

    def ask_llm_api(self, prompt_text: str, prompt_obj: Optional[CustomPrompt] = None):
        # Show the AI panel
        self.separator.set_no_show_all(False)
        self.separator.show()
        self.set_no_show_all(False)
        self.show()
        self.show_all()
        self.queue_resize()

        if not hasattr(self, "_ai_request_id"):
            self._ai_request_id = 0
        self._ai_request_id += 1
        current_req_id = self._ai_request_id

        self._ai_streaming = True
        self._ai_current_assistant_text = ""
        self._ai_response_div_added = False
        self._ai_assistant_html_base = ""
        if getattr(self, "_ai_render_timeout_id", 0) != 0:
            GLib.source_remove(self._ai_render_timeout_id)
            self._ai_render_timeout_id = 0

        self._start_new_conversation(prompt_text)
        self._ai_last_prompt_obj = prompt_obj

        base_url, api_key, model_name, display_name, temperature, max_tokens, top_p = self._read_model_config(prompt_obj)
        self._ai_active_model_info = {
            "alias": display_name.split(" (")[0] if " (" in display_name else display_name,
            "base_url": base_url,
            "model_name": model_name,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
        }
        self._ai_lbl.set_markup(f"<b>AI 助手看盘</b>\n<span size='small' foreground='#888888'>({display_name})</span>")

        self._ai_spinner.show()
        self._ai_spinner.start()

        if not api_key or not base_url or not model_name:
            self._ai_streaming = False
            self._ai_spinner.stop()
            self._ai_spinner.hide()
            missing = []
            if not api_key:
                missing.append("API Key")
            if not base_url:
                missing.append("Base URL")
            if not model_name:
                missing.append("Model Name")
            error_msg = (
                "❌ [错误] 模型配置不完整，缺少: " + "、".join(missing) + "。\n\n"
                "请检查 **Prompts Config → ⚙️ API Settings** 中的模型配置，\n"
                "或在环境变量中设置 DEEPSEEK/OPENAI 的 BASE_URL、API_KEY、MODEL_NAME。"
            )
            self._ai_markdown_text = error_msg
            html = _markdown_to_html_safe(
                error_msg,
                fallback_content=f"<p style='color: #f43f5e; font-weight: bold;'>{error_msg}</p>"
            )
            self._ai_webview.load_html(self.get_html_template(self._theme, html), "file:///")
            return

        self._ai_cancel_event.clear()
        self._update_send_button(True)
        msgs_for_llm, extra_sys = self._build_llm_messages()
        threading.Thread(
            target=self._run_llm_api_request,
            args=(base_url, api_key, model_name, msgs_for_llm, current_req_id,
                  temperature, max_tokens, top_p, self._ai_markdown_text,
                  self._ai_conversation_id, extra_sys),
            daemon=True
        ).start()

    def _run_llm_api_request(self, base_url: str, api_key: str, model_name: str, messages: list,
                              req_id: int, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS,
                              top_p: float = DEFAULT_TOP_P, markdown_text: str = "", conv_id: str = "",
                              extra_system_messages: Optional[list] = None):
        """Start the ReAct loop by delegating execution to the run_llm_react_loop orchestrator."""
        cancel_event = threading.Event()
        
        # Initialize conversation background state
        state = {
            "streaming": True,
            "messages": list(messages),  # Create a shallow copy of messages list
            "cancel_event": cancel_event,
            "current_assistant_text": "",
            "current_reasoning_text": "",
            "response_div_added": False,
            "ai_markdown_text": markdown_text,
            "req_id": req_id,
        }
        self._ai_running_convs[conv_id] = state

        def reset_iteration_state():
            st = self._ai_running_convs.get(conv_id)
            if st:
                st["current_assistant_text"] = ""
                st["current_reasoning_text"] = ""
                st["response_div_added"] = False
            if self._ai_conversation_id == conv_id:
                self._ai_assistant_buffer = ""
                self._ai_current_assistant_text = ""
                self._ai_response_div_added = False
                self._ai_assistant_html_base = ""
                self._ai_current_reasoning_text = ""
                self._ai_dirty_stream = False
                # v2: 清理 token + reasoning buffer
                self._token_buffer = ""
                self._flush_scheduled = False
                self._reasoning_buffer = ""
                self._reasoning_flush_scheduled = False

        def append_message_callback(msg):
            st = self._ai_running_convs.get(conv_id)
            if st:
                st["messages"].append(msg)
            if self._ai_conversation_id == conv_id:
                if st:
                    self._ai_messages = st["messages"]
                else:
                    # State was popped (e.g. pause during tool execution).
                    # Append directly to keep message history valid.
                    self._ai_messages.append(msg)
                    if msg.get("role") == "tool" and self._ai_streaming is False:
                        GLib.idle_add(self._re_render_after_tool_cancel)
                GLib.idle_add(self._render_current_assistant_message, req_id)

        def set_reasoning_callback(text):
            st = self._ai_running_convs.get(conv_id)
            if st:
                st["current_reasoning_text"] = text
            if self._ai_conversation_id == conv_id:
                self._ai_current_reasoning_text = text
                if self._streaming_mode == self._STREAM_MODE_OFF:
                    self._ai_dirty_stream = True

        def set_assistant_callback(text):
            st = self._ai_running_convs.get(conv_id)
            if st:
                st["current_assistant_text"] = text
            if self._ai_conversation_id == conv_id:
                self._ai_current_assistant_text = text
                # v2: text mode 下由 token batching 驱动渲染，不设 dirty flag
                if self._streaming_mode == self._STREAM_MODE_OFF:
                    self._ai_dirty_stream = True

        def append_html_callback(html):
            if self._ai_conversation_id == conv_id:
                GLib.idle_add(self.append_html_to_webview, html)

        def on_token_delta_fn(text):
            """v2: 增量回调，后台线程收到 token delta 时调用。"""
            if self._ai_conversation_id == conv_id:
                GLib.idle_add(self._on_token_delta, text)

        def on_reasoning_delta_fn(text):
            """v2: 推理增量回调，后台线程收到 reasoning delta 时调用。"""
            if self._ai_conversation_id == conv_id:
                GLib.idle_add(self._on_reasoning_delta, text)

        run_llm_react_loop(
            llm_client=self._llm_client,
            base_url=base_url,
            api_key=api_key,
            model_name=model_name,
            messages=state["messages"],
            req_id=req_id,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            cancel_event=cancel_event,
            get_current_request_id_fn=lambda: req_id,
            append_message_fn=append_message_callback,
            append_html_to_webview_fn=append_html_callback,
            handle_ask_user_question_fn=self._handle_ask_user_question,
            on_llm_api_finished_fn=self._on_llm_api_finished,
            finalize_after_tool_loop_fn=self._finalize_after_tool_loop,
            set_tool_iteration_fn=lambda val: setattr(self, "_ai_tool_iteration", val),
            reset_iteration_state_fn=reset_iteration_state,
            set_reasoning_text_fn=set_reasoning_callback,
            set_assistant_text_fn=set_assistant_callback,
            on_token_delta_fn=on_token_delta_fn,
            on_reasoning_delta_fn=on_reasoning_delta_fn,
            switch_to_html_mode_fn=self._switch_to_html_mode,
            conv_id=conv_id,
            extra_system_messages=extra_system_messages,
        )

    def _contains_ask_user_question(self, turn_msgs: List[Dict]) -> bool:
        """检查给定的 turn_msgs 中是否包含 ask_user_question 相关的工具调用或响应。"""
        return any(
            (msg.get("role") == "tool" and msg.get("name") == "ask_user_question") or
            (msg.get("role") == "assistant" and any(tc.get("function", {}).get("name") == "ask_user_question" for tc in msg.get("tool_calls", []) or []))
            for msg in turn_msgs
        )

    def _append_assistant_turn_to_cache(self, turn_msgs: List[Dict], combined_html: str, start_idx: int, has_ask: Optional[bool] = None):
        """增量更新当前会话的 Markdown 和 HTML 缓存。"""
        if has_ask is None:
            has_ask = self._contains_ask_user_question(turn_msgs)

        if has_ask:
            self._ai_markdown_text = self._rebuild_markdown_from_messages(self._ai_messages)
            self._last_rendered_html = _markdown_to_html_safe(self._ai_markdown_text, fallback_content="")
            if self._ai_conversation_id:
                self._ai_html_cache[self._ai_conversation_id] = self._last_rendered_html
            return

        assistant_md = (
            f'<div class="msg-row assistant" markdown="1">\n'
            f'{ASSISTANT_AVATAR_HTML}\n'
            f'<div class="msg-bubble assistant" markdown="1">\n'
            f'{combined_html}\n'
            f'<copy-marker data-msg-index="{start_idx}"></copy-marker>\n'
            f'</div>\n'
            f'</div>\n\n'
        )
        self._ai_markdown_text += assistant_md

        assistant_html = (
            f'<div class="msg-row assistant">\n'
            f'{ASSISTANT_AVATAR_HTML}\n'
            f'<div class="msg-bubble assistant">\n'
            f'{combined_html}\n'
            f'<copy-marker data-msg-index="{start_idx}"></copy-marker>\n'
            f'</div>\n'
            f'</div>\n\n'
        )
        self._last_rendered_html += assistant_html
        if self._ai_conversation_id:
            self._ai_html_cache[self._ai_conversation_id] = self._last_rendered_html

    def _finalize_after_tool_loop(self, req_id: int):
        """Finalize after tool loop ends (used when tool iteration limit hit)."""
        conv_id = None
        for cid, st in self._ai_running_convs.items():
            if st.get("req_id") == req_id:
                conv_id = cid
                break

        if not conv_id:
            conv_id = self._ai_conversation_id
            state = None
        else:
            state = self._ai_running_convs.get(conv_id)

        if state:
            state["streaming"] = False

        if self._ai_conversation_id == conv_id:
            target_messages = state["messages"] if state else self._ai_messages
            self._ai_messages = target_messages

            # ── 旧版路径（特性开关关闭时走旧版渲染） ──
            if self._streaming_mode == self._STREAM_MODE_OFF:
                msg_id = f"msg-{req_id}"
                last_user_idx = -1
                for idx in range(len(target_messages) - 1, -1, -1):
                    if target_messages[idx].get("role") == "user":
                        last_user_idx = idx
                        break
                turn_msgs = target_messages[last_user_idx + 1:] if last_user_idx != -1 else target_messages
                
                output = render_turn(TurnRenderInput(
                    turn_messages=turn_msgs,
                    all_messages=target_messages,
                    is_streaming=False,
                ))
                combined_html = output.combined_html
                is_split = output.is_split

                js_final = build_update_js(msg_id, output)
                if hasattr(self, "_ai_webview") and self._ai_webview:
                    self._ai_webview.run_javascript(js_final, None, None)

                start_idx = last_user_idx + 1
                self._append_assistant_turn_to_cache(turn_msgs, combined_html, start_idx, has_ask=is_split)

                js_sync = (
                    "window._isStreaming = false;"
                    "_scrollToBottom();"
                    "_throttledWindowing();"
                    "_initRoundNav();"
                )
                if hasattr(self, "_ai_webview") and self._ai_webview:
                    self._ai_webview.run_javascript(js_sync, None, None)

            self._ai_spinner.stop()
            self._ai_spinner.hide()
            self._ai_streaming = False
            self._update_send_button(False)
            self._ai_entry.placeholder_text = ""
        else:
            if state:
                target_messages = state["messages"]
                self._render_background_conversation(conv_id, target_messages, state)

        self._ai_running_convs.pop(conv_id, None)
        self._handle_stream_end(req_id)

    def _handle_ask_user_question(self, tool_call: dict) -> str:
        try:
            arguments = json.loads(tool_call.get("function", {}).get("arguments", "{}"))
        except json.JSONDecodeError:
            return "[询问用户失败：参数解析错误]"

        question = arguments.get("question", "")
        if not question:
            return "[询问用户失败：问题为空]"

        event = threading.Event()
        self._ai_ask_user_state = {
            "question": question,
            "event": event,
            "answer": None,
        }

        rendered_question = _markdown_to_html_safe(question)
        question_html = (
            '<div class="tool-ask-user">'
            '<div class="tool-ask-user-header">💬 Agent 需要确认</div>'
            f'<div class="tool-ask-user-body">{rendered_question}</div>'
            '<div class="tool-ask-user-footer">✏️ 在下方输入框中回答，或输入 /cancel 取消</div>'
            '</div>'
        )
        GLib.idle_add(self._enable_ask_user_entry)

        if not event.wait(timeout=300):
            # Timeout — user did not answer within 5 minutes
            self._ai_ask_user_state = None
            GLib.idle_add(self._ai_entry.grab_focus)
            GLib.idle_add(self._update_send_button, True)
            return "[询问用户超时：用户未在 5 分钟内回答]"

        state = getattr(self, "_ai_ask_user_state", None)
        answer = state.get("answer", "") if state else ""
        self._ai_ask_user_state = None
        GLib.idle_add(self._ai_entry.grab_focus)

        if not answer:
            return "[用户取消了回答]"
        return answer

    def _enable_ask_user_entry(self):
        self._ai_entry.placeholder_text = "请输入回答..."
        self._ai_send_btn.set_label("发送")
        self._ai_send_btn.set_sensitive(True)
        self._ai_entry.grab_focus()

    def _render_background_conversation(self, conv_id: str, target_messages: list, state):
        """渲染背景对话（非当前可见），只更新 cache 不操作 WebView。"""
        output = render_turn(TurnRenderInput(
            turn_messages=target_messages,
            all_messages=target_messages,
            is_streaming=False,
        ))

        cached_html = self._ai_html_cache.get(conv_id)
        if cached_html is not None:
            combined_html = output.combined_html
            if output.has_ask_question:
                assistant_html = combined_html
                assistant_md = output.raw_markdown
            else:
                last_user_idx = -1
                for idx in range(len(target_messages) - 1, -1, -1):
                    if target_messages[idx].get("role") == "user":
                        last_user_idx = idx
                        break
                start_idx = last_user_idx + 1
                assistant_html = (
                    f'<div class="msg-row assistant">\n'
                    f'{ASSISTANT_AVATAR_HTML}\n'
                    f'<div class="msg-bubble assistant">\n'
                    f'{combined_html}\n'
                    f'<copy-marker data-msg-index="{start_idx}"></copy-marker>\n'
                    f'</div>\n'
                    f'</div>\n\n'
                )
                assistant_md = (
                    f'<div class="msg-row assistant" markdown="1">\n'
                    f'{ASSISTANT_AVATAR_HTML}\n'
                    f'<div class="msg-bubble assistant" markdown="1">\n'
                    f'{combined_html}\n'
                    f'<copy-marker data-msg-index="{start_idx}"></copy-marker>\n'
                    f'</div>\n'
                    f'</div>\n\n'
                )
            self._ai_html_cache[conv_id] = cached_html + assistant_html
            state["ai_markdown_text"] += assistant_md
        else:
            rebuilt_markdown = self._rebuild_markdown_from_messages(target_messages)
            html = _markdown_to_html_safe(rebuilt_markdown, fallback_content="")
            self._ai_html_cache[conv_id] = html

        try:
            conv = self._conversation_store.load_conversation(conv_id)
            messages_objs = _to_chat_messages(target_messages)
            if conv:
                conv.messages = messages_objs
            else:
                local_title = "New Conversation"
                if target_messages:
                    local_title = _extract_local_title(target_messages[0].get("content", ""))
                model_snapshot = self._build_model_snapshot()
                conv = Conversation(
                    id=conv_id,
                    title=local_title,
                    system_prompt="",
                    messages=messages_objs,
                    model_config_snapshot=model_snapshot,
                    created_at=int(time.time() * 1000),
                    updated_at=int(time.time() * 1000),
                )
            self._conversation_store.save_conversation(conv, bump_updated_at=True)

            if conv.title in ("New Conversation", "(untitled)") and target_messages:
                first_msg = target_messages[0].get("content", "")
                if first_msg:
                    title_cfg = self._get_title_model_config()
                    if title_cfg:
                        base_url, api_key, model_name, temperature, max_tokens, top_p = title_cfg
                    else:
                        base_url, api_key, model_name, _, temperature, max_tokens, top_p = self._read_model_config(
                            None, getattr(self, "_ai_active_model_info", None)
                        )
                    if base_url and api_key:
                        threading.Thread(
                            target=self._generate_conversation_title,
                            args=(first_msg, conv_id, base_url, api_key, model_name,
                                  temperature, max_tokens, top_p),
                            daemon=True
                        ).start()
        except Exception as e:
            print(f"Error saving background finished conversation: {e}", flush=True)

    def _finalize_streaming_render(self):
        """流结束时 flush 剩余 buffer，触发前端最终 HTML 渲染（仅当前可见对话）。"""
        if self._streaming_mode == self._STREAM_MODE_OFF:
            return

        # 1. flush 所有累积 buffer
        self._flush_all_buffers()

        # 2. 构建最终 HTML
        req_id = getattr(self, "_ai_request_id", 0)
        msg_id = f"msg-{req_id}"
        turn_msgs = self._get_turn_messages()
        output = render_turn(TurnRenderInput(
            turn_messages=turn_msgs,
            all_messages=self._ai_messages,
            streaming_reasoning="",
            streaming_content=self._ai_current_assistant_text,
            is_streaming=False,
        ))

        # 3. 使用 build_update_js + updateMessageContainer 做最终渲染
        #    复用旧版渲染路径，比 onStreamEnd 方式更可靠
        js_final = build_update_js(msg_id, output)
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(js_final, None, None)

        # 4. cache 更新
        last_user_idx = -1
        for idx in range(len(self._ai_messages) - 1, -1, -1):
            if self._ai_messages[idx].get("role") == "user":
                last_user_idx = idx
                break
        start_idx = last_user_idx + 1
        self._append_assistant_turn_to_cache(turn_msgs, output.combined_html, start_idx, has_ask=output.is_split)

        # 5. JS 同步
        js_sync = (
            "window._isStreaming = false;"
            "_scrollToBottom();"
            "_throttledWindowing();"
            "_initRoundNav();"
        )
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(js_sync, None, None)

        # 6. 清理
        self._token_buffer = ""
        self._flush_scheduled = False
        self._reasoning_buffer = ""
        self._reasoning_flush_scheduled = False
        self._streaming_container_created = False

    def _handle_stream_end(self, req_id: int):
        """Common cleanup after a conversation turn ends (save, prune, title gen)."""
        if getattr(self, "_ai_request_id", 0) != req_id:
            return
        self._finalize_streaming_render()
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript("_scrollToBottom();", None, None)
        self._ai_streaming = False
        self._update_send_button(False)
        self._ai_entry.placeholder_text = "输入后续问题..."
        try:
            model_snapshot = self._build_model_snapshot()
            self._save_current_conversation(model_snapshot)
        except Exception as e:
            print(f"Error saving conversation: {e}", flush=True)
        self._prune_messages()
        try:
            title_cfg = self._get_title_model_config()
            if title_cfg:
                base_url, api_key, model_name, temperature, max_tokens, top_p = title_cfg
            else:
                base_url, api_key, model_name, _, temperature, max_tokens, top_p = self._read_model_config(
                    self._ai_last_prompt_obj,
                    getattr(self, "_ai_active_model_info", None)
                )
            if (not self._ai_title_generated
                    and self._ai_conversation_id
                    and self._ai_messages
                    and base_url and api_key):
                self._ai_title_generated = True
                first_msg = self._ai_messages[0].get("content", "")
                if first_msg:
                    threading.Thread(
                        target=self._generate_conversation_title,
                        args=(first_msg, self._ai_conversation_id, base_url, api_key, model_name,
                              temperature, max_tokens, top_p),
                        daemon=True
                    ).start()
        except Exception as e:
            print(f"Title generation error: {e}", flush=True)
        try:
            self._ai_history_popover.refresh_dropdown()
        except Exception as e:
            print(f"Dropdown refresh error: {e}", flush=True)

    def _get_turn_messages(self) -> List[Dict]:
        """Get messages for the current active turn (from last user msg onward)."""
        last_user_idx = -1
        for idx in range(len(self._ai_messages) - 1, -1, -1):
            if self._ai_messages[idx].get("role") == "user":
                last_user_idx = idx
                break
        return self._ai_messages[last_user_idx + 1:] if last_user_idx != -1 else self._ai_messages

    def _render_current_assistant_message(self, req_id: int):
        # v2: text mode 下由 token batching 处理渲染，跳过全量 HTML 构建
        if self._streaming_mode == self._STREAM_MODE_TEXT:
            if not self._streaming_container_created:
                conv_id = None
                for cid, st in self._ai_running_convs.items():
                    if st.get("req_id") == req_id:
                        conv_id = cid
                        break
                if not conv_id or self._ai_conversation_id != conv_id:
                    return
                st = self._ai_running_convs.get(conv_id)
                if not st or not st.get("streaming", False):
                    return
                msg_id = f"msg-{req_id}"
                js = f"appendMessageContainer('{msg_id}');"
                self._ai_webview.run_javascript(js, None, None)
                self._streaming_container_created = True
            return

        conv_id = None
        for cid, st in self._ai_running_convs.items():
            if st.get("req_id") == req_id:
                conv_id = cid
                break
        if not conv_id or self._ai_conversation_id != conv_id:
            return

        st = self._ai_running_convs.get(conv_id)
        if not st or not st.get("streaming", False):
            return
        
        msg_id = f"msg-{req_id}"
        turn_msgs = self._get_turn_messages()

        if not st.get("response_div_added", False):
            js = f"appendMessageContainer('{msg_id}');"
            self._ai_webview.run_javascript(js, None, None)
            st["response_div_added"] = True
            if self._ai_conversation_id == conv_id:
                self._ai_response_div_added = True

        output = render_turn(TurnRenderInput(
            turn_messages=turn_msgs,
            all_messages=self._ai_messages,
            streaming_reasoning=st.get("current_reasoning_text", ""),
            streaming_content=st.get("current_assistant_text", ""),
            is_streaming=True,
        ))
        js_update = build_update_js(msg_id, output)
        self._ai_webview.run_javascript(js_update, None, None)

    def _get_pygments_css(self, theme: str) -> str:
        return _get_pygments_css(theme, self._pygments_css_cache)

    def get_html_template(self, theme_name, initial_html=""):
        pygments_css = self._get_pygments_css(theme_name)
        return get_html_template(theme_name, initial_html, pygments_css)

    def _render_markdown(self, text: str):
        if not text:
            js_code = "updateContent('');"
            self._ai_webview.run_javascript(js_code, None, None)
            return

        fallback_msg = (
            "<p style='color: #f43f5e; font-weight: bold;'>❌ [错误] 缺少运行时依赖库。</p>"
            "<p>请在终端中运行以下命令安装所需依赖，并重启服务：</p>"
            "<pre><code>~/.local/share/opencode-switcher/venv/bin/pip install markdown pygments</code></pre>"
            f"<hr><pre><code>{text}</code></pre>"
        )
        html = _markdown_to_html_safe(text, fallback_content=fallback_msg)
        self._last_rendered_html = html
        if self._ai_conversation_id:
            self._ai_html_cache[self._ai_conversation_id] = html
        
        js_code = f"updateContent({json.dumps(html)});"
        self._ai_webview.run_javascript(js_code, None, None)

    def _on_llm_api_finished(self, req_id: int):
        """Called when LLM stream completes with a pure text response (no tool_calls)."""
        conv_id = None
        for cid, st in self._ai_running_convs.items():
            if st.get("req_id") == req_id:
                conv_id = cid
                break

        if not conv_id:
            conv_id = self._ai_conversation_id
            state = None
        else:
            state = self._ai_running_convs.get(conv_id)

        assistant_text = state["current_assistant_text"] if state else self._ai_current_assistant_text
        reasoning = state["current_reasoning_text"] if state else self._ai_current_reasoning_text
        assistant_msg = {"role": "assistant", "content": assistant_text}
        if reasoning:
            assistant_msg["reasoning_content"] = reasoning

        target_messages = state["messages"] if state else self._ai_messages
        if target_messages and target_messages[-1].get("role") == "user":
            target_messages.append(assistant_msg)
        elif target_messages and assistant_text:
            target_messages.append(assistant_msg)

        # ── 旧版路径（特性开关关闭时走旧版渲染） ──
        if self._streaming_mode == self._STREAM_MODE_OFF:
            msg_id = f"msg-{req_id}"
            last_user_idx = -1
            for idx in range(len(target_messages) - 1, -1, -1):
                if target_messages[idx].get("role") == "user":
                    last_user_idx = idx
                    break
            turn_msgs = target_messages[last_user_idx + 1:] if last_user_idx != -1 else target_messages
            
            output = render_turn(TurnRenderInput(
                turn_messages=turn_msgs,
                all_messages=target_messages,
                is_streaming=False,
            ))
            combined_html = output.combined_html
            is_split = output.is_split

            js_final = build_update_js(msg_id, output)
            if hasattr(self, "_ai_webview") and self._ai_webview:
                self._ai_webview.run_javascript(js_final, None, None)

            if state:
                state["current_assistant_text"] = ""
                state["current_reasoning_text"] = ""
                state["response_div_added"] = False
                state["streaming"] = False

            if self._ai_conversation_id == conv_id:
                self._ai_messages = target_messages
                self._ai_assistant_buffer = ""
                self._ai_current_assistant_text = ""
                self._ai_current_reasoning_text = ""
                self._ai_response_div_added = False
                self._ai_assistant_html_base = ""
                self._ai_dirty_stream = False
                self._ai_streaming = False

                if getattr(self, "_ai_render_timeout_id", 0) != 0:
                    GLib.source_remove(self._ai_render_timeout_id)
                    self._ai_render_timeout_id = 0

                start_idx = last_user_idx + 1
                self._append_assistant_turn_to_cache(turn_msgs, combined_html, start_idx, has_ask=is_split)

                js_sync = (
                    "window._isStreaming = false;"
                    "_scrollToBottom();"
                    "_throttledWindowing();"
                    "_initRoundNav();"
                )
                if hasattr(self, "_ai_webview") and self._ai_webview:
                    self._ai_webview.run_javascript(js_sync, None, None)

                self._ai_spinner.stop()
                self._ai_spinner.hide()
                self._update_send_button(False)
                self._ai_entry.placeholder_text = ""
            else:
                self._render_background_conversation(conv_id, target_messages, state)

            self._ai_running_convs.pop(conv_id, None)
            self._handle_stream_end(req_id)
            return

        # ── 新版路径 ──
        if state:
            state["current_assistant_text"] = ""
            state["current_reasoning_text"] = ""
            state["response_div_added"] = False
            state["streaming"] = False

        if self._ai_conversation_id == conv_id:
            self._ai_messages = target_messages
            self._ai_assistant_buffer = ""
            self._ai_current_assistant_text = ""
            self._ai_current_reasoning_text = ""
            self._ai_response_div_added = False
            self._ai_assistant_html_base = ""
            self._ai_dirty_stream = False
            self._ai_streaming = False

            if getattr(self, "_ai_render_timeout_id", 0) != 0:
                GLib.source_remove(self._ai_render_timeout_id)
                self._ai_render_timeout_id = 0

            self._ai_spinner.stop()
            self._ai_spinner.hide()
            self._update_send_button(False)
            self._ai_entry.placeholder_text = ""
        else:
            # 背景对话：由 _render_background_conversation 处理 cache 和保存
            if state:
                target_messages = state["messages"]
                self._render_background_conversation(conv_id, target_messages, state)

        self._ai_running_convs.pop(conv_id, None)
        self._handle_stream_end(req_id)

    def _adjust_ai_entry_height(self):
        buf = self._ai_entry.get_buffer()
        start = buf.get_start_iter()
        end = buf.get_end_iter()
        text = buf.get_text(start, end, True)

        newline_count = text.count('\n')
        target_lines = min(max(1, newline_count + 1), 5)

        layout = self._ai_entry.create_pango_layout("Ag")
        _, logical = layout.get_pixel_extents()
        line_height = logical.height + 2
        margin_px = self._ai_entry.get_top_margin() + self._ai_entry.get_bottom_margin()
        height = int(target_lines * line_height + margin_px)

        self._ai_entry_sw.set_size_request(-1, height)
        self._ai_entry.queue_resize()

    def _update_send_button(self, sending: bool, sensitive: bool = True):
        """Switch the send button between '发送' (idle) and '暂停' (streaming) states.

        Args:
            sending: True to show "暂停" (streaming), False to show "发送" (idle).
            sensitive: Whether the button is clickable.
        """
        self._ai_send_btn.set_label(AI_BTN_LABEL_STOP if sending else AI_BTN_LABEL_SEND)
        self._ai_send_btn.set_sensitive(sensitive)

    # ── Sub-agent status bar (polling + UI) ──────────────────────────────────

    def _on_destroy(self, widget):
        """Clean up by unregistering the subagent status listener on destroy."""
        try:
            from tool_registry import unregister_subagent_status_listener
            unregister_subagent_status_listener(self._on_subagent_status_changed)
        except Exception:
            pass

    def _on_decide_policy(self, webview, decision, decision_type):
        """处理 WebView 导航策略：opencode:// URI 和外部链接。"""
        if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            nav_action = decision.get_navigation_action()
            uri = nav_action.get_request().get_uri()
            if uri and uri.startswith("opencode://copy-response"):
                qs = parse_qs(urlparse(uri).query)
                index_str = qs.get("index", [None])[0]
                if index_str is not None:
                    try:
                        index = int(index_str)
                        msgs = self._ai_messages
                        if 0 <= index < len(msgs) and msgs[index].get("role") in ("assistant", "tool"):
                            turn_msgs = []
                            temp_idx = index
                            while temp_idx < len(msgs) and msgs[temp_idx].get("role") in ("assistant", "tool"):
                                turn_msgs.append(msgs[temp_idx])
                                temp_idx += 1
                            content_parts = []
                            for msg in turn_msgs:
                                if msg.get("role") == "assistant" and msg.get("content"):
                                    content_str = msg["content"]
                                    content_str = _strip_ai_markup(content_str)
                                    if content_str.strip():
                                        content_parts.append(content_str.strip())
                            content = "\n\n".join(content_parts).strip()
                            if content:
                                if self.on_ai_copy_started:
                                    self.on_ai_copy_started()
                                _copy_to_clipboard(content)
                                if self.on_ai_copy_finished:
                                    GLib.idle_add(self.on_ai_copy_finished)
                    except (ValueError, IndexError):
                        pass
                decision.ignore()
                return True
            if uri and uri.startswith("opencode://copy-input"):
                qs = parse_qs(urlparse(uri).query)
                index_str = qs.get("index", [None])[0]
                if index_str is not None:
                    try:
                        index = int(index_str)
                        msgs = self._ai_messages
                        if 0 <= index < len(msgs) and msgs[index].get("role") == "user":
                            content = msgs[index].get("content", "")
                            if content:
                                if isinstance(content, list):
                                    content = _vision_content_to_text(content)
                                if content:
                                    if self.on_ai_copy_started:
                                        self.on_ai_copy_started()
                                    _copy_to_clipboard(content)
                                    if self.on_ai_copy_finished:
                                        GLib.idle_add(self.on_ai_copy_finished)
                    except (ValueError, IndexError):
                        pass
                decision.ignore()
                return True
            if uri and uri.startswith("opencode://retry"):
                qs = parse_qs(urlparse(uri).query)
                index_str = qs.get("index", [None])[0]
                if index_str is not None:
                    try:
                        self._retry_response(int(index_str))
                    except (ValueError, IndexError):
                        pass
                decision.ignore()
                return True
            if uri and uri.startswith("opencode://rollback-round"):
                decision.ignore()
                qs = parse_qs(urlparse(uri).query)
                round_str = qs.get("round", [None])[0]
                if round_str is not None:
                    try:
                        self._rollback_to_round(int(round_str))
                    except (ValueError, IndexError):
                        pass
                return True
            if uri and not (uri.startswith("file://") or uri == "about:blank"):
                try:
                    Gio.AppInfo.launch_default_for_uri(uri, None)
                except Exception as e:
                    print(f"Error launching external link {uri}: {e}", flush=True)
                decision.ignore()
                return True
        return False

    def _on_webview_crashed(self, webview, event):
        """WebView 进程崩溃时自动重建。"""
        if getattr(self, "_webview_suspended", False):
            # 非崩溃，是 suspend 主动终止，不需要重建
            return
        print(f"[opencode-switcher] WebView process crashed, rebuilding...", flush=True)

        current_html = self._last_rendered_html or ""
        old_webview = self._ai_webview
        parent = old_webview.get_parent()

        # 复用已有的 web context，避免重复创建
        self._ai_webview = WebKit2.WebView.new_with_context(self._ai_web_context)

        settings = self._ai_webview.get_settings()
        settings.enable_webgl = False
        settings.enable_html5_database = False
        settings.enable_html5_local_storage = False

        self._ai_webview.load_html(
            self.get_html_template(self._theme, current_html if current_html else ""),
            "file:///"
        )

        if parent:
            GLib.idle_add(lambda: parent.remove(old_webview) or True)
            GLib.idle_add(lambda: parent.add(self._ai_webview) or self._ai_webview.show() or True)

        if current_html:
            self._ai_webview.run_javascript(f"updateContent({json.dumps(current_html)});", None, None)

        self._ai_webview.connect("web-process-terminated", self._on_webview_crashed)
        self._ai_webview.connect("decide-policy", self._on_decide_policy)
        self._ai_webview.connect("context-menu", lambda *_: True)

    def _on_subagent_status_changed(self, sid: str, info: Optional[dict]):
        """Event-driven callback triggered when a subagent's status changes."""
        try:
            active_conv_id = self._ai_conversation_id
            
            # If info is None, it represents a deletion event
            if info is None:
                self._remove_subagent_block(sid)
                self._update_subagent_bar_visibility()
                return

            # Check if this subagent belongs to the active conversation
            if info.get("conv_id") != active_conv_id:
                return

            status = info.get("status")
            if status == "removed":
                self._remove_subagent_block(sid)
            else:
                if sid in self._ai_subagent_blocks:
                    self._update_subagent_block(sid, info)
                else:
                    self._create_subagent_block(sid, info)
            
            self._update_subagent_bar_visibility()
        except Exception as e:
            import sys
            print(f"[opencode-switcher] error in _on_subagent_status_changed: {e}", file=sys.stderr)

    def _refresh_subagent_bar(self):
        """Clear and rebuild subagent status blocks for the active conversation."""
        try:
            self._clear_subagent_bar_instantly()
            from tool_registry import get_subagent_status_map
            status_map = get_subagent_status_map()
            active_conv_id = self._ai_conversation_id
            
            for sid, info in status_map.items():
                if info.get("conv_id") == active_conv_id:
                    self._create_subagent_block(sid, info)
                    
            self._update_subagent_bar_visibility()
        except Exception as e:
            import sys
            print(f"[opencode-switcher] error in _refresh_subagent_bar: {e}", file=sys.stderr)

    def _create_subagent_block(self, sid: Any, info: dict):
        """Create a FlowBoxChild for a sub-agent status block."""
        child = Gtk.FlowBoxChild.new()
        box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
        
        # Option 2: Extract and show only the local numeric ID part in label
        local_id = sid.split("-")[-1] if isinstance(sid, str) and "-" in sid else sid
        label = Gtk.Label.new(f"  子代理 {local_id}  ")
        label.set_margin_start(4)
        label.set_margin_end(4)
        label.set_margin_top(2)
        label.set_margin_bottom(2)
        box.pack_start(label, True, True, 0)
        child.add(box)

        status = info.get("status", "unknown")
        task = info.get("task", "")
        box_ctx = box.get_style_context()
        
        # Tooltip text shows full ID and task description
        tooltip_text = f"ID: {sid}\n任务: {task}"
        
        if status == "completed":
            box_ctx.add_class("subagent-block-done")
            child.set_tooltip_text(tooltip_text)
        elif status == "running":
            box_ctx.add_class("subagent-block-running")
            child.set_tooltip_text(f"运行中 — {tooltip_text}")
        else:
            box_ctx.add_class("subagent-block-failed")
            child.set_tooltip_text(tooltip_text)

        self._ai_subagent_bar.add(child)
        self._ai_subagent_blocks[sid] = (child, child, box)
        self._ai_subagent_bar.show_all()

    def _update_subagent_block(self, sid: Any, info: dict):
        """Update an existing block when sub-agent status changes."""
        entry = self._ai_subagent_blocks.get(sid)
        if entry is None:
            return
        child, event_box, box = entry
        status = info.get("status", "unknown")
        ctx = box.get_style_context()
        task = info.get("task", "")

        tooltip_text = f"ID: {sid}\n任务: {task}"

        if status == "completed":
            ctx.remove_class("subagent-block-running")
            ctx.add_class("subagent-block-done")
            event_box.set_tooltip_text(tooltip_text)
        elif status == "running":
            if ctx.has_class("subagent-block-done"):
                ctx.remove_class("subagent-block-done")
                self._ai_selected_subagents.discard(sid)
            ctx.add_class("subagent-block-running")
            event_box.set_tooltip_text(f"运行中 — {tooltip_text}")

    def _remove_subagent_block(self, sid: Any):
        """Remove a sub-agent block and clean up state."""
        self._ai_selected_subagents.discard(sid)
        entry = self._ai_subagent_blocks.pop(sid, None)
        if entry:
            if not self._ai_subagent_blocks:
                self._ai_subagent_bar.get_style_context().remove_class("subagent-status-bar")
                self._ai_subagent_bar.hide()
            child, _event_box, _box = entry
            self._ai_subagent_bar.remove(child)

    def _clear_subagent_bar_instantly(self):
        """Instantly clear all subagent blocks from the status bar UI."""
        self._ai_subagent_bar.get_style_context().remove_class("subagent-status-bar")
        self._ai_subagent_bar.hide()
        for child in self._ai_subagent_bar.get_children():
            self._ai_subagent_bar.remove(child)
        self._ai_subagent_blocks.clear()
        self._ai_selected_subagents.clear()
        self._update_subagent_bar_visibility()

    def _update_subagent_bar_visibility(self):
        """Show or hide the subagent bar based on whether any blocks exist."""
        has_blocks = len(self._ai_subagent_blocks) > 0
        if has_blocks:
            self._ai_subagent_bar.get_style_context().add_class("subagent-status-bar")
            self._ai_subagent_bar.set_no_show_all(False)
            self._ai_subagent_bar.show_all()
        else:
            self._ai_subagent_bar.get_style_context().remove_class("subagent-status-bar")
            self._ai_subagent_bar.hide()
            self._ai_subagent_bar.set_no_show_all(True)

    def _on_subagent_block_click(self, sid: Any):
        """Toggle selection state of a completed sub-agent block."""
        entry = self._ai_subagent_blocks.get(sid)
        if entry is None:
            return True
        child, event_box, box = entry
        from tool_registry import get_subagent_status_map
        info = get_subagent_status_map().get(sid, {})
        if info.get("status") != "completed":
            return True
        ctx = box.get_style_context()
        if sid in self._ai_selected_subagents:
            self._ai_selected_subagents.discard(sid)
            ctx.remove_class("subagent-block-selected")
        else:
            self._ai_selected_subagents.add(sid)
            ctx.add_class("subagent-block-selected")
        return True  # Stop event propagation to prevent FlowBox default behavior

    def _on_subagent_child_activated(self, flowbox, child):
        """Handle child activation signal from FlowBox to toggle selection."""
        sid = None
        for k, v in self._ai_subagent_blocks.items():
            if v[0] == child:
                sid = k
                break
        if sid is not None:
            self._on_subagent_block_click(sid)

    def _on_send_clicked(self, _btn=None):
        # Check for pending AskUserQuestion first — must precede streaming check
        ask_state = getattr(self, "_ai_ask_user_state", None)
        if ask_state is not None:
            buf = self._ai_entry.get_buffer()
            start = buf.get_start_iter()
            end = buf.get_end_iter()
            text = buf.get_text(start, end, True).strip()
            if not text:
                return
            buf.set_text("")
            self._ai_entry.placeholder_text = "输入后续问题..."
            self._update_send_button(False)

            if text in ("/cancel", "/abort"):
                safe_q = html.escape(ask_state["question"])
                self.append_html_to_webview(
                    f'<div class="chat-system-error">❌ 已取消问题：「{safe_q}」</div>'
                )
                ask_state["answer"] = ""
                ask_state["event"].set()
                self._update_send_button(True)
                self._ai_entry.placeholder_text = "输入后续问题..."
                return

            # If user types a system command while waiting, cancel the question
            text_cmd = text.split()[0] if text else ""
            known_cmds = {cmd for cmd, _ in self._AI_COMMANDS}
            if text_cmd in known_cmds:
                cmd_name = html.escape(text_cmd)
                self.append_html_to_webview(
                    f'<div class="chat-system-error">❌ 问题已取消（检测到系统命令「{cmd_name}」）。'
                    f'请重新输入命令。</div>'
                )
                ask_state["answer"] = ""
                ask_state["event"].set()
                self._update_send_button(True)
                self._ai_entry.placeholder_text = "输入后续问题..."
                return

            ask_state["answer"] = text
            ask_state["event"].set()
            return

        if self._ai_streaming:
            active_state = self._ai_running_convs.get(self._ai_conversation_id)
            if active_state:
                active_state["cancel_event"].set()
                self._ai_running_convs.pop(self._ai_conversation_id, None)
            self._llm_client.cancel_active_request()
            self._update_send_button(False, sensitive=False)
            self._ai_entry.placeholder_text = "正在中止..."
            self._ai_spinner.stop()
            self._ai_spinner.hide()
            self._ai_streaming = False
            return
        buf = self._ai_entry.get_buffer()
        start = buf.get_start_iter()
        end = buf.get_end_iter()
        text = buf.get_text(start, end, True).strip()
        if text:
            if not self._ai_history_queries or self._ai_history_queries[-1] != text:
                self._ai_history_queries.append(text)
            self._ai_history_index = -1
            self._ai_current_draft = ""
        # Allow send with empty text if there is a pending image or selected sub-agents
        if not text and not self._ai_pending_image_data_uri and not self._ai_selected_subagents:
            return
        if text == "/new":
            buf.set_text("")
            self.start_new_conversation()
            return
        if text == "/delete":
            buf.set_text("")
            conv_id = self._ai_conversation_id
            if conv_id:
                self._conversation_store.delete_conversation(conv_id)
                self._ai_html_cache.pop(conv_id, None)
            self._reset_ai_panel_silent()
            return
        if text == "/retry":
            buf.set_text("")
            self._handle_retry_command()
            return
        if text == "/rollback":
            buf.set_text("")
            self._handle_rollback_command()
            return
        if text == "/title":
            buf.set_text("")
            self._handle_title_command("")
            return
        if text.startswith("/title "):
            buf.set_text("")
            title_text = text[len("/title "):].strip()
            self._handle_title_command(title_text)
            return
        if text == "/model":
            buf.set_text("")
            # 在 WebView 中显示当前模型信息
            model_info = getattr(self, "_ai_active_model_info", None)
            if model_info:
                alias = model_info.get("alias", "?")
                mname = model_info.get("model_name", "?")
                info_html = (
                    f'<div class="chat-model-info">'
                    f'📋 当前模型: <strong>{alias}</strong> ({mname})<br/>'
                    f'<span>输入 /model &lt;别名&gt; 快速切换</span></div>'
                )
                self.append_html_to_webview(info_html)
            self._show_model_selector()
            return
        if text.startswith("/model "):
            buf.set_text("")
            self._switch_model_by_alias(text[len("/model "):].strip())
            return
        if text == "/cd":
            buf.set_text("")
            self._select_and_set_bash_cwd()
            return
        if text.startswith("/cd "):
            buf.set_text("")
            arg = text[len("/cd "):].strip()
            from tool_registry import set_bash_cwd
            result = set_bash_cwd(arg, session_key=self._ai_conversation_id)
            self.append_html_to_webview(
                f'<div class="chat-status-notice">{html.escape(result)}</div>'
            )
            return
        # Handle selected sub-agent blocks: build notification text and send
        if self._ai_selected_subagents:
            from tool_registry import get_subagent_status_map, check_background_subagents
            # Drain any pending background results first
            check_background_subagents()
            parts = []
            for sid in sorted(self._ai_selected_subagents):
                info = get_subagent_status_map().get(sid, {})
                task_desc = info.get("task", "未知任务")
                parts.append(
                    f"后台子代理 {sid} 已完成\n"
                    f"任务: {task_desc}\n"
                    f"结果文件: /tmp/opencode_subagent_{sid}_result.txt"
                )
            bg_text = "\n\n---\n\n".join(parts)
            if text:
                text = f"{bg_text}\n\n---\n\n{text}"
            else:
                text = bg_text

            # Clean up selected blocks — hide bar and remove styling FIRST to avoid visual flash
            from tool_registry import remove_subagent_status
            self._ai_subagent_bar.get_style_context().remove_class("subagent-status-bar")
            self._ai_subagent_bar.hide()
            for sid in list(self._ai_selected_subagents):
                entry = self._ai_subagent_blocks.get(sid)
                if entry:
                    child, _event_box, _box = entry
                    self._ai_subagent_bar.remove(child)
                self._ai_subagent_blocks.pop(sid, None)
                remove_subagent_status(sid)
            self._ai_selected_subagents.clear()
            self._update_subagent_bar_visibility()

            buf.set_text("")
            self._send_user_message(text)
            self._remove_pending_image()
            return

        buf.set_text("")
        self._send_user_message(text)
        self._remove_pending_image()

    def _switch_model_by_alias(self, alias: str):
        """Switch AI model by alias. Updates active model info and header label."""
        # 大小写不敏感匹配，兼容用户输入与存储别名的大小写差异（如 /model GPT-4 匹配 gpt-4）
        model = next((m for m in self._llm_settings_store.models if m.alias.lower() == alias.lower()), None)
        if not model:
            lines = [f"❌ 未找到模型别名 **\"{alias}\"**。\n", "可用模型:\n"]
            for m in self._llm_settings_store.models:
                lines.append(f"- **{m.alias}**" + (" (默认)" if m.is_default else "") + f" — `{m.model_name}`")
            lines.append("\n前往 **Prompts Config → ⚙️ API Settings** 管理模型配置。")
            error_msg = "\n".join(lines)
            html = _markdown_to_html_safe(
                error_msg,
                fallback_content=f"<p>Model '{alias}' not found</p>"
            )
            self.append_html_to_webview(html)
            return

        self._ai_active_model_info = {
            "alias": model.alias,
            "base_url": model.base_url.strip(),
            "model_name": model.model_name.strip(),
            "temperature": model.temperature,
            "max_tokens": model.max_tokens,
            "top_p": model.top_p,
        }
        self._ai_last_prompt_obj = None  # manual switch overrides prompt binding

        display_name = f"{model.alias} ({model.model_name})"
        self._ai_lbl.set_markup(
            f"<b>AI 助手看盘</b>\n<span size='small' foreground='#888888'>({display_name})</span>"
        )
        notice_html = (
            f'<div class="chat-status-notice">'
            f'🔄 已切换至 <strong>{model.alias}</strong> ({model.model_name})</div>'
        )
        self.append_html_to_webview(notice_html)

    def _cancel_streaming_if_active(self):
        """If a streaming response is in progress, cancel it and reset state."""
        if self._ai_streaming:
            self._ai_cancel_event.set()
            self._llm_client.cancel_active_request()
            # Preserve partial assistant content before resetting state
            partial = getattr(self, "_ai_current_assistant_text", "")
            if partial.strip():
                self._ai_messages.append({"role": "assistant", "content": partial})
            self._update_send_button(False)
            self._ai_streaming = False
            self._ai_spinner.stop()
            self._ai_spinner.hide()

    def _re_render_after_tool_cancel(self):
        """Re-render and save conversation after tool result appended post-cancel."""
        if self._ai_streaming:
            return
        self._ai_markdown_text = self._rebuild_markdown_from_messages(self._ai_messages)
        self._render_markdown(self._ai_markdown_text)
        try:
            self._save_current_conversation(self._build_model_snapshot())
        except Exception:
            pass

    def _build_conversation_rounds(self, msgs: list) -> list:
        """将消息列表聚合为以 user 提问为起点的轮次结构列表。

        每个元素形如 {"user_idx": int, "user_msg": str|list, "asst_msg": str|list}。
        工具调用消息（role=tool）和中间 assistant 片段会被跳过，以第一个出现的
        非空 assistant 消息作为该轮的 asst_msg。
        """
        rounds = []
        for idx, m in enumerate(msgs):
            role = m.get("role")
            if role == "user":
                rounds.append({
                    "user_idx": idx,
                    "user_msg": m.get("content", ""),
                    "asst_msg": ""
                })
            elif role == "assistant" and rounds:
                rounds[-1]["asst_msg"] = m.get("content", "")
        return rounds

    def _handle_retry_command(self):
        self._cancel_streaming_if_active()

        msgs = self._ai_messages
        if not msgs:
            return

        # 逆向寻找到最后一个用户提问的节点
        user_index = len(msgs) - 1
        while user_index >= 0 and msgs[user_index].get("role") != "user":
            user_index -= 1

        if user_index < 0:
            return

        user_content = msgs[user_index].get("content", "")
        if isinstance(user_content, list):
            # 针对多模态列表，提取文本片段
            last_user_content = next(
                (p["text"] for p in user_content if isinstance(p, dict) and p.get("type") == "text"),
                ""
            )
        else:
            last_user_content = user_content

        # 将历史消息完全回滚到该用户提问前的状态
        self._ai_messages = msgs[:user_index]

        buf = self._ai_entry.get_buffer()
        buf.set_text(last_user_content)
        buf.place_cursor(buf.get_end_iter())

        self._ai_markdown_text = self._rebuild_markdown_from_messages(self._ai_messages)
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript("_autoScroll = true;", None, None)
        self._render_markdown(self._ai_markdown_text)
        self._save_current_conversation()

    def _handle_rollback_command(self):
        self._cancel_streaming_if_active()

        msgs = self._ai_messages
        rounds = self._build_conversation_rounds(msgs)

        if not rounds:
            self.append_html_to_webview(
                '<div class="chat-system-error">'
                '⚠️ 没有可回滚的对话轮次。请先进行对话。</div>'
            )
            return

        try:
            html_val = self._build_round_cards_html(rounds)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.append_html_to_webview(
                f'<div class="chat-system-error">'
                f'❌ 生成回滚列表时出错: {html.escape(str(e))}</div>'
            )
            return

        self.append_html_to_webview(html_val)

    def _handle_title_command(self, title_text: str):
        """Handle /title command: set custom title or regenerate via LLM.

        Called from _on_send_clicked (GTK signal callback, main thread).
        Mode 2 sets title inline; Mode 1 spawns a background thread for LLM call.
        """
        if not self._ai_conversation_id or not self._ai_messages:
            self.append_html_to_webview(
                '<div class="chat-simple-error">没有活跃的对话可供设置标题。</div>'
            )
            return

        if title_text:
            # Mode 2: manual title — set immediately
            self._ai_title_generated = True
            self._on_title_generated(self._ai_conversation_id, title_text)
            escaped = html.escape(title_text)
            self.append_html_to_webview(
                f'<div class="chat-simple-info">标题已设置为: {escaped}</div>'
            )
        else:
            # Mode 1: generate via LLM using first 3 rounds
            self._cancel_streaming_if_active()

            context_msgs = self._ai_messages[:6]
            context_lines = []
            for m in context_msgs:
                role = "User" if m.get("role") == "user" else "Assistant"
                content = m.get("content", "")
                context_lines.append(f"{role}: {content}")
            context_text = "\n\n".join(context_lines)

            if not context_text.strip():
                self.append_html_to_webview(
                    '<div class="chat-simple-error">对话内容为空，无法生成标题。</div>'
                )
                return

            try:
                title_cfg = self._get_title_model_config()
                if title_cfg:
                    base_url, api_key, model_name, temperature, max_tokens, top_p = title_cfg
                else:
                    base_url, api_key, model_name, _, temperature, max_tokens, top_p = self._read_model_config(
                        self._ai_last_prompt_obj,
                        getattr(self, "_ai_active_model_info", None)
                    )
            except Exception:
                base_url = ""
                api_key = ""

            if base_url and api_key:
                self._ai_title_generated = True
                self._ai_pending_title_notification = True
                self.append_html_to_webview(
                    '<div class="chat-simple-info">正在根据对话内容重新生成标题...</div>'
                )
                threading.Thread(
                    target=self._generate_title_from_context,
                    args=(context_text, self._ai_conversation_id, base_url, api_key, model_name,
                          temperature, max_tokens, top_p),
                    daemon=True
                ).start()
            else:
                self.append_html_to_webview(
                    '<div class="chat-simple-error">LLM 配置不完整，无法生成标题。</div>'
                )

    def _rollback_to_round(self, round_index: int):
        msgs = self._ai_messages

        rounds = self._build_conversation_rounds(msgs)
        total_rounds = len(rounds)
        next_round_idx = round_index + 1
        if next_round_idx >= total_rounds:
            return

        target_user_idx = rounds[next_round_idx]["user_idx"]
        user_content = msgs[target_user_idx].get("content", "")
        if isinstance(user_content, list):
            discarded = next(
                (p["text"] for p in user_content if isinstance(p, dict) and p.get("type") == "text"),
                ""
            )
        else:
            discarded = user_content

        self._ai_messages = msgs[:target_user_idx]
        buf = self._ai_entry.get_buffer()
        buf.set_text(discarded)
        buf.place_cursor(buf.get_end_iter())

        self._ai_markdown_text = self._rebuild_markdown_from_messages(self._ai_messages)
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript("_autoScroll = true;", None, None)
        self._render_markdown(self._ai_markdown_text)
        try:
            model_snapshot = self._build_model_snapshot()
            self._save_current_conversation(model_snapshot)
        except Exception as e:
            print(f"Error saving conversation after rollback: {e}", flush=True)

    def _build_round_cards_html(self, rounds):
        """Build HTML displaying conversation rounds as clickable cards."""
        def _strip_html(text):
            return re.sub(r'<[^>]+>', '', text).strip()

        cards_html = []
        total_rounds = len(rounds)
        for i, rd in enumerate(rounds):
            user_msg = rd["user_msg"]
            asst_msg = rd["asst_msg"]
            if isinstance(user_msg, list):
                user_msg = _vision_content_to_text(user_msg)
            if isinstance(asst_msg, list):
                asst_msg = _vision_content_to_text(asst_msg)
            _u = _strip_html(user_msg)
            _a = _strip_html(asst_msg)
            user_preview = html.escape(_u[:80] + ("..." if len(_u) > 80 else ""))
            asst_preview = html.escape(_a[:80] + ("..." if len(_a) > 80 else ""))
            is_last = (i == total_rounds - 1)
            round_label = f"第 {i + 1} 轮" + ("（当前）" if is_last else "")
            if is_last:
                action_html = '<span class="rollback-current-tag">← 当前</span>'
            else:
                action_html = (
                    f'<button onclick="window.location=\'opencode://rollback-round?round={i}\'" '
                    f'class="rollback-btn">↩ 回滚到此</button>'
                )
            cards_html.append(
                f'<div class="rollback-card">'
                f'<div class="rollback-card-header">'
                f'<span class="rollback-round-label">{round_label}</span>'
                f'{action_html}</div>'
                f'<div class="rollback-user-preview">You: {user_preview}</div>'
                f'<div class="rollback-asst-preview">AI: {asst_preview}</div></div>'
            )

        rollback_html = (
            f'<div class="rollback-panel">'
            f'<div class="rollback-title">══ 对话回滚 ══ '
            f'<span>共 {total_rounds} 轮</span>'
            f'</div>{"".join(cards_html)}'
            f'<div style="text-align:right; margin-top:4px;">'
            f'<span class="rollback-close-btn" '
            f'onclick="this.closest(\'.rollback-panel\').style.display=\'none\';">'
            f'[× 关闭]</span></div></div>'
        )
        return rollback_html

    def _show_model_selector(self):
        for old in self._ai_model_listbox.get_children():
            self._ai_model_listbox.remove(old)

        for m in self._llm_settings_store.models:
            row = Gtk.ListBoxRow()
            row.model_alias = m.alias
            hbox = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
            hbox.set_margin_start(8)
            hbox.set_margin_end(8)
            hbox.set_margin_top(6)
            hbox.set_margin_bottom(6)

            name_lbl = Gtk.Label.new(m.alias)
            name_lbl.set_xalign(0)
            name_lbl.set_markup(f"<b>{m.alias}</b>")
            if m.is_default:
                default_lbl = Gtk.Label.new("(默认)")
                default_lbl.get_style_context().add_class("model-default-tag")
                default_lbl.set_opacity(0.9)
                hbox.pack_start(default_lbl, False, False, 0)

            detail_lbl = Gtk.Label.new(m.model_name)
            detail_lbl.set_xalign(1)
            detail_lbl.set_opacity(0.6)

            hbox.pack_start(name_lbl, True, True, 0)
            hbox.pack_start(detail_lbl, False, False, 0)
            row.add(hbox)
            self._ai_model_listbox.add(row)

        self._ai_model_listbox.show_all()
        # 高亮当前正在使用的模型
        current_alias = (getattr(self, "_ai_active_model_info", None) or {}).get("alias")
        target_row = None
        if current_alias:
            for child in self._ai_model_listbox.get_children():
                if getattr(child, "model_alias", None) == current_alias:
                    target_row = child
                    break
        if not target_row:
            target_row = self._ai_model_listbox.get_row_at_index(0)
        if target_row:
            self._ai_model_listbox.select_row(target_row)

        child = self._ai_model_popover.get_child()
        if child:
            child.show_all()
        self._ai_model_popover.popup()
        self._ai_model_listbox.grab_focus()

    def _hide_model_selector(self):
        if not self._ai_model_popover.get_visible():
            return
        self._ai_model_popover.popdown()

    def _on_model_popover_closed(self, popover):
        self._ai_entry.grab_focus()

    def _on_model_selector_activated(self, listbox, row):
        if not row:
            return
        alias = row.model_alias
        self._hide_model_selector()
        self._switch_model_by_alias(alias)

    def _on_ai_entry_key_press(self, widget, event):
        keyname = Gdk.keyval_name(event.keyval)
        is_shift = (event.state & Gdk.ModifierType.SHIFT_MASK) != 0
        is_ctrl = (event.state & Gdk.ModifierType.CONTROL_MASK) != 0

        if self._ai_cmd_popover is not None and self._ai_cmd_popover.is_visible():
            if keyname in ("Up", "KP_Up"):
                current = self._ai_cmd_popover.listbox.get_selected_row()
                if current:
                    above = current.get_prev_sibling()
                    if above:
                        self._ai_cmd_popover.listbox.select_row(above)
                return True
            if keyname in ("Down", "KP_Down"):
                current = self._ai_cmd_popover.listbox.get_selected_row()
                if current:
                    below = current.get_next_sibling()
                    if below:
                        self._ai_cmd_popover.listbox.select_row(below)
                else:
                    first = self._ai_cmd_popover.listbox.get_row_at_index(0)
                    if first:
                        self._ai_cmd_popover.listbox.select_row(first)
                return True
            if keyname in ("Return", "KP_Enter"):
                self._ai_cmd_popover.confirm_command_completion()
                return True
            if keyname == "Tab":
                self._ai_cmd_popover.confirm_command_completion()
                return True
            if keyname == "Escape":
                self._ai_cmd_popover.dismiss()
                return True
            return False

        if keyname == "Tab":
            buf = self._ai_entry.get_buffer()
            start = buf.get_start_iter()
            end = buf.get_end_iter()
            text = buf.get_text(start, end, True).strip()
            if text.startswith("/") and " " not in text:
                search = text.lstrip("/")
                matches = [cmd for cmd, _ in self._AI_COMMANDS if cmd.startswith("/" + search)]
                if len(matches) == 1:
                    buf.set_text(matches[0] + " ")
                    buf.place_cursor(buf.get_end_iter())
                    return True
                elif len(matches) > 1:
                    self._rebuild_command_popover(text)
                    return True
            return False

        if is_ctrl and keyname in ("l", "L"):
            self._reset_ai_panel_silent()
            return True

        if keyname in ("Up", "KP_Up", "Down", "KP_Down"):
            buf = self._ai_entry.get_buffer()
            start = buf.get_start_iter()
            end = buf.get_end_iter()
            text_val = buf.get_text(start, end, True)
            cursor_iter = buf.get_iter_at_mark(buf.get_insert())
            
            cursor_line = cursor_iter.get_line()
            total_lines = buf.get_line_count()

            if keyname in ("Up", "KP_Up") and cursor_line == 0:
                if self._ai_history_queries:
                    if self._ai_history_index == -1:
                        self._ai_current_draft = text_val
                        self._ai_history_index = len(self._ai_history_queries) - 1
                    elif self._ai_history_index > 0:
                        self._ai_history_index -= 1
                    
                    hist_text = self._ai_history_queries[self._ai_history_index]
                    buf.set_text(hist_text)
                    buf.place_cursor(buf.get_end_iter())
                    return True
            
            elif keyname in ("Down", "KP_Down") and cursor_line == total_lines - 1:
                if self._ai_history_index != -1:
                    if self._ai_history_index < len(self._ai_history_queries) - 1:
                        self._ai_history_index += 1
                        hist_text = self._ai_history_queries[self._ai_history_index]
                        buf.set_text(hist_text)
                    else:
                        self._ai_history_index = -1
                        buf.set_text(self._ai_current_draft)
                    buf.place_cursor(buf.get_end_iter())
                    return True

        is_enter = keyname in ("Return", "KP_Enter")
        if not is_enter:
            return False

        # Shift+Enter (without Ctrl) → newline
        if is_shift and not is_ctrl:
            return False

        try:
            self._on_send_clicked()
        except Exception as e:
            print(f"[key-press] send error: {e}", flush=True)
        return True

    def _on_ai_entry_button_press(self, widget, event):
        if event.button != 3:
            return False
        menu = Gtk.Menu.new()
        paste_item = Gtk.MenuItem.new_with_label("粘贴")
        paste_item.connect("activate", lambda *_: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).request_text(
            lambda clip, text: widget.get_buffer().insert_at_cursor(text if text else "")
        ))
        menu.append(paste_item)
        copy_item = Gtk.MenuItem.new_with_label("复制")
        copy_item.connect("activate", lambda *_: widget.emit("copy-clipboard"))
        menu.append(copy_item)
        select_all = Gtk.MenuItem.new_with_label("全选")
        select_all.connect("activate", lambda *_: widget.emit("select-all", True))
        menu.append(select_all)
        if self.on_menu_shown:
            self.on_menu_shown()
        menu.connect("deactivate", lambda *_: GLib.timeout_add(300, self._on_ai_menu_deactivated))
        menu.show_all()
        menu.popup(None, None, None, None, event.button, event.time)
        return True

    def _on_ai_menu_deactivated(self):
        if self.on_menu_hidden:
            self.on_menu_hidden()
        return False

    def _on_ai_entry_paste_clipboard(self, entry):
        """Fires on any paste operation into the AI entry.

        Does NOT block text paste. Schedules an async check for clipboard image.
        """
        GLib.idle_add(self._async_check_clipboard_image)
        return False

    def _async_check_clipboard_image(self):
        threading.Thread(target=self._do_capture_clipboard_image, daemon=True).start()
        return False

    def _do_capture_clipboard_image(self):
        from clipboard_store import _capture_image
        image_data = _capture_image()
        if not image_data:
            return
        h = hashlib.sha256(image_data).hexdigest()[:16]
        img_dir = os.path.join(CONFIG_DIR, "images")
        try:
            os.makedirs(img_dir, exist_ok=True)
            img_path = os.path.join(img_dir, f"{h}.png")
            if not os.path.exists(img_path):
                with open(img_path, "wb") as f:
                    f.write(image_data)
            data_uri = _image_to_data_uri(img_path)
            if data_uri:
                GLib.idle_add(self.set_pending_image, h, img_path, data_uri)
        except Exception:
            pass

    def set_pending_image(self, img_hash: str, img_path: str, data_uri: str):
        self._ai_pending_image_hash = img_hash
        self._ai_pending_image_path = img_path
        self._ai_pending_image_data_uri = data_uri
        self._show_attachment_bar()

    def _remove_pending_image(self):
        self._ai_pending_image_hash = None
        self._ai_pending_image_path = None
        self._ai_pending_image_data_uri = None
        self._hide_attachment_bar()

    def _show_attachment_bar(self):
        if not self._ai_pending_image_path or not os.path.isfile(self._ai_pending_image_path):
            return
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(
                self._ai_pending_image_path, 60, 60
            )
            self._ai_attach_thumb.set_from_pixbuf(pixbuf)
        except Exception:
            self._ai_attach_thumb.clear()
        fname = os.path.basename(self._ai_pending_image_path)
        self._ai_attach_label.set_text(f"📎 {fname}")
        
        # Explicitly show the container and its children because set_no_show_all(True) blocks show_all()
        self._ai_attachment_bar.show()
        self._ai_attach_thumb.show()
        self._ai_attach_label.show()
        self._ai_attach_remove_btn.show()
        self.queue_resize()

    def _hide_attachment_bar(self):
        self._ai_attachment_bar.hide()
        self.queue_resize()

    def _on_ai_attach_btn_clicked(self, _btn):
        dialog = Gtk.FileChooserDialog(
            title="选择图片",
            parent=self.get_toplevel(),
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.ACCEPT
        )

        dialog.connect("show", lambda *_: self.on_dialog_shown and self.on_dialog_shown())
        dialog.connect("destroy", lambda *_: self.on_dialog_hidden and self.on_dialog_hidden())

        filter_image = Gtk.FileFilter()
        filter_image.set_name("图片文件 (png/jpg/jpeg/webp)")
        filter_image.add_mime_type("image/png")
        filter_image.add_mime_type("image/jpeg")
        filter_image.add_mime_type("image/webp")
        dialog.add_filter(filter_image)

        response = dialog.run()
        if response == Gtk.ResponseType.ACCEPT:
            filename = dialog.get_filename()
            dialog.destroy()
            if filename:
                self._attach_image_from_file(filename)
        else:
            dialog.destroy()

    def _attach_image_from_file(self, filepath: str):
        def do_background_attach():
            try:
                with open(filepath, "rb") as f:
                    image_data = f.read()
                h = hashlib.sha256(image_data).hexdigest()[:16]
                img_dir = os.path.join(CONFIG_DIR, "images")
                os.makedirs(img_dir, exist_ok=True)
                ext = os.path.splitext(filepath)[1].lower()
                if ext not in [".png", ".jpg", ".jpeg", ".webp"]:
                    ext = ".png"
                img_path = os.path.join(img_dir, f"{h}{ext}")
                if not os.path.exists(img_path):
                    with open(img_path, "wb") as f:
                        f.write(image_data)
                data_uri = _image_to_data_uri(img_path)
                if data_uri:
                    GLib.idle_add(self.set_pending_image, h, img_path, data_uri)
            except Exception:
                pass

        threading.Thread(target=do_background_attach, daemon=True).start()

    def _on_ai_entry_drag_data_received(self, widget, context, x, y, selection_data, info, time):
        uris = selection_data.get_uris()
        if uris:
            for uri in uris:
                parsed = urlparse(uri)
                if parsed.scheme == "file":
                    filepath = urllib.parse.unquote(parsed.path)
                    mime_type, _ = mimetypes.guess_type(filepath)
                    if mime_type and mime_type.startswith("image/"):
                        self._attach_image_from_file(filepath)
                        widget.stop_emission_by_name("drag-data-received")
                        context.finish(True, False, time)
                        return
        context.finish(False, False, time)

    def _on_ai_entry_changed(self):
        buf = self._ai_entry.get_buffer()
        start = buf.get_start_iter()
        end = buf.get_end_iter()
        text = buf.get_text(start, end, True).strip()

        if text.startswith("/") and " " not in text:
            self._ai_cmd_popover.rebuild(text)
        else:
            self._ai_cmd_popover.dismiss()

    @staticmethod
    def _rebuild_markdown_from_messages(
        messages: List[Dict],
        streaming_reasoning: str = "",
        streaming_content: str = "",
        is_streaming: bool = False
    ) -> str:
        """Convert OpenAI-format message list back to rendered markdown text."""
        return _rebuild_markdown_from_messages(
            messages,
            streaming_reasoning=streaming_reasoning,
            streaming_content=streaming_content,
            is_streaming=is_streaming
        )

    def _prune_messages(self):
        # Read latest values from shared settings store (supports live UI changes)
        if self._ai_settings_store is not None:
            soft_limit = self._ai_settings_store.soft_limit
            trim_target = self._ai_settings_store.trim_target
            enable_summary = self._ai_settings_store.enable_summary
            summary_threshold = self._ai_settings_store.summary_threshold
        else:
            _fallback = AISettingsStore()
            soft_limit = _fallback.soft_limit
            trim_target = _fallback.trim_target
            enable_summary = _fallback.enable_summary
            summary_threshold = _fallback.summary_threshold

        if len(self._ai_messages) <= soft_limit:
            return

        # 摘要压缩：在丢弃之前先压缩为摘要
        if enable_summary and not self._ai_summary_generating:
            first = self._ai_messages[:1]
            rest = self._ai_messages[1:]
            target_len = trim_target - 1
            start_idx = len(rest) - target_len
            if start_idx < 0:
                start_idx = 0
            pruned = rest[:start_idx]
            if pruned and len(pruned) >= summary_threshold:
                self._ai_summary_generating = True
                self._show_summary_status()
                threading.Thread(
                    target=self._generate_summary_async,
                    args=(list(pruned), trim_target),
                    daemon=True
                ).start()
                return

        self._apply_prune(trim_target)

    def _apply_prune(self, trim_target: int, save_summary: bool = False):
        """根据 trim_target 从当前 _ai_messages 重新计算裁剪位置。

        使用当前 _ai_messages 实时计算，避免异步回调中引用过期导致数据丢失。
        """
        if len(self._ai_messages) <= 1:
            self._clear_summary_status()
            return
        first = self._ai_messages[:1]
        rest = self._ai_messages[1:]
        target_len = trim_target - 1
        start_idx = len(rest) - target_len
        if start_idx < 0:
            start_idx = 0

        # Adjust start_idx backward if it lands on a "tool" message to keep
        # the tool call sequence intact.
        while start_idx > 0 and rest[start_idx].get("role") == "tool":
            start_idx -= 1

        # If we reached the very beginning (start_idx == 0) and rest[0] is still "tool",
        # it means the initiating assistant message was pruned. To prevent sending
        # orphan tool messages (which crashes the API), we must move start_idx forward
        # past the block of tool messages.
        if start_idx == 0 and rest and rest[0].get("role") == "tool":
            while start_idx < len(rest) and rest[start_idx].get("role") == "tool":
                start_idx += 1

        self._ai_messages = first + rest[start_idx:]
        self._ai_markdown_text = self._rebuild_markdown_from_messages(self._ai_messages)
        self._render_markdown(self._ai_markdown_text)
        if save_summary:
            self._save_summary_to_conversation()
        self._clear_summary_status()

    def _generate_summary_async(self, pruned_messages: list, trim_target: int):
        """在后台线程中调用 LLM，将即将丢弃的消息压缩为摘要。"""
        save_summary = False
        try:
            max_chars = (self._ai_settings_store.summary_max_chars
                         if self._ai_settings_store else 500)

            convo_lines = []
            for m in pruned_messages:
                role = m.get("role", "unknown")
                content = m.get("content", "")
                if isinstance(content, list):
                    content = str(content)
                content_str = str(content)
                if len(content_str) > 500:
                    content_str = content_str[:500] + "...(截断)"
                convo_lines.append(f"{role.upper()}: {content_str}")

            convo_text = "\n".join(convo_lines)

            prev = f"已有摘要：\n{self._ai_summary}\n\n" if self._ai_summary else ""
            prompt = (
                f"{prev}请将以下对话压缩为简洁摘要，保留用户需求、决策、偏好、"
                f"关键约定（如代码风格、命名规范）和已取得的进展：\n\n"
                f"{convo_text}\n\n"
                f"要求：第三人称、客观简洁、不超过{max_chars}字。"
            )

            base_url, api_key, model_name, _, temperature, max_tokens, top_p = \
                self._read_model_config(self._ai_last_prompt_obj,
                                        getattr(self, "_ai_active_model_info", None))

            if not base_url or not api_key or not model_name:
                print(f"[summary] 模型配置不完整，跳过摘要生成", flush=True)
                return

            print(f"[summary] 开始生成摘要 (已丢弃 {len(pruned_messages)} 条消息, max_chars={max_chars}, model={model_name}, prompt_len={len(prompt)}字)", flush=True)
            result = self._call_llm_sync(
                [{"role": "user", "content": prompt}],
                base_url, api_key, model_name,
                timeout=30,
                temperature=0.3,
                # max_tokens 需预留推理模型的 reasoning token 空间
                max_tokens=max(4096, max_chars * 4),
                top_p=top_p,
            )

            if result:
                result = result.strip()
                if self._ai_summary:
                    self._ai_summary = (
                        f"{self._ai_summary}\n"
                        f"后续对话摘要：{result}"
                    )
                else:
                    self._ai_summary = result
                if len(self._ai_summary) > max_chars * 3:
                    self._ai_summary = self._ai_summary[-max_chars * 3:]
                save_summary = True
                print(f"[summary] 摘要生成成功 ({len(result)} 字符)", flush=True)
            else:
                print(f"[summary] LLM 返回空结果，摘要未生成 (model={model_name})", flush=True)

        except Exception as e:
            print(f"[summary] 摘要生成失败: {e}", flush=True)
            import traceback
            traceback.print_exc()
        finally:
            self._ai_summary_generating = False
            GLib.idle_add(self._apply_prune, trim_target, save_summary)

    def _save_summary_to_conversation(self):
        """在主线程中仅保存摘要到对话文件，不重建消息列表。"""
        try:
            if not self._ai_conversation_id:
                return
            conv = self._conversation_store.load_conversation(self._ai_conversation_id)
            if conv:
                conv.summary = self._ai_summary
                self._conversation_store.save_conversation(conv, bump_updated_at=False)
        except Exception as e:
            print(f"Error saving summary to conversation: {e}", flush=True)

    def _show_summary_status(self):
        self._ai_entry.set_sensitive(False)
        self._ai_send_btn.set_sensitive(False)
        self._ai_entry.placeholder_text = "摘要压缩中..."
        self.append_html_to_webview(
            '<div id="summary-status" class="summary-status">'
            '⏳ 正在压缩历史对话为摘要...</div>'
        )

    def _clear_summary_status(self):
        self._ai_entry.set_sensitive(True)
        self._ai_send_btn.set_sensitive(True)
        self._ai_entry.placeholder_text = "输入后续问题..."
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(
                "var e=document.getElementById('summary-status');if(e)e.remove();",
                None, None
            )

    def append_html_to_webview(self, html: str):
        """Insert HTML snippet before end of content div and scroll to bottom."""
        escaped = json.dumps(html)
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(
                f"document.getElementById('content').insertAdjacentHTML('beforeend', {escaped});"
                f"_scrollToBottom();",
                None, None
            )

    def _build_model_snapshot(self) -> Dict[str, Any]:
        """Build a model_config_snapshot from active model info or resolved config."""
        active = getattr(self, "_ai_active_model_info", None)
        if active:
            return dict(active)  # shallow copy to prevent caller from mutating _ai_active_model_info
        base_url, api_key, model_name, _, temperature, max_tokens, top_p = self._read_model_config(
            self._ai_last_prompt_obj, None
        )
        return {
            "alias": "Default",
            "base_url": base_url,
            "model_name": model_name,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
        }

    def _save_current_conversation(self, model_snapshot: Dict[str, Any],
                                    preserve_updated_at: bool = False):
        """Save or update the current active conversation to the store, preserving its title."""
        local_title = "New Conversation"
        if self._ai_messages:
            local_title = _extract_local_title(
                self._ai_messages[0].get("content", "")
            )

        if not self._ai_conversation_id:
            now = int(time.time() * 1000)
            self._ai_conversation_created_at = now
            conv = self._conversation_store.create_conversation(
                title=local_title,
                model_config=model_snapshot
            )
            self._ai_conversation_id = conv.id
            conv.messages = [_dict_to_chat_message(m) for m in self._ai_messages]
            conv.summary = self._ai_summary
            self._conversation_store.save_conversation(conv, bump_updated_at=not preserve_updated_at)
        else:
            conv = self._conversation_store.load_conversation(self._ai_conversation_id)
            if conv:
                conv.messages = [_dict_to_chat_message(m) for m in self._ai_messages]
                conv.model_config_snapshot = model_snapshot
                if not self._ai_summary_generating:
                    conv.summary = self._ai_summary
            else:
                conv = Conversation(
                    id=self._ai_conversation_id,
                    title=local_title,
                    system_prompt="",
                    messages=[_dict_to_chat_message(m) for m in self._ai_messages],
                    model_config_snapshot=model_snapshot,
                    created_at=self._ai_conversation_created_at,
                    updated_at=int(time.time() * 1000),
                )
            self._conversation_store.save_conversation(conv, bump_updated_at=not preserve_updated_at)

        if self._ai_conversation_id:
            self._ai_html_cache[self._ai_conversation_id] = getattr(self, "_last_rendered_html", "")

    def _switch_to_conversation(self, conv_id: str):
        """Switch AI panel to display a different conversation by ID."""
        if not hasattr(self, "_ai_request_id"):
            self._ai_request_id = 0
        self._ai_request_id += 1

        # Save current conversation if it has content and is not streaming
        if self._ai_messages and self._ai_conversation_id:
            is_currently_running = self._ai_running_convs.get(self._ai_conversation_id, {}).get("streaming", False)
            if not is_currently_running:
                try:
                    model_snapshot = self._build_model_snapshot()
                    self._save_current_conversation(model_snapshot, preserve_updated_at=True)
                except Exception as e:
                    print(f"Error saving before switch: {e}", flush=True)

        self._clear_subagent_bar_instantly()

        # Cancel any pending render timeout
        if getattr(self, "_ai_render_timeout_id", 0) != 0:
            GLib.source_remove(self._ai_render_timeout_id)
            self._ai_render_timeout_id = 0

        # Load target conversation
        conv = self._conversation_store.load_conversation(conv_id)
        if not conv:
            return

        # Restore state from loaded conversation (preserve tool call fields)
        st = self._ai_running_convs.get(conv_id)
        if st and st.get("streaming"):
            self._ai_messages = st["messages"]
            self._ai_conversation_id = conv_id
            self._ai_conversation_created_at = conv.created_at
            self._ai_summary = conv.summary if conv else ""
            
            cached_html = self._ai_html_cache.get(conv_id)
            if cached_html is not None:
                self._last_rendered_html = cached_html
                self._ai_markdown_text = st["ai_markdown_text"]
                js_code = f"updateContent({json.dumps(cached_html)});"
                self._ai_webview.run_javascript(js_code, None, None)
            else:
                self._ai_markdown_text = st["ai_markdown_text"]
                self._render_markdown(self._ai_markdown_text)

            self._ai_current_assistant_text = st.get("current_assistant_text", "")
            self._ai_current_reasoning_text = st.get("current_reasoning_text", "")
            self._ai_response_div_added = st.get("response_div_added", False)
            self._ai_streaming = True
            
            self._update_send_button(True)
            self._ai_entry.placeholder_text = "等待回复中..."
            self._ai_spinner.show()
            self._ai_spinner.start()
            
        else:
            self._ai_messages = []
            for m in conv.messages:
                msg = {"role": m.role, "content": m.content}
                if m.tool_call_id:
                    msg["tool_call_id"] = m.tool_call_id
                if m.name:
                    msg["name"] = m.name
                if m.tool_calls:
                    msg["tool_calls"] = m.tool_calls
                if m.reasoning_content:
                    msg["reasoning_content"] = m.reasoning_content
                self._ai_messages.append(msg)
            self._ai_conversation_id = conv.id
            self._ai_conversation_created_at = conv.created_at
            self._ai_summary = conv.summary if conv else ""
            self._ai_current_assistant_text = ""
            self._ai_current_reasoning_text = ""
            self._ai_response_div_added = False
            self._ai_streaming = False
            
            self._update_send_button(False)
            self._ai_entry.placeholder_text = ""
            self._ai_spinner.stop()
            self._ai_spinner.hide()

            cached_html = self._ai_html_cache.get(conv_id)
            if cached_html is not None:
                self._last_rendered_html = cached_html
                self._ai_markdown_text = self._rebuild_markdown_from_messages(self._ai_messages)
                js_code = f"updateContent({json.dumps(cached_html)});"
                self._ai_webview.run_javascript(js_code, None, None)
            else:
                self._ai_markdown_text = self._rebuild_markdown_from_messages(self._ai_messages)
                self._prune_messages()
                self._render_markdown(self._ai_markdown_text)
        
        self._refresh_subagent_bar()

        # Update model info display label
        _, _, _, display_name, _, _, _ = self._read_model_config(None, self._ai_active_model_info)
        self._ai_lbl.set_markup(f"<b>AI 助手看盘</b>\n<span size='small' foreground='#888888'>({display_name})</span>")

        # Ensure AI panel + input area are visible
        self.separator.set_no_show_all(False)
        self.separator.show()
        self.set_no_show_all(False)
        self.show()
        self._ai_input_area.set_no_show_all(False)
        self.show_all()
        self._ai_entry.get_buffer().set_text("")
        self._ai_entry.grab_focus()
        self.queue_resize()
        try:
            self._ai_history_popover.refresh_dropdown()
        except Exception as e:
            print(f"Failed to refresh dropdown in switch: {e}", flush=True)

    def _get_sorted_conversations(self) -> List[Dict[str, Any]]:
        """Return all conversations sorted by updated_at descending (newest first)."""
        summaries = self._conversation_store.list_conversations()
        existing_ids = {s.get("id") for s in summaries}

        # Add active conversation if not on disk
        active_id = self._ai_conversation_id
        if active_id and active_id not in existing_ids:
            if self._ai_messages:
                first_msg = self._ai_messages[0].get("content", "")
                if isinstance(first_msg, list):
                    first_msg = next((p["text"] for p in first_msg if isinstance(p, dict) and p.get("type") == "text"), "")
                title = first_msg[:30] if first_msg else "New Conversation"
                summaries.append({
                    "id": active_id,
                    "title": title,
                    "message_count": len(self._ai_messages),
                    "updated_at": int(time.time() * 1000),
                })
                existing_ids.add(active_id)

        # Add any running background conversations not on disk
        for cid, st in self._ai_running_convs.items():
            if cid not in existing_ids:
                msgs = st.get("messages", [])
                if msgs:
                    first_msg = msgs[0].get("content", "")
                    if isinstance(first_msg, list):
                        first_msg = next((p["text"] for p in first_msg if isinstance(p, dict) and p.get("type") == "text"), "")
                    title = first_msg[:30] if first_msg else "New Conversation"
                    summaries.append({
                        "id": cid,
                        "title": title,
                        "message_count": len(msgs),
                        "updated_at": int(time.time() * 1000),
                    })
                    existing_ids.add(cid)

        summaries.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
        return summaries

    def navigate_conversation(self, direction: int):
        """Navigate conversation history via keyboard shortcut.

        Args:
            direction: +1 for next (Down arrow → older in DESC list),
                       -1 for previous (Up arrow → newer in DESC list).
        """
        # Allow navigation during streaming

        summaries = self._get_sorted_conversations()
        if not summaries:
            return

        if self._ai_conversation_id is None:
            target_idx = len(summaries) - 1 if direction < 0 else 0
        else:
            current_idx = -1
            for i, s in enumerate(summaries):
                if s.get("id") == self._ai_conversation_id:
                    current_idx = i
                    break
            if current_idx == -1:
                return
            target_idx = current_idx + direction
            if target_idx < 0 or target_idx >= len(summaries):
                return

        target_id = summaries[target_idx].get("id")
        if target_id and target_id != self._ai_conversation_id:
            if getattr(self, "_ai_history_popover", None) and self._ai_history_popover.get_visible():
                self._ai_history_popover.popdown()
            self._switch_to_conversation(target_id)

    def _call_llm_sync(self, messages: list, base_url: str, api_key: str,
                        model_name: str, timeout: int = 15,
                        temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS,
                        top_p: float = DEFAULT_TOP_P) -> Optional[str]:
        return self._llm_client.sync_chat_completion(
            base_url, api_key, model_name, messages, timeout=timeout,
            temperature=temperature, max_tokens=max_tokens, top_p=top_p,
        ).get("content")

    def _call_llm_and_set_title(self, prompt: str, conv_id: str,
                                 base_url: str, api_key: str, model_name: str,
                                 temperature: float, max_tokens: int, top_p: float,
                                 log_label: str = "conversation title"):
        """Call LLM with a title-generation prompt, parse <title> and update conversation.

        Shared by _generate_conversation_title and _generate_title_from_context.
        Designed to run in a background thread (result dispatched via GLib.idle_add).
        """
        try:
            content = self._call_llm_sync(
                [{"role": "user", "content": prompt}],
                base_url, api_key, model_name, timeout=15,
                temperature=temperature, max_tokens=max_tokens, top_p=top_p,
            )
            if content:
                m = re.search(r'<title>(.+?)</title>', content, re.IGNORECASE)
                if m:
                    title = m.group(1).strip()
                    GLib.idle_add(self._on_title_generated, conv_id, title)
        except Exception as e:
            print(f"Error generating {log_label}: {e}", flush=True)

    def _generate_conversation_title(self, first_message: str, conv_id: str,
                                      base_url: str, api_key: str, model_name: str,
                                      temperature: float = DEFAULT_TEMPERATURE,
                                      max_tokens: int = DEFAULT_MAX_TOKENS,
                                      top_p: float = DEFAULT_TOP_P):
        """Background thread: generate a short title using only the first message."""
        title_prompt = (
            f"第一条消息：\n{first_message}\n\n"
            f"请为以上对话的第一条消息生成一个简明、专业的中文标题。\n"
            f"规则：\n"
            f"1. 概括用户提问的核心意图、主题或所涉及的关键技术，避免“代码分析”、“陈述文本解释”等泛泛而谈的废话。\n"
            f"2. 标题长度严格控制在 12 个汉字以内。\n"
            f"3. 必须且只能按照以下 XML 标签格式输出，不要附加任何解释、前缀、后缀、反引号或多余字符：\n"
            f"   <title>具体标题</title>\n"
            f"示例：\n"
            f"输入：如何用Python爬取动态网页数据？\n"
            f"输出：<title>Python动态爬虫</title>\n"
            f"输入：try {{ await client.session.get(id) }} catch {{ ... }}\n"
            f"输出：<title>异步错误处理</title>"
        )
        self._call_llm_and_set_title(
            title_prompt, conv_id, base_url, api_key, model_name,
            temperature, max_tokens, top_p, log_label="conversation title"
        )

    def _generate_title_from_context(self, context_text: str, conv_id: str,
                                      base_url: str, api_key: str, model_name: str,
                                      temperature: float = DEFAULT_TEMPERATURE,
                                      max_tokens: int = DEFAULT_MAX_TOKENS,
                                      top_p: float = DEFAULT_TOP_P):
        """Background thread: generate a short title based on full conversation context."""
        title_prompt = (
            f"对话内容：\n{context_text}\n\n"
            f"请为以上对话生成一个简明、专业的中文标题。\n"
            f"规则：\n"
            f"1. 概括整个对话的核心意图、主题或所涉及的关键技术。\n"
            f"2. 标题长度严格控制在 12 个汉字以内。\n"
            f"3. 必须且只能按照以下 XML 标签格式输出，不要附加任何解释、前缀、后缀、反引号或多余字符：\n"
            f"   <title>具体标题</title>\n"
            f"示例：\n"
            f"对话内容：\n"
            f"User: 如何用Python爬取动态网页数据？\n"
            f"Assistant: 可以使用requests库配合BeautifulSoup解析HTML...\n"
            f"User: 如果页面是异步加载的呢？\n"
            f"输出：<title>Python异步爬虫方案</title>"
        )
        self._call_llm_and_set_title(
            title_prompt, conv_id, base_url, api_key, model_name,
            temperature, max_tokens, top_p, log_label="conversation title from context"
        )

    def _on_title_generated(self, conv_id: str, title: str):
        """Idle callback: update conversation title in store, refresh dropdown,
        and notify webview if triggered by /title command."""
        conv = self._conversation_store.load_conversation(conv_id)
        if conv:
            conv.title = title
            self._conversation_store.save_conversation(conv, bump_updated_at=False)
        self._ai_history_popover.refresh_dropdown()
        if getattr(self, "_ai_pending_title_notification", False):
            self._ai_pending_title_notification = False
            escaped = html.escape(title)
            self.append_html_to_webview(
                f'<div class="chat-simple-info">标题已生成: {escaped}</div>'
            )

    def is_visible(self) -> bool:
        return self.get_visible()

    def on_panel_shown(self):
        if getattr(self, "_suspend_timeout_id", 0) != 0:
            GLib.source_remove(self._suspend_timeout_id)
            self._suspend_timeout_id = 0
        
        if getattr(self, "_webview_suspended", False):
            self._webview_suspended = False
            cached_html = self._ai_html_cache.get(self._ai_conversation_id)
            html = self.get_html_template(self._theme, cached_html or "")
            self._ai_webview.load_html(html, "file:///")
            print("[AI] WebView restored from suspension.", flush=True)
        self._ai_entry.grab_focus()

    def on_panel_hidden(self):
        if getattr(self, "_suspend_timeout_id", 0) != 0:
            GLib.source_remove(self._suspend_timeout_id)
            self._suspend_timeout_id = 0
        
        self._suspend_timeout_id = GLib.timeout_add_seconds(
            self._SUSPEND_DELAY_SECONDS, self._suspend_webview_cb
        )
        print(f"[AI] suspend timer started: {self._SUSPEND_DELAY_SECONDS}s, running_convs={len(self._ai_running_convs)}", flush=True)

    def _suspend_webview_cb(self) -> bool:
        any_running = any(st.get("streaming", False) for st in self._ai_running_convs.values())
        if any_running:
            print(f"[AI] suspend deferred: {sum(1 for st in self._ai_running_convs.values() if st.get('streaming'))} convs still streaming", flush=True)
            return True

        self._suspend_timeout_id = 0

        if not getattr(self, "_webview_suspended", False):
            if self._ai_conversation_id:
                self._ai_html_cache[self._ai_conversation_id] = getattr(self, "_last_rendered_html", "")
            
            self._webview_suspended = True  # 先标记，防止崩溃恢复干扰
            self._ai_webview.terminate_web_process()
            print("[AI] WebView suspended, web process terminated.", flush=True)
            
        return False

    def hide_panel(self):
        self.on_panel_hidden()
        self._update_send_button(False)
        self.set_no_show_all(True)
        self.hide()
        self.separator.set_no_show_all(True)
        self.separator.hide()
        self._ai_panel_visible_saved = False
        self.queue_resize()

    def _reset_ai_panel_silent(self):
        self._ai_spinner.stop()
        self._ai_spinner.hide()
        self._update_send_button(False)
        self._ai_streaming = False
        self._ai_entry.placeholder_text = ""
        self._last_rendered_html = ""
        self._ai_messages = []
        self._ai_conversation_id = uuid4().hex[:12]
        self._clear_subagent_bar_instantly()
        self._refresh_subagent_bar()
        self._ai_assistant_buffer = ""
        self._ai_markdown_text = ""
        self._ai_current_assistant_text = ""
        self._ai_response_div_added = False
        self._ai_assistant_html_base = ""
        self._ai_webview.load_html(self.get_html_template(self._theme), "file:///")
        self._ai_entry.get_buffer().set_text("")
        _, _, _, display_name, _, _, _ = self._read_model_config(None, None)
        self._ai_lbl.set_markup(f"<b>AI 助手看盘</b>\n<span size='small' foreground='#888888'>({display_name})</span>")
        self._ai_active_model_info = None
        self._ai_last_prompt_obj = None
        self._ai_title_generated = False
        self._ai_pending_title_notification = False
        self._ai_summary = ""
        self._ai_summary_generating = False
        
        self._ai_input_area.set_no_show_all(False)
        self._ai_input_area.show_all()
        
        self._ai_entry.grab_focus()
        self.queue_resize()
        self._ai_history_popover.refresh_dropdown()

    def start_new_conversation(self):
        """保存当前对话（若有内容），确保 AI 看盘面板可见，并启动一个全新的空白对话。"""
        if not hasattr(self, "_ai_request_id"):
            self._ai_request_id = 0
        self._ai_request_id += 1

        if self._ai_conversation_id:
            self._ai_html_cache[self._ai_conversation_id] = getattr(self, "_last_rendered_html", "")

        # 2. 若当前已有对话内容并且不在运行中，自动保存当前对话
        if self._ai_messages and self._ai_conversation_id:
            is_currently_running = self._ai_running_convs.get(self._ai_conversation_id, {}).get("streaming", False)
            if not is_currently_running:
                try:
                    model_snapshot = self._build_model_snapshot()
                    self._save_current_conversation(model_snapshot, preserve_updated_at=True)
                except Exception as e:
                    print(f"Error saving before new conversation: {e}", flush=True)

        # 4. 确保 AI 面板显示
        self.separator.set_no_show_all(False)
        self.separator.show()
        self.set_no_show_all(False)
        self.show()
        self.show_all()

        # 5. 重置 AI 会话所有的底层状态变量并刷新下拉框
        self._reset_ai_panel_silent()

    def open_ai_and_load_recent(self):
        self.on_panel_shown()
        self.separator.set_no_show_all(False)
        self.separator.show()
        self.set_no_show_all(False)
        self.show()
        self.show_all()
        self.queue_resize()

        summaries = self._get_sorted_conversations()
        if summaries:
            latest_id = summaries[0].get("id")
            if latest_id:
                if latest_id == self._ai_conversation_id and self._ai_messages:
                    self._ai_history_popover.refresh_dropdown()
                    if self._ai_input_area.get_visible():
                        self._ai_entry.grab_focus()
                else:
                    self._switch_to_conversation(latest_id)
        else:
            self._reset_ai_panel_silent()

    def show_panel(self):
        self.on_panel_shown()
        self.set_no_show_all(False)
        self.show()
        self.show_all()
        self.separator.set_no_show_all(False)
        self.separator.show()
        self.queue_resize()

    def is_popup_shown(self):
        return bool(self._ai_history_popover and self._ai_history_popover.get_visible())

    def reset_state(self):
        self._reset_ai_panel_silent()

    def grab_entry_focus(self):
        self._ai_entry.grab_focus()

    def insert_text_to_input(self, text: str):
        """从外部向 AI 输入框插入文本并聚焦。"""
        buffer = self._ai_entry.get_buffer()
        buffer.insert(buffer.get_end_iter(), text)
        self._ai_entry.grab_focus()

    def _select_and_set_bash_cwd(self):
        """Open a directory chooser dialog to let the user select a folder to set as the active bash working directory."""
        toplevel = self.get_toplevel()
        if not isinstance(toplevel, Gtk.Window):
            toplevel = None

        dialog = Gtk.FileChooserDialog(
            title="选择 Bash 工作目录",
            transient_for=toplevel,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_button("_取消", Gtk.ResponseType.CANCEL)
        dialog.add_button("_选择", Gtk.ResponseType.ACCEPT)

        # Connect focus protection hooks to prevent transient dialog from dismissing switcher main window
        if self.on_dialog_shown:
            dialog.connect("show", lambda *_: self.on_dialog_shown())
        if self.on_dialog_hidden:
            dialog.connect("destroy", lambda *_: self.on_dialog_hidden())

        # Set initial folder to current bash CWD if valid
        from tool_registry import get_bash_cwd
        current_cwd = get_bash_cwd(session_key=self._ai_conversation_id)
        if os.path.isdir(current_cwd):
            dialog.set_current_folder(current_cwd)

        def _on_dialog_response(dlg, response):
            if response == Gtk.ResponseType.ACCEPT:
                chosen = dlg.get_filename()
                dlg.destroy()
                if chosen:
                    from tool_registry import set_bash_cwd
                    result = set_bash_cwd(chosen, session_key=self._ai_conversation_id)
                    self.append_html_to_webview(
                        f'<div class="chat-status-notice">{html.escape(result)}</div>'
                    )
            else:
                dlg.destroy()

        dialog.connect("response", _on_dialog_response)
        dialog.show_all()

    def set_theme(self, name):
        self._theme = name
        self._ai_html_cache.clear()
        pygments_css = self._get_pygments_css(name)
        html_content = ""
        if self._ai_markdown_text:
            html_content = _markdown_to_html_safe(self._ai_markdown_text)
        html = get_html_template(name, html_content, pygments_css)
        self._ai_webview.load_html(html, "file:///")
