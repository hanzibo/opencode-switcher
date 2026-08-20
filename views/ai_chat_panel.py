import gi
import subprocess
import threading
import os
import re
import html
import tool_registry
from tool_registry.notification import execute_send_notification
gi.require_version("Gtk", "3.0")
gi.require_version("Gio", "2.0")
gi.require_version("GdkPixbuf", "2.0")
try:
    gi.require_version("WebKit2", "4.1")
except ValueError:
    try:
        gi.require_version("WebKit2", "4.0")
    except ValueError:
        pass
import sys
import hashlib
import mimetypes
import urllib.parse
from gi.repository import Gtk, Gdk, GLib, Gio, Pango, GdkPixbuf, PangoCairo, WebKit2
from typing import Optional, Callable, List, Dict, Any, Tuple, Set
from copy import deepcopy
from uuid import uuid4
from stores.clipboard_store import ClipboardItem, CategoryItem, CategoryStore, CustomCategory, capture_clipboard_once, CustomPrompt, CustomPromptsStore, LLMSettingsStore, LLMModelConfig, ConversationStore, ChatMessage, Conversation, AISettingsStore, DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS, DEFAULT_TOP_P, CONFIG_DIR, _DEFAULT_SUMMARY_TEMPLATE
import time
import requests
import json
import base64
from system.utils import relative_time, request_window_focus
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
    set_code_highlight,
)
from ai_text_utils.render import _render_tool_card_standalone
from ai_engine.render_pipeline import render_turn, TurnRenderInput, build_update_js
from stores.theme_config import get_ai_gtk_colors

from views.ai_chat.constants import (
    TEMPLATE_REGEX,
    PROMPT_PLACEHOLDER_RE,
    _MPS_MEMORY_LIMIT,
    _MPS_POLL_INTERVAL,
    _MPS_CONSERVATIVE,
    _MPS_STRICT,
    AI_BTN_LABEL_SEND,
    AI_BTN_LABEL_STOP,
    _AI_HEADER_TITLE,
    _to_chat_messages,
    _ai_stream_request_key,
    _ai_summary_request_key,
    _webview_shell_fingerprint,
    _should_full_reload_webview,
    _MARKUP_TITLE_RE,
    _MARKUP_MODEL_RE,
    _WebViewBridgeBase,
    _HeaderSpinnerBridge,
    _HeaderTitleBridge,
    _HistoryPopoverBridge,
)
from views.ai_chat.mcp_mixin import MCPMixin
from views.ai_chat.subagent_mixin import SubagentMixin
from views.ai_chat.streaming_mixin import StreamingMixin
from views.ai_chat.webview_mixin import WebViewMixin
from views.ai_chat.runner_mixin import RunnerMixin
from views.ai_chat.session_mixin import SessionMixin
from ai_engine.ai_html_template import get_html_template, _get_pygments_css, get_shared_web_context
from dialogs.dynamic_copy_dialog import show_dynamic_copy_dialog
from dialogs.sort_dialog import show_sort_dialog
from dialogs.recycle_bin_dialog import show_recycle_bin_dialog
from dialogs.sort_cats_dialog import show_sort_cats_dialog
from ai_engine.llm_client import _LLMHttpClient, _LLMHttpError, LLMRequestConfig
from system.event_types import StreamEventType
from dialogs.prompt_dialog import show_prompt_dialog
from dialogs.prompts_config_dialog import show_prompts_config_dialog
from views.ai_popovers import AICommandPopover
from ai_engine.ai_tool_loop import run_llm_react_loop, ToolLoopContext


class AIChatPanel(Gtk.Box, MCPMixin, SubagentMixin, StreamingMixin, WebViewMixin, RunnerMixin, SessionMixin):
    # Slash commands available in the AI chat input box (command, description)
    _AI_COMMANDS = [
        ("/new", "新对话"),
        ("/delete", "删除并新建"),
        ("/fork", "建立当前对话的分支 (Fork)"),
        ("/retry", "回滚到上一轮"),
        ("/rollback", "回滚到任意轮"),
        ("/title", "设置/生成标题"),
        ("/model", "切换模型"),
        ("/cd", "切换 bash 工作路径"),
        ("/summary", "压缩上下文（/summary keep=N，保留最近N条，默认50）"),
        ("/skill", "查看与手动触发 AI Skill"),
        ("/ai-polish", "扩展润色提问，去除歧义与不严谨"),
    ]
    _SUSPEND_DELAY_SECONDS = 60
    # ── Streaming: Token batching ──
    _BATCH_FLUSH_MS = 60                    # 批处理窗口（ms）
    _STREAM_PERF_LOG = False
    _MPS = None  # WebKit memory pressure settings (lazy init)
    # ── Token 计数常量 ──
    _TOKEN_CALIBRATION_FACTOR = 0.89  # cl100k_base 对中文模型约高估 12%
    _ESTIMATED_OVERHEAD_PER_MSG = 20  # role/tool_call_id 等结构字段的字符开销估算

    def _safe_grab_ai_entry_focus(self):
        """带 realize 守卫的输入框焦点获取。

        GTK 会向未 realize 的 widget 分发事件并触发
        ``gtk_widget_event: assertion 'WIDGET_REALIZED_FOR_EVENT'`` 断言；
        启动期/异步回调路径上 _ai_entry 可能尚未 realize，此处守卫跳过。
        """
        if self._ai_entry.get_realized():
            self._ai_entry.grab_focus()

    def __init__(self, conversation_store, llm_settings_store, ai_settings_store=None, theme="dark", ai_commands=None, pygments_css_cache=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._conversation_store = conversation_store
        self._llm_settings_store = llm_settings_store
        self._ai_settings_store = ai_settings_store
        if self._ai_settings_store is not None:
            set_code_highlight(self._ai_settings_store.enable_code_highlight)
        self._theme = theme
        self._ai_commands = ai_commands
        self._pygments_css_cache = pygments_css_cache or {}

        # AI streaming & conversation state
        self._ai_streaming = False
        self._ai_pending_image_hash = None
        self._ai_pending_image_path = None
        self._ai_pending_image_data_uri = None
        self._ai_cancelling = False
        self._ai_messages = []
        self._ai_system_prompt: str = ""  # 会话级系统提示词快照（新对话从 Settings 快照，旧对话从会话记录加载）
        self._ai_conversation_id = uuid4().hex[:12]
        self._ai_history_popover = None
        self._ai_history_btn = None
        self._ai_history_btn_label = None

        # ── MCP（Model Context Protocol）集成 ──
        self._mcp_bridge = None
        self._mcp_client_mgr = None
        self._cached_mcp_tools: Optional[list] = None
        self._mcp_initialized = False
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
        self._deleted_conversation_ids = set()
        self._last_rendered_html = ""
        # 已装载外壳指纹 (theme, pygments_css)，须与 ai_html_template 外壳缓存键一致；None=未装载
        self._loaded_shell_fingerprint = None
        # 初始面板会话：立即锚定创建时间（不得为 0）。主路径 _send_user_message 不经过
        # _start_new_conversation，若此处残留 0，首条消息流结束保存会把 created_at=0 落盘。
        self._ai_conversation_created_at = int(time.time() * 1000)
        self._ai_title_generated = False
        self._ai_history_queries = []
        self._ai_history_index = -1
        self._ai_current_draft = ""
        self._llm_client = _LLMHttpClient()
        self._ai_panel_visible_saved = False
        self._ai_has_shown = False          # 首开守卫：从未显示过时不允许挂起杀 WebProcess
        self._ai_recent_load_pending = False  # 方案3：延后加载防重入
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
        # 文档装载状态：仅当 load-changed FINISHED 后为 True。load_html 一发出
        # 即置 False——装载完成前禁止 in-place updateContent（会打到未就绪文档）
        self._webview_ready = False

        # ── Streaming: Token batching state ──
        self._token_buffer = ""
        self._flush_scheduled = False
        self._reasoning_buffer = ""
        self._reasoning_flush_scheduled = False
        self._last_flushed_len = 0
        self._streaming_container_created = False
        # A→B→A 切回 / DOM 重建后，JS 端 _reasoningCache 已被 updateContent 的
        # resetReasoning() 清空；置位本标记让下一次流式容器 append 时用 Python 端
        # 累积的 current_reasoning_text 重新播种（一次性消费，普通流式不受影响）。
        self._reseed_reasoning_on_container = False

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
        from views.clipboard_panel import _textview_draw_placeholder, _copy_to_clipboard
        self._copy_to_clipboard = _copy_to_clipboard  # 保存为实例变量供 _on_decide_policy 使用，避免模块级循环导入

        # ── AI header 已迁移至 WebView 内（HTML #ai-header）──
        # 保留 _ai_lbl / _ai_spinner / _ai_history_popover 三个桥对象，
        # 接口与原 GTK 组件兼容（set_markup / show/start/stop/hide /
        # refresh_dropdown/popdown），内部转发到 WebView JS，
        # 使既有调用点与测试保持零改动。
        # ponytail: GTK header 迁移至 WebView #ai-header（桥对象见 _HeaderTitleBridge/_HistoryPopoverBridge）
        self._ai_lbl = _HeaderTitleBridge(self)
        self._ai_spinner = _HeaderSpinnerBridge(self)
        self._ai_history_btn = None
        self._ai_history_btn_label = None
        self._ai_history_popover = _HistoryPopoverBridge(self)
        # 无 GTK header：WebView 从面板顶部开始，内部含固定装饰区

        # Scrolled Text view
        ai_scrolled = Gtk.ScrolledWindow.new()
        self._ai_scrolled = ai_scrolled
        ai_scrolled.set_name("aiScrolled")
        ai_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        ai_scrolled.set_vexpand(True)

        self._ai_web_context = get_shared_web_context()
        if self._ai_web_context:
            self._ai_webview = WebKit2.WebView.new_with_context(self._ai_web_context)
        else:
            self._ai_webview = WebKit2.WebView.new()
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
        settings.set_enable_back_forward_navigation_gestures(False)
        settings.set_enable_dns_prefetching(False)
        settings.set_enable_caret_browsing(False)
        settings.set_enable_smooth_scrolling(False)

        # Allow file:// page to load file:// subresources (KaTeX CSS/JS/fonts)
        settings.set_allow_file_access_from_file_urls(True)

        # 首次装载：用真实主题（而非写死 "dark"），避免启动期 set_theme 二次整页重载
        self._load_webview_html("", force=True)

        self._ai_webview.connect("decide-policy", self._on_decide_policy)
        self._ai_webview.connect("context-menu", lambda *_: True)
        self._ai_webview.connect("web-process-terminated", self._on_webview_crashed)
        self._ai_webview.connect("load-changed", self._on_webview_load_changed)
        self._ai_webview.connect("realize", lambda *_: self._apply_webview_gtk_background())
        ai_scrolled.add(self._ai_webview)

        # Synchronize background colors to prevent Wayland resize flickering/leaks
        c = self._get_gtk_colors(self._theme)

        self.override_background_color(Gtk.StateFlags.NORMAL, c["bg"])
        ai_scrolled.override_background_color(Gtk.StateFlags.NORMAL, c["bg"])
        self._ai_webview.set_background_color(c["bg"])
        self._apply_webview_gtk_background()

        self.pack_start(ai_scrolled, True, True, 0)

        # Multi-turn conversation input area (hidden until first response)
        self._ai_input_area = Gtk.Box.new(Gtk.Orientation.VERTICAL, 2)
        self._ai_input_area.set_no_show_all(True)
        self._ai_input_area.set_margin_top(4)
        self._ai_input_area.override_background_color(Gtk.StateFlags.NORMAL, c["input_bg"])

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

        # ── 输入框下方状态栏：token 计数 + 快捷键提示（替换原斜杠命令说明） ──
        self._ai_hint_label = Gtk.Label.new("")
        self._ai_hint_label.set_xalign(1)
        self._ai_hint_label.get_style_context().add_class("ai-hint-label")
        self._ai_hint_label.set_margin_end(4)
        self._ai_hint_label.set_opacity(0.6)
        self._ai_input_area.pack_start(self._ai_hint_label, False, False, 0)
        self._update_token_display()

        self._ai_cmd_popover = AICommandPopover(self._ai_entry, self._AI_COMMANDS)

        self.pack_start(self._ai_input_area, False, False, 0)

        try:
            _subagent_css = b"""
                .subagent-status-bar { margin: 4px 8px 2px 8px; min-height: 28px; background-color: #181124; border-radius: 12px; padding: 4px 8px; border: 1px solid rgba(168,85,247,0.22); }
                .subagent-block-running { background-color: #a855f7; color: #ffffff; border-radius: 8px; font-size: 12px; border: 2px solid transparent; }
                .subagent-block-done { background-color: #22c55e; color: #ffffff; border-radius: 8px; font-size: 12px; border: 2px solid transparent; }
                .subagent-block-done:hover { background-color: #16a34a; }
                .subagent-block-failed { background-color: #ef4444; color: #ffffff; border-radius: 8px; font-size: 12px; border: 2px solid transparent; }
                .subagent-block-selected { border-color: #ffffff; }
                .subagent-spinner { min-width: 14px; min-height: 14px; margin-left: 4px; }
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
        """Clean up resources on destroy."""
        try:
            from tool_registry import unregister_subagent_status_listener
            unregister_subagent_status_listener(self._on_subagent_status_changed)
        except Exception:
            pass
        # 关闭 MCP 连接并停止 asyncio 桥接器（先断开全部 Server，再停桥）
        self.shutdown_mcp()

    def shutdown_mcp(self) -> None:
        """关闭 MCP 连接并停止 asyncio 桥接器（幂等，有界阻塞）。

        供面板 destroy 与应用退出路径调用：
        - 将 client_manager.shutdown()（内部为 disconnect_all）提交到桥接器
          事件循环并等待其完成，再停止事件循环/线程，确保 stdio 子进程
          被终止并回收，不遗留孤立 MCP 子进程。
        - 桥接器未启动 / 已停止 / 清理失败时安全返回（异常记录日志）。
        """
        if self._mcp_bridge is None:
            return
        try:
            if self._mcp_client_mgr is not None:
                self._mcp_bridge.shutdown(
                    cleanup=self._mcp_client_mgr.shutdown(),
                )
            else:
                self._mcp_bridge.shutdown()
        except Exception as e:
            print(f"[MCP] 关闭 MCP 连接时异常: {e}", flush=True)

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

    def _delete_conversation_cleanup(self, conv_id: str):
        """删除指定会话时，关断后台流与工具循环，释放 CWD 绑定并弹出 HTML 缓存。"""
        if not conv_id:
            return
        if conv_id in self._ai_running_convs:
            self._cancel_streams_for_conversation(conv_id)
        # pop 必须在 cancel 之后（cancel 内部用 .get() 读 state）：
        # 清理运行状态，避免删除激活会话后残留幽灵条目、流结束被误收尾。
        self._ai_running_convs.pop(conv_id, None)
        try:
            from tool_registry.bash import close_bash_session
            close_bash_session(conv_id)
        except Exception:
            pass
        self._conversation_store.delete_conversation(conv_id)
        # tombstone：标记已删除 id，后续流结束/后台渲染/保存一律短路，防复活
        getattr(self, "_deleted_conversation_ids", set()).add(conv_id)
        self._ai_html_cache.pop(conv_id, None)

    def _on_send_clicked(self, _btn=None):
        if self._ai_streaming or self._ai_cancelling:
            if not self._ai_cancelling:
                # 首次点击暂停：发信号，不销毁状态，由后台线程回调清理
                self._ai_cancelling = True
                active_state = self._ai_running_convs.get(self._ai_conversation_id)
                if active_state and active_state.get("cancel_event"):
                    # 定向取消当前会话（主流 + 摘要），并行会话不受影响
                    self._cancel_streams_for_conversation(self._ai_conversation_id)
                    # 与 _cancel_streaming_if_active 一致：清空流式缓存，避免
                    # 取消后 _on_llm_api_finished 误弹"回答完成"通知（半截回答）。
                    active_state["current_assistant_text"] = ""
                    active_state["current_reasoning_text"] = ""
                    self._ai_current_assistant_text = ""
                    self._ai_current_reasoning_text = ""
                else:
                    # 兜底：当前会话无活跃 state（如流属于背景会话），
                    # 取消所有运行中的流，避免只关 response 导致 SSE 误报
                    for st in list(self._ai_running_convs.values()):
                        ce = st.get("cancel_event")
                        if ce:
                            ce.set()
                    self._llm_client.cancel_active_request()
                    self._ai_current_assistant_text = ""
                    self._ai_current_reasoning_text = ""
                self._update_send_button(False, sensitive=False)
                self._ai_entry.placeholder_text = "正在中止..."
                self._ai_spinner.stop()
                self._ai_lbl.set_markup(f"<b>{_AI_HEADER_TITLE}</b>\n<span size='small' foreground='#f43f5e'>(正在中止...)</span>")
                # 看门狗：10 秒后若线程未响应则强制清理（移除可能存在的旧看门狗）
                if getattr(self, "_ai_cancel_watchdog_id", 0) != 0:
                    GLib.source_remove(self._ai_cancel_watchdog_id)
                self._ai_cancel_watchdog_id = GLib.timeout_add(10000, self._force_cleanup_after_cancel)
            # 取消中忽略重复点击
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
                self._delete_conversation_cleanup(conv_id)
            self._reset_ai_panel_silent()
            return
        if text == "/fork":
            buf.set_text("")
            self._handle_fork_command(None)
            return
        if text.startswith("/fork "):
            buf.set_text("")
            fork_title = text[len("/fork "):].strip() or None
            self._handle_fork_command(fork_title)
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
        if text == "/summary" or text.startswith("/summary "):
            buf.set_text("")
            self._handle_summary_command(text)
            return
        if text == "/skill" or text.startswith("/skill:") or text.startswith("/skill ") or text.startswith("skill:"):
            buf.set_text("")
            self._handle_skill_command(text)
            return
        if text == "/ai-polish" or text.startswith("/ai-polish ") or text == "/ai_polish" or text.startswith("/ai_polish "):
            buf.set_text("")
            raw_input = ""
            if text.startswith("/ai-polish "):
                raw_input = text[len("/ai-polish "):].strip()
            elif text.startswith("/ai_polish "):
                raw_input = text[len("/ai_polish "):].strip()

            if not raw_input:
                info_html = (
                    '<div class="chat-model-info" style="color: #f43f5e; border-color: #f43f5e;">'
                    '❌ <strong>用法错误</strong>：用法为 <code>/ai-polish &lt;原始提问文本&gt;</code>'
                    '</div>'
                )
                self.append_html_to_webview(info_html)
                return

            self._handle_ai_polish_command(raw_input)
            return
        # Handle selected sub-agent blocks: build notification text and send
        if self._ai_selected_subagents:
            from tool_registry import get_subagent_status_map, check_background_subagents
            # 按 sid 精确消费结果（UI 主线程 conv_id 恒为 None，按 conv_id 匹配必然失败，
            # 会导致结果残留泄漏；同时避免 tool loop 重复注入）
            check_background_subagents(subagent_ids=list(self._ai_selected_subagents))
            parts = []
            for sid in sorted(self._ai_selected_subagents):
                info = get_subagent_status_map().get(sid, {})
                task_desc = info.get("task", "未知任务")
                # 状态文案动态化：failed 时不再硬编码"已完成"（🟡-2）
                status = info.get("status", "completed")
                if status == "failed":
                    status_text = "已失败"
                elif status == "completed":
                    status_text = "已完成"
                else:
                    status_text = "已结束"
                parts.append(
                    f"后台子代理 {sid} {status_text}\n"
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
                    child, _event_box, _box, _spinner = entry
                    _spinner.stop()
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

    def _on_ai_entry_key_press(self, widget, event):
        keyname = Gdk.keyval_name(event.keyval)
        is_shift = (event.state & Gdk.ModifierType.SHIFT_MASK) != 0
        is_ctrl = (event.state & Gdk.ModifierType.CONTROL_MASK) != 0

        if self._ai_cmd_popover is not None and self._ai_cmd_popover.is_visible():
            if keyname in ("Up", "KP_Up"):
                current = self._ai_cmd_popover.listbox.get_selected_row()
                if current:
                    idx = current.get_index()
                    if idx > 0:
                        above = self._ai_cmd_popover.listbox.get_row_at_index(idx - 1)
                        if above:
                            self._ai_cmd_popover.listbox.select_row(above)
                            self._ai_cmd_popover.scroll_to_row(above)
                return True
            if keyname in ("Down", "KP_Down"):
                current = self._ai_cmd_popover.listbox.get_selected_row()
                if current:
                    idx = current.get_index()
                    below = self._ai_cmd_popover.listbox.get_row_at_index(idx + 1)
                    if below:
                        self._ai_cmd_popover.listbox.select_row(below)
                        self._ai_cmd_popover.scroll_to_row(below)
                else:
                    first = self._ai_cmd_popover.listbox.get_row_at_index(0)
                    if first:
                        self._ai_cmd_popover.listbox.select_row(first)
                        self._ai_cmd_popover.scroll_to_row(first)
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

        # Ctrl+Shift+Up/Down → 窗口级会话切换（views/panel.py:_on_window_key），
        # 不得被输入历史导航吞掉；直接放行让事件继续传播到窗口处理器。
        if is_ctrl and is_shift and keyname in ("Up", "KP_Up", "Down", "KP_Down"):
            return False

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
        from stores.clipboard_store import _capture_image
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
        raw_text = buf.get_text(start, end, True)
        text = raw_text.strip()

        if text.startswith("/") and (" " not in text or text.startswith("/skill")):
            self._ai_cmd_popover.rebuild(text)
        elif text.startswith("skill:"):
            self._ai_cmd_popover.rebuild(text)
        else:
            self._ai_cmd_popover.dismiss()

    def is_visible(self) -> bool:
        return self.get_visible()

    def is_webview_ready(self) -> bool:
        """WebView 文档是否已装载就绪（load-changed FINISHED）。"""
        return bool(getattr(self, "_webview_ready", False))

    def wait_ai_webview_ready(self, timeout: float = 10.0) -> None:
        """冷启动等待 WebKit 进程/文档就绪（App.run 注册热键前调用）。

        重启系统后首次打开 AI 面板时，WebView 首次 realize/装载会与仍在
        冷 spawn 的 WebKitWebProcess 做同步 IPC（实测冷缓存 ~2s），阻塞主线程
        造成 1-2s 卡死。这里在启动阶段泵主循环，让 load-changed FINISHED
        正常触发，保证用户首次打开时进程已就绪——realize 不再等 spawn。
        超时则放弃（保持原行为，绝不无限等待）。
        """
        if self.is_webview_ready():
            return
        t0 = time.monotonic()
        while not self.is_webview_ready():
            if time.monotonic() - t0 > timeout:
                print(f"[AI] wait webview ready: timeout after {timeout:.1f}s", flush=True)
                return
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            time.sleep(0.01)
        print(f"[AI] webview ready after {(time.monotonic()-t0)*1000:.0f}ms", flush=True)

    def on_panel_shown(self):
        # AI 面板显示时预先初始化 MCP，让第一轮对话就能用上工具
        self._init_mcp()

        if getattr(self, "_suspend_timeout_id", 0) != 0:
            GLib.source_remove(self._suspend_timeout_id)
            self._suspend_timeout_id = 0
        
        if getattr(self, "_webview_suspended", False):
            self._webview_suspended = False
            cached_html = self._ai_html_cache.get(self._ai_conversation_id)
            # 先登记待重放标记：FINISHED 后由 _on_webview_load_changed 重新应用最近
            # 标题 markup——load_html 完成前立即 run_javascript 会被重建的 DOM 丢弃
            self._ai_lbl._pending_header_reapply = True
            # 进程已终止，必须整页重建；force=True 防止指纹守卫抑制恢复重载
            self._load_webview_html(cached_html or "", force=True)
            print("[AI] WebView restored from suspension.", flush=True)
        if getattr(self, "_ai_entry", None):
            self._ai_entry.grab_focus()

    def on_panel_hidden(self):
        # 首开守卫：面板从未显示给用户之前不启动挂起定时器。
        # 避免重启系统后应用自启阶段内部 hide 触发挂起，把 __init__ 冷启动
        # 拉起的 WebKitWebProcess 杀掉，导致首次 Ctrl+Shift+X 冷 spawn 卡顿 1-2s。
        if not getattr(self, "_ai_has_shown", False):
            return
        if getattr(self, "_suspend_timeout_id", 0) != 0:
            GLib.source_remove(self._suspend_timeout_id)
            self._suspend_timeout_id = 0
        
        self._suspend_timeout_id = GLib.timeout_add_seconds(
            self._SUSPEND_DELAY_SECONDS, self._suspend_webview_cb
        )
        print(f"[AI] suspend timer started: {self._SUSPEND_DELAY_SECONDS}s, running_convs={len(self._ai_running_convs)}", flush=True)

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
        # /new、/delete、Ctrl+L 等开启的全新空白会话：快照当前 Settings 中的系统提示词
        self._snapshot_system_prompt()
        self._clear_subagent_bar_instantly()
        self._refresh_subagent_bar()
        self._ai_assistant_buffer = ""
        self._ai_markdown_text = ""
        self._ai_current_assistant_text = ""
        self._ai_response_div_added = False
        self._ai_assistant_html_base = ""
        self._load_webview_html("")
        self._ai_entry.get_buffer().set_text("")
        _, _, _, display_name, _, _, _, _, _ = self._read_model_config(None, None)
        self._ai_lbl.set_markup(f"<b>{_AI_HEADER_TITLE}</b>\n<span size='small' foreground='#888888'>({display_name})</span>")
        self._ai_active_model_info = None
        self._ai_last_prompt_obj = None
        self._ai_title_generated = False
        self._ai_pending_title_notification = False
        self._ai_summary = ""
        self._ai_summary_generating = False
        
        # 继承上一个会话的 Bash 工作路径，显式绑定至新生成的会话 ID
        try:
            import tool_registry
            prev_cwd = tool_registry.get_bash_cwd(session_key=getattr(self, "_ai_conversation_id", None))
            self._ai_conversation_id = uuid4().hex[:12]
            tool_registry.set_bash_cwd(prev_cwd, session_key=self._ai_conversation_id)
        except Exception:
            self._ai_conversation_id = uuid4().hex[:12]

        # 锚定新空白会话的 created_at：不得继承上一个会话的陈旧值（/new、/delete、Ctrl+L 均经此共享重置路径）
        self._ai_conversation_created_at = int(time.time() * 1000)

        self._ai_input_area.set_no_show_all(False)
        self._ai_input_area.show_all()
        
        self._ai_entry.grab_focus()
        self.queue_resize()
        self._ai_history_popover.refresh_dropdown()
        self._update_token_display()

    def start_new_conversation(self):
        """保存当前对话（若有内容），确保 AI 看盘面板可见，并启动一个全新的空白对话。"""
        self._ai_recent_load_pending = False  # 取消延迟加载任务，防止覆盖新新建的空白会话
        self._ai_has_shown = True  # 用户级显示入口：解除首开挂起守卫
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
        self._ai_has_shown = True  # 用户级显示入口：解除首开挂起守卫
        self.on_panel_shown()
        self.separator.set_no_show_all(False)
        self.separator.show()
        self.set_no_show_all(False)
        self.show()
        self.show_all()
        self.queue_resize()

        # 方案3：内容加载延后到下一帧 idle——先让面板首帧绘制出来，
        # 避免 WebProcess 冷 spawn / 会话渲染阻塞 UI 线程造成"卡一下"
        if not self._ai_recent_load_pending:
            self._ai_recent_load_pending = True
            GLib.idle_add(self._load_recent_conversation_deferred)

    def _load_recent_conversation_deferred(self) -> bool:
        """首帧绘制后加载最近会话（原 open_ai_and_load_recent 的加载部分）。"""
        if not getattr(self, "_ai_recent_load_pending", False):
            return False  # 已被 start_new_conversation 取消，跳过加载最近会话
        self._ai_recent_load_pending = False
        try:
            if not self.get_visible():
                return False  # 用户已关闭 AI 面板，跳过加载
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
        except Exception as e:
            print(f"[AI] deferred recent load error: {e}", flush=True)
        return False

    def show_panel(self):
        self._ai_has_shown = True  # 用户级显示入口：解除首开挂起守卫
        self.on_panel_shown()
        self.set_no_show_all(False)
        self.show()
        self.show_all()
        self.separator.set_no_show_all(False)
        self.separator.show()
        self.queue_resize()

    def is_popup_shown(self):
        # ponytail: 下拉可见性已迁至 JS 侧（_closeHistoryDropdown），桥 get_visible
        # 删除后此方法恒 False（与原实现 get_visible 恒 False 行为一致）
        return False

    def reset_state(self):
        self._reset_ai_panel_silent()

    def grab_entry_focus(self):
        self._ai_entry.grab_focus()

    def insert_text_to_input(self, text: str):
        """从外部向 AI 输入框光标处插入文本并聚焦。"""
        buffer = self._ai_entry.get_buffer()
        buffer.insert_at_cursor(text)
        self._safe_grab_ai_entry_focus()

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

        # 主题切换：header/spinner 均为 WebView 内 HTML，随下方整页重建自动跟随，
        # 无需单独更新 GTK 组件。

        # Update GTK widget background colors to match the new theme
        c = self._get_gtk_colors(name)

        for w in (self, self._ai_scrolled):
            if w is not None:
                try:
                    w.override_background_color(Gtk.StateFlags.NORMAL, c["bg"])
                except Exception:
                    pass
        if self._ai_input_area is not None:
            try:
                self._ai_input_area.override_background_color(Gtk.StateFlags.NORMAL, c["input_bg"])
            except Exception:
                pass
        if self._ai_webview:
            self._ai_webview.set_background_color(c["bg"])
            self._apply_webview_gtk_background()

        # WebView 外壳重建守卫：DOM 已存活且外壳指纹（theme + pygments）未变时，
        # 无任何内容需要重载——直接跳过整页重建。
        requested = _webview_shell_fingerprint(name, self._get_pygments_css(name))
        if self._webview_dom_live() and self._loaded_shell_fingerprint == requested:
            return

        if not self._webview_dom_live():
            # suspend/crash 场景：恢复路径（on_panel_shown / crash 重建）会以
            # self._theme + _ai_html_cache 整页重建；此处只需保证缓存持有最新内容。
            if self._ai_conversation_id:
                self._ai_html_cache[self._ai_conversation_id] = getattr(self, "_last_rendered_html", "")
            return

        # 外壳变化 → 整页重建。内容源必须是最近一次完整渲染快照
        # _last_rendered_html（含流式/JS 增量后的最终状态），而不是可能滞后的
        # _ai_markdown_text（不含工具卡片、流式容器等 DOM 增量）。
        html_content = self._last_rendered_html
        if not html_content and self._ai_markdown_text:
            html_content = _markdown_to_html_safe(self._ai_markdown_text)
        self._load_webview_html(html_content, force=True)

        # 流式进行中的会话：重建后 DOM 回到快照，立即重绘当前回合恢复流式显示
        active_st = self._ai_running_convs.get(self._ai_conversation_id) if self._ai_conversation_id else None
        if active_st and active_st.get("streaming") and active_st.get("req_id") is not None:
            GLib.idle_add(self._render_current_assistant_message, active_st["req_id"])
