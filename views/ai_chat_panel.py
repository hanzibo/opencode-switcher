"""Backward-compatible facade for views.ai_chat package.

Re-exports AIChatPanel and all related constants, bridges, and text utilities
so existing imports from views.ai_chat_panel continue to work seamlessly.
"""

import gi
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
from gi.repository import Gtk, Gdk, GLib, Gio, Pango, GdkPixbuf, PangoCairo, WebKit2

from stores.clipboard_store import (
    ClipboardItem,
    CategoryItem,
    CategoryStore,
    CustomCategory,
    capture_clipboard_once,
    CustomPrompt,
    CustomPromptsStore,
    LLMSettingsStore,
    LLMModelConfig,
    ConversationStore,
    ChatMessage,
    Conversation,
    AISettingsStore,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TOP_P,
    CONFIG_DIR,
    _DEFAULT_SUMMARY_TEMPLATE,
)

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
    _AI_COMMANDS,
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

from views.ai_chat.panel import AIChatPanel

from views.ai_chat.mcp_mixin import MCPMixin
from views.ai_chat.subagent_mixin import SubagentMixin
from views.ai_chat.streaming_mixin import StreamingMixin
from views.ai_chat.webview_mixin import WebViewMixin
from views.ai_chat.runner_mixin import RunnerMixin
from views.ai_chat.session_mixin import SessionMixin

# Re-export text utility functions for backward compatibility with older tests/callers
from ai_text_utils import (
    _dict_to_chat_message,
    _extract_after_header,
    _escape_math,
    _unescape_math,
    _markdown_to_html_safe,
    _ensure_list_blankline,
    _ensure_table_blankline,
    _close_unclosed_code_blocks,
    _fix_latex,
    _clean_history_title,
    _extract_local_title,
    _rebuild_markdown_from_messages,
    _vision_content_to_markdown,
    _resolve_vision_image_src,
    _vision_content_to_text,
    _image_hash_path,
    _image_to_data_uri,
    _cached_image_to_data_uri,
    _model_supports_vision,
    USER_AVATAR_HTML,
    ASSISTANT_AVATAR_HTML,
    _strip_ai_markup,
    _preserve_newlines,
    set_code_highlight,
)

__all__ = [
    "AIChatPanel",
    "TEMPLATE_REGEX",
    "PROMPT_PLACEHOLDER_RE",
    "_MPS_MEMORY_LIMIT",
    "_MPS_POLL_INTERVAL",
    "_MPS_CONSERVATIVE",
    "_MPS_STRICT",
    "AI_BTN_LABEL_SEND",
    "AI_BTN_LABEL_STOP",
    "_AI_HEADER_TITLE",
    "_AI_COMMANDS",
    "_to_chat_messages",
    "_ai_stream_request_key",
    "_ai_summary_request_key",
    "_webview_shell_fingerprint",
    "_should_full_reload_webview",
    "_MARKUP_TITLE_RE",
    "_MARKUP_MODEL_RE",
    "_WebViewBridgeBase",
    "_HeaderSpinnerBridge",
    "_HeaderTitleBridge",
    "_HistoryPopoverBridge",
    "MCPMixin",
    "SubagentMixin",
    "StreamingMixin",
    "WebViewMixin",
    "RunnerMixin",
    "SessionMixin",
    "execute_send_notification",
]
