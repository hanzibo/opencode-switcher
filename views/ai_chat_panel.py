"""AI 聊天主面板 — 组合子组件并提供统一对外接口。

职责：
- 实例化并组合 AIChatWebView、AIChatHistoryManager、AIChatInputArea
- MCP 集成（初始化、连接、工具缓存）
- ReAct 工具循环编排（_run_llm_api_request）
- 对外暴露 toggle_ai / set_theme / load_sessions 等标准接口
- 子代理状态栏管理
"""

import gi
import threading
import os
import re
import html
import json
import time
import tool_registry
gi.require_version("Gtk", "3.0")
gi.require_version("Gio", "2.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, Gdk, GLib, Pango
from typing import Optional, Callable, List, Dict, Any, Tuple, Set
from uuid import uuid4
from stores.clipboard_store import (
    CustomPrompt, ConversationStore, AISettingsStore, Conversation,
    DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS, DEFAULT_TOP_P,
    _DEFAULT_SUMMARY_TEMPLATE, CONFIG_DIR,
)
from ai_text_utils import (
    _markdown_to_html_safe, _close_unclosed_code_blocks,
    _rebuild_markdown_from_messages, _resolve_vision_image_src,
    _preserve_newlines, _vision_content_to_text,
    _extract_local_title, _strip_ai_markup,
    _image_to_data_uri, _dict_to_chat_message,
    USER_AVATAR_HTML, set_code_highlight,
)
from ai_text_utils.render import _render_tool_card_standalone
from ai_engine.render_pipeline import render_turn, TurnRenderInput, build_update_js
from ai_engine.ai_html_template import get_html_template
from ai_engine.llm_client import _LLMHttpClient, _LLMHttpError, LLMRequestConfig
from ai_engine.ai_tool_loop import run_llm_react_loop, ToolLoopContext
from dialogs.dynamic_copy_dialog import show_dynamic_copy_dialog
from dialogs.sort_dialog import show_sort_dialog
from dialogs.recycle_bin_dialog import show_recycle_bin_dialog
from dialogs.sort_cats_dialog import show_sort_cats_dialog
from dialogs.prompt_dialog import show_prompt_dialog
from dialogs.prompts_config_dialog import show_prompts_config_dialog
from stores.theme_config import get_ai_gtk_colors
from system.event_types import StreamEventType

# New sub-modules
from views.ai_popovers import AICommandPopover, HistoryPopover
from views.ai_chat_webview import AIChatWebView
from views.ai_chat_history import AIChatHistoryManager, _to_chat_messages
from views.ai_chat_input import AIChatInputArea

TEMPLATE_REGEX = re.compile(r"\$\{(\d+)(?::((?:[^}=]|\\:|\\=)+))?(?<!\\)(?:=([^}]*))?\}")
PROMPT_PLACEHOLDER_RE = re.compile(r'\\\\|\\(\$\{&})|(\$\{&})')


class AIChatPanel(Gtk.Box):
    """主容器，组合 WebView / History / Input 子组件，编排 AI 对话全流程。"""

    _SUSPEND_DELAY_SECONDS = 5
    _AI_COMMANDS = [
        ("/new", "新对话"),
        ("/delete", "删除并新建"),
        ("/retry", "回滚到上一轮"),
        ("/rollback", "回滚到任意轮"),
        ("/title", "设置/生成标题"),
        ("/model", "切换模型"),
        ("/cd", "切换 bash 工作路径"),
        ("/summary", "压缩上下文"),
        ("/skill", "查看与手动触发 AI Skill"),
    ]

    def __init__(self, conversation_store, llm_settings_store, ai_settings_store=None,
                 theme="dark", ai_commands=None, pygments_css_cache=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._conversation_store = conversation_store
        self._llm_settings_store = llm_settings_store
        self._ai_settings_store = ai_settings_store
        if self._ai_settings_store is not None:
            set_code_highlight(self._ai_settings_store.enable_code_highlight)
        self._theme = theme
        self._ai_commands = ai_commands or []
        self._pygments_css_cache = pygments_css_cache or {}

        # ── Core AI state ──
        self._ai_streaming = False
        self._ai_cancel_event = threading.Event()
        self._ai_cancelling = False
        self._ai_messages: List[Dict] = []
        self._ai_conversation_id = uuid4().hex[:12]
        self._ai_conversation_created_at = 0
        self._ai_request_id = 0
        self._ai_current_assistant_text = ""
        self._ai_current_reasoning_text = ""
        self._ai_assistant_buffer = ""
        self._ai_markdown_text = ""
        self._ai_response_div_added = False
        self._ai_assistant_html_base = ""
        self._ai_last_prompt_obj = None
        self._ai_active_model_info = None
        self._ai_summary = ""
        self._ai_summary_generating = False
        self._ai_title_generated = False
        self._ai_pending_title_notification = False
        self._ai_tool_iteration = 0
        self._ai_html_cache: Dict[str, str] = {}
        self._last_rendered_html = ""
        self._ai_history_queries: List[str] = []
        self._ai_history_index = -1
        self._ai_current_draft = ""
        self._ai_ask_user_state = None
        self._ai_selected_subagents: Set[str] = set()
        self._ai_subagent_blocks: Dict[str, tuple] = {}
        self._ai_render_timeout_id = 0
        self._webview_suspended = False
        self._suspend_timeout_id = 0

        # ── MCP state ──
        self._mcp_bridge = None
        self._mcp_client_mgr = None
        self._cached_mcp_tools: Optional[list] = None
        self._mcp_initialized = False

        # ── LLM client ──
        self._llm_client = _LLMHttpClient()

        # ── Running convs (thread-safe, accessed via GLib.idle_add) ──
        self._ai_running_convs: Dict[str, Dict] = {}

        # ── Callback hooks (set by parent ClipboardPanel) ──
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

        # ── Separator (packed by parent) ──
        self.separator = Gtk.Separator.new(Gtk.Orientation.VERTICAL)
        self.separator.set_no_show_all(True)

        # Margins & visibility
        self.set_margin_start(8)
        self.set_margin_end(8)
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_no_show_all(True)

        self._build_ui()

    def _build_ui(self):
        """构建 UI：组合子组件。"""
        from views.clipboard_panel import _textview_draw_placeholder, _copy_to_clipboard
        self._copy_to_clipboard = _copy_to_clipboard

        # ── Header ──
        ai_hdr = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 6)
        self._ai_hdr = ai_hdr
        self._ai_lbl = Gtk.Label.new()
        self._ai_lbl.set_markup("<b>AI 助手看盘</b>")
        self._ai_lbl.set_xalign(0)
        ai_hdr.pack_start(self._ai_lbl, True, True, 0)

        self._ai_spinner = Gtk.Spinner.new()
        self._ai_spinner.set_no_show_all(True)
        ai_hdr.pack_start(self._ai_spinner, False, False, 0)

        # History button
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

        # History Popover
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

        # Separator line
        ai_sep = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        self.pack_start(ai_sep, False, False, 0)

        # ── WebView (scrolled) ──
        ai_scrolled = Gtk.ScrolledWindow.new()
        self._ai_scrolled = ai_scrolled
        ai_scrolled.set_name("aiScrolled")
        ai_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        ai_scrolled.set_vexpand(True)

        # Create WebView sub-component
        self._ai_webview_component = AIChatWebView(
            theme_name=self._theme,
            pygments_css_cache=self._pygments_css_cache,
            on_copy_to_clipboard_cb=self._copy_to_clipboard,
            on_copy_started_cb=lambda: self.on_ai_copy_started() if self.on_ai_copy_started else None,
            on_copy_finished_cb=lambda: self.on_ai_copy_finished() if self.on_ai_copy_finished else None,
        )
        # Wire WebView state references
        self._ai_webview_component.set_state_references(
            is_streaming_fn=lambda: self._ai_streaming,
            get_messages_fn=lambda: self._ai_messages,
            get_assistant_text_fn=lambda: self._ai_current_assistant_text,
            get_reasoning_text_fn=lambda: self._ai_current_reasoning_text,
            get_show_tool_details_fn=lambda: getattr(self._ai_settings_store, 'show_tool_details', True) if self._ai_settings_store else True,
            get_request_id_fn=lambda: self._ai_request_id,
            get_running_convs_fn=lambda: self._ai_running_convs,
            get_conversation_id_fn=lambda: self._ai_conversation_id,
        )
        # Wire retry/rollback callbacks from WebView navigation
        self._ai_webview_component._on_retry_requested = lambda idx: self._retry_response(idx)
        self._ai_webview_component._on_rollback_requested = lambda r: self._rollback_to_round(r)

        # We need to add the webview to scrolled window by accessing it
        ai_scrolled.add(self._ai_webview_component)

        # Sync background colors
        c = get_ai_gtk_colors(self._theme)
        bg_rgba = Gdk.RGBA(*c["bg"])
        hdr_bg_rgba = Gdk.RGBA(*c["header_bg"])
        self.override_background_color(Gtk.StateFlags.NORMAL, bg_rgba)
        ai_scrolled.override_background_color(Gtk.StateFlags.NORMAL, bg_rgba)
        self._ai_webview_component.webview.set_background_color(bg_rgba)
        ai_hdr.override_background_color(Gtk.StateFlags.NORMAL, hdr_bg_rgba)
        self.pack_start(ai_scrolled, True, True, 0)

        # ── Input Area (sub-component) ──
        self._ai_input_area = AIChatInputArea(
            on_send_clicked_cb=self._on_send_clicked,
            on_new_conversation_cb=self.start_new_conversation,
            on_attach_cb=None,
            on_title_command_cb=self._handle_title_command,
            on_summary_command_cb=self._handle_summary_command,
            on_rollback_command_cb=self._handle_rollback_command,
            on_retry_command_cb=self._handle_retry_command,
            on_model_command_cb=self._switch_model_by_alias,
            on_cd_command_cb=self._select_and_set_bash_cwd,
            on_skill_command_cb=self._handle_skill_command,
            on_delete_command_cb=self._reset_ai_panel_silent,
            get_streaming_state_fn=lambda: self._ai_streaming,
            get_cancelling_state_fn=lambda: self._ai_cancelling,
            get_pending_image_fn=lambda: self._ai_pending_image_data_uri if hasattr(self, '_ai_pending_image_data_uri') else None,
            get_selected_subagents_fn=lambda: self._ai_selected_subagents,
            get_ai_messages_fn=lambda: self._ai_messages,
            get_llm_settings_store_fn=lambda: self._llm_settings_store,
            get_active_model_info_fn=lambda: self._ai_active_model_info,
            get_ai_conversation_id_fn=lambda: self._ai_conversation_id,
            get_conversation_store_fn=lambda: self._conversation_store,
            on_copy_started_cb=lambda: self.on_ai_copy_started() if self.on_ai_copy_started else None,
            on_copy_finished_cb=lambda: self.on_ai_copy_finished() if self.on_ai_copy_finished else None,
            get_history_queries_fn=lambda: self._ai_history_queries,
            set_history_queries_fn=lambda v: setattr(self, '_ai_history_queries', v),
            get_history_index_fn=lambda: self._ai_history_index,
            set_history_index_fn=lambda v: setattr(self, '_ai_history_index', v),
            get_current_draft_fn=lambda: self._ai_current_draft,
            set_current_draft_fn=lambda v: setattr(self, '_ai_current_draft', v),
            on_dialog_shown_cb=lambda: self.on_dialog_shown() if self.on_dialog_shown else None,
            on_dialog_hidden_cb=lambda: self.on_dialog_hidden() if self.on_dialog_hidden else None,
            on_menu_shown_cb=lambda: self.on_menu_shown() if self.on_menu_shown else None,
            on_menu_hidden_cb=lambda: self.on_menu_hidden() if self.on_menu_hidden else None,
            append_html_cb=self._ai_webview_component.append_html,
            get_ai_markdown_text_fn=lambda: self._ai_markdown_text,
        )
        # Apply input bg color
        input_bg_rgba = Gdk.RGBA(*c["input_bg"])
        self._ai_input_area.override_background_color(Gtk.StateFlags.NORMAL, input_bg_rgba)

        self.pack_start(self._ai_input_area, False, False, 0)

        # Sub-agent CSS
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

    # ═══════════════════════════════════════════════════════════════
    # Model config
    # ═══════════════════════════════════════════════════════════════

    def _get_gtk_colors(self, theme_name: str) -> dict:
        raw = get_ai_gtk_colors(theme_name)
        return {k: Gdk.RGBA(*v) for k, v in raw.items()}

    def _read_model_config(self, prompt_obj: Optional[CustomPrompt] = None,
                           model_info: Optional[Dict] = None):
        """解析模型配置（完整保留原逻辑）。"""
        from stores.clipboard_store import LLMModelConfig
        bound_alias = None
        if model_info:
            bound_alias = model_info.get("alias")
        elif prompt_obj:
            bound_alias = getattr(prompt_obj, "bound_model_alias", None)

        model_config = None
        if bound_alias:
            model_config = next((m for m in self._llm_settings_store.models if m.alias == bound_alias), None)

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
            base_url = api_key = model_name = ""
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

        if model_info:
            if "temperature" in model_info:
                temperature = model_info["temperature"]
            if "max_tokens" in model_info:
                max_tokens = model_info["max_tokens"]
            if "top_p" in model_info:
                top_p = model_info["top_p"]

        thinking_enabled = (model_info and "thinking_enabled" in model_info and model_info["thinking_enabled"]) or (model_config and model_config.thinking_enabled) or False
        reasoning_effort = (model_info and "reasoning_effort" in model_info and model_info["reasoning_effort"]) or (model_config and model_config.reasoning_effort) or "high"

        display_name = f"{model_config.alias} ({model_name})" if model_config else model_name
        return base_url, api_key, model_name, display_name, temperature, max_tokens, top_p, thinking_enabled, reasoning_effort

    def _get_title_model_config(self):
        """Return (base_url, api_key, model_name, temperature, max_tokens, top_p) for title model."""
        model = next((m for m in self._llm_settings_store.models if getattr(m, 'is_title_model', False)), None)
        if not model:
            return None
        return (model.base_url.strip(), model.api_key.strip(), model.model_name.strip(),
                model.temperature, model.max_tokens, model.top_p)

    # ═══════════════════════════════════════════════════════════════
    # MCP 集成
    # ═══════════════════════════════════════════════════════════════

    def _init_mcp(self) -> None:
        if self._mcp_initialized:
            return
        from mcp_integration import GtkAsyncioBridge, MCPClientManager, MCPServerConfig
        self._mcp_bridge = GtkAsyncioBridge.get()
        self._mcp_bridge.start()
        self._mcp_client_mgr = MCPClientManager(self._mcp_bridge)
        if self._ai_settings_store is not None:
            self._load_and_connect_mcp_servers()
        self._mcp_initialized = True
        print(f"[MCP] init ok, servers={self._mcp_client_mgr.get_server_count()}", flush=True)

    def _load_and_connect_mcp_servers(self) -> None:
        if self._ai_settings_store is None or self._mcp_client_mgr is None:
            return
        from mcp_integration import MCPServerConfig
        server_dicts = getattr(self._ai_settings_store, "mcp_servers", None) or []
        for sd in server_dicts:
            config = MCPServerConfig.from_dict(sd)
            if not (config.enabled and config.auto_connect):
                continue
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
        if self._mcp_client_mgr is None:
            return
        self._mcp_bridge.call_async(
            self._mcp_client_mgr.list_all_tools(),
            callback=self._on_mcp_tools_ready,
        )

    def _on_mcp_tools_ready(self, tools: list, err: Optional[Exception]) -> None:
        if err:
            print(f"[MCP] list tools failed: {err}", flush=True)
            return
        if tools:
            self._cached_mcp_tools = tools
            print(f"[MCP] cached {len(tools)} tools from {self._mcp_client_mgr.get_server_count()} servers", flush=True)

    def _reconfigure_mcp(self) -> None:
        if not self._mcp_initialized or self._mcp_client_mgr is None:
            return
        from mcp_integration import MCPServerConfig
        server_dicts = getattr(self._ai_settings_store, "mcp_servers", None) or []
        new_configs = {}
        for sd in server_dicts:
            config = MCPServerConfig.from_dict(sd)
            if not (config.enabled and config.auto_connect):
                continue
            if config.transport == "stdio" and not config.command:
                continue
            if config.transport == "http" and not config.url:
                continue
            new_configs[config.name] = config
        has_disconnect = False
        for name in list(self._mcp_client_mgr.get_all_server_names()):
            if name not in new_configs:
                has_disconnect = True
                self._mcp_bridge.call_async(
                    self._mcp_client_mgr.disconnect(name),
                    callback=lambda result, err, n=name: print(f"[MCP] disconnected {n}", flush=True),
                )
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
        if has_disconnect and has_connect:
            self._cached_mcp_tools = None
        elif has_disconnect and not has_connect:
            self._cached_mcp_tools = None

    # ═══════════════════════════════════════════════════════════════
    # 消息发送 & 流式编排
    # ═══════════════════════════════════════════════════════════════

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
        wv = self._ai_webview_component
        wv.load_html(wv.get_html_template(self._theme, user_html), "file:///")

    def _send_user_message(self, text: str):
        """发送用户消息，编排 LLM 请求。"""
        wv = self._ai_webview_component
        wv.init_streaming_state()
        self._init_mcp()

        # Build content with optional image
        img_data = self._ai_input_area.get_pending_image()
        if img_data:
            content = [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"hash": img_data["hash"], "detail": "high"}},
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

        if self._ai_render_timeout_id != 0:
            GLib.source_remove(self._ai_render_timeout_id)
            self._ai_render_timeout_id = 0

        self._ai_markdown_text = self._rebuild_markdown()
        self._last_rendered_html = _markdown_to_html_safe(self._ai_markdown_text, fallback_content="")
        if self._ai_conversation_id:
            self._ai_html_cache[self._ai_conversation_id] = self._last_rendered_html
            wv.last_rendered_html = self._last_rendered_html

        wv.run_javascript("_autoScroll = true;")
        wv.render_markdown(self._ai_markdown_text)

        self._ai_spinner.show()
        self._ai_spinner.start()

        base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = self._read_model_config(
            self._ai_last_prompt_obj, self._ai_active_model_info
        )

        if not base_url or not model_name or not api_key:
            self._ai_streaming = False
            self._ai_spinner.stop()
            self._ai_spinner.hide()
            self._ai_input_area.set_send_button_sensitive(True)
            self._ai_input_area.set_placeholder("")
            error_msg = "❌ [错误] 模型配置不完整。\n\n请检查 Prompts Config → API Settings"
            self._ai_markdown_text += f'\n\n{error_msg}\n\n'
            wv.render_markdown(self._ai_markdown_text)
            return

        self._ai_cancel_event.clear()
        self._ai_input_area.update_send_button(True)
        self._ai_input_area.set_placeholder("等待回复中...")
        msgs_for_llm, extra_sys = self._build_llm_messages()
        threading.Thread(
            target=self._run_llm_api_request,
            args=(base_url, api_key, model_name, msgs_for_llm, current_req_id,
                  temperature, max_tokens, top_p, self._ai_markdown_text,
                  self._ai_conversation_id, extra_sys),
            daemon=True
        ).start()

    def ask_llm_api(self, prompt_text: str, prompt_obj: Optional[CustomPrompt] = None):
        self._init_mcp()
        self.separator.set_no_show_all(False)
        self.separator.show()
        self.set_no_show_all(False)
        self.show()
        self.show_all()
        self.queue_resize()

        self._ai_request_id += 1
        current_req_id = self._ai_request_id
        self._ai_streaming = True
        self._ai_current_assistant_text = ""
        self._ai_response_div_added = False
        self._ai_assistant_html_base = ""
        if self._ai_render_timeout_id != 0:
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
            missing = [k for k, v in [("API Key", api_key), ("Base URL", base_url), ("Model Name", model_name)] if not v]
            error_msg = "❌ [错误] 模型配置不完整，缺少: " + "、".join(missing)
            self._ai_markdown_text = error_msg
            html_content = _markdown_to_html_safe(error_msg)
            wv = self._ai_webview_component
            wv.load_html(wv.get_html_template(self._theme, html_content), "file:///")
            return

        self._ai_cancel_event.clear()
        self._ai_input_area.update_send_button(True)
        msgs_for_llm, extra_sys = self._build_llm_messages()
        threading.Thread(
            target=self._run_llm_api_request,
            args=(base_url, api_key, model_name, msgs_for_llm, current_req_id,
                  temperature, max_tokens, top_p, self._ai_markdown_text,
                  self._ai_conversation_id, extra_sys, thinking_enabled, reasoning_effort),
            daemon=True
        ).start()

    def _build_llm_messages(self) -> tuple:
        extra = []
        if self._ai_summary:
            extra.append({"role": "system", "content": f"【历史摘要】\n{self._ai_summary}"})
        return list(self._ai_messages), extra

    def _run_llm_api_request(self, base_url, api_key, model_name, messages, req_id,
                              temperature, max_tokens, top_p, markdown_text, conv_id,
                              extra_system_messages=None, thinking_enabled=False, reasoning_effort="high"):
        """Run ReAct loop via run_llm_react_loop."""
        cancel_event = threading.Event()
        state = {
            "streaming": True,
            "messages": list(messages),
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
                wv = self._ai_webview_component
                wv._token_buffer = ""
                wv._flush_scheduled = False
                wv._reasoning_buffer = ""
                wv._reasoning_flush_scheduled = False

        def append_message_callback(msg):
            st = self._ai_running_convs.get(conv_id)
            if st:
                st["messages"].append(msg)
            if self._ai_conversation_id == conv_id:
                if st:
                    self._ai_messages = st["messages"]
                else:
                    self._ai_messages.append(msg)
                    if msg.get("role") == "tool" and self._ai_streaming is False:
                        GLib.idle_add(self._re_render_after_tool_cancel)
                enable_inc = (self._ai_settings_store and self._ai_settings_store.enable_incremental_tools) if self._ai_settings_store else False
                is_active = (req_id == self._ai_request_id)
                dom_ready = (self._ai_webview_component._streaming_container_created
                             and (st.get("response_div_added", False) if st else False))
                if msg.get("role") == "tool" and enable_inc and is_active and dom_ready:
                    pass
                else:
                    GLib.idle_add(self._ai_webview_component.render_current_assistant_message, req_id)

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

        def append_html_callback(html_text):
            if self._ai_conversation_id == conv_id:
                GLib.idle_add(self._ai_webview_component.append_html, html_text)

        def on_token_delta_fn(text):
            if self._ai_conversation_id == conv_id:
                GLib.idle_add(self._ai_webview_component.on_token_delta, text)

        def on_reasoning_delta_fn(text):
            if self._ai_conversation_id == conv_id:
                GLib.idle_add(self._ai_webview_component.on_reasoning_delta, text)

        def on_tool_result_fn(tool_call_id, result_text, status):
            if self._ai_conversation_id == conv_id:
                GLib.idle_add(self._ai_webview_component.on_tool_result, tool_call_id, result_text, status, req_id)

        config = LLMRequestConfig(
            base_url=base_url, api_key=api_key, model_name=model_name,
            temperature=temperature, max_tokens=max_tokens, top_p=top_p,
            timeout=30, extra_system_messages=extra_system_messages,
            thinking_enabled=thinking_enabled, reasoning_effort=reasoning_effort,
        )

        ctx = ToolLoopContext(
            req_id=req_id,
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
            on_tool_result_fn=on_tool_result_fn,
            on_tool_calls_started_fn=self._ai_webview_component.on_tool_calls_started,
            conv_id=conv_id,
            mcp_tool_definitions=self._cached_mcp_tools,
            mcp_client_manager=self._mcp_client_mgr,
            disabled_tools=getattr(self._ai_settings_store, "disabled_tools", []) if self._ai_settings_store else [],
        )

        run_llm_react_loop(self._llm_client, config, ctx, state["messages"])

    # ═══════════════════════════════════════════════════════════════
    # ReAct 循环回调
    # ═══════════════════════════════════════════════════════════════

    def _finalize_after_tool_loop(self, req_id: int):
        conv_id = None
        for cid, st in list(self._ai_running_convs.items()):
            if st.get("req_id") == req_id:
                conv_id = cid
                break
        if not conv_id:
            conv_id = self._ai_conversation_id
            state = None
        else:
            state = self._ai_running_convs.get(conv_id)
        if conv_id and state is None and not self._ai_cancelling:
            return
        if state:
            state["streaming"] = False
        if self._ai_conversation_id == conv_id:
            target_messages = state["messages"] if state else self._ai_messages
            self._ai_messages = target_messages
            self._ai_spinner.stop()
            self._ai_spinner.hide()
            self._ai_streaming = False
            self._ai_input_area.update_send_button(False)
            self._ai_input_area.set_placeholder("")
        else:
            if state:
                self._render_background_conversation(conv_id, state["messages"], state)
        self._ai_running_convs.pop(conv_id, None)
        self._ai_cancelling = False
        self._handle_stream_end(req_id)

    def _on_llm_api_finished(self, req_id: int):
        conv_id = None
        for cid, st in list(self._ai_running_convs.items()):
            if st.get("req_id") == req_id:
                conv_id = cid
                break
        if not conv_id:
            conv_id = self._ai_conversation_id
            state = None
        else:
            state = self._ai_running_convs.get(conv_id)
        if conv_id and state is None and not self._ai_cancelling:
            return
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
            if self._ai_render_timeout_id != 0:
                GLib.source_remove(self._ai_render_timeout_id)
                self._ai_render_timeout_id = 0
            self._ai_spinner.stop()
            self._ai_spinner.hide()
            self._ai_input_area.update_send_button(False)
            self._ai_input_area.set_placeholder("")
        else:
            if state:
                self._render_background_conversation(conv_id, target_messages, state)
        self._ai_running_convs.pop(conv_id, None)
        self._ai_cancelling = False
        self._handle_stream_end(req_id)

    def _handle_stream_end(self, req_id: int):
        """Stream end: finalize render, save, prune, title gen."""
        if self._ai_request_id != req_id:
            return
        wv = self._ai_webview_component
        show_details = getattr(self._ai_settings_store, 'show_tool_details', True) if self._ai_settings_store else True
        wv.finalize_streaming_render(
            self._ai_messages, self._ai_markdown_text,
            self._ai_request_id, self._ai_conversation_id,
            self._ai_current_assistant_text, self._ai_current_reasoning_text,
            show_details,
        )
        wv.run_javascript("_scrollToBottom();")
        self._append_assistant_turn_to_cache()
        self._ai_streaming = False
        self._ai_input_area.update_send_button(False)
        self._ai_input_area.set_placeholder("输入后续问题...")
        try:
            model_snapshot = self._build_model_snapshot()
            self._save_current_conversation(model_snapshot)
        except Exception as e:
            print(f"Save error: {e}", flush=True)
        self._prune_messages()
        try:
            title_cfg = self._get_title_model_config()
            if title_cfg:
                base_url, api_key, model_name, temperature, max_tokens, top_p = title_cfg
            else:
                base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = self._read_model_config(
                    self._ai_last_prompt_obj, self._ai_active_model_info
                )
            if (not self._ai_title_generated and self._ai_conversation_id
                    and self._ai_messages and base_url and api_key):
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
            print(f"Title gen error: {e}", flush=True)
        try:
            self._ai_history_popover.refresh_dropdown()
        except Exception:
            pass
        self._update_token_display()

    def _handle_ask_user_question(self, tool_call: dict) -> str:
        try:
            arguments = json.loads(tool_call.get("function", {}).get("arguments", "{}"))
        except json.JSONDecodeError:
            return "[询问用户失败：参数解析错误]"
        question = arguments.get("question", "")
        if not question:
            return "[询问用户失败：问题为空]"
        event = threading.Event()
        self._ai_ask_user_state = {"question": question, "event": event, "answer": None}
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
            self._ai_ask_user_state = None
            GLib.idle_add(self._ai_input_area.grab_focus_entry)
            return "[询问用户超时：用户未在 5 分钟内回答]"
        state = getattr(self, "_ai_ask_user_state", None)
        answer = state.get("answer", "") if state else ""
        self._ai_ask_user_state = None
        GLib.idle_add(self._ai_input_area.grab_focus_entry)
        return answer if answer else "[用户取消了回答]"

    def _enable_ask_user_entry(self):
        self._ai_input_area.set_placeholder("请输入回答...")
        self._ai_input_area.update_send_button(False)
        self._ai_input_area.set_send_button_sensitive(True)
        self._ai_input_area.grab_focus_entry()

    def _retry_response(self, assistant_index: int):
        """Retry: cancel active, rollback, resend."""
        if self._ai_streaming:
            active_state = self._ai_running_convs.get(self._ai_conversation_id)
            if active_state:
                active_state["cancel_event"].set()
                self._ai_running_convs.pop(self._ai_conversation_id, None)
            self._llm_client.cancel_active_request()
            self._ai_input_area.update_send_button(False)
            self._ai_streaming = False
            self._ai_spinner.stop()
            self._ai_spinner.hide()

        msgs = self._ai_messages
        if not (0 <= assistant_index < len(msgs)) or msgs[assistant_index].get("role") != "assistant":
            return
        user_index = assistant_index
        while user_index >= 0 and msgs[user_index].get("role") != "user":
            user_index -= 1
        if user_index < 0:
            return
        self._ai_messages = msgs[:user_index + 1]
        self._ai_markdown_text = self._rebuild_markdown()
        wv = self._ai_webview_component
        wv.run_javascript("_autoScroll = true;")
        wv.render_markdown(self._ai_markdown_text)
        wv.init_streaming_state()
        self._ai_request_id += 1
        current_req_id = self._ai_request_id
        self._ai_streaming = True
        self._ai_current_assistant_text = ""
        self._ai_response_div_added = False
        self._ai_assistant_html_base = ""
        self._ai_spinner.show()
        self._ai_spinner.start()
        self._ai_input_area.update_send_button(True)
        self._ai_input_area.set_placeholder("等待回复中...")
        base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = self._read_model_config(
            self._ai_last_prompt_obj, self._ai_active_model_info
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

    def _re_render_after_tool_cancel(self):
        if self._ai_streaming:
            return
        self._ai_markdown_text = self._rebuild_markdown()
        self._ai_webview_component.render_markdown(self._ai_markdown_text)
        try:
            self._save_current_conversation(self._build_model_snapshot())
        except Exception:
            pass

    def _get_show_tool_details(self) -> bool:
        return getattr(self._ai_settings_store, 'show_tool_details', True) if self._ai_settings_store else True

    def _rebuild_markdown(self, messages=None) -> str:
        msgs = messages if messages is not None else self._ai_messages
        return _rebuild_markdown_from_messages(msgs, show_details=self._get_show_tool_details())

    def _append_assistant_turn_to_cache(self):
        self._ai_markdown_text = self._rebuild_markdown()
        self._last_rendered_html = _markdown_to_html_safe(self._ai_markdown_text, fallback_content="")
        if self._ai_conversation_id:
            self._ai_html_cache[self._ai_conversation_id] = self._last_rendered_html
            self._ai_webview_component.last_rendered_html = self._last_rendered_html

    def _render_background_conversation(self, conv_id: str, target_messages: list, state: dict):
        show_details = self._get_show_tool_details()
        from ai_text_utils import _markdown_to_html_safe
        output = render_turn(TurnRenderInput(
            turn_messages=target_messages, all_messages=target_messages,
            is_streaming=False, show_tool_details=show_details,
        ))
        rebuilt = self._rebuild_markdown(target_messages)
        html_content = _markdown_to_html_safe(rebuilt, fallback_content="")
        self._ai_html_cache[conv_id] = html_content
        state["ai_markdown_text"] = rebuilt
        try:
            conv = self._conversation_store.load_conversation(conv_id)
            msg_objs = _to_chat_messages(target_messages)
            if conv:
                conv.messages = msg_objs
            else:
                local_title = _extract_local_title(target_messages[0].get("content", "")) if target_messages else "New Conversation"
                model_snapshot = self._build_model_snapshot()
                conv = Conversation(
                    id=conv_id, title=local_title,
                    system_prompt="", messages=msg_objs,
                    model_config_snapshot=model_snapshot,
                    created_at=int(time.time() * 1000),
                    updated_at=int(time.time() * 1000),
                )
            self._conversation_store.save_conversation(conv, bump_updated_at=True)
            if conv.title in ("New Conversation", "(untitled)") and target_messages:
                first_msg = target_messages[0].get("content", "")
                if first_msg:
                    title_cfg = self._get_title_model_config()
                    if not title_cfg:
                        base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = self._read_model_config(None, self._ai_active_model_info)
                        title_cfg = (base_url, api_key, model_name, temperature, max_tokens, top_p)
                    if title_cfg and title_cfg[1]:
                        threading.Thread(target=self._generate_conversation_title, args=(first_msg, conv_id, *title_cfg), daemon=True).start()
        except Exception as e:
            print(f"Error saving bg conv: {e}", flush=True)

    # ═══════════════════════════════════════════════════════════════
    # 发送按钮 & 命令处理
    # ═══════════════════════════════════════════════════════════════

    def _on_send_clicked(self, _btn=None):
        """发送按钮/Enter 处理逻辑（完整保留原命令处理）。"""
        # Check pending AskUserQuestion
        ask_state = getattr(self, "_ai_ask_user_state", None)
        if ask_state is not None:
            text = self._ai_input_area.get_text()
            if not text:
                return
            self._ai_input_area.clear_text()
            self._ai_input_area.set_placeholder("输入后续问题...")
            self._ai_input_area.update_send_button(False)
            if text in ("/cancel", "/abort"):
                safe_q = html.escape(ask_state["question"])
                self._ai_webview_component.append_html(
                    f'<div class="chat-system-error">❌ 已取消问题：「{safe_q}」</div>'
                )
                ask_state["answer"] = ""
                ask_state["event"].set()
                self._ai_input_area.update_send_button(True)
                self._ai_input_area.set_placeholder("输入后续问题...")
                return
            text_cmd = text.split()[0] if text else ""
            known_cmds = {cmd for cmd, _ in self._AI_COMMANDS}
            if text_cmd in known_cmds:
                cmd_name = html.escape(text_cmd)
                self._ai_webview_component.append_html(
                    f'<div class="chat-system-error">❌ 问题已取消（检测到系统命令「{cmd_name}」）。'
                    f'请重新输入命令。</div>'
                )
                ask_state["answer"] = ""
                ask_state["event"].set()
                self._ai_input_area.update_send_button(True)
                self._ai_input_area.set_placeholder("输入后续问题...")
                return
            ask_state["answer"] = text
            ask_state["event"].set()
            return

        # Streaming cancel
        if self._ai_streaming or self._ai_cancelling:
            if not self._ai_cancelling:
                self._ai_cancelling = True
                active_state = self._ai_running_convs.get(self._ai_conversation_id)
                if active_state:
                    active_state["cancel_event"].set()
                self._llm_client.cancel_active_request()
                self._ai_input_area.update_send_button(False, sensitive=False)
                self._ai_input_area.set_placeholder("正在中止...")
                GLib.timeout_add(10000, self._force_cleanup_after_cancel)
            return

        text = self._ai_input_area.get_text()
        if not text and not self._ai_input_area.get_pending_image() and not self._ai_selected_subagents:
            return

        self._ai_input_area.clear_text()

        # ── Command dispatch ──
        if text == "/new":
            self.start_new_conversation()
            return
        if text == "/delete":
            conv_id = self._ai_conversation_id
            if conv_id:
                try:
                    from tool_registry.bash import close_bash_session
                    close_bash_session(conv_id)
                except Exception:
                    pass
                self._conversation_store.delete_conversation(conv_id)
                self._ai_html_cache.pop(conv_id, None)
            self._reset_ai_panel_silent()
            return
        if text == "/retry":
            self._handle_retry_command()
            return
        if text == "/rollback":
            self._handle_rollback_command()
            return
        if text == "/title":
            self._handle_title_command("")
            return
        if text.startswith("/title "):
            self._handle_title_command(text[len("/title "):].strip())
            return
        if text == "/model":
            model_info = self._ai_active_model_info
            if model_info:
                alias = model_info.get("alias", "?")
                mname = model_info.get("model_name", "?")
                info_html = f'<div class="chat-model-info">📋 当前模型: <strong>{alias}</strong> ({mname})<br/><span>输入 /model &lt;别名&gt; 快速切换</span></div>'
                self._ai_webview_component.append_html(info_html)
            self._ai_input_area.show_model_selector()
            return
        if text.startswith("/model "):
            self._switch_model_by_alias(text[len("/model "):].strip())
            return
        if text == "/cd":
            self._select_and_set_bash_cwd()
            return
        if text.startswith("/cd "):
            arg = text[len("/cd "):].strip()
            from tool_registry import set_bash_cwd
            result = set_bash_cwd(arg, session_key=self._ai_conversation_id)
            self._ai_webview_component.append_html(f'<div class="chat-status-notice">{html.escape(result)}</div>')
            return
        if text == "/summary" or text.startswith("/summary "):
            self._handle_summary_command(text)
            return
        if text == "/skill" or text.startswith("/skill:") or text.startswith("/skill ") or text.startswith("skill:"):
            self._handle_skill_command(text)
            return

        # Selected sub-agents
        if self._ai_selected_subagents:
            from tool_registry import get_subagent_status_map, check_background_subagents
            check_background_subagents()
            parts = []
            for sid in sorted(self._ai_selected_subagents):
                info = get_subagent_status_map().get(sid, {})
                task_desc = info.get("task", "未知任务")
                parts.append(f"后台子代理 {sid} 已完成\n任务: {task_desc}\n结果文件: /tmp/opencode_subagent_{sid}_result.txt")
            bg_text = "\n\n---\n\n".join(parts)
            if text:
                text = f"{bg_text}\n\n---\n\n{text}"
            else:
                text = bg_text
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

        # Record history
        if text and (not self._ai_history_queries or self._ai_history_queries[-1] != text):
            self._ai_history_queries.append(text)
        self._ai_history_index = -1
        self._ai_current_draft = ""

        self._send_user_message(text)
        self._ai_input_area._remove_pending_image()

    def _switch_model_by_alias(self, alias: str):
        model = next((m for m in self._llm_settings_store.models if m.alias.lower() == alias.lower()), None)
        if not model:
            lines = [f"❌ 未找到模型别名 **\"{alias}\"**。\n", "可用模型:\n"]
            for m in self._llm_settings_store.models:
                lines.append(f"- **{m.alias}**" + (" (默认)" if m.is_default else "") + f" — `{m.model_name}`")
            error_msg = "\n".join(lines)
            html_content = _markdown_to_html_safe(error_msg, fallback_content=f"<p>Model '{alias}' not found</p>")
            self._ai_webview_component.append_html(html_content)
            return
        self._ai_active_model_info = {
            "alias": model.alias, "base_url": model.base_url.strip(),
            "model_name": model.model_name.strip(), "temperature": model.temperature,
            "max_tokens": model.max_tokens, "top_p": model.top_p,
            "thinking_enabled": model.thinking_enabled, "reasoning_effort": model.reasoning_effort,
        }
        self._ai_last_prompt_obj = None
        display_name = f"{model.alias} ({model.model_name})"
        self._ai_lbl.set_markup(f"<b>AI 助手看盘</b>\n<span size='small' foreground='#888888'>({display_name})</span>")
        self._ai_webview_component.append_html(
            f'<div class="chat-status-notice">🔄 已切换至 <strong>{model.alias}</strong> ({model.model_name})</div>'
        )

    def _handle_retry_command(self):
        self._cancel_streaming_if_active()
        msgs = self._ai_messages
        if not msgs:
            return
        user_index = len(msgs) - 1
        while user_index >= 0 and msgs[user_index].get("role") != "user":
            user_index -= 1
        if user_index < 0:
            return
        user_content = msgs[user_index].get("content", "")
        if isinstance(user_content, list):
            last_user_content = next((p["text"] for p in user_content if isinstance(p, dict) and p.get("type") == "text"), "")
        else:
            last_user_content = user_content
        self._ai_messages = msgs[:user_index]
        self._ai_input_area.set_text(last_user_content)
        self._ai_markdown_text = self._rebuild_markdown()
        self._ai_webview_component.run_javascript("_autoScroll = true;")
        self._ai_webview_component.render_markdown(self._ai_markdown_text)
        self._save_current_conversation(self._build_model_snapshot())

    def _handle_rollback_command(self):
        self._cancel_streaming_if_active()
        msgs = self._ai_messages
        rounds = AIChatHistoryManager.build_conversation_rounds(msgs)
        if not rounds:
            self._ai_webview_component.append_html('<div class="chat-system-error">⚠️ 没有可回滚的对话轮次。</div>')
            return
        try:
            html_val = AIChatHistoryManager.build_round_cards_html(rounds)
        except Exception as e:
            import traceback; traceback.print_exc()
            self._ai_webview_component.append_html(f'<div class="chat-system-error">❌ 生成回滚列表时出错: {html.escape(str(e))}</div>')
            return
        self._ai_webview_component.append_html(html_val)

    def _handle_title_command(self, title_text: str):
        if not self._ai_conversation_id or not self._ai_messages:
            self._ai_webview_component.append_html('<div class="chat-simple-error">没有活跃的对话可供设置标题。</div>')
            return
        if title_text:
            self._ai_title_generated = True
            self._on_title_generated(self._ai_conversation_id, title_text)
            self._ai_webview_component.append_html(f'<div class="chat-simple-info">标题已设置为: {html.escape(title_text)}</div>')
        else:
            self._cancel_streaming_if_active()
            context_msgs = self._ai_messages[:6]
            context_lines = []
            for m in context_msgs:
                role = "User" if m.get("role") == "user" else "Assistant"
                content = m.get("content", "")
                context_lines.append(f"{role}: {content}")
            context_text = "\n\n".join(context_lines)
            if not context_text.strip():
                self._ai_webview_component.append_html('<div class="chat-simple-error">对话内容为空，无法生成标题。</div>')
                return
            title_cfg = self._get_title_model_config()
            if not title_cfg:
                base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = self._read_model_config(self._ai_last_prompt_obj, self._ai_active_model_info)
                title_cfg = (base_url, api_key, model_name, temperature, max_tokens, top_p)
            if title_cfg and title_cfg[1]:
                self._ai_title_generated = True
                self._ai_pending_title_notification = True
                self._ai_webview_component.append_html('<div class="chat-simple-info">正在根据对话内容重新生成标题...</div>')
                threading.Thread(target=self._generate_title_from_context, args=(context_text, self._ai_conversation_id, *title_cfg), daemon=True).start()
            else:
                self._ai_webview_component.append_html('<div class="chat-simple-error">LLM 配置不完整，无法生成标题。</div>')

    def _handle_summary_command(self, text: str):
        if self._ai_settings_store is not None:
            default_keep = self._ai_settings_store.trim_target
        else:
            default_keep = AISettingsStore().trim_target
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
        if not self._ai_messages:
            self._ai_webview_component.append_html('<div class="chat-simple-error">对话为空，无法压缩。</div>')
            return
        total = len(self._ai_messages)
        if keep >= total:
            self._ai_webview_component.append_html(f'<div class="chat-simple-error">消息数不足（共 {total} 条，需保留 {keep} 条），无法压缩。</div>')
            return
        keep = max(5, min(keep, total - 1))
        self._ai_webview_component.append_html(f'<div class="chat-simple-info">⏳ 开始压缩上下文，保留最近 {keep} 条...</div>')
        if self._ai_summary_generating:
            self._ai_webview_component.append_html('<div class="chat-simple-error">已在生成摘要中，请等待完成后再试。</div>')
            return
        if self._ai_settings_store and not self._ai_settings_store.enable_summary:
            self._ai_webview_component.append_html('<div class="chat-simple-error">摘要功能未启用。</div>')
            return
        pruned = self._ai_messages[:-keep]
        trim_target = keep + 1
        self._ai_summary_generating = True
        self._show_summary_status()
        threading.Thread(target=self._generate_summary_async, args=(list(pruned), trim_target), daemon=True).start()

    def _handle_skill_command(self, text: str):
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
        if not skill_name:
            skills = store.get_skills(cwd=cwd)
            if not skills:
                info_html = '<div class="chat-status-notice">🔍 当前未发现可用的 Skill。</div>'
            else:
                items = "".join([f'<li><strong>skill:{html.escape(sk.name)}</strong> — {html.escape(sk.description)}</li>' for sk in skills])
                info_html = f'<div class="chat-model-info">🛠️ <strong>可用 Skill ({len(skills)}):</strong><ul>{items}</ul></div>'
            self._ai_webview_component.append_html(info_html)
            return
        content = store.get_skill_content(skill_name, cwd=cwd)
        if not content:
            available = store.get_skills(cwd=cwd)
            names = [s.name for s in available]
            self._ai_webview_component.append_html(f'<div class="chat-system-error">❌ 找不到「{html.escape(skill_name)}」Skill。当前: {html.escape(str(names))}</div>')
            return
        _MAX_SKILL_PAYLOAD_LEN = 30000
        if len(content) > _MAX_SKILL_PAYLOAD_LEN:
            content = content[:_MAX_SKILL_PAYLOAD_LEN] + "\n\n...[截断]"
        self._ai_webview_component.append_html(f'<div class="chat-status-notice">📖 已激活 Skill：「{html.escape(skill_name)}」</div>')
        prompt_payload = f"[手动触发 Skill: {skill_name}]\n\n{content}\n\n请严格按上述 Skill 指导完成任务。"
        self._ai_input_area.set_text(prompt_payload)
        # Trigger send
        self._on_send_clicked()

    def _rollback_to_round(self, round_index: int):
        msgs = self._ai_messages
        rounds = AIChatHistoryManager.build_conversation_rounds(msgs)
        total_rounds = len(rounds)
        next_round_idx = round_index + 1
        if next_round_idx >= total_rounds:
            return
        target_user_idx = rounds[next_round_idx]["user_idx"]
        user_content = msgs[target_user_idx].get("content", "")
        if isinstance(user_content, list):
            discarded = next((p["text"] for p in user_content if isinstance(p, dict) and p.get("type") == "text"), "")
        else:
            discarded = user_content
        self._ai_messages = msgs[:target_user_idx]
        self._ai_input_area.set_text(discarded)
        self._ai_markdown_text = self._rebuild_markdown()
        self._ai_webview_component.run_javascript("_autoScroll = true;")
        self._ai_webview_component.render_markdown(self._ai_markdown_text)
        try:
            self._save_current_conversation(self._build_model_snapshot())
        except Exception as e:
            print(f"Save after rollback error: {e}", flush=True)

    def _cancel_streaming_if_active(self):
        if self._ai_streaming:
            self._ai_cancel_event.set()
            self._llm_client.cancel_active_request()
            partial = getattr(self, "_ai_current_assistant_text", "")
            if partial.strip():
                self._ai_messages.append({"role": "assistant", "content": partial})
            self._ai_input_area.update_send_button(False)
            self._ai_streaming = False
            self._ai_cancelling = False
            self._ai_spinner.stop()
            self._ai_spinner.hide()

    def _force_cleanup_after_cancel(self) -> bool:
        if not self._ai_cancelling:
            return False
        self._ai_cancelling = False
        self._ai_running_convs.pop(self._ai_conversation_id, None)
        self._ai_streaming = False
        self._ai_input_area.update_send_button(True, sensitive=True)
        self._ai_input_area.set_placeholder("输入后续问题...")
        self._ai_input_area.grab_focus_entry()
        self._ai_spinner.stop()
        self._ai_spinner.hide()
        return False

    def _build_model_snapshot(self) -> Dict[str, Any]:
        active = getattr(self, "_ai_active_model_info", None)
        if active:
            return dict(active)
        base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = self._read_model_config(None, None)
        return {
            "alias": "Default", "base_url": base_url, "model_name": model_name,
            "temperature": temperature, "max_tokens": max_tokens, "top_p": top_p,
            "thinking_enabled": False, "reasoning_effort": "high",
        }

    def _save_current_conversation(self, model_snapshot: Dict[str, Any],
                                    preserve_updated_at: bool = False):
        local_title = "New Conversation"
        if self._ai_messages:
            local_title = _extract_local_title(self._ai_messages[0].get("content", ""))
        if not self._ai_conversation_id:
            now = int(time.time() * 1000)
            self._ai_conversation_created_at = now
            conv = self._conversation_store.create_conversation(title=local_title, model_config=model_snapshot)
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
                    id=self._ai_conversation_id, title=local_title,
                    system_prompt="", messages=[_dict_to_chat_message(m) for m in self._ai_messages],
                    model_config_snapshot=model_snapshot,
                    created_at=self._ai_conversation_created_at,
                    updated_at=int(time.time() * 1000),
                )
            self._conversation_store.save_conversation(conv, bump_updated_at=not preserve_updated_at)
        if self._ai_conversation_id:
            self._ai_html_cache[self._ai_conversation_id] = getattr(self, "_last_rendered_html", "")

    # ═══════════════════════════════════════════════════════════════
    # 对话切换 / 排序 / 标题生成 / Prune / Token
    # ═══════════════════════════════════════════════════════════════

    def _switch_to_conversation(self, conv_id: str):
        if not hasattr(self, "_ai_request_id"):
            self._ai_request_id = 0
        self._ai_request_id += 1
        if self._ai_messages and self._ai_conversation_id:
            is_currently_running = self._ai_running_convs.get(self._ai_conversation_id, {}).get("streaming", False)
            if not is_currently_running:
                try:
                    self._save_current_conversation(self._build_model_snapshot(), preserve_updated_at=True)
                except Exception as e:
                    print(f"Save before switch error: {e}", flush=True)
        self._clear_subagent_bar_instantly()
        if self._ai_render_timeout_id != 0:
            GLib.source_remove(self._ai_render_timeout_id)
            self._ai_render_timeout_id = 0
        conv = self._conversation_store.load_conversation(conv_id)
        if not conv:
            return
        wv = self._ai_webview_component
        st = self._ai_running_convs.get(conv_id)
        if st and st.get("streaming"):
            self._ai_messages = st["messages"]
            self._ai_conversation_id = conv_id
            self._ai_conversation_created_at = conv.created_at
            self._ai_summary = conv.summary if conv else ""
            cached = self._ai_html_cache.get(conv_id)
            if cached is not None:
                self._last_rendered_html = cached
                self._ai_markdown_text = st["ai_markdown_text"]
                wv.run_javascript(f"updateContent({json.dumps(cached)});")
            else:
                self._ai_markdown_text = st["ai_markdown_text"]
                wv.render_markdown(self._ai_markdown_text)
            self._ai_current_assistant_text = st.get("current_assistant_text", "")
            self._ai_current_reasoning_text = st.get("current_reasoning_text", "")
            self._ai_response_div_added = st.get("response_div_added", False)
            self._ai_streaming = True
            self._ai_input_area.update_send_button(True)
            self._ai_input_area.set_placeholder("等待回复中...")
            self._ai_spinner.show()
            self._ai_spinner.start()
        else:
            self._ai_messages = []
            for m in conv.messages:
                msg = {"role": m.role, "content": m.content}
                if m.tool_call_id: msg["tool_call_id"] = m.tool_call_id
                if m.name: msg["name"] = m.name
                if m.tool_calls: msg["tool_calls"] = m.tool_calls
                if m.reasoning_content: msg["reasoning_content"] = m.reasoning_content
                self._ai_messages.append(msg)
            self._ai_conversation_id = conv.id
            self._ai_conversation_created_at = conv.created_at
            self._ai_summary = conv.summary or ""
            self._ai_current_assistant_text = ""
            self._ai_current_reasoning_text = ""
            self._ai_response_div_added = False
            self._ai_streaming = False
            self._ai_input_area.update_send_button(False)
            self._ai_input_area.set_placeholder("")
            self._ai_spinner.stop()
            self._ai_spinner.hide()
            cached = self._ai_html_cache.get(conv_id)
            if cached is not None:
                self._last_rendered_html = cached
                self._ai_markdown_text = _rebuild_markdown_from_messages(self._ai_messages)
                wv.run_javascript(f"updateContent({json.dumps(cached)});")
            else:
                self._ai_markdown_text = _rebuild_markdown_from_messages(self._ai_messages)
                self._prune_messages()
                wv.render_markdown(self._ai_markdown_text)
        self._refresh_subagent_bar()
        _, _, _, display_name, _, _, _, _, _ = self._read_model_config(None, self._ai_active_model_info)
        self._ai_lbl.set_markup(f"<b>AI 助手看盘</b>\n<span size='small' foreground='#888888'>({display_name})</span>")
        self.separator.set_no_show_all(False)
        self.separator.show()
        self.set_no_show_all(False)
        self.show()
        self._ai_input_area.set_no_show_all(False)
        self.show_all()
        self._ai_input_area.clear_text()
        self._ai_input_area.grab_focus_entry()
        self.queue_resize()
        try:
            self._ai_history_popover.refresh_dropdown()
        except Exception:
            pass
        self._update_token_display()

    def _get_sorted_conversations(self) -> List[Dict[str, Any]]:
        """Return conversations sorted by updated_at descending."""
        summaries = self._conversation_store.list_conversations()
        existing_ids = {s.get("id") for s in summaries}
        active_id = self._ai_conversation_id
        if active_id and active_id not in existing_ids:
            if self._ai_messages:
                first_msg = self._ai_messages[0].get("content", "")
                if isinstance(first_msg, list):
                    first_msg = next((p["text"] for p in first_msg if isinstance(p, dict) and p.get("type") == "text"), "")
                title = first_msg[:30] if first_msg else "New Conversation"
                summaries.append({"id": active_id, "title": title, "message_count": len([m for m in self._ai_messages if (m.get("role") if isinstance(m, dict) else getattr(m, "role", "")) != "system"]), "updated_at": int(time.time() * 1000)})
                existing_ids.add(active_id)
        for cid, st in list(self._ai_running_convs.items()):
            if cid not in existing_ids:
                msgs = st.get("messages", [])
                if msgs:
                    first_msg = msgs[0].get("content", "")
                    if isinstance(first_msg, list):
                        first_msg = next((p["text"] for p in first_msg if isinstance(p, dict) and p.get("type") == "text"), "")
                    title = first_msg[:30] if first_msg else "New Conversation"
                    summaries.append({"id": cid, "title": title, "message_count": len([m for m in msgs if (m.get("role") if isinstance(m, dict) else getattr(m, "role", "")) != "system"]), "updated_at": int(time.time() * 1000)})
                    existing_ids.add(cid)
        summaries.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
        return summaries

    def navigate_conversation(self, direction: int):
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
            if self._ai_history_popover and self._ai_history_popover.get_visible():
                self._ai_history_popover.popdown()
            self._switch_to_conversation(target_id)

    # ── 标题生成 ──

    def _call_llm_sync(self, messages, base_url, api_key, model_name, timeout=15,
                        temperature=DEFAULT_TEMPERATURE, max_tokens=DEFAULT_MAX_TOKENS,
                        top_p=DEFAULT_TOP_P):
        config = LLMRequestConfig(base_url=base_url, api_key=api_key, model_name=model_name,
                                  timeout=timeout, temperature=temperature,
                                  max_tokens=max_tokens, top_p=top_p)
        return self._llm_client.sync_chat_completion(config, messages).get("content")

    def _call_llm_and_set_title(self, prompt, conv_id, base_url, api_key, model_name,
                                 temperature, max_tokens, top_p, log_label="title"):
        try:
            content = self._call_llm_sync([{"role": "user", "content": prompt}],
                                           base_url, api_key, model_name, 15,
                                           temperature, max_tokens, top_p)
            if content:
                m = re.search(r'<title>(.+?)</title>', content, re.IGNORECASE)
                if m:
                    GLib.idle_add(self._on_title_generated, conv_id, m.group(1).strip())
        except Exception as e:
            print(f"Title gen error ({log_label}): {e}", flush=True)

    def _generate_conversation_title(self, first_message, conv_id, base_url, api_key, model_name,
                                      temperature=DEFAULT_TEMPERATURE, max_tokens=DEFAULT_MAX_TOKENS,
                                      top_p=DEFAULT_TOP_P):
        prompt = f"第一条消息：\n{first_message}\n\n请为以上对话的第一条消息生成一个简明、专业的中文标题。\n规则：\n1. 概括用户提问的核心意图、主题或所涉及的关键技术\n2. 标题长度严格控制在 12 个汉字以内。\n3. 必须且只能按照以下 XML 标签格式输出：\n   <title>具体标题</title>"
        self._call_llm_and_set_title(prompt, conv_id, base_url, api_key, model_name,
                                      temperature, max_tokens, top_p)

    def _generate_title_from_context(self, context_text, conv_id, base_url, api_key, model_name,
                                      temperature=DEFAULT_TEMPERATURE, max_tokens=DEFAULT_MAX_TOKENS,
                                      top_p=DEFAULT_TOP_P):
        prompt = f"对话内容：\n{context_text}\n\n请为以上对话生成一个简明、专业的中文标题。\n规则：\n1. 概括整个对话的核心意图、主题或所涉及的关键技术\n2. 标题长度严格控制在 12 个汉字以内\n3. 必须且只能按照以下 XML 标签格式输出：\n   <title>具体标题</title>"
        self._call_llm_and_set_title(prompt, conv_id, base_url, api_key, model_name,
                                      temperature, max_tokens, top_p, log_label="title from context")

    def _on_title_generated(self, conv_id: str, title: str):
        conv = self._conversation_store.load_conversation(conv_id)
        if conv:
            conv.title = title
            self._conversation_store.save_conversation(conv, bump_updated_at=False)
        self._ai_history_popover.refresh_dropdown()
        if getattr(self, "_ai_pending_title_notification", False):
            self._ai_pending_title_notification = False
            self._ai_webview_component.append_html(f'<div class="chat-simple-info">标题已生成: {html.escape(title)}</div>')

    # ── Prune / Summary ──

    def _prune_messages(self):
        if self._ai_settings_store is not None:
            soft_limit = self._ai_settings_store.soft_limit
            trim_target = self._ai_settings_store.trim_target
            enable_summary = self._ai_settings_store.enable_summary
            summary_threshold = self._ai_settings_store.summary_threshold
        else:
            fallback = AISettingsStore()
            soft_limit = fallback.soft_limit
            trim_target = fallback.trim_target
            enable_summary = fallback.enable_summary
            summary_threshold = fallback.summary_threshold
        if len(self._ai_messages) <= soft_limit:
            return
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
                threading.Thread(target=self._generate_summary_async, args=(list(pruned), trim_target), daemon=True).start()
                return
        self._apply_prune(trim_target)

    def _apply_prune(self, trim_target: int, save_summary: bool = False):
        if len(self._ai_messages) <= 1:
            self._clear_summary_status()
            return
        first = self._ai_messages[:1]
        rest = self._ai_messages[1:]
        target_len = trim_target - 1
        start_idx = len(rest) - target_len
        if start_idx < 0:
            start_idx = 0
        while start_idx > 0 and rest[start_idx].get("role") == "tool":
            start_idx -= 1
        if start_idx == 0 and rest and rest[0].get("role") == "tool":
            while start_idx < len(rest) and rest[start_idx].get("role") == "tool":
                start_idx += 1
        self._ai_messages = first + rest[start_idx:]
        self._ai_markdown_text = self._rebuild_markdown()
        self._ai_webview_component.render_markdown(self._ai_markdown_text)
        if save_summary:
            self._save_summary_to_conversation()
            try:
                self._save_current_conversation(self._build_model_snapshot(), preserve_updated_at=True)
            except Exception:
                pass
            try:
                self._ai_history_popover.refresh_dropdown()
            except Exception:
                pass
        self._clear_summary_status()
        self._update_token_display()

    def _generate_summary_async(self, pruned_messages, trim_target):
        """Background thread: generate summary via LLM."""
        save_summary = False
        cancel_event = threading.Event()
        idle_timeout_sec = 25
        total_timeout_sec = 120
        failure_reason = None
        has_received_token = False

        total_timer = threading.Timer(total_timeout_sec, cancel_event.set)
        total_timer.daemon = True
        total_timer.start()
        idle_timer = None

        def _reset_idle_timer():
            nonlocal idle_timer
            if idle_timer:
                idle_timer.cancel()
            idle_timer = threading.Timer(idle_timeout_sec, cancel_event.set)
            idle_timer.daemon = True
            idle_timer.start()

        try:
            max_chars = self._ai_settings_store.summary_max_chars if self._ai_settings_store else 500
            convo_lines = []
            for m in pruned_messages:
                role = m.get("role", "unknown")
                content = m.get("content", "")
                if isinstance(content, list):
                    content = str(content)
                s = str(content)
                if len(s) > 500:
                    s = s[:500] + "...(截断)"
                convo_lines.append(f"{role.upper()}: {s}")
            convo_text = "\n".join(convo_lines)
            prev_summary = f"已有摘要：\n{self._ai_summary}\n\n" if self._ai_summary else ""
            template = self._ai_settings_store.summary_prompt_template if self._ai_settings_store else _DEFAULT_SUMMARY_TEMPLATE
            try:
                prompt = template.format(prev_summary=prev_summary, conversation_text=convo_text, max_chars=max_chars)
            except (KeyError, ValueError) as e:
                failure_reason = f"模板格式错误：{e}"
                return

            base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = self._read_model_config(self._ai_last_prompt_obj, self._ai_active_model_info)
            if not base_url or not api_key or not model_name:
                failure_reason = "模型配置不完整"
                return

            result_parts = []
            summary_config = LLMRequestConfig(
                base_url=base_url, api_key=api_key, model_name=model_name,
                temperature=0.3, max_tokens=max(4096, max_chars * 4),
                top_p=top_p, timeout=idle_timeout_sec,
            )
            for event in self._llm_client.stream_chat_completion(
                summary_config, [{"role": "user", "content": prompt}], cancel_event=cancel_event
            ):
                if cancel_event.is_set():
                    break
                if event.type == StreamEventType.TEXT_DELTA and event.text_delta:
                    if not has_received_token:
                        has_received_token = True
                        _reset_idle_timer()
                        total_timer.cancel()
                    else:
                        _reset_idle_timer()
                    result_parts.append(event.text_delta)
                    GLib.idle_add(self._update_summary_display, event.text_delta)
                elif event.type == StreamEventType.STREAM_END:
                    break

            if cancel_event.is_set():
                failure_reason = f"摘要生成超时" if not has_received_token else f"摘要生成超时（流式停顿{idle_timeout_sec}秒）"
            else:
                result = "".join(result_parts).strip()
                if result:
                    self._ai_summary = f"{self._ai_summary}\n后续对话摘要：{result}" if self._ai_summary else result
                    if len(self._ai_summary) > max_chars * 3:
                        self._ai_summary = self._ai_summary[-max_chars * 3:]
                    save_summary = True

        except _LLMHttpError as e:
            failure_reason = f"摘要生成失败（{e}）"
        except Exception as e:
            failure_reason = f"摘要生成异常：{e}"
            import traceback; traceback.print_exc()
        finally:
            total_timer.cancel()
            if idle_timer:
                idle_timer.cancel()
            self._ai_summary_generating = False
            if failure_reason:
                GLib.idle_add(self._show_summary_failure, failure_reason)
            else:
                GLib.idle_add(self._apply_prune, trim_target, save_summary)

    def _show_summary_failure(self, reason: str):
        self._clear_summary_status()
        html_content = f'<div class="chat-message system-message" style="margin:8px 0;padding:8px 12px;background:var(--notice-bg,#fff3cd);border-left:4px solid var(--notice-border,#ffc107);border-radius:4px;font-size:13px;">⚠️ <b>上下文压缩失败</b><br>{reason}</div>'
        self._ai_webview_component.append_html(html_content)

    def _save_summary_to_conversation(self):
        try:
            if not self._ai_conversation_id:
                return
            conv = self._conversation_store.load_conversation(self._ai_conversation_id)
            if conv:
                conv.summary = self._ai_summary
                self._conversation_store.save_conversation(conv, bump_updated_at=False)
        except Exception as e:
            print(f"Save summary error: {e}", flush=True)

    def _show_summary_status(self):
        self._ai_input_area.set_send_button_sensitive(False)
        self._ai_input_area.set_placeholder("摘要压缩中...")
        self._ai_webview_component.append_html(
            '<div id="summary-display" class="summary-display">'
            '<div class="summary-header">📝 摘要压缩中</div>'
            '<div class="summary-content"></div></div>'
        )

    def _update_summary_display(self, text: str):
        if not text:
            return
        escaped = json.dumps(text)
        if self._ai_webview_component.webview:
            self._ai_webview_component.run_javascript(
                f"(function(){{var e=document.getElementById('summary-display');if(e){{var c=e.querySelector('.summary-content');if(c)c.textContent+={escaped};_scrollToBottom();}}}})();"
            )

    def _clear_summary_status(self):
        self._ai_input_area.set_send_button_sensitive(True)
        self._ai_input_area.set_placeholder("输入后续问题...")
        self._ai_input_area.grab_focus_entry()
        if self._ai_webview_component.webview:
            self._ai_webview_component.run_javascript(
                "var e=document.getElementById('summary-display');if(e)e.remove();_scrollToBottom();"
            )

    def append_html_to_webview(self, html_content: str):
        self._ai_webview_component.append_html(html_content)

    def _update_token_display(self):
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            n = 3
            for msg in self._ai_messages:
                n += 4
                for k, v in msg.items():
                    n += len(enc.encode(str(v)))
                    if k == "name":
                        n -= 1
            count = int(n * 0.89)
        except ImportError:
            total = sum(len(str(v)) for msg in self._ai_messages for v in msg.values())
            count = int(total / 2.5) if total else 0
        self._ai_input_area.update_hint_label(count)

    # ═══════════════════════════════════════════════════════════════
    # 子代理状态栏
    # ═══════════════════════════════════════════════════════════════

    def _on_subagent_status_changed(self, sid: str, info: Optional[dict]):
        try:
            active_conv_id = self._ai_conversation_id
            if info is None:
                self._remove_subagent_block(sid)
                self._update_subagent_bar_visibility()
                return
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
            import sys; print(f"[opencode-switcher] subagent error: {e}", file=sys.stderr)

    def _refresh_subagent_bar(self):
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
            import sys; print(f"[opencode-switcher] refresh subagent error: {e}", file=sys.stderr)

    def _create_subagent_block(self, sid: Any, info: dict):
        child = Gtk.FlowBoxChild.new()
        box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 4)
        local_id = sid.split("-")[-1] if isinstance(sid, str) and "-" in sid else sid
        label = Gtk.Label.new(f"  子代理 {local_id}  ")
        label.set_margin_start(4); label.set_margin_end(4)
        label.set_margin_top(2); label.set_margin_bottom(2)
        box.pack_start(label, True, True, 0)
        child.add(box)
        status = info.get("status", "unknown")
        task = info.get("task", "")
        tooltip_text = f"ID: {sid}\n任务: {task}"
        ctx = box.get_style_context()
        if status == "completed":
            ctx.add_class("subagent-block-done")
        elif status == "running":
            ctx.add_class("subagent-block-running")
        else:
            ctx.add_class("subagent-block-failed")
        child.set_tooltip_text(tooltip_text)
        # Use the input area's subagent bar
        self._ai_subagent_bar = self._ai_input_area._ai_subagent_bar
        self._ai_subagent_bar.add(child)
        self._ai_subagent_blocks[sid] = (child, child, box)
        self._ai_subagent_bar.show_all()

    def _update_subagent_block(self, sid: Any, info: dict):
        entry = self._ai_subagent_blocks.get(sid)
        if entry is None:
            return
        child, event_box, box = entry
        status = info.get("status", "unknown")
        ctx = box.get_style_context()
        if status == "completed":
            ctx.remove_class("subagent-block-running")
            ctx.add_class("subagent-block-done")
        elif status == "running":
            ctx.remove_class("subagent-block-done")
            ctx.add_class("subagent-block-running")

    def _remove_subagent_block(self, sid: Any):
        self._ai_selected_subagents.discard(sid)
        entry = self._ai_subagent_blocks.pop(sid, None)
        if entry:
            child, _event_box, _box = entry
            self._ai_subagent_bar.remove(child)
            self._update_subagent_bar_visibility()

    def _clear_subagent_bar_instantly(self):
        if hasattr(self, '_ai_subagent_bar') and self._ai_subagent_bar:
            self._ai_subagent_bar.hide()
            for child in self._ai_subagent_bar.get_children():
                self._ai_subagent_bar.remove(child)
        self._ai_subagent_blocks.clear()
        self._ai_selected_subagents.clear()
        self._update_subagent_bar_visibility()

    def _update_subagent_bar_visibility(self):
        if not hasattr(self, '_ai_subagent_bar'):
            return
        has_blocks = len(self._ai_subagent_blocks) > 0
        if has_blocks:
            self._ai_subagent_bar.set_no_show_all(False)
            self._ai_subagent_bar.show_all()
        else:
            self._ai_subagent_bar.hide()
            self._ai_subagent_bar.set_no_show_all(True)

    # ═══════════════════════════════════════════════════════════════
    # 面板显隐控制
    # ═══════════════════════════════════════════════════════════════

    def is_visible(self) -> bool:
        return self.get_visible()

    def is_popup_shown(self):
        return bool(self._ai_history_popover and self._ai_history_popover.get_visible())

    def on_panel_shown(self):
        self._init_mcp()
        if self._suspend_timeout_id != 0:
            GLib.source_remove(self._suspend_timeout_id)
            self._suspend_timeout_id = 0
        if self._ai_webview_component.is_suspended:
            cached_html = self._ai_html_cache.get(self._ai_conversation_id)
            self._ai_webview_component.restore(
                self._theme, cached_html or ""
            )
        self._ai_input_area.grab_focus_entry()

    def on_panel_hidden(self):
        if self._suspend_timeout_id != 0:
            GLib.source_remove(self._suspend_timeout_id)
            self._suspend_timeout_id = 0
        self._suspend_timeout_id = GLib.timeout_add_seconds(
            self._SUSPEND_DELAY_SECONDS, self._suspend_webview_cb
        )

    def _suspend_webview_cb(self) -> bool:
        running_states = list(self._ai_running_convs.values())
        any_running = any(st.get("streaming", False) for st in running_states)
        if any_running:
            return True
        self._suspend_timeout_id = 0
        if not self._ai_webview_component.is_suspended:
            if self._ai_conversation_id and self._ai_messages:
                self._ai_markdown_text = self._rebuild_markdown()
                self._last_rendered_html = _markdown_to_html_safe(self._ai_markdown_text, fallback_content="")
                self._ai_html_cache[self._ai_conversation_id] = self._last_rendered_html
                self._ai_webview_component.last_rendered_html = self._last_rendered_html
            self._ai_webview_component.suspend(self._ai_webview_component.last_rendered_html)
        return False

    def hide_panel(self):
        self.on_panel_hidden()
        self._ai_input_area.update_send_button(False)
        self.set_no_show_all(True)
        self.hide()
        self.separator.set_no_show_all(True)
        self.separator.hide()
        self._ai_panel_visible_saved = False
        self.queue_resize()

    def show_panel(self):
        self.on_panel_shown()
        self.set_no_show_all(False)
        self.show()
        self.show_all()
        self.separator.set_no_show_all(False)
        self.separator.show()
        self.queue_resize()

    def reset_state(self):
        self._reset_ai_panel_silent()

    def grab_entry_focus(self):
        self._ai_input_area.grab_focus_entry()

    def insert_text_to_input(self, text: str):
        buf = self._ai_input_area._ai_entry.get_buffer()
        buf.insert_at_cursor(text)
        self._ai_input_area.grab_focus_entry()

    def _reset_ai_panel_silent(self):
        self._ai_spinner.stop()
        self._ai_spinner.hide()
        self._ai_input_area.update_send_button(False)
        self._ai_streaming = False
        self._ai_input_area.set_placeholder("")
        self._last_rendered_html = ""
        self._ai_messages = []
        self._clear_subagent_bar_instantly()
        self._refresh_subagent_bar()
        self._ai_assistant_buffer = ""
        self._ai_markdown_text = ""
        self._ai_current_assistant_text = ""
        self._ai_response_div_added = False
        self._ai_assistant_html_base = ""
        wv = self._ai_webview_component
        wv.load_html(wv.get_html_template(self._theme), "file:///")
        self._ai_input_area.clear_text()
        _, _, _, display_name, _, _, _, _, _ = self._read_model_config(None, None)
        self._ai_lbl.set_markup(f"<b>AI 助手看盘</b>\n<span size='small' foreground='#888888'>({display_name})</span>")
        self._ai_active_model_info = None
        self._ai_last_prompt_obj = None
        self._ai_title_generated = False
        self._ai_pending_title_notification = False
        self._ai_summary = ""
        self._ai_summary_generating = False
        try:
            from tool_registry import get_bash_cwd, set_bash_cwd
            prev_cwd = get_bash_cwd(session_key=getattr(self, "_ai_conversation_id", None))
            self._ai_conversation_id = uuid4().hex[:12]
            set_bash_cwd(prev_cwd, session_key=self._ai_conversation_id)
        except Exception:
            self._ai_conversation_id = uuid4().hex[:12]
        self._ai_input_area.set_no_show_all(False)
        self._ai_input_area.show_all()
        self._ai_input_area.grab_focus_entry()
        self.queue_resize()
        self._ai_history_popover.refresh_dropdown()
        self._update_token_display()

    def start_new_conversation(self):
        if not hasattr(self, "_ai_request_id"):
            self._ai_request_id = 0
        self._ai_request_id += 1
        if self._ai_conversation_id:
            self._ai_html_cache[self._ai_conversation_id] = getattr(self, "_last_rendered_html", "")
        if self._ai_messages and self._ai_conversation_id:
            is_currently_running = self._ai_running_convs.get(self._ai_conversation_id, {}).get("streaming", False)
            if not is_currently_running:
                try:
                    self._save_current_conversation(self._build_model_snapshot(), preserve_updated_at=True)
                except Exception:
                    pass
        self.separator.set_no_show_all(False)
        self.separator.show()
        self.set_no_show_all(False)
        self.show()
        self.show_all()
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
                        self._ai_input_area.grab_focus_entry()
                else:
                    self._switch_to_conversation(latest_id)
        else:
            self._reset_ai_panel_silent()

    def _select_and_set_bash_cwd(self):
        toplevel = self.get_toplevel()
        if not isinstance(toplevel, Gtk.Window):
            toplevel = None
        dialog = Gtk.FileChooserDialog(
            title="选择 Bash 工作目录", transient_for=toplevel,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_button("_取消", Gtk.ResponseType.CANCEL)
        dialog.add_button("_选择", Gtk.ResponseType.ACCEPT)
        if self.on_dialog_shown:
            dialog.connect("show", lambda *_: self.on_dialog_shown())
        if self.on_dialog_hidden:
            dialog.connect("destroy", lambda *_: self.on_dialog_hidden())
        from tool_registry import get_bash_cwd
        current_cwd = get_bash_cwd(session_key=self._ai_conversation_id)
        if os.path.isdir(current_cwd):
            dialog.set_current_folder(current_cwd)
        def _on_response(dlg, response):
            if response == Gtk.ResponseType.ACCEPT:
                chosen = dlg.get_filename()
                dlg.destroy()
                if chosen:
                    from tool_registry import set_bash_cwd
                    result = set_bash_cwd(chosen, session_key=self._ai_conversation_id)
                    self._ai_webview_component.append_html(f'<div class="chat-status-notice">{html.escape(result)}</div>')
            else:
                dlg.destroy()
        dialog.connect("response", _on_response)
        dialog.show_all()

    # ═══════════════════════════════════════════════════════════════
    # 主题 & 清理
    # ═══════════════════════════════════════════════════════════════

    def set_theme(self, name):
        self._theme = name
        self._ai_html_cache.clear()
        c = self._get_gtk_colors(name)
        bg = Gdk.RGBA(*c["bg"])
        hdr_bg = Gdk.RGBA(*c["header_bg"])
        input_bg = Gdk.RGBA(*c["input_bg"])
        for w in (self, self._ai_scrolled):
            if w is not None:
                try:
                    w.override_background_color(Gtk.StateFlags.NORMAL, bg)
                except Exception:
                    pass
        if self._ai_input_area:
            try:
                self._ai_input_area.override_background_color(Gtk.StateFlags.NORMAL, input_bg)
            except Exception:
                pass
        if self._ai_hdr:
            try:
                self._ai_hdr.override_background_color(Gtk.StateFlags.NORMAL, hdr_bg)
            except Exception:
                pass
        wv = self._ai_webview_component
        if wv.webview:
            wv.webview.set_background_color(bg)
        wv.apply_theme(name, self._ai_markdown_text)

    def _on_destroy(self, widget):
        """Clean up resources."""
        try:
            from tool_registry import unregister_subagent_status_listener
            unregister_subagent_status_listener(self._on_subagent_status_changed)
        except Exception:
            pass
        if self._mcp_client_mgr is not None and self._mcp_bridge is not None:
            try:
                self._mcp_bridge.call_async(self._mcp_client_mgr.disconnect_all())
            except Exception:
                pass
        if self._mcp_bridge is not None:
            try:
                self._mcp_bridge.stop()
            except Exception:
                pass
