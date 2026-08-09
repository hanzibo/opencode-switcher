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


from ai_engine.ai_html_template import get_html_template, _get_pygments_css, get_shared_web_context
from dialogs.dynamic_copy_dialog import show_dynamic_copy_dialog
from dialogs.sort_dialog import show_sort_dialog
from dialogs.recycle_bin_dialog import show_recycle_bin_dialog
from dialogs.sort_cats_dialog import show_sort_cats_dialog
from ai_engine.llm_client import _LLMHttpClient, _LLMHttpError, LLMRequestConfig
from system.event_types import StreamEventType
from dialogs.prompt_dialog import show_prompt_dialog
from dialogs.prompts_config_dialog import show_prompts_config_dialog
from views.ai_popovers import AICommandPopover, HistoryPopover
from ai_engine.ai_tool_loop import run_llm_react_loop, ToolLoopContext

AI_BTN_LABEL_SEND = "发送"
AI_BTN_LABEL_STOP = "暂停"


def _to_chat_messages(msgs: List[Dict]) -> List[ChatMessage]:
    from stores.clipboard_store import ChatMessage
    return [ChatMessage(role=m["role"], content=m["content"], 
                        tool_call_id=m.get("tool_call_id"),
                        name=m.get("name"),
                        tool_calls=m.get("tool_calls"),
                        reasoning_content=m.get("reasoning_content")) for m in msgs]


def _ai_stream_request_key(conv_id: str, req_id: int) -> tuple:
    """为一次主 ReAct 流生成稳定请求键。

    键 = (conv_id, req_id)，跨 ReAct 多轮迭代复用同一个键；重试/新请求会
    递增 req_id 得到新键，不与被取消的旧流冲突。并行会话键互不相同，
    取消一个不会误伤另一个。
    """
    return ("ai", conv_id, req_id)


def _ai_summary_request_key(conv_id: str) -> tuple:
    """为摘要流生成稳定请求键（同一会话的摘要流全局唯一，可被定向取消）。"""
    return ("summary", conv_id)


def _webview_shell_fingerprint(theme_name: str, pygments_css: str) -> tuple:
    """WebView 外壳指纹：(theme, pygments_css)。

    与 ``ai_engine.ai_html_template._get_html_shell`` 的 LRU 缓存键完全一致——
    主题或代码高亮样式任一变化，指纹即变化，此时必须完整重载外壳。
    """
    return (theme_name, pygments_css)


def _should_full_reload_webview(loaded_fingerprint, requested_fingerprint,
                                webview_live: bool, webview_suspended: bool,
                                webview_ready: bool) -> bool:
    """决定是否需要完整 ``load_html`` 而非 in-place ``updateContent`` 换内容。

    任一条件命中都必须完整重载（指纹守卫绝不能吞掉这些必需的重载）：
    - WebView 未构建或 DOM 不可用（webview_live=False）
    - Web 进程已被 suspend 主动终止（webview_suspended=True，恢复必须重建 DOM）
    - 文档尚未就绪（webview_ready=False）——上一轮 load_html 还在装载中，
      in-place JS 可能打到未加载完的文档而静默失败
    - 请求的外壳（主题/pygments CSS）与当前已加载的不一致

    仅当 DOM 存活、文档就绪且外壳指纹一致时返回 False —— 此时内容可原地替换。
    """
    if not webview_live:
        return True
    if webview_suspended:
        return True
    if not webview_ready:
        return True
    return loaded_fingerprint != requested_fingerprint


class AIChatPanel(Gtk.Box):
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

    # 主题颜色——通过 theme_config 统一管理
    def _get_gtk_colors(self, theme_name: str) -> dict:
        """Return dict with 'bg', 'header_bg', 'input_bg' as Gdk.RGBA."""
        raw = get_ai_gtk_colors(theme_name)
        return {k: Gdk.RGBA(*v) for k, v in raw.items()}

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

        # Title / Header
        ai_hdr = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        self._ai_hdr = ai_hdr
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
            on_popover_closed=lambda: self.on_combo_popup_hidden() if self.on_combo_popup_hidden else None,
            on_delete_conversation_fn=self._delete_conversation_cleanup,
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
        settings.set_enable_hyperlink_auditing(False)
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
        ai_scrolled.add(self._ai_webview)

        # Synchronize background colors to prevent Wayland resize flickering/leaks
        c = self._get_gtk_colors(self._theme)

        self.override_background_color(Gtk.StateFlags.NORMAL, c["bg"])
        ai_scrolled.override_background_color(Gtk.StateFlags.NORMAL, c["bg"])
        self._ai_webview.set_background_color(c["bg"])
        ai_hdr.override_background_color(Gtk.StateFlags.NORMAL, c["header_bg"])

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
        self._ai_hint_label.get_style_context().add_class("dim-label")
        self._ai_hint_label.set_margin_end(4)
        self._ai_hint_label.set_opacity(0.6)
        self._ai_input_area.pack_start(self._ai_hint_label, False, False, 0)
        self._update_token_display()

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

        # 思考配置优先级：model_info（对话快照）> model_config（当前设置）> 默认值
        if model_info and "thinking_enabled" in model_info:
            thinking_enabled = model_info["thinking_enabled"]
        else:
            thinking_enabled = model_config.thinking_enabled if model_config else False

        if model_info and "reasoning_effort" in model_info:
            reasoning_effort = model_info["reasoning_effort"]
        else:
            reasoning_effort = model_config.reasoning_effort if model_config else "high"

        display_name = f"{model_config.alias} ({model_name})" if model_config else model_name
        return base_url, api_key, model_name, display_name, temperature, max_tokens, top_p, thinking_enabled, reasoning_effort

    def _get_title_model_config(self):
        """Return (base_url, api_key, model_name, temperature, max_tokens, top_p)
        for the model marked as title-generation model, or None if not set."""
        model = next((m for m in self._llm_settings_store.models if m.is_title_model), None)
        if not model:
            return None
        return (model.base_url.strip(), model.api_key.strip(), model.model_name.strip(),
                model.temperature, model.max_tokens, model.top_p)

    # ── MCP 集成 ────────────────────────────────────────────────

    def _init_mcp(self) -> None:
        """初始化 MCP 桥接器和客户端管理器。

        应在 AI 面板首次显示或首次用户输入前调用。
        可重复调用（幂等）。
        """
        if self._mcp_initialized:
            return

        from mcp_integration import GtkAsyncioBridge, MCPClientManager, MCPServerConfig

        self._mcp_bridge = GtkAsyncioBridge.get()
        self._mcp_bridge.start()
        self._mcp_client_mgr = MCPClientManager(self._mcp_bridge)

        # 从设置加载配置并 auto_connect
        if self._ai_settings_store is not None:
            self._load_and_connect_mcp_servers()

        self._mcp_initialized = True
        print(f"[MCP] 初始化完成，已连接 {self._mcp_client_mgr.get_server_count()} 个 Server", flush=True)

    def _load_and_connect_mcp_servers(self) -> None:
        """从 AISettingsStore 加载 MCP Server 配置并自动连接。"""
        if self._ai_settings_store is None or self._mcp_client_mgr is None:
            return

        from mcp_integration import MCPServerConfig
        from mcp_integration.gtk_asyncio_bridge import GtkAsyncioBridge

        server_dicts = getattr(self._ai_settings_store, "mcp_servers", None) or []
        for sd in server_dicts:
            config = MCPServerConfig.from_dict(sd)
            if not (config.enabled and config.auto_connect):
                continue
            # stdio 需要 command，http 需要 url
            if config.transport == "stdio" and not config.command:
                continue
            if config.transport == "http" and not config.url:
                continue
            if config.transport == "http":
                self._mcp_bridge.call_async(
                    self._mcp_client_mgr.connect_http(config),
                    callback=lambda result, err, n=config.name: (
                        print(f"[MCP] {n}: {result[1] if result and not err else err}", flush=True)
                        or (self._refresh_mcp_tools() if result and result[0] else None)
                    ),
                )
            else:
                self._mcp_bridge.call_async(
                    self._mcp_client_mgr.connect_stdio(config),
                    callback=lambda result, err, n=config.name: (
                        print(f"[MCP] {n}: {result[1] if result and not err else err}", flush=True)
                        or (self._refresh_mcp_tools() if result and result[0] else None)
                    ),
                )

    def _refresh_mcp_tools(self) -> None:
        """异步预取 MCP 工具列表并缓存。"""
        if self._mcp_client_mgr is None:
            return
        self._mcp_bridge.call_async(
            self._mcp_client_mgr.list_all_tools(),
            callback=self._on_mcp_tools_ready,
        )

    def _on_mcp_tools_ready(self, tools: list, err: Optional[Exception]) -> None:
        """MCP 工具列表就绪后的回调。"""
        if err:
            print(f"[MCP] 获取工具列表失败: {err}", flush=True)
            return
        if tools:
            self._cached_mcp_tools = tools
            server_count = self._mcp_client_mgr.get_server_count()
            print(
                f"[MCP] 已缓存 {len(tools)} 个工具，来自 {server_count} 个 Server",
                flush=True,
            )

    def _reconfigure_mcp(self) -> None:
        """根据配置变更重新连接/断开 MCP Server。

        在 Settings 保存后调用，使 MCP 配置即时生效而不需重启。
        """
        if not self._mcp_initialized or self._mcp_client_mgr is None:
            return

        from mcp_integration import MCPServerConfig

        # 1. 读取新配置
        server_dicts = getattr(self._ai_settings_store, "mcp_servers", None) or []
        new_configs = {}
        for sd in server_dicts:
            config = MCPServerConfig.from_dict(sd)
            if not (config.enabled and config.auto_connect):
                continue
            # stdio 需要 command，http 需要 url
            if config.transport == "stdio" and not config.command:
                continue
            if config.transport == "http" and not config.url:
                continue
            new_configs[config.name] = config

        # 2. 断开已禁用或不再存在的 Server
        has_disconnect = False
        for name in list(self._mcp_client_mgr.get_all_server_names()):
            if name not in new_configs:
                has_disconnect = True
                self._mcp_bridge.call_async(
                    self._mcp_client_mgr.disconnect(name),
                    callback=lambda result, err, n=name: (
                        print(f"[MCP] 已断开 Server: {n}", flush=True)
                    ),
                )

        # 3. 连接新增或已启用的 Server
        has_connect = False
        for name, config in new_configs.items():
            if not self._mcp_client_mgr.is_connected(name):
                has_connect = True
                self._mcp_bridge.call_async(
                    self._mcp_client_mgr.connect_by_config(config),
                    callback=lambda result, err, n=name: (
                        print(f"[MCP] {n}: {result[1] if result and not err else err}", flush=True)
                        or (self._refresh_mcp_tools() if result and result[0] else None)
                    ),
                )

        # 4. 如果断开或改名，立即清空缓存避免使用旧工具名
        if has_disconnect and has_connect:
            self._cached_mcp_tools = None
            print("[MCP] Server 变更，清空工具缓存等待刷新", flush=True)
        elif has_disconnect and not has_connect:
            self._cached_mcp_tools = None
            print("[MCP] 所有 Server 已禁用，清空工具缓存", flush=True)

    def _snapshot_system_prompt(self) -> None:
        """从 Settings 快照系统提示词（仅新会话入口调用；旧对话由 _switch_to_conversation 加载自身快照）。"""
        self._ai_system_prompt = AISettingsStore().system_prompt

    def _start_new_conversation(self, prompt_text: str):
        self._ai_messages = [{"role": "user", "content": prompt_text}]
        # 新对话建立时快照当前 Settings 中的系统提示词；此后该对话沿用此快照，不受 Settings 热加载影响
        self._snapshot_system_prompt()
        self._ai_conversation_id = uuid4().hex[:12]
        # 锚定新对话的创建时间戳：流结束保存时若无落盘会话则以此落盘（不得继承陈旧值/0）
        self._ai_conversation_created_at = int(time.time() * 1000)
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
        # 记录本次完整渲染结果，供主题重建/crash 恢复使用最新快照
        self._last_rendered_html = user_html
        self._load_webview_html(user_html)

    def _build_llm_messages(self) -> tuple:
        """构建发送给 LLM 的消息列表和额外 system 消息。

        Returns:
            tuple: (messages_list, extra_system_messages)
            - messages_list: 纯对话消息列表（不含摘要）
            - extra_system_messages: 系统提示词 + 历史摘要 system 消息列表（如有），
              仅在 HTTP 请求层注入，不污染 self._ai_messages
        """
        extra = []
        if self._ai_system_prompt:
            extra.append({
                "role": "system",
                "content": self._ai_system_prompt
            })
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
        self._flush_source_id = 0
        self._reasoning_buffer = ""
        self._reasoning_flush_scheduled = False
        self._reasoning_flush_source_id = 0
        self._last_flushed_len = 0
        self._streaming_container_created = False
        self._reseed_reasoning_on_container = False

    def _active_stream_req_id(self) -> int:
        """当前可见会话绑定流的 req_id（容器/增量 flush/最终渲染共用的 id 源）。

        A→B→A 切回后全局 ``_ai_request_id`` 已随每次切换递增，与可见会话仍在运行
        的流实例的 ``req_id`` 不同。``msg-<id>`` 容器、``appendStreamToken``、
        ``appendStreamReasoning`` 与最终 ``updateMessageContainer`` 必须全部落到
        同一 id 上，否则会出现重复空容器且最终渲染指向错误/不存在的 id。流状态已
        弹出（收尾中）时回退到全局 ``_ai_request_id``，维持原有单流行为。
        """
        if self._ai_conversation_id:
            active_st = self._ai_running_convs.get(self._ai_conversation_id)
            if active_st and active_st.get("req_id") is not None:
                return active_st["req_id"]
        return getattr(self, "_ai_request_id", 0)

    def _ensure_streaming_container(self, req_id: Optional[int] = None) -> bool:
        """确保流式消息容器已创建，若未创建则发送 appendMessageContainer JS。

        ``req_id`` 显式传入时优先使用（finalize 阶段流状态已弹出、
        ``_active_stream_req_id`` 会回退到全局 id），缺省时沿用
        ``_active_stream_req_id()`` 的既有单流/切回语义。
        """
        if not self._streaming_container_created and hasattr(self, "_ai_webview") and self._ai_webview:
            if req_id is None:
                req_id = self._active_stream_req_id()
            msg_id = f"msg-{req_id}"
            self._ai_webview.run_javascript(f"appendMessageContainer('{msg_id}');", None, None)
            self._streaming_container_created = True
            return True
        return self._streaming_container_created

    def _on_token_delta(self, text: str):
        """收到 LLM 文本增量，累积到 buffer 并安排 60ms flush（主线程调用）。"""
        if not self._ai_streaming:
            return  # 流已结束，忽略延迟回调防止重复渲染
        if self._STREAM_PERF_LOG:
            print(f"[perf] token_delta: +{len(text)}ch, buffer={len(self._token_buffer)}ch", flush=True)

        self._token_buffer += text

        if not self._flush_scheduled:
            self._flush_scheduled = True
            self._flush_source_id = GLib.timeout_add(self._BATCH_FLUSH_MS, self._flush_token_buffer)

    def _flush_token_buffer(self, req_id: Optional[int] = None) -> bool:
        """60ms 定时器回调：将累积的 token 文本批量 flush 到 WebView。

        ``req_id`` 显式传入时透传给 ``_ensure_streaming_container``（finalize 阶
        段定位流实例容器）；定时器调用（无参）时缺省走 ``_active_stream_req_id``。
        """
        if not self._ai_streaming:
            self._token_buffer = ""
            self._flush_scheduled = False
            self._flush_source_id = 0
            return False  # 流已结束，丢弃残留 buffer
        if self._STREAM_PERF_LOG:
            print(f"[perf] flush_token: {len(self._token_buffer)}ch → JS", flush=True)
        self._flush_scheduled = False
        self._flush_source_id = 0

        if not self._token_buffer:
            return False

        self._ensure_streaming_container(req_id)

        js_code = f"appendStreamToken({json.dumps(self._token_buffer)});"
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(js_code, None, None)

        self._token_buffer = ""
        return False

    # ── Reasoning delta batching (与 token batching 对称) ──

    def _on_reasoning_delta(self, text: str):
        """收到 LLM 推理增量，累积到 buffer 并安排 60ms flush。"""
        if not self._ai_streaming:
            return  # 流已结束，忽略延迟回调
        if self._STREAM_PERF_LOG:
            print(f"[perf] reasoning_delta: +{len(text)}ch, buffer={len(self._reasoning_buffer)}ch", flush=True)
        self._reasoning_buffer += text
        if not self._reasoning_flush_scheduled:
            self._reasoning_flush_scheduled = True
            self._reasoning_flush_source_id = GLib.timeout_add(self._BATCH_FLUSH_MS, self._flush_reasoning_buffer)

    def _flush_reasoning_buffer(self) -> bool:
        """60ms 定时器回调：将累积的推理文本批量 flush 到 WebView。"""
        if not self._ai_streaming:
            self._reasoning_buffer = ""
            self._reasoning_flush_scheduled = False
            self._reasoning_flush_source_id = 0
            return False  # 流已结束，丢弃残留 buffer
        self._reasoning_flush_scheduled = False
        self._reasoning_flush_source_id = 0
        if not self._reasoning_buffer:
            return False

        self._ensure_streaming_container()

        js_code = f"appendStreamReasoning({json.dumps(self._reasoning_buffer)});"
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(js_code, None, None)
        self._reasoning_buffer = ""
        return False

    def _on_tool_calls_started(self, req_id: int):
        """工具调用开始时：停止推理状态，通知 JS 端结束 thinking 动画。

        归属守卫：反查 ``_ai_running_convs`` 定位回调 ``req_id`` 的归属会话，仅当
        归属会话就是当前可见会话且状态仍在流式运行时，才允许取消 flush 定时器
        并发送 ``finishReasoning()``——背景会话、未知/被取代的 ``req_id`` 一律
        no-op，避免后台流取消可见流的 flush 定时器或提前终结可见流的 thinking
        动画（见 docs/plans/ai-streaming-quality-phase1-plan.md 第 2 项）。
        """
        if self._STREAM_PERF_LOG:
            print(f"[perf] tool_calls_started: req={req_id}", flush=True)

        # 归属守卫：反查运行态 → 归属会话必须等于当前可见会话且仍在流式运行
        conv_id = None
        for cid, st in list(getattr(self, "_ai_running_convs", {}).items()):
            if st.get("req_id") == req_id:
                conv_id = cid
                break
        if not conv_id or conv_id != getattr(self, "_ai_conversation_id", None):
            return
        st = getattr(self, "_ai_running_convs", {}).get(conv_id)
        if not st or not st.get("streaming", False):
            return

        # 取消排期定时器 + 发送 finishReasoning 到 JS
        if hasattr(self, "_ai_webview") and self._ai_webview:
            if self._reasoning_flush_source_id:
                GLib.source_remove(self._reasoning_flush_source_id)
                self._reasoning_flush_source_id = 0
                self._reasoning_flush_scheduled = False
            if self._flush_source_id:
                GLib.source_remove(self._flush_source_id)
                self._flush_source_id = 0
                self._flush_scheduled = False
            self._ai_webview.run_javascript("finishReasoning();", None, None)

    def _find_tool_call_by_id(self, tool_call_id: str) -> Optional[dict]:
        """在 _ai_messages 中查找指定 tool_call_id 的工具调用定义。"""
        for msg in self._ai_messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if tc.get("id") == tool_call_id:
                        return tc
        return None

    @property
    def _show_tool_details(self) -> bool:
        return getattr(self._ai_settings_store, 'show_tool_details', True) if self._ai_settings_store else True

    def _on_tool_result(self, tool_call_id: str, result_text: str, status: str, req_id: int):
        if (not self._ai_settings_store
                or not self._ai_settings_store.enable_incremental_tools):
            return

        # 归属守卫：反查运行态定位回调 req_id 的归属会话，仅当归属会话就是
        # 当前可见会话且该流仍在流式运行时，才接受工具结果更新工具卡片。
        # 不能用全局 _ai_request_id 与 req_id 判等——A→B→A 切回后全局计数器已
        # 递增，而可见会话仍在运行的流 req_id 未变 → 合法前台结果会被误丢弃
        # （见 docs/plans/ai-streaming-quality-phase1-plan.md 第 3 项）。
        # 背景会话、未知/被取代的 req_id、非流式状态一律拒绝。
        conv_id = None
        for cid, st in list(getattr(self, "_ai_running_convs", {}).items()):
            if st.get("req_id") == req_id:
                conv_id = cid
                break
        if not conv_id or conv_id != getattr(self, "_ai_conversation_id", None):
            return
        st = getattr(self, "_ai_running_convs", {}).get(conv_id)
        if not st:
            return
        if not st.get("streaming", False) and status != "cancelled":
            return

        if not hasattr(self, "_ai_webview") or not self._ai_webview:
            return

        tool_call = self._find_tool_call_by_id(tool_call_id)
        if not tool_call:
            return

        card_html = _render_tool_card_standalone(tool_call, result_text, status,
                                                  show_details=self._show_tool_details)
        js_code = f"updateToolCard({json.dumps(tool_call_id)}, {json.dumps(card_html)});"
        self._ai_webview.run_javascript(js_code, None, None)

    # ────────────────────────────────────────────────────────────────────

    def _sanitize_tool_calls_schema(self, messages: list) -> bool:
        """防错兜底：校验并修复消息历史中未回应的 assistant tool_calls，防止发送新消息触发 400 错误。"""
        if not messages:
            return False
        modified = False
        i = 0
        while i < len(messages):
            msg = messages[i]
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls and isinstance(tool_calls, list):
                    responded_ids = set()
                    j = i + 1
                    while j < len(messages) and isinstance(messages[j], dict) and messages[j].get("role") == "tool":
                        t_id = messages[j].get("tool_call_id")
                        if t_id:
                            responded_ids.add(t_id)
                        j += 1
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            tc_id = tc.get("id")
                            if tc_id and tc_id not in responded_ids:
                                tool_name = tc.get("function", {}).get("name", "tool")
                                messages.insert(j, {
                                    "role": "tool",
                                    "tool_call_id": tc_id,
                                    "name": tool_name,
                                    "content": "⚠️ [操作已取消]",
                                })
                                responded_ids.add(tc_id)
                                j += 1
                                modified = True
                    i = j - 1
            i += 1
        return modified

    def _send_user_message(self, text: str):
        self._sanitize_tool_calls_schema(self._ai_messages)
        self._init_streaming_state()
        # 初始化 MCP（幂等，仅首次有效）
        self._init_mcp()
        # 空会话首条消息：快照当前 Settings 中的系统提示词（幂等；旧对话 _ai_messages 非空不会刷新）
        if not self._ai_messages:
            self._snapshot_system_prompt()
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
        self._update_token_display()
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

        base_url, api_key, model_name, _, temperature, max_tokens, top_p, thinking_enabled, reasoning_effort = self._read_model_config(
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

        self._update_send_button(True)
        self._ai_entry.placeholder_text = "等待回复中..."
        msgs_for_llm, extra_sys = self._build_llm_messages()
        threading.Thread(
            target=self._run_llm_api_request,
            args=(base_url, api_key, model_name, msgs_for_llm, current_req_id,
                  temperature, max_tokens, top_p, self._ai_markdown_text,
                  self._ai_conversation_id, extra_sys, thinking_enabled, reasoning_effort),
            daemon=True
        ).start()
    def _retry_response(self, assistant_index: int):
        """删除指定的 assistant 回复并重新请求 LLM（丢弃该回复之后的所有消息）。"""
        if self._ai_streaming:
            active_state = self._ai_running_convs.get(self._ai_conversation_id)
            if active_state:
                self._cancel_streams_for_conversation(self._ai_conversation_id)
                self._ai_running_convs.pop(self._ai_conversation_id, None)
            else:
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

        base_url, api_key, model_name, _, temperature, max_tokens, top_p, thinking_enabled, reasoning_effort = self._read_model_config(
            self._ai_last_prompt_obj,
            getattr(self, "_ai_active_model_info", None)
        )
        msgs_for_llm, extra_sys = self._build_llm_messages()
        threading.Thread(
            target=self._run_llm_api_request,
            args=(base_url, api_key, model_name, msgs_for_llm, current_req_id,
                  temperature, max_tokens, top_p, self._ai_markdown_text,
                  self._ai_conversation_id, extra_sys, thinking_enabled, reasoning_effort),
            daemon=True
        ).start()

    def ask_llm_api(self, prompt_text: str, prompt_obj: Optional[CustomPrompt] = None):
        self._ai_has_shown = True  # 用户级显示入口：解除首开挂起守卫
        # 初始化 MCP（幂等，仅首次有效）
        self._init_mcp()

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

        base_url, api_key, model_name, display_name, temperature, max_tokens, top_p, thinking_enabled, reasoning_effort = self._read_model_config(prompt_obj)
        self._ai_active_model_info = {
            "alias": display_name.split(" (")[0] if " (" in display_name else display_name,
            "base_url": base_url,
            "model_name": model_name,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
            "thinking_enabled": thinking_enabled,
            "reasoning_effort": reasoning_effort,
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
            self._last_rendered_html = html
            self._load_webview_html(html)
            return

        self._update_send_button(True)
        msgs_for_llm, extra_sys = self._build_llm_messages()
        threading.Thread(
            target=self._run_llm_api_request,
            args=(base_url, api_key, model_name, msgs_for_llm, current_req_id,
                  temperature, max_tokens, top_p, self._ai_markdown_text,
                  self._ai_conversation_id, extra_sys, thinking_enabled, reasoning_effort),
            daemon=True
        ).start()

    def _run_llm_api_request(self, base_url: str, api_key: str, model_name: str, messages: list,
                              req_id: int, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS,
                              top_p: float = DEFAULT_TOP_P, markdown_text: str = "", conv_id: str = "",
                              extra_system_messages: Optional[list] = None,
                              thinking_enabled: bool = False, reasoning_effort: str = "high"):
        """Start the ReAct loop by delegating execution to the run_llm_react_loop orchestrator."""
        # 等待 MCP 工具缓存就绪（异步预取可能还未完成，但后续迭代会拿到）
        cancel_event = threading.Event()
        # 主 ReAct 流稳定请求键：(conv_id, req_id)，跨迭代复用；重试递增 req_id 得新键
        request_key = _ai_stream_request_key(conv_id, req_id)

        # Initialize conversation background state
        state = {
            "streaming": True,
            "messages": deepcopy(messages),  # Deep copy messages list to isolate background state
            "cancel_event": cancel_event,
            "current_assistant_text": "",
            "current_reasoning_text": "",
            "response_div_added": False,
            "ai_markdown_text": markdown_text,
            "req_id": req_id,
            "request_key": request_key,
            # 会话级元数据：未落盘会话切回时从运行态恢复 created_at/system_prompt
            "created_at": getattr(self, "_ai_conversation_created_at", 0),
            "system_prompt": getattr(self, "_ai_system_prompt", ""),
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
                # v2: 清理 token + reasoning buffer
                self._token_buffer = ""
                self._flush_scheduled = False
                self._reasoning_buffer = ""
                self._reasoning_flush_scheduled = False

        def append_message_callback(msg):
            msg_copy = deepcopy(msg) if isinstance(msg, dict) else msg
            st = self._ai_running_convs.get(conv_id)
            if st:
                st["messages"].append(msg_copy)
            if self._ai_conversation_id == conv_id:
                if st:
                    self._ai_messages = st["messages"]
                else:
                    # State was popped (e.g. pause during tool execution).
                    # Append directly to keep message history valid.
                    self._ai_messages.append(msg_copy)
                    if msg.get("role") == "tool" and self._ai_streaming is False:
                        GLib.idle_add(self._re_render_after_tool_cancel)

                # v3: tool result 由增量管道处理，跳过全量渲染
                # is_active_stream 确保历史对话重建时不走增量（会漏渲染）
                # dom_ready 确保初始骨架渲染（_render_current_assistant_message）已执行完毕
                enable_inc = (self._ai_settings_store
                              and self._ai_settings_store.enable_incremental_tools)
                is_active_stream = (req_id == getattr(self, "_ai_request_id", 0))
                dom_ready = (self._streaming_container_created
                             and (st.get("response_div_added", False) if st else False))
                if msg.get("role") == "tool" and enable_inc and is_active_stream and dom_ready:
                    pass
                else:
                    GLib.idle_add(self._render_current_assistant_message, req_id)

        def set_reasoning_callback(text):
            st = self._ai_running_convs.get(conv_id)
            if st:
                st["current_reasoning_text"] = text
            if self._ai_conversation_id == conv_id:
                self._ai_current_reasoning_text = text

        def set_assistant_callback(text):
            st = self._ai_running_convs.get(conv_id)
            if st:
                st["current_assistant_text"] = text
            if self._ai_conversation_id == conv_id:
                self._ai_current_assistant_text = text

        def append_html_callback(html):
            if self._ai_conversation_id == conv_id:
                GLib.idle_add(self.append_html_to_webview, html)

        def on_llm_error_fn(reason):
            """LLM 请求失败/超时回调（后台线程调用）。

            经 GLib.idle_add 转主线程后由 _render_llm_error 最终校验会话归属：
            - 后台会话/已切换会话的错误不渲染（丢弃）
            - 错误气泡仅 append 到 DOM，不持久化（会话缓存重建后不保留）
            """
            GLib.idle_add(self._render_llm_error, conv_id, reason)

        def on_token_delta_fn(text):
            """v2: 增量回调，后台线程收到 token delta 时调用。"""
            if self._ai_conversation_id == conv_id:
                GLib.idle_add(self._on_token_delta, text)

        def on_reasoning_delta_fn(text):
            """v2: 推理增量回调，后台线程收到 reasoning delta 时调用。"""
            if self._ai_conversation_id == conv_id:
                GLib.idle_add(self._on_reasoning_delta, text)

        def on_tool_result_fn(tool_call_id: str, result_text: str, status: str):
            """v3: 工具结果回调，后台线程收到工具执行结果时调用。"""
            if self._ai_conversation_id == conv_id:
                GLib.idle_add(self._on_tool_result, tool_call_id, result_text, status, req_id)

        # ── 构建 LLMRequestConfig ──
        config = LLMRequestConfig(
            base_url=base_url,
            api_key=api_key,
            model_name=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            timeout=120,
            extra_system_messages=extra_system_messages,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
        )

        # ── 构建 ToolLoopContext（替代 20+ 个独立回调参数） ──
        ctx = ToolLoopContext(
            req_id=req_id,
            cancel_event=cancel_event,
            # ⚠️ 死代码（既有问题，未修）：恒返回本请求 req_id → ai_tool_loop 中
            # get_current_request_id_fn() != req_id 检查永不成立。本意是检测
            # "用户已重试/新请求启动"，需改为读取面板当前 self._ai_request_id。
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
            on_tool_result_fn=on_tool_result_fn,
            on_tool_calls_started_fn=self._on_tool_calls_started,
            on_llm_error_fn=on_llm_error_fn,
            conv_id=conv_id,
            mcp_tool_definitions=getattr(self, "_cached_mcp_tools", None),
            mcp_client_manager=getattr(self, "_mcp_client_mgr", None),
            disabled_tools=getattr(self._ai_settings_store, "disabled_tools", []),
            request_key=request_key,
        )

        run_llm_react_loop(
            llm_client=self._llm_client,
            config=config,
            ctx=ctx,
            messages=state["messages"],
        )

    def _append_assistant_turn_to_cache(self):
        """更新当前会话的 Markdown 和 HTML 缓存。

        全量从 _ai_messages 重建，避免 assistant 已渲染 HTML 被二次
        markdown 处理时与用户消息中的代码 fence 交互导致实体重复转义。
        """
        self._ai_markdown_text = self._rebuild_markdown_from_messages(self._ai_messages)
        self._last_rendered_html = _markdown_to_html_safe(self._ai_markdown_text, fallback_content="")
        if self._ai_conversation_id:
            self._ai_html_cache[self._ai_conversation_id] = self._last_rendered_html

    def _finalize_after_tool_loop(self, req_id: int):
        """Finalize after tool loop ends (used when tool iteration limit hit).

        归属守卫：反查 ``_ai_running_convs`` 失败时回退到当前会话，但必须取回当前
        会话的运行态校验 ``req_id``——/retry 会在同一会话内弹掉旧状态并启动更新
        ``req_id`` 的新流，被取代的旧完成回调一律不得终结/持久化新流（见
        ``_on_llm_api_finished`` 的同款守卫）。
        """
        conv_id = None
        for cid, st in list(self._ai_running_convs.items()):
            if st.get("req_id") == req_id:
                conv_id = cid
                break

        if not conv_id:
            # 反查失败 → 回退到当前会话；取回其运行态（可能已被同会话内更新的流接管）
            conv_id = self._ai_conversation_id
            state = self._ai_running_convs.get(conv_id) if conv_id else None
        else:
            state = self._ai_running_convs.get(conv_id)

        # 看门狗已提前清理（state 被弹掉）或被取代的旧完成（state 属于更新的流）：
        # 一律安全返回，不触碰新流状态（不标记 streaming=False、不渲染、不保存）。
        if state is None or state.get("req_id") != req_id:
            return

        if state:
            state["streaming"] = False

        if self._ai_conversation_id == conv_id:
            target_messages = state["messages"] if state else self._ai_messages
            self._ai_messages = target_messages

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
        self._ai_cancelling = False
        self._handle_stream_end(req_id, conv_id)

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
            show_tool_details=self._show_tool_details,
        ))

        # ★ 全量重建缓存，不使用增量追加，避免内容重复/混入
        rebuilt_markdown = self._rebuild_markdown_from_messages(target_messages)
        html = _markdown_to_html_safe(rebuilt_markdown, fallback_content="")
        self._ai_html_cache[conv_id] = html
        state["ai_markdown_text"] = rebuilt_markdown

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
                    system_prompt=state.get("system_prompt", ""),
                    messages=messages_objs,
                    model_config_snapshot=model_snapshot,
                    created_at=state.get("created_at", int(time.time() * 1000)),
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
                        base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = self._read_model_config(
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

    def _finalize_streaming_render(self, req_id: Optional[int] = None):
        """流结束时 flush 剩余 buffer，触发前端最终 HTML 渲染（仅当前可见对话）。

        ``req_id`` 为被终结流的实例 id：A→B→A 切回后它与全局 ``_ai_request_id``
        不同，必须用它定位容器，最终 ``updateMessageContainer`` 才能命中流式渲染
        期间的 ``msg-<req_id>`` 容器（而非递增后的空 id）。缺省时回退到全局
        ``_ai_request_id``（无切换的单流行为不变）。
        """

        # 0. 取消所有排期的 60ms 定时器，防止 _flush_reasoning_buffer 在 finalize 之前
        #    向 JS 队列插入 appendStreamReasoning 导致不必要的状态切换
        if self._reasoning_flush_source_id:
            GLib.source_remove(self._reasoning_flush_source_id)
            self._reasoning_flush_source_id = 0
            self._reasoning_flush_scheduled = False
        if self._flush_source_id:
            GLib.source_remove(self._flush_source_id)
            self._flush_source_id = 0
            self._flush_scheduled = False

        # 1. flush 剩余 buffer
        # reasoning buffer：用 _appendReasoningCacheOnly 只追加缓存，不触发 _startReasoning
        if self._reasoning_buffer:
            js_code = f"_appendReasoningCacheOnly({json.dumps(self._reasoning_buffer)});"
            if hasattr(self, "_ai_webview") and self._ai_webview:
                self._ai_webview.run_javascript(js_code, None, None)
            self._reasoning_buffer = ""
        # token buffer：正常 flush（显式传入 req_id，避免流状态已弹出后回退全局 id）
        if self._token_buffer:
            self._flush_token_buffer(req_id)

        # 2. 构建最终 HTML
        if req_id is None:
            req_id = getattr(self, "_ai_request_id", 0)
        msg_id = f"msg-{req_id}"
        turn_msgs = self._get_turn_messages()
        output = render_turn(TurnRenderInput(
            turn_messages=turn_msgs,
            all_messages=self._ai_messages,
            streaming_reasoning="",
            streaming_content=self._ai_current_assistant_text,
            is_streaming=False,
            show_tool_details=self._show_tool_details,
        ))

        # 3. 使用 build_update_js + updateMessageContainer 做最终渲染
        #    复用旧版渲染路径，比 onStreamEnd 方式更可靠。
        #    置位 _isStreaming=false 与最终渲染合并在同一段 JS 中（原子化），
        #    保证 updateMessageContainer 执行时 body.streaming 已移除（表格
        #    auto 布局一次到位、_debouncedRenderMath 立即渲染公式），
        #    且不依赖两次 run_javascript 之间的 FIFO 顺序。
        js_final = (
            f"window._isStreaming = false;"
            f"{build_update_js(msg_id, output)}"
        )
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(js_final, None, None)

        # 4. cache 更新
        last_user_idx = -1
        for idx in range(len(self._ai_messages) - 1, -1, -1):
            if self._ai_messages[idx].get("role") == "user":
                last_user_idx = idx
                break
        start_idx = last_user_idx + 1
        self._append_assistant_turn_to_cache()

        # 5. JS 同步：结束 reasoning + 复制按钮 + 窗口控制
        #    （_isStreaming 已在步骤 2.5 提前置 false，此处不再重复设置）
        js_sync = (
            f"finishReasoning();"
            f"(function(){{"
            f"var m=document.getElementById('{msg_id}')?.querySelector('copy-marker');"
            f"if(m&&!m.dataset.msgIndex)m.dataset.msgIndex='{start_idx}';"
            f"addCopyButtons();"
            f"}})();"
            f"_scrollToBottom();"
            f"_throttledWindowing();"
            f"_initRoundNav();"
        )
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(js_sync, None, None)

        # 6. 清理
        self._token_buffer = ""
        self._flush_scheduled = False
        self._flush_source_id = 0
        self._reasoning_buffer = ""
        self._reasoning_flush_scheduled = False
        self._reasoning_flush_source_id = 0
        self._streaming_container_created = False

    def _handle_stream_end(self, req_id: int, conv_id: Optional[str] = None):
        """Common cleanup after a conversation turn ends (save, prune, title gen).

        判定该流是否属于当前可见会话：流状态按 ``conv_id`` 存放
        （``_ai_running_convs[conv_id]["req_id"]``），会话切换会递增全局
        ``_ai_request_id``，因此不能用它与 ``req_id`` 判等——A→B→A 切回后旧流的
        ``req_id`` 与新的全局值不同，但该流仍属于当前会话，必须照常收尾保存。
        ``conv_id`` 由 ``_on_llm_api_finished``/``_finalize_after_tool_loop``
        解析后传入（此时状态已从 ``_ai_running_convs`` 弹出）；未传入（直接调用/
        测试）时反查 ``_ai_running_convs``。过期/背景会话的流结束一律不终结当前
        可见会话。
        """
        if conv_id is None:
            for cid, st in list(getattr(self, "_ai_running_convs", {}).items()):
                if st.get("req_id") == req_id:
                    conv_id = cid
                    break
        if conv_id is not None and conv_id != getattr(self, "_ai_conversation_id", None):
            return
        # 防御：归属无法解析且当前无可见会话（孤儿/陈旧完成）→ 无可终结/保存的目标。
        # 注意：A→B→A 切回后流仍属当前会话，conv_id 必可解析，不受此分支影响。
        if conv_id is None and getattr(self, "_ai_conversation_id", None) is None:
            return
        self._finalize_streaming_render(req_id)
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript("removeTypingIndicators(); _scrollToBottom();", None, None)
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
                base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = self._read_model_config(
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
        self._update_token_display()

    def _get_turn_messages(self) -> List[Dict]:
        """Get messages for the current active turn (from last user msg onward)."""
        last_user_idx = -1
        for idx in range(len(self._ai_messages) - 1, -1, -1):
            if self._ai_messages[idx].get("role") == "user":
                last_user_idx = idx
                break
        return self._ai_messages[last_user_idx + 1:] if last_user_idx != -1 else self._ai_messages

    def _render_current_assistant_message(self, req_id: int):
        """Render the current assistant message for the given request ID."""
        conv_id = None
        for cid, st in list(self._ai_running_convs.items()):
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

            # A→B→A 切回 / DOM 重建：updateContent 已调用 chat.js resetReasoning()
            # 清空 JS 端 _reasoningCache，而流式期间 _render_reasoning_html 返回空串，
            # updateMessageContainer 不会重建推理区域 → 必须用 appendStreamReasoning
            # 重新播种累积推理；工具阶段已开始时再 finishReasoning 切换为 Thought badge。
            # 一次性消费：标志只在切回/重建路径置位，普通流式增量不受影响。
            if getattr(self, "_reseed_reasoning_on_container", False):
                reasoning_text = st.get("current_reasoning_text", "")
                if reasoning_text:
                    self._ai_webview.run_javascript(
                        f"appendStreamReasoning({json.dumps(reasoning_text)});", None, None
                    )
                    if self._turn_has_tool_phase(turn_msgs):
                        self._ai_webview.run_javascript("finishReasoning();", None, None)
                self._reseed_reasoning_on_container = False

        output = render_turn(TurnRenderInput(
            turn_messages=turn_msgs,
            all_messages=self._ai_messages,
            streaming_reasoning=st.get("current_reasoning_text", ""),
            streaming_content=st.get("current_assistant_text", ""),
            is_streaming=True,
            show_tool_details=self._show_tool_details,
        ))
        js_update = build_update_js(msg_id, output)
        self._ai_webview.run_javascript(js_update, None, None)

    def _turn_has_tool_phase(self, turn_msgs: List[Dict]) -> bool:
        """当前轮是否已进入工具阶段（未解决的 tool_calls 或已有 tool 结果消息）。"""
        for msg in turn_msgs:
            if msg.get("role") == "tool":
                return True
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                return True
        return False

    def _get_pygments_css(self, theme: str) -> str:
        return _get_pygments_css(theme, self._pygments_css_cache)

    def get_html_template(self, theme_name, initial_html=""):
        pygments_css = self._get_pygments_css(theme_name)
        return get_html_template(theme_name, initial_html, pygments_css)

    def _webview_dom_live(self) -> bool:
        """WebView 是否存在且 DOM 可用（未被 suspend 主动终止）。"""
        if not (hasattr(self, "_ai_webview") and self._ai_webview):
            return False
        if getattr(self, "_webview_suspended", False):
            return False
        return True

    def _reset_streaming_dom_state(self) -> None:
        """DOM 被整体替换（load_html 或 updateContent 重建 #content）后，
        流式容器与回复 div 均不复存在，须在下一轮渲染时重建。"""
        self._streaming_container_created = False
        self._ai_response_div_added = False
        active_st = self._ai_running_convs.get(self._ai_conversation_id) if self._ai_conversation_id else None
        if active_st:
            active_st["response_div_added"] = False
        # DOM 重建经 updateContent→resetReasoning() 清空 JS 推理缓存，且流式期间
        # _render_reasoning_html 返回空串 → 下一次流式容器 append 须重新播种累积推理。
        self._reseed_reasoning_on_container = True

    def _rebind_active_stream(self, st: dict) -> None:
        """A→B→A 切回后重新绑定当前可见的流式会话。

        ``_switch_to_conversation`` 对**正在流式**的会话用 ``updateContent`` 原地
        重建 ``#content``，流式消息容器与回复 div 均不复存在，但
        ``st["response_div_added"]``/``_streaming_container_created`` 仍保留切走前
        的 True——导致下一次渲染 tick 跳过 ``appendMessageContainer``，增量更新落到
        已被销毁的 DOM 节点上（更新丢失）。

        本方法复位这些陈旧 DOM 标记，并以目标状态流的 ``req_id`` 调度下一次渲染
        tick（``_render_current_assistant_message``，沿用 _apply_theme 恢复路径的
        ``GLib.idle_add`` 模式），使 WebView 重建 ``appendMessageContainer`` 并增量
        更新累积的推理/工具调用/工具结果状态。

        注意：不修改全局 ``_ai_request_id``——它随会话切换递增；流结束的归属判定由
        ``_handle_stream_end`` 按 ``conv_id`` 完成（见其 docstring）。
        """
        self._streaming_container_created = False
        self._ai_response_div_added = False
        st["response_div_added"] = False
        # updateContent 已重建 #content 并 resetReasoning 清空 JS 推理缓存：
        # 下一次流式容器 append 时须用累积推理重新播种（一次性，见
        # _render_current_assistant_message）。
        self._reseed_reasoning_on_container = True
        req_id = st.get("req_id")
        if req_id is not None:
            GLib.idle_add(self._render_current_assistant_message, req_id)

    def _load_webview_html(self, initial_html: str = "", *, force: bool = False) -> None:
        """将 ``initial_html`` 装载进 WebView。

        当 DOM 存活且外壳指纹（theme, pygments_css）未变时，用 in-place
        ``updateContent`` 替换内容，跳过整页 ``load_html``（避免无谓的
        外壳重解析/重排版）；否则完整重载并记录新指纹。

        ``force=True`` 用于初始化 / crash 恢复 / suspend 恢复 / 主题变更——
        这些路径必须完整重载，指纹守卫不会抑制它们。
        """
        fingerprint = _webview_shell_fingerprint(self._theme, self._get_pygments_css(self._theme))
        if not force and not _should_full_reload_webview(
                self._loaded_shell_fingerprint, fingerprint,
                self._webview_dom_live(),
                getattr(self, "_webview_suspended", False),
                getattr(self, "_webview_ready", False)):
            js_code = f"updateContent({json.dumps(initial_html)});"
            self._ai_webview.run_javascript(js_code, None, None)
            self._reset_streaming_dom_state()
            return
        if getattr(self, "_webview_suspended", False):
            # 重新装载使 Web 进程复活，suspend 标记不再成立（避免恢复路径二次重载）
            self._webview_suspended = False
        self._loaded_shell_fingerprint = fingerprint
        self._webview_ready = False  # 装载已发出，FINISHED 前 in-place 更新一律禁止
        self._ai_webview.load_html(self.get_html_template(self._theme, initial_html), "file:///")
        self._reset_streaming_dom_state()

    def _on_webview_load_changed(self, webview, event):
        """跟踪文档装载状态：FINISHED → 就绪；其余事件（PROVISIONAL/COMMITTED/失败）→ 装载中。"""
        if event == WebKit2.LoadEvent.FINISHED:
            self._webview_ready = True
        else:
            self._webview_ready = False

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
        """Called when LLM stream completes with a pure text response (no tool_calls).

        归属守卫：反查 ``_ai_running_convs`` 失败时回退到当前会话，但必须取回当前
        会话的运行态校验 ``req_id``——/retry 会在同一会话内弹掉旧状态并启动更新
        ``req_id`` 的新流，被取代的旧完成回调不得终结/持久化新流（不得标记
        streaming=False、追加 assistant 文本、渲染或保存）。
        """
        conv_id = None
        for cid, st in list(self._ai_running_convs.items()):
            if st.get("req_id") == req_id:
                conv_id = cid
                break

        if not conv_id:
            # 反查失败 → 回退到当前会话；取回其运行态（可能已被同会话内更新的流接管）
            conv_id = self._ai_conversation_id
            state = self._ai_running_convs.get(conv_id) if conv_id else None
        else:
            state = self._ai_running_convs.get(conv_id)

        # 看门狗已提前清理（state 被弹掉）或被取代的旧完成（state 属于更新的流）：
        # 一律安全返回，不触碰新流状态（不标记 streaming=False、不渲染、不保存）。
        if state is None or state.get("req_id") != req_id:
            return

        assistant_text = state["current_assistant_text"] if state else self._ai_current_assistant_text
        reasoning = state["current_reasoning_text"] if state else self._ai_current_reasoning_text
        assistant_msg = {"role": "assistant", "content": assistant_text}
        if reasoning:
            assistant_msg["reasoning_content"] = reasoning

        target_messages = state["messages"] if state else self._ai_messages
        # L-2：仅在有实际内容时追加 assistant 消息——防止取消/暂停无输出时
        # 产生空 assistant 消息，也避免与 _cancel_streaming_if_active 的清空
        # 配合后出现 {role: assistant, content: ""} 残留。
        if target_messages and (assistant_text or reasoning):
            target_messages.append(assistant_msg)

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

        # 记录取消状态快照：_ai_cancelling 随后被重置，通知判定需用取消前值。
        was_cancelling = self._ai_cancelling
        self._ai_running_convs.pop(conv_id, None)
        self._ai_cancelling = False

        if getattr(self, "_ai_cancel_watchdog_id", 0) != 0:
            GLib.source_remove(self._ai_cancel_watchdog_id)
            self._ai_cancel_watchdog_id = 0

            model_info = getattr(self, "_ai_active_model_info", None)
            _, _, _, display_name, _, _, _, _, _ = self._read_model_config(None, model_info)
            if hasattr(self, "_ai_lbl") and self._ai_lbl:
                self._ai_lbl.set_markup(f"<b>AI 助手看盘</b>\n<span size='small' foreground='#888888'>({display_name})</span>")

        # 无条件消费错误标志：即使本轮零输出（错误发生后文本为空），也须消费，
        # 避免残留到同一会话下一轮成功回答被误判"错误未消费"而吞掉通知。
        error_pending = getattr(self, "_ai_error_pending_conv", None)
        if error_pending == conv_id:
            self._ai_error_pending_conv = None

        # 主对话 AI 正式回答输出结束 → 自动弹桌面通知。
        # 仅在当前可见会话、未被取消、本轮回有实际回答内容、且本轮无错误时触发。
        if (self._ai_conversation_id == conv_id
                and not was_cancelling
                and (assistant_text or reasoning)
                and error_pending != conv_id
                and getattr(self._ai_settings_store, "enable_answer_notification", True)):
            self._notify_ai_answer_finished(assistant_text or reasoning)

        self._handle_stream_end(req_id, conv_id)

    def _notify_ai_answer_finished(self, answer_text: str) -> None:
        """主对话 AI 正式回答结束后弹桌面通知（best-effort，后台线程不阻塞 UI）。

        复用 tool_registry/notification.py 的 notify-send 封装；发送在 daemon
        线程执行（notify-send 自身有 10s 超时），任何失败静默（仅打日志），
        绝不阻塞主线程渲染/保存收尾。
        """
        try:
            preview = re.sub(r"\s+", " ", answer_text or "").strip()[:80]
            threading.Thread(
                target=execute_send_notification,
                kwargs={
                    "summary": "🤖 AI 回答完成",
                    "body": preview or "回答已生成",
                    "urgency": "normal",
                    "expire_time": 8000,
                    "icon": "dialog-information",
                },
                daemon=True,
            ).start()
        except Exception as e:
            print(f"[notify] 通知发送异常: {e}", flush=True)

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

    def _on_decide_policy(self, webview, decision, decision_type):
        """处理 WebView 导航策略：opencode:// URI 和外部链接。"""
        if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            nav_action = decision.get_navigation_action()
            uri = nav_action.get_request().get_uri()
            # 统一取消导航：所有 opencode:// 自定义协议和外部链接都不应实际加载
            if uri and (uri.startswith("opencode://") or not (uri.startswith("file://") or uri == "about:blank")):
                decision.ignore()
            else:
                return False
            if uri.startswith("opencode://copy-response"):
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
                                self._copy_to_clipboard(content)
                                if self.on_ai_copy_finished:
                                    GLib.idle_add(self.on_ai_copy_finished)
                    except (ValueError, IndexError):
                        pass
                return True
            if uri.startswith("opencode://copy-input"):
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
                                    self._copy_to_clipboard(content)
                                    if self.on_ai_copy_finished:
                                        GLib.idle_add(self.on_ai_copy_finished)
                    except (ValueError, IndexError):
                        pass
                return True
            if uri.startswith("opencode://retry"):
                qs = parse_qs(urlparse(uri).query)
                index_str = qs.get("index", [None])[0]
                if index_str is not None:
                    try:
                        self._retry_response(int(index_str))
                    except (ValueError, IndexError):
                        pass
                return True
            if uri.startswith("opencode://rollback-round"):
                qs = parse_qs(urlparse(uri).query)
                round_str = qs.get("round", [None])[0]
                if round_str is not None:
                    try:
                        self._rollback_to_round(int(round_str))
                    except (ValueError, IndexError):
                        pass
                return True
            # 外部链接：在默认浏览器中打开
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
            except Exception as e:
                print(f"Error launching external link {uri}: {e}", flush=True)
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

        # 进程已死，DOM 必须整页重建；_load_webview_html(force=True) 同时重置
        # 流式容器状态（DOM 已重建，流式容器需重新创建）
        self._load_webview_html(current_html if current_html else "", force=True)

        if parent:
            def _reparent_webview():
                if parent:
                    parent.remove(old_webview)
                    parent.add(self._ai_webview)
                    self._ai_webview.show()
                return False
            GLib.idle_add(_reparent_webview)

        if current_html:
            self._ai_webview.run_javascript(f"updateContent({json.dumps(current_html)});", None, None)

        self._ai_webview.connect("web-process-terminated", self._on_webview_crashed)
        self._ai_webview.connect("decide-policy", self._on_decide_policy)
        self._ai_webview.connect("context-menu", lambda *_: True)
        self._ai_webview.connect("load-changed", self._on_webview_load_changed)

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

    def _build_subagent_tooltip(self, sid: Any, info: dict) -> str:
        """构建子代理状态块浮窗文本（悬停显示，含轮次/工具计数与工具历史）。

        工具名来自函数名，无 HTML 注入风险；使用纯文本 set_tooltip_text。
        """
        status = info.get("status", "unknown")
        action = info.get("action", "")
        turn = info.get("turn", 0)
        tool_count = info.get("tool_calls_count", 0)
        if status == "running":
            status_line = "状态：运行中 🔄"
        elif status == "completed":
            status_line = "状态：已完成 ✅"
        else:
            status_line = "状态：失败 ❌"
        lines = [
            f"子代理 {sid}",
            status_line,
        ]
        if status == "running":
            lines.append(f"动作：{action or 'Thinking'}")
        lines.append(f"轮次：第 {turn} 轮")
        lines.append(f"工具调用：{tool_count} 次")
        lines.append("── 最近工具 ──")
        hist = info.get("tools_history") or []
        if hist:
            lines.extend(f"{i}. {name}" for i, name in enumerate(hist, 1))
        else:
            lines.append("（暂无）")
        return "\n".join(lines)

    def _create_subagent_block(self, sid: Any, info: dict):
        """Create a FlowBoxChild for a sub-agent status block."""
        child = Gtk.FlowBoxChild.new()
        box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)

        # 进度感知：运行中左侧持续旋转的加载圈（A 方案）
        spinner = Gtk.Spinner.new()
        spinner.get_style_context().add_class("subagent-spinner")  # 尺寸由 CSS 控制（🔵-3）
        spinner.set_no_show_all(True)  # show_all() 不显示未手动 show 的 spinner（🟡-1）
        box.pack_start(spinner, False, False, 0)

        local_id = sid.split("-")[-1] if isinstance(sid, str) and "-" in sid else sid
        status = info.get("status", "unknown")
        turn = info.get("turn", 0)
        tool_count = info.get("tool_calls_count", 0)

        if status == "running":
            # 轮次/工具计数（A+B 方案）：第 N 轮每轮必变，工具×M 单调递增
            spinner.set_no_show_all(False)
            label_text = f"子代理 {local_id} · 第 {turn} 轮 · 工具×{tool_count}"
            spinner.start()
        else:
            label_text = f"子代理 {local_id}"
            spinner.stop()
            spinner.hide()

        label = Gtk.Label.new(label_text)
        label.set_margin_start(4)
        label.set_margin_end(4)
        label.set_margin_top(2)
        label.set_margin_bottom(2)
        box.pack_start(label, True, True, 0)
        child.add(box)

        tooltip_text = self._build_subagent_tooltip(sid, info)
        box_ctx = box.get_style_context()
        if status == "completed":
            box_ctx.add_class("subagent-block-done")
        elif status == "running":
            box_ctx.add_class("subagent-block-running")
        else:
            box_ctx.add_class("subagent-block-failed")
        child.set_tooltip_text(tooltip_text)

        self._ai_subagent_bar.add(child)
        self._ai_subagent_blocks[sid] = (child, child, box, spinner)
        self._ai_subagent_bar.show_all()

    def _update_subagent_block(self, sid: Any, info: dict):
        """Update an existing block when sub-agent status changes."""
        entry = self._ai_subagent_blocks.get(sid)
        if entry is None:
            return
        child, event_box, box, spinner = entry
        status = info.get("status", "unknown")
        action = info.get("action", "")
        turn = info.get("turn", 0)
        tool_count = info.get("tool_calls_count", 0)
        local_id = sid.split("-")[-1] if isinstance(sid, str) and "-" in sid else sid
        ctx = box.get_style_context()

        # 更新标签文本（box 内第一个 child 是 spinner，其后是 Label）
        lbl = next((w for w in box.get_children() if isinstance(w, Gtk.Label)), None)
        if lbl is not None:
            if status == "running":
                lbl.set_text(f"子代理 {local_id} · 第 {turn} 轮 · 工具×{tool_count}")
            else:
                lbl.set_text(f"子代理 {local_id}")

        # 更新 spinner 生命周期：running 持续旋转，终态停止并隐藏（🟡-1）
        if status == "running":
            spinner.set_no_show_all(False)
            spinner.show()
            spinner.start()
        else:
            spinner.stop()
            spinner.hide()

        tooltip_text = self._build_subagent_tooltip(sid, info)
        if status == "completed":
            ctx.remove_class("subagent-block-running")
            ctx.add_class("subagent-block-done")
            event_box.set_tooltip_text(tooltip_text)
        elif status == "running":
            if ctx.has_class("subagent-block-done"):
                ctx.remove_class("subagent-block-done")
                self._ai_selected_subagents.discard(sid)
            ctx.add_class("subagent-block-running")
            event_box.set_tooltip_text(tooltip_text)
        else:
            ctx.remove_class("subagent-block-running")
            ctx.add_class("subagent-block-failed")
            event_box.set_tooltip_text(tooltip_text)

    def _remove_subagent_block(self, sid: Any):
        """Remove a sub-agent block and clean up state."""
        self._ai_selected_subagents.discard(sid)
        entry = self._ai_subagent_blocks.pop(sid, None)
        if not entry:
            return
        child, _event_box, _box, spinner = entry
        spinner.stop()  # 防御：移除前停止动画，避免残留旋转
        # 防御：child 可能已被手动移除（如发送路径先 remove 再触发异步 None 事件，
        # 或清理竞态下重复移除），GTK 对非子 widget 的 remove 虽为 no-op，仍显式跳过
        if child.get_parent() is not None:
            self._ai_subagent_bar.remove(child)
        if not self._ai_subagent_blocks:
            self._ai_subagent_bar.get_style_context().remove_class("subagent-status-bar")
            self._ai_subagent_bar.hide()

    def _clear_subagent_bar_instantly(self):
        """Instantly clear all subagent blocks from the status bar UI."""
        self._ai_subagent_bar.get_style_context().remove_class("subagent-status-bar")
        self._ai_subagent_bar.hide()
        for _sid, entry in self._ai_subagent_blocks.items():
            entry[3].stop()  # 停止所有 spinner，避免动画残留
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
        child, event_box, box, _spinner = entry
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

    def _delete_conversation_cleanup(self, conv_id: str):
        """删除指定会话时，关断后台流与工具循环，释放 CWD 绑定并弹出 HTML 缓存。"""
        if not conv_id:
            return
        if conv_id in self._ai_running_convs:
            self._cancel_streams_for_conversation(conv_id)
        try:
            from tool_registry.bash import close_bash_session
            close_bash_session(conv_id)
        except Exception:
            pass
        self._conversation_store.delete_conversation(conv_id)
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
                self._ai_lbl.set_markup("<b>AI 助手看盘</b>\n<span size='small' foreground='#f43f5e'>(正在中止...)</span>")
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
            "thinking_enabled": model.thinking_enabled,
            "reasoning_effort": model.reasoning_effort,
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

    def _cancel_streams_for_conversation(self, conv_id: str) -> bool:
        """定向取消某个会话的活跃流（主 ReAct 流 + 摘要流），不触碰其他会话。

        看门狗/用户取消均经此路径。主 ReAct 流与摘要流各自持有稳定
        request_key，取消时按键精确中止；并行会话键不同，互不误伤。

        Returns
        -------
        bool
            True 表示该会话确有活跃主流（已置 cancel_event 并按键中止），
            False 表示没有（调用方应回退到旧的无键取消全部语义）。
        """
        st = self._ai_running_convs.get(conv_id)
        if not st:
            return False
        ce = st.get("cancel_event")
        if ce:
            ce.set()
        request_key = st.get("request_key")
        if request_key is not None:
            self._llm_client.cancel_active_request(request_key)
        if conv_id:
            self._llm_client.cancel_active_request(_ai_summary_request_key(conv_id))
        return True

    def _cancel_streaming_if_active(self):
        """If a streaming response is in progress, cancel it and reset state."""
        if self._ai_streaming:
            # 设置当前会话的 cancel_event（与暂停按钮一致），使 parse_sse_events
            # 走静默返回而非抛 _LLMHttpError；找不到则兜底取消所有运行中的流。
            active_state = self._ai_running_convs.get(self._ai_conversation_id)
            if active_state and active_state.get("cancel_event"):
                self._cancel_streams_for_conversation(self._ai_conversation_id)
                # L-2：清空流式缓存。partial 由 _on_llm_api_finished 统一追加
                # （此处不手动追加），避免同一段文本在后台收尾时被重复写入历史。
                # 推理缓存同步清空，避免取消后因 reasoning 非空误弹"完成"通知。
                active_state["current_assistant_text"] = ""
                active_state["current_reasoning_text"] = ""
                self._ai_current_assistant_text = ""
                self._ai_current_reasoning_text = ""
            else:
                for st in list(self._ai_running_convs.values()):
                    ce = st.get("cancel_event")
                    if ce:
                        ce.set()
                self._llm_client.cancel_active_request()
            self._update_send_button(False)
            self._ai_streaming = False
            self._ai_cancelling = False
            self._ai_spinner.stop()
            self._ai_spinner.hide()

    def _force_cleanup_after_cancel(self) -> bool:
        """看门狗：暂停后后台线程未在超时内完成，强制清理。"""
        if not self._ai_cancelling:
            return False  # 已清理过
        self._ai_cancelling = False
        self._ai_running_convs.pop(self._ai_conversation_id, None)
        self._ai_streaming = False
        if self._sanitize_tool_calls_schema(self._ai_messages):
            self._re_render_after_tool_cancel()
        self._update_send_button(True, sensitive=True)
        self._ai_entry.placeholder_text = "输入后续问题..."
        self._ai_entry.grab_focus()
        self._ai_spinner.stop()
        self._ai_spinner.hide()
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript("removeTypingIndicators();", None, None)
        self._ai_cancel_watchdog_id = 0
        model_info = getattr(self, "_ai_active_model_info", None)
        _, _, _, display_name, _, _, _, _, _ = self._read_model_config(None, model_info)
        if hasattr(self, "_ai_lbl") and self._ai_lbl:
            self._ai_lbl.set_markup(f"<b>AI 助手看盘</b>\n<span size='small' foreground='#888888'>({display_name})</span>")
        print("[cancel] 看门狗触发：强制清理取消状态", flush=True)
        return False  # 单次 GLib timeout

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

    def _render_llm_error(self, conv_id: str, reason: str):
        """主线程：在 WebView 中渲染 LLM 请求失败/超时错误气泡。

        由后台线程经 GLib.idle_add 转接调用。主线程内做最终校验：
        - 会话归属：错误属于已切换走的会话则丢弃（闭合 M-3 竞态窗口）
        - 用户主动暂停/取消（_ai_cancelling）期间不弹气泡（覆盖超时 Timer
          与暂停竞态、以及取消路径误报等场景）
        文本已在此处 html.escape，杜绝注入。
        """
        if self._ai_conversation_id != conv_id:
            return
        if self._ai_cancelling:
            return
        # 错误标志：_on_llm_api_finished 通知前消费，避免 LLM 失败也弹"回答完成"。
        self._ai_error_pending_conv = conv_id
        safe = html.escape(reason)
        self.append_html_to_webview(
            '<div class="chat-system-error">❌ ' + safe + '</div>'
        )

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
        self._save_current_conversation(self._build_model_snapshot())

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

    def _handle_fork_command(self, custom_title: Optional[str] = None):
        """Handle /fork command: create a duplicated conversation branch with a new ID."""
        if not self._ai_conversation_id or not self._ai_messages:
            self.append_html_to_webview(
                '<div class="chat-simple-error">⚠️ 当前没有活跃且包含消息的对话可供 Fork 分支。</div>'
            )
            return

        if self._ai_streaming or self._ai_cancelling:
            self.append_html_to_webview(
                '<div class="chat-simple-error">⚠️ 当前正处于回复生成状态，请在当前轮次完成后再执行 /fork 分支。</div>'
            )
            return

        # 1. Save current conversation state first
        try:
            model_snapshot = self._build_model_snapshot()
            self._save_current_conversation(model_snapshot, preserve_updated_at=True)
        except Exception as e:
            print(f"Error saving conversation before fork: {e}", flush=True)
            self.append_html_to_webview(
                f'<div class="chat-simple-error">❌ 分支建立失败：无法保存当前对话状态 ({html.escape(str(e))})。</div>'
            )
            return

        current_id = self._ai_conversation_id

        # 2. Fork conversation in store
        new_conv = self._conversation_store.fork_conversation(current_id, custom_title)
        if not new_conv:
            self.append_html_to_webview(
                '<div class="chat-simple-error">❌ 分支建立失败：无法生成新对话分支或磁盘写入失败。</div>'
            )
            return

        # 3. Switch to the newly created conversation branch (skip duplicate save)
        self._switch_to_conversation(new_conv.id, save_current=False)

        # 4. Append success notification message to the new conversation view
        escaped_title = html.escape(new_conv.title)
        escaped_id = html.escape(new_conv.id)
        msg_count = len(new_conv.messages)
        self.append_html_to_webview(
            f'<div class="chat-simple-info">'
            f'🔀 <strong>已成功建立并切换至对话分支</strong><br/>'
            f'📌 标题: <strong>{escaped_title}</strong> (ID: <code>{escaped_id}</code>)<br/>'
            f'📦 已继承原对话全部 {msg_count} 条上下文记录</div>'
        )

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
                    base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = self._read_model_config(
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

    def _handle_summary_command(self, text: str):
        """Handle /summary command: summarize old messages and trim to keep N.

        Called from _on_send_clicked (GTK signal callback, main thread).
        Format: /summary keep=N  (default from settings trim_target)
        """
        # 1. Read default from settings (与自动裁剪统一)
        if self._ai_settings_store is not None:
            default_keep = self._ai_settings_store.trim_target
        else:
            from stores.clipboard_store import AISettingsStore
            default_keep = AISettingsStore().trim_target

        # 2. Parse keep parameter
        keep = default_keep
        if text.startswith("/summary "):
            arg = text[len("/summary "):].strip()
            if arg.startswith("keep="):
                try:
                    keep = int(arg[len("keep="):])
                except ValueError:
                    pass
            else:
                try:
                    keep = int(arg)
                except ValueError:
                    pass

        # 2. Validate conversation state
        if not self._ai_messages:
            self.append_html_to_webview(
                '<div class="chat-simple-error">对话为空，无法压缩。</div>'
            )
            return

        total = len(self._ai_messages)
        if keep >= total:
            self.append_html_to_webview(
                f'<div class="chat-simple-error">消息数不足（共 {total} 条，需保留 {keep} 条），无法压缩。</div>'
            )
            return

        keep = max(5, min(keep, total - 1))
        self.append_html_to_webview(
            f'<div class="chat-simple-info">⏳ 开始压缩上下文，保留最近 {keep} 条，旧消息正在压缩为摘要...</div>'
        )

        # 3. Check not already generating
        if self._ai_summary_generating:
            self.append_html_to_webview(
                '<div class="chat-simple-error">已在生成摘要中，请等待完成后再试。</div>'
            )
            return

        # 4. Check summary is enabled
        if self._ai_settings_store and not self._ai_settings_store.enable_summary:
            self.append_html_to_webview(
                '<div class="chat-simple-error">摘要功能未启用（设置中 enable_summary=False），请在设置中开启。</div>'
            )
            return

        # 5. Calculate pruned messages and start async
        pruned = self._ai_messages[:-keep]  # old messages to summarize
        trim_target = keep + 1               # _apply_prune: keep trim_target-1 + first = trim_target
        self._ai_summary_generating = True
        self._show_summary_status()

        threading.Thread(
            target=self._generate_summary_async,
            args=(list(pruned), trim_target),
            daemon=True
        ).start()

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

    def _handle_skill_command(self, text: str):
        """处理 /skill、/skill:<name>、/skill <name> 手动检索与触发。"""
        raw_arg = text.strip()
        skill_name = ""
        if raw_arg.startswith("/skill:"):
            skill_name = raw_arg[len("/skill:"):].strip()
        elif raw_arg.startswith("/skill "):
            skill_name = raw_arg[len("/skill "):].strip()
        elif raw_arg.startswith("skill:"):
            skill_name = raw_arg[len("skill:"):].strip()

        from stores.skill_store import SkillStore
        from tool_registry import get_bash_cwd
        cwd = get_bash_cwd(session_key=self._ai_conversation_id)
        store = SkillStore()

        if not skill_name or skill_name == "/skill":
            skills = store.get_skills(cwd=cwd)
            if not skills:
                info_html = (
                    '<div class="chat-status-notice">'
                    '🔍 当前未发现可用的 Skill。<br/>'
                    '<span size="small" foreground="#888888">'
                    '全局目录: ~/.config/opencode-switcher/skills/<br/>'
                    '项目目录: .opencode/skills/'
                    '</span></div>'
                )
            else:
                items_html = "".join([
                    f'<li><strong>skill:{html.escape(sk.name)}</strong> — {html.escape(sk.description)}</li>'
                    for sk in skills
                ])
                info_html = (
                    f'<div class="chat-model-info">'
                    f'🛠️ <strong>当前可用 Skill 列表 ({len(skills)} 个):</strong>'
                    f'<ul style="margin: 6px 0 0 16px; padding: 0;">{items_html}</ul>'
                    f'<span style="font-size: small; color: #888888;">提示：输入 /skill:&lt;name&gt; 可手动触发特定 Skill</span>'
                    f'</div>'
                )
            self.append_html_to_webview(info_html)
            return

        content = store.get_skill_content(skill_name, cwd=cwd)
        if not content:
            available = store.get_skills(cwd=cwd)
            names = [s.name for s in available]
            self.append_html_to_webview(
                f'<div class="chat-system-error">❌ 找不到名为「{html.escape(skill_name)}」的 Skill。'
                f'当前可用: {html.escape(str(names))}</div>'
            )
            return

        _MAX_SKILL_PAYLOAD_LEN = 30000
        if len(content) > _MAX_SKILL_PAYLOAD_LEN:
            content = content[:_MAX_SKILL_PAYLOAD_LEN] + "\n\n...[内容过长已自动截断]"

        notice_html = (
            f'<div class="chat-status-notice">'
            f'📖 <strong>已手动调取并激活 Skill：「{html.escape(skill_name)}」</strong>'
            f'</div>'
        )
        self.append_html_to_webview(notice_html)

        prompt_payload = f"[手动触发 Skill: {skill_name}]\n\n{content}\n\n请严格按上述 Skill 指导完成任务。"
        buf = self._ai_entry.get_buffer()
        buf.set_text(prompt_payload)
        self._on_send_clicked()

    def _handle_ai_polish_command(self, raw_input: str):
        """处理 /ai-polish <raw_text> 命令：
        使用无上下文的独立润色模型请求，设置 30s 超时控制，成功后自动回填至输入框供用户二次确认。
        """
        # 1. 从 _ai_messages 逆向提取最近一次 assistant 的正式回答
        last_asst_text = ""
        for msg in reversed(self._ai_messages):
            if msg.get("role") == "assistant" and isinstance(msg.get("content"), str) and msg.get("content").strip():
                last_asst_text = msg.get("content").strip()
                break

        # 2. 从 AI 设置中获取自定义润色 Prompt 模板并动态替换占位符（支持 - 与 _ 两种写法）
        from stores.clipboard_store import _DEFAULT_POLISH_TEMPLATE
        raw_template = getattr(self._ai_settings_store, "polish_prompt_template", "") or ""
        template = raw_template.strip() or _DEFAULT_POLISH_TEMPLATE
        last_answer_fill = last_asst_text if last_asst_text else "(无历史对话，此为首条提问)"

        has_placeholder = (
            "{model-last-answer}" in template or "{model_last_answer}" in template or
            "{user-original-message}" in template or "{user_original_message}" in template
        )
        if has_placeholder:
            prompt = (
                template
                .replace("{model-last-answer}", last_answer_fill)
                .replace("{model_last_answer}", last_answer_fill)
                .replace("{user-original-message}", raw_input)
                .replace("{user_original_message}", raw_input)
            )
        else:
            # 兜底：若用户修改后未包含任何占位符，直接追加用户原始文本
            prompt = f"{template}\n\n{raw_input}"

        # 3. 设置输入框青绿色状态并禁用输入
        buf = self._ai_entry.get_buffer()
        buf.set_text("")
        old_placeholder = getattr(self._ai_entry, "placeholder_text", "给 AI 助手发送消息...")
        self._ai_entry.placeholder_text = "✨ 等待 AI 润色中..."
        self._ai_entry.set_sensitive(False)
        self._update_send_button(False, sensitive=False)
        target_conv_id = getattr(self, "_ai_conversation_id", None)

        # 4. 获取润色模型配置
        polish_config = self._llm_settings_store.get_polish_model()
        if not polish_config or not getattr(polish_config, "api_key", "").strip() or not getattr(polish_config, "base_url", "").strip():
            self._ai_entry.placeholder_text = old_placeholder
            self._ai_entry.set_sensitive(True)
            self._update_send_button(False, sensitive=True)
            buf.set_text(raw_input)
            info_html = (
                '<div class="chat-model-info" style="color: #f43f5e; border-color: #f43f5e;">'
                '❌ <strong>润色失败</strong>：未配置有效润色/默认模型 API Key 或 Base URL。'
                '</div>'
            )
            self.append_html_to_webview(info_html)
            return

        def _on_polish_complete(success: bool, result_text: str):
            # 会话竞态防护：仅当当前展示的会话依然是发起润色时的会话，才向输入框与控件恢复状态
            if getattr(self, "_ai_conversation_id", None) == target_conv_id:
                self._ai_entry.placeholder_text = old_placeholder
                self._ai_entry.set_sensitive(True)
                self._update_send_button(False, sensitive=True)
                if success and result_text:
                    buf.set_text(result_text)
                    self._ai_entry.grab_focus()
                    info_html = (
                        '<div class="chat-model-info" style="color: #10b981; border-color: #10b981;">'
                        '✨ <strong>AI 润色成功</strong>：已将优化后的文本填回输入框，请确认或修改后按 Enter 发送。'
                        '</div>'
                    )
                    self.append_html_to_webview(info_html)
                else:
                    buf.set_text(raw_input)
                    self._ai_entry.grab_focus()
                    err_msg = result_text if result_text else "超时（30秒）或服务响应异常"
                    escaped_err = html.escape(err_msg)
                    info_html = (
                        '<div class="chat-model-info" style="color: #f59e0b; border-color: #f59e0b;">'
                        f'⚠️ <strong>AI 润色失败</strong>（{escaped_err}），已自动恢复未润色的原始提问。'
                        '</div>'
                    )
                    self.append_html_to_webview(info_html)

        def _worker_thread():
            try:
                from ai_engine.llm_client import LLMRequestConfig
                config = LLMRequestConfig(
                    base_url=polish_config.base_url,
                    api_key=polish_config.api_key,
                    model_name=polish_config.model_name,
                    temperature=getattr(polish_config, "temperature", 1.0),
                    max_tokens=getattr(polish_config, "max_tokens", 4096),
                    top_p=getattr(polish_config, "top_p", 1.0),
                    timeout=30,
                )
                messages = [{"role": "user", "content": prompt}]
                msg = self._llm_client.sync_chat_completion(config, messages)
                content = msg.get("content") if msg else None
                if content:
                    GLib.idle_add(_on_polish_complete, True, content.strip())
                else:
                    GLib.idle_add(_on_polish_complete, False, "模型响应为空")
            except Exception as e:
                GLib.idle_add(_on_polish_complete, False, str(e))

        t = threading.Thread(target=_worker_thread, daemon=True)
        t.start()

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

    def _rebuild_markdown_from_messages(
        self,
        messages: List[Dict],
        streaming_reasoning: str = "",
        streaming_content: str = "",
        is_streaming: bool = False
    ) -> str:
        """Convert OpenAI-format message list back to rendered markdown text."""
        show_details = self._show_tool_details
        return _rebuild_markdown_from_messages(
            messages,
            streaming_reasoning=streaming_reasoning,
            streaming_content=streaming_content,
            is_streaming=is_streaming,
            show_details=show_details,
        )

    # ── Token 计数（混合方案） ──

    def _estimate_token_count(self, messages: Optional[List[Dict]] = None) -> int:
        """估算消息列表的 token 数。

        混合方案：
          - tiktoken cl100k_base × 校准因子 0.89（兼容 DeepSeek/Qwen 等中文模型）
          - 无 tiktoken 时退化为字符级启发式（总字符 / 2.5）
        """
        if messages is None:
            messages = self._ai_messages
        if not messages:
            return 0
        try:
            # 方案 A：tiktoken 编码 + 校准
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            n = 3  # <|start|>assistant<|message|>
            for msg in messages:
                n += 4  # role / content / name 分隔符
                for key, value in msg.items():
                    n += len(enc.encode(str(value)))
                    if key == "name":
                        n -= 1
            # 校准系数：cl100k_base 对中文优化模型约高估 12%
            return int(n * self._TOKEN_CALIBRATION_FACTOR)
        except ImportError:
            # 方案 B：字符级启发式回退
            total_chars = 0
            for msg in messages:
                for key, value in msg.items():
                    total_chars += len(str(value))
                total_chars += self._ESTIMATED_OVERHEAD_PER_MSG
            return int(total_chars / 2.5)

    def _update_token_display(self, tokens: Optional[int] = None):
        """更新输入框下方的 token 计数显示。

        ``tokens`` 可由后台线程预计算后传入——大会话的 tiktoken 统计在冷启动
        可达秒级，首次渲染走 ``_async_render_conversation`` 时避免主线程阻塞。
        """
        if tokens is None:
            tokens = self._estimate_token_count()
        label = f"Shift+Enter \u21b5 \u00b7 Enter \u53d1\u9001"
        if tokens > 0:
            label = f"\U0001f4dd {tokens:,} tokens  |  " + label
        if hasattr(self, "_ai_hint_label"):
            self._ai_hint_label.set_text(label)

    def _prune_messages(self, defer_render: bool = False):
        """按 soft_limit 裁剪超长会话；``defer_render=True`` 时仅裁剪不渲染（异步路径）。

        摘要压缩分支不受影响：无论 defer_render 为何值，启用摘要且未生成中时
        一律提前 return（启动摘要线程），由摘要完成回调统一裁剪+渲染。
        """
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

        self._apply_prune(trim_target, defer_render=defer_render)

    def _apply_prune(self, trim_target: int, save_summary: bool = False, defer_render: bool = False):
        """根据 trim_target 从当前 _ai_messages 重新计算裁剪位置。

        使用当前 _ai_messages 实时计算，避免异步回调中引用过期导致数据丢失。

        ``defer_render=True`` 时仅裁剪列表、跳过 rebuild/render/token 统计——
        渲染由 ``_async_render_conversation`` 的 worker 统一完成（避免主线程
        同步渲染冻结，且不重复渲染）。
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
        if defer_render:
            # 异步渲染路径：仅裁剪列表，rebuild/render/token 由
            # _async_render_conversation 的 worker 统一完成
            self._clear_summary_status()
            return
        self._ai_markdown_text = self._rebuild_markdown_from_messages(self._ai_messages)
        self._render_markdown(self._ai_markdown_text)
        if save_summary:
            self._save_summary_to_conversation()
            # 同时写回裁剪后的消息列表，确保对话文件与内存状态一致
            try:
                self._save_current_conversation(self._build_model_snapshot(),
                                                preserve_updated_at=True)
            except Exception as e:
                print(f"[prune] 保存裁剪后对话失败: {e}", flush=True)
            # 刷新历史下拉框中的消息条数
            try:
                self._ai_history_popover.refresh_dropdown()
            except Exception as e:
                print(f"[prune] 刷新历史下拉框失败: {e}", flush=True)
        self._clear_summary_status()
        self._update_token_display()

    def _generate_summary_async(self, pruned_messages: list, trim_target: int):
        """在后台线程中调用 LLM（流式），将即将丢弃的消息压缩为摘要并实时显示。

        若 LLM 返回空、超时或网络异常，不裁剪对话，
        通过系统消息提醒用户更换模型重试。

        超时逻辑：从首次收到 token 开始计时 25s，每收到新 token 重置计时器。
        仅当模型真正停顿 25s 才中断，避免长摘要被误杀。
        """
        save_summary = False
        cancel_event = threading.Event()
        summary_key = _ai_summary_request_key(self._ai_conversation_id)
        idle_timeout_sec = 120  # 流式停顿超时（秒），收到 token 则重置
        total_timeout_sec = 120  # 总超时硬限制（防止无限等待）
        failure_reason = None
        has_received_token = False  # 是否已收到首个 token

        def _cancel_summary_stream():
            """摘要看门狗：置位取消标志并按 request_key 强关本会话摘要流。

            与主 ReAct 流 _fire_timeout 的双通道取消一致；按键精确中止，
            不误伤并行会话的其他流。流已结束时按键不存在为无操作。
            """
            cancel_event.set()
            self._llm_client.cancel_active_request(summary_key)

        # 总超时硬限制（从调用开始算）
        total_timer = threading.Timer(total_timeout_sec, _cancel_summary_stream)
        total_timer.daemon = True
        total_timer.start()

        # 空闲超时定时器：每次收到 token 后重置
        idle_timer = None
        def _reset_idle_timer():
            nonlocal idle_timer
            if idle_timer:
                idle_timer.cancel()
            idle_timer = threading.Timer(idle_timeout_sec, _cancel_summary_stream)
            idle_timer.daemon = True
            idle_timer.start()

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

            prev_summary = f"已有摘要：\n{self._ai_summary}\n\n" if self._ai_summary else ""
            template = (self._ai_settings_store.summary_prompt_template
                        if self._ai_settings_store else _DEFAULT_SUMMARY_TEMPLATE)
            try:
                prompt = template.format(
                    prev_summary=prev_summary,
                    conversation_text=convo_text,
                    max_chars=max_chars,
                )
            except (KeyError, ValueError) as e:
                print(f"[summary] 模板格式错误（可用占位符：{{prev_summary}}、{{conversation_text}}、{{max_chars}}）: {e}", flush=True)
                failure_reason = f"模板格式错误：{e}"
                return

            base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = \
                self._read_model_config(self._ai_last_prompt_obj,
                                        getattr(self, "_ai_active_model_info", None))

            if not base_url or not api_key or not model_name:
                print(f"[summary] 模型配置不完整，跳过摘要生成", flush=True)
                failure_reason = "当前模型配置不完整（缺少 Base URL / API Key / Model Name）"
                return

            print(f"[summary] 开始流式生成摘要 (已丢弃 {len(pruned_messages)} 条消息, max_chars={max_chars}, model={model_name}, prompt_len={len(prompt)}字)", flush=True)

            result_parts = []
            summary_config = LLMRequestConfig(
                base_url=base_url,
                api_key=api_key,
                model_name=model_name,
                temperature=0.3,
                max_tokens=max(4096, max_chars * 4),
                top_p=top_p,
                timeout=idle_timeout_sec,
            )
            for event in self._llm_client.stream_chat_completion(
                summary_config,
                [{"role": "user", "content": prompt}],
                cancel_event=cancel_event,
                request_key=summary_key,
            ):
                if cancel_event.is_set():
                    break
                if event.type == StreamEventType.TEXT_DELTA and event.text_delta:
                    if not has_received_token:
                        has_received_token = True
                        # 首次收到 token 后启动空闲超时
                        _reset_idle_timer()
                        # 同时取消总超时（已收到 token，模型正在工作）
                        total_timer.cancel()
                    else:
                        # 每收到一个 token 重置空闲超时
                        _reset_idle_timer()
                    result_parts.append(event.text_delta)
                    GLib.idle_add(self._update_summary_display, event.text_delta)
                elif event.type == StreamEventType.STREAM_END:
                    break

            if cancel_event.is_set():
                if has_received_token:
                    failure_reason = f"摘要生成超时（流式停顿超过{idle_timeout_sec}秒），请更换模型重试"
                else:
                    failure_reason = f"摘要生成超时（{total_timeout_sec}秒内未收到首个token），请更换模型重试"
                print(f"[summary] {failure_reason} (model={model_name})", flush=True)
            else:
                result = "".join(result_parts).strip()
                if result:
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
                    failure_reason = f"模型 {model_name} 返回空结果，请更换模型重试"
                    print(f"[summary] {failure_reason}", flush=True)

        except _LLMHttpError as e:
            failure_reason = f"摘要生成失败（{e}），请更换模型重试"
            print(f"[summary] {failure_reason}", flush=True)
        except Exception as e:
            failure_reason = f"摘要生成异常：{e}"
            print(f"[summary] {failure_reason}", flush=True)
            import traceback
            traceback.print_exc()
        finally:
            total_timer.cancel()
            if idle_timer:
                idle_timer.cancel()
            self._ai_summary_generating = False

            # 生成失败时通过系统消息提醒用户
            if failure_reason:
                GLib.idle_add(self._show_summary_failure, failure_reason)
            else:
                GLib.idle_add(self._apply_prune, trim_target, save_summary)

    def _show_summary_failure(self, reason: str):
        """在对话中插入一条系统消息说明摘要生成失败的原因。"""
        self._clear_summary_status()
        html = (
            '<div class="chat-message system-message" style="margin:8px 0;padding:8px 12px;'
            'background:var(--notice-bg,#fff3cd);border-left:4px solid var(--notice-border,#ffc107);'
            'border-radius:4px;font-size:13px;color:var(--notice-text,#856404);">'
            '⚠️ <b>上下文压缩失败</b><br>'
            f'{reason}'
            '</div>'
        )
        self.append_html_to_webview(html)

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
            '<div id="summary-display" class="summary-display">'
            '<div class="summary-header">📝 摘要压缩中</div>'
            '<div class="summary-content"></div>'
            '</div>'
        )

    def _update_summary_display(self, text: str):
        """后台线程调用，通过 GLib.idle_add 推送摘要流式文本到 WebView（主线程执行）。"""
        if not text or not hasattr(self, "_ai_webview") or not self._ai_webview:
            return
        escaped = json.dumps(text)
        self._ai_webview.run_javascript(
            f"(function(){{"
            f"var e=document.getElementById('summary-display');"
            f"if(e){{"
            f"var c=e.querySelector('.summary-content');"
            f"if(c)c.textContent+={escaped};"
            f"_scrollToBottom();"
            f"}}}})();",
            None, None
        )

    def _clear_summary_status(self):
        self._ai_entry.set_sensitive(True)
        self._ai_send_btn.set_sensitive(True)
        self._ai_entry.placeholder_text = "输入后续问题..."
        self._ai_entry.grab_focus()
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(
                "var e=document.getElementById('summary-display');if(e)e.remove();"
                "_scrollToBottom();",
                None, None
            )

    def append_html_to_webview(self, html: str):
        """Insert HTML snippet before end of content div and scroll to bottom."""
        escaped = json.dumps(html)
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(
                f"document.getElementById('content').insertAdjacentHTML('beforeend', {escaped});"
                f"_wrapTables(document.getElementById('content'));"
                f"_scrollToBottom();",
                None, None
            )

    def _build_model_snapshot(self) -> Dict[str, Any]:
        """Build a model_config_snapshot from active model info or resolved config."""
        active = getattr(self, "_ai_active_model_info", None)
        if active:
            return dict(active)  # shallow copy to prevent caller from mutating _ai_active_model_info
        base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = self._read_model_config(
            self._ai_last_prompt_obj, None
        )
        return {
            "alias": "Default",
            "base_url": base_url,
            "model_name": model_name,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
            "thinking_enabled": False,
            "reasoning_effort": "high",
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
                system_prompt=self._ai_system_prompt,
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
                conv.system_prompt = self._ai_system_prompt
                if not self._ai_summary_generating:
                    conv.summary = self._ai_summary
            else:
                # 首条消息落盘兜底：初始面板会话（主路径不经过 _start_new_conversation）
                # 若 created_at 仍为 0/未锚定，此时必须锚定，不得把 0 写入磁盘。
                if not self._ai_conversation_created_at:
                    self._ai_conversation_created_at = int(time.time() * 1000)
                conv = Conversation(
                    id=self._ai_conversation_id,
                    title=local_title,
                    system_prompt=self._ai_system_prompt,
                    messages=[_dict_to_chat_message(m) for m in self._ai_messages],
                    model_config_snapshot=model_snapshot,
                    created_at=self._ai_conversation_created_at,
                    updated_at=int(time.time() * 1000),
                )
            self._conversation_store.save_conversation(conv, bump_updated_at=not preserve_updated_at)

        if self._ai_conversation_id:
            self._ai_html_cache[self._ai_conversation_id] = getattr(self, "_last_rendered_html", "")

    def _switch_to_conversation(self, conv_id: str, save_current: bool = True):
        """Switch AI panel to display a different conversation by ID."""
        self._ai_render_async = False  # 非异步分支走同步 token/渲染
        if not hasattr(self, "_ai_request_id"):
            self._ai_request_id = 0
        self._ai_request_id += 1

        # Save current conversation if requested, has content, and is not streaming
        if save_current and self._ai_messages and self._ai_conversation_id:
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

        # 先解析运行态：未落盘的正在流式会话（磁盘不存在）也能恢复
        st = self._ai_running_convs.get(conv_id)

        # Load target conversation
        conv = self._conversation_store.load_conversation(conv_id)
        if not conv and not (st and st.get("streaming")):
            return

        # Restore state from loaded conversation (preserve tool call fields)
        if st and st.get("streaming"):
            self._ai_messages = st["messages"]
            self._ai_conversation_id = conv_id
            if conv:
                # 已落盘会话：created_at/system_prompt 以磁盘为准（运行态不得覆盖）
                self._ai_conversation_created_at = conv.created_at
                self._ai_summary = conv.summary
                self._ai_system_prompt = conv.system_prompt  # 旧对话加载自身快照，不读 Settings（无热加载）
            else:
                # 未落盘的流式会话：元数据从运行态恢复（无磁盘快照可读）
                self._ai_summary = ""
                self._ai_conversation_created_at = st.get("created_at", self._ai_conversation_created_at)
                self._ai_system_prompt = st.get("system_prompt", "")
            
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
            
            # A→B→A 切回：updateContent 已重建 #content，流式容器/回复 div 不复存在。
            # 复位陈旧 DOM 标记并调度下一次渲染 tick 重建容器（流结束的归属判定
            # 由 _handle_stream_end 按 conv_id 完成，见其 docstring）。
            self._rebind_active_stream(st)
            
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
            self._ai_system_prompt = conv.system_prompt if conv else ""  # 旧对话加载自身快照，不读 Settings（无热加载）
            self._ai_current_assistant_text = ""
            self._ai_current_reasoning_text = ""
            self._ai_response_div_added = False
            self._ai_streaming = False
            
            self._ai_recent_load_pending = False
            self._update_send_button(False)
            self._ai_entry.placeholder_text = ""
            self._ai_spinner.stop()
            self._ai_spinner.hide()

            cached_html = self._ai_html_cache.get(conv_id)
            if cached_html is not None and getattr(self, "_webview_ready", False) and not getattr(self, "_webview_suspended", False):
                self._last_rendered_html = cached_html
                self._ai_markdown_text = self._rebuild_markdown_from_messages(self._ai_messages)
                js_code = f"updateContent({json.dumps(cached_html)});"
                self._ai_webview.run_javascript(js_code, None, None)
                self._update_token_display()
            else:
                # 首次渲染：重建 markdown + 转 HTML + token 统计较重
                # （冷启动可达数秒，pygments/tiktoken 首次加载）。
                # 放后台线程执行，主线程只做状态更新——面板立即响应，内容稍后弹出。
                self._prune_messages(defer_render=True)
                self._ai_render_async = True
                self._async_render_conversation(conv_id)
        
        self._refresh_subagent_bar()

        # Update model info display label
        _, _, _, display_name, _, _, _, _, _ = self._read_model_config(None, self._ai_active_model_info)
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
        if getattr(self, "_ai_render_async", False):
            # token 统计已由 _async_render_conversation 的 worker 计算，
            # 由 _apply_async_render 更新——此处跳过同步估算（冷启动 tiktoken 可达秒级）
            pass
        else:
            self._update_token_display()

    def _async_render_conversation(self, conv_id: str) -> None:
        """后台渲染会话 HTML 并统计 token，完成后 idle 回主线程应用。

        首次打开/切换未缓存会话时，markdown+pygments 渲染与 tiktoken 统计
        在冷启动可达数秒；移出主线程后面板立即响应，内容稍后显示。
        """
        messages = list(self._ai_messages)
        show_details = self._show_tool_details
        snapshot_len = len(messages)

        def _worker():
            try:
                text = _rebuild_markdown_from_messages(
                    messages, show_details=show_details,
                )
                html = _markdown_to_html_safe(text, fallback_content="")
                tokens = self._estimate_token_count(messages)
                GLib.idle_add(self._apply_async_render, conv_id, html, text, tokens, snapshot_len)
            except Exception as e:
                print(f"[AI] async render error: {e}", flush=True)
                # M1 兜底：渲染失败时回主线程同步渲染，避免内容永不显示、
                # token 显示陈旧、_ai_render_async 残留。
                GLib.idle_add(self._fallback_sync_render, conv_id, messages, show_details)

        threading.Thread(target=_worker, daemon=True).start()

    def _fallback_sync_render(self, conv_id: str, messages: list, show_details: bool) -> bool:
        """worker 渲染异常时的兜底：主线程同步渲染（原 _render_markdown 路径）。"""
        self._ai_render_async = False
        try:
            if self._ai_conversation_id != conv_id:
                return False
            text = _rebuild_markdown_from_messages(messages, show_details=show_details)
            html = _markdown_to_html_safe(text, fallback_content="")
            self._last_rendered_html = html
            self._ai_html_cache[conv_id] = html
            self._ai_markdown_text = text
            js_code = f"updateContent({json.dumps(html)});"
            self._ai_webview.run_javascript(js_code, None, None)
            self._update_token_display()
        except Exception as e:
            print(f"[AI] fallback sync render error: {e}", flush=True)
        return False

    def _apply_async_render(self, conv_id: str, html: str, markdown_text: str, tokens: int, snapshot_len: int) -> bool:
        """主线程应用后台渲染结果（仅当用户仍停留在目标会话且快照未过期）。"""
        try:
            if self._ai_conversation_id != conv_id:
                return False  # 渲染期间用户已切换会话，丢弃过期结果
            if len(self._ai_messages) != snapshot_len:
                return False  # 快照已被裁剪/摘要化，丢弃过期渲染（避免覆盖摘要显示）
            self._ai_render_async = False  # 确认归属后才清位
            self._last_rendered_html = html
            self._ai_html_cache[conv_id] = html
            self._ai_markdown_text = markdown_text
            js_code = f"updateContent({json.dumps(html)});"
            self._ai_webview.run_javascript(js_code, None, None)
            self._update_token_display(tokens)
        except Exception as e:
            print(f"[AI] apply async render error: {e}", flush=True)
        return False

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
        for cid, st in list(self._ai_running_convs.items()):
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
        config = LLMRequestConfig(
            base_url=base_url, api_key=api_key, model_name=model_name,
            timeout=timeout, temperature=temperature,
            max_tokens=max_tokens, top_p=top_p,
        )
        return self._llm_client.sync_chat_completion(
            config, messages,
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
            # 进程已终止，必须整页重建；force=True 防止指纹守卫抑制恢复重载
            self._load_webview_html(cached_html or "", force=True)
            print("[AI] WebView restored from suspension.", flush=True)
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

    def _suspend_webview_cb(self) -> bool:
        # 防御：首开守卫——从未显示过时即使定时器意外存在也不杀进程
        if not getattr(self, "_ai_has_shown", False):
            self._suspend_timeout_id = 0
            return False

        running_states = list(self._ai_running_convs.values())
        any_running = any(st.get("streaming", False) for st in running_states)
        if any_running:
            print(f"[AI] suspend deferred: {sum(1 for st in running_states if st.get('streaming'))} convs still streaming", flush=True)
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
        self._ai_lbl.set_markup(f"<b>AI 助手看盘</b>\n<span size='small' foreground='#888888'>({display_name})</span>")
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
        return bool(self._ai_history_popover and self._ai_history_popover.get_visible())

    def reset_state(self):
        self._reset_ai_panel_silent()

    def grab_entry_focus(self):
        self._ai_entry.grab_focus()

    def insert_text_to_input(self, text: str):
        """从外部向 AI 输入框光标处插入文本并聚焦。"""
        buffer = self._ai_entry.get_buffer()
        buffer.insert_at_cursor(text)
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
        if self._ai_hdr is not None:
            try:
                self._ai_hdr.override_background_color(Gtk.StateFlags.NORMAL, c["header_bg"])
            except Exception:
                pass
        if self._ai_webview:
            self._ai_webview.set_background_color(c["bg"])

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
