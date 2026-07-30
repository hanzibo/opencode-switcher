"""WebView & JS 通信层 — AI 聊天面板的 WebKit2 WebView 封装。

职责：
- WebKit2 WebView 初始化与内存管理
- HTML/JS 渲染与通信
- 流式 Token/Reasoning 批处理
- WebView 进程崩溃恢复与安全挂起
"""

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("GdkPixbuf", "2.0")

import json
import html
from gi.repository import Gtk, Gdk, GLib, Gio, WebKit2
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qs

from ai_text_utils import (
    _markdown_to_html_safe,
    _close_unclosed_code_blocks,
    _preserve_newlines,
    _resolve_vision_image_src,
    _vision_content_to_text,
    _strip_ai_markup,
    _rebuild_markdown_from_messages,
    USER_AVATAR_HTML,
)
from ai_text_utils.render import _render_tool_card_standalone
from ai_engine.render_pipeline import render_turn, TurnRenderInput, build_update_js
from stores.theme_config import get_ai_gtk_colors
from ai_engine.ai_html_template import get_html_template, _get_pygments_css, get_shared_web_context

# WebView memory pressure settings — applied at WebContext construction time
_MPS_MEMORY_LIMIT = 300
_MPS_POLL_INTERVAL = 5
_MPS_CONSERVATIVE = 0.2
_MPS_STRICT = 0.4


class AIChatWebView(Gtk.Box):
    """封装 WebKit2 WebView，提供 HTML 渲染、JS 通信与流式批处理能力。"""

    # ── Streaming: Token batching ──
    _BATCH_FLUSH_MS = 60                    # 批处理窗口（ms）
    _STREAM_PERF_LOG = False

    def __init__(
        self,
        theme_name: str = "dark",
        pygments_css_cache: Optional[Dict] = None,
        on_copy_to_clipboard_cb=None,
        on_toggle_details_cb=None,
        on_copy_started_cb=None,
        on_copy_finished_cb=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._theme_name = theme_name
        self._pygments_css_cache = pygments_css_cache or {}
        self._on_copy_to_clipboard_cb = on_copy_to_clipboard_cb
        self._on_toggle_details_cb = on_toggle_details_cb
        self._on_copy_started_cb = on_copy_started_cb
        self._on_copy_finished_cb = on_copy_finished_cb

        # WebView state
        self._ai_web_context = get_shared_web_context()
        self._ai_webview = None
        self._webview_suspended = False
        self._last_rendered_html = ""
        self._streaming_container_created = False

        # ── Streaming: Token batching state ──
        self._token_buffer = ""
        self._flush_scheduled = False
        self._flush_source_id = 0
        self._reasoning_buffer = ""
        self._reasoning_flush_scheduled = False
        self._reasoning_flush_source_id = 0
        self._last_flushed_len = 0

        # External state references (set by parent panel)
        self._is_streaming = False
        self._get_ai_messages = None       # callable -> List[Dict]
        self._get_current_assistant_text = None
        self._get_current_reasoning_text = None
        self._get_show_tool_details = None
        self._get_ai_request_id = None
        self._get_running_convs = None
        self._get_ai_conversation_id = None

        self._build_webview()

    def _get_gtk_colors(self, theme_name: str) -> dict:
        """Return dict with 'bg', 'header_bg', 'input_bg' as Gdk.RGBA."""
        raw = get_ai_gtk_colors(theme_name)
        return {k: Gdk.RGBA(*v) for k, v in raw.items()}

    def _build_webview(self):
        """创建并配置 WebKit2 WebView。"""
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

        self._ai_webview.load_html(self.get_html_template("dark"), "file:///")

        self._ai_webview.connect("decide-policy", self._on_decide_policy)
        self._ai_webview.connect("context-menu", lambda *_: True)
        self._ai_webview.connect("web-process-terminated", self._on_webview_crashed)

        self.pack_start(self._ai_webview, True, True, 0)

    def set_state_references(
        self,
        is_streaming_fn=None,
        get_messages_fn=None,
        get_assistant_text_fn=None,
        get_reasoning_text_fn=None,
        get_show_tool_details_fn=None,
        get_request_id_fn=None,
        get_running_convs_fn=None,
        get_conversation_id_fn=None,
    ):
        """注入父面板的状态引用，使 WebView 能访问运行状态。"""
        if is_streaming_fn:
            self._is_streaming = is_streaming_fn
        if get_messages_fn:
            self._get_ai_messages = get_messages_fn
        if get_assistant_text_fn:
            self._get_current_assistant_text = get_assistant_text_fn
        if get_reasoning_text_fn:
            self._get_current_reasoning_text = get_reasoning_text_fn
        if get_show_tool_details_fn:
            self._get_show_tool_details = get_show_tool_details_fn
        if get_request_id_fn:
            self._get_ai_request_id = get_request_id_fn
        if get_running_convs_fn:
            self._get_running_convs = get_running_convs_fn
        if get_conversation_id_fn:
            self._get_ai_conversation_id = get_conversation_id_fn

    # ── HTML / JS 渲染 ────────────────────────────────────────────

    def get_pygments_css(self, theme: str) -> str:
        return _get_pygments_css(theme, self._pygments_css_cache)

    def get_html_template(self, theme_name, initial_html=""):
        pygments_css = self.get_pygments_css(theme_name)
        return get_html_template(theme_name, initial_html, pygments_css)

    def render_markdown(self, text: str):
        """将 Markdown 文本渲染到 WebView。"""
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
        html_content = _markdown_to_html_safe(text, fallback_content=fallback_msg)
        self._last_rendered_html = html_content

        js_code = f"updateContent({json.dumps(html_content)});"
        self._ai_webview.run_javascript(js_code, None, None)

    def append_html(self, html_content: str):
        """Insert HTML snippet before end of content div and scroll to bottom."""
        escaped = json.dumps(html_content)
        if self._ai_webview:
            self._ai_webview.run_javascript(
                f"document.getElementById('content').insertAdjacentHTML('beforeend', {escaped});"
                f"_scrollToBottom();",
                None, None
            )

    def run_javascript(self, js_code: str):
        """Run JavaScript in the WebView."""
        if self._ai_webview:
            self._ai_webview.run_javascript(js_code, None, None)

    def load_html(self, html_content: str, base_uri: str = "file:///"):
        """Load HTML content into the WebView."""
        if self._ai_webview:
            self._ai_webview.load_html(html_content, base_uri)

    # ── 流式批处理 ────────────────────────────────────────────────

    def init_streaming_state(self):
        """在每轮对话开始时初始化流式状态。"""
        self._token_buffer = ""
        self._flush_scheduled = False
        self._flush_source_id = 0
        self._reasoning_buffer = ""
        self._reasoning_flush_scheduled = False
        self._reasoning_flush_source_id = 0
        self._last_flushed_len = 0
        self._streaming_container_created = False

    def ensure_streaming_container(self) -> bool:
        """确保流式消息容器已创建。"""
        if not self._streaming_container_created and self._ai_webview:
            req_id = self._get_ai_request_id() if callable(self._get_ai_request_id) else 0
            msg_id = f"msg-{req_id}"
            self._ai_webview.run_javascript(f"appendMessageContainer('{msg_id}');", None, None)
            self._streaming_container_created = True
            return True
        return self._streaming_container_created

    def on_token_delta(self, text: str):
        """收到 LLM 文本增量，累积到 buffer 并安排 60ms flush。"""
        is_streaming = self._is_streaming() if callable(self._is_streaming) else self._is_streaming
        if not is_streaming:
            return
        if self._STREAM_PERF_LOG:
            print(f"[perf] token_delta: +{len(text)}ch, buffer={len(self._token_buffer)}ch", flush=True)

        self._token_buffer += text

        if not self._flush_scheduled:
            self._flush_scheduled = True
            self._flush_source_id = GLib.timeout_add(self._BATCH_FLUSH_MS, self._flush_token_buffer)

    def _flush_token_buffer(self) -> bool:
        """60ms 定时器回调：将累积的 token 文本批量 flush 到 WebView。"""
        is_streaming = self._is_streaming() if callable(self._is_streaming) else self._is_streaming
        if not is_streaming:
            self._token_buffer = ""
            self._flush_scheduled = False
            self._flush_source_id = 0
            return False
        if self._STREAM_PERF_LOG:
            print(f"[perf] flush_token: {len(self._token_buffer)}ch → JS", flush=True)
        self._flush_scheduled = False
        self._flush_source_id = 0

        if not self._token_buffer:
            return False

        self.ensure_streaming_container()

        js_code = f"appendStreamToken({json.dumps(self._token_buffer)});"
        if self._ai_webview:
            self._ai_webview.run_javascript(js_code, None, None)

        self._token_buffer = ""
        return False

    def on_reasoning_delta(self, text: str):
        """收到 LLM 推理增量，累积到 buffer 并安排 60ms flush。"""
        is_streaming = self._is_streaming() if callable(self._is_streaming) else self._is_streaming
        if not is_streaming:
            return
        if self._STREAM_PERF_LOG:
            print(f"[perf] reasoning_delta: +{len(text)}ch, buffer={len(self._reasoning_buffer)}ch", flush=True)
        self._reasoning_buffer += text
        if not self._reasoning_flush_scheduled:
            self._reasoning_flush_scheduled = True
            self._reasoning_flush_source_id = GLib.timeout_add(self._BATCH_FLUSH_MS, self._flush_reasoning_buffer)

    def _flush_reasoning_buffer(self) -> bool:
        """60ms 定时器回调：将累积的推理文本批量 flush 到 WebView。"""
        is_streaming = self._is_streaming() if callable(self._is_streaming) else self._is_streaming
        if not is_streaming:
            self._reasoning_buffer = ""
            self._reasoning_flush_scheduled = False
            self._reasoning_flush_source_id = 0
            return False
        self._reasoning_flush_scheduled = False
        self._reasoning_flush_source_id = 0
        if not self._reasoning_buffer:
            return False

        self.ensure_streaming_container()

        js_code = f"appendStreamReasoning({json.dumps(self._reasoning_buffer)});"
        if self._ai_webview:
            self._ai_webview.run_javascript(js_code, None, None)
        self._reasoning_buffer = ""
        return False

    def on_tool_calls_started(self, req_id: int = 0, *args, **kwargs) -> bool:
        """工具调用开始时：停止推理状态，通知 JS 端结束 thinking 动画，并即刻渲染初始工具卡片 DOM。"""
        if self._STREAM_PERF_LOG:
            print(f"[perf] tool_calls_started", flush=True)

        if self._ai_webview:
            if self._reasoning_flush_source_id:
                GLib.source_remove(self._reasoning_flush_source_id)
                self._reasoning_flush_source_id = 0
                self._reasoning_flush_scheduled = False
            if self._flush_source_id:
                GLib.source_remove(self._flush_source_id)
                self._flush_source_id = 0
                self._flush_scheduled = False
            self._ai_webview.run_javascript("finishReasoning();", None, None)

            # 即刻渲染当前 assistant 消息（在 DOM 中创建 data-tool-call-id 卡片占位符）
            req_id_to_use = req_id or (self._get_ai_request_id() if callable(self._get_ai_request_id) else 0)
            if req_id_to_use:
                self.render_current_assistant_message(req_id_to_use)
        return False

    def on_tool_result(self, tool_call_id: str, result_text: str, status: str, req_id: int = 0, *args, **kwargs) -> bool:
        """增量更新工具调用结果卡片。"""
        show_details = self._get_show_tool_details() if callable(self._get_show_tool_details) else True
        if not show_details:
            return False

        # 查找 tool_call 定义
        messages = self._get_ai_messages() if callable(self._get_ai_messages) else []
        tool_call = None
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if tc.get("id") == tool_call_id:
                        tool_call = tc
                        break
        if not tool_call:
            return False

        card_html = _render_tool_card_standalone(tool_call, result_text, status,
                                                  show_details=show_details)
        js_code = f"updateToolCard({json.dumps(tool_call_id)}, {json.dumps(card_html)});"
        if self._ai_webview:
            self._ai_webview.run_javascript(js_code, None, None)
            req_id_to_use = req_id or (self._get_ai_request_id() if callable(self._get_ai_request_id) else 0)
            if req_id_to_use:
                self.render_current_assistant_message(req_id_to_use)
        return False

    def render_current_assistant_message(self, req_id: int):
        """Render the current assistant message for the given request ID."""
        conv_id = self._get_ai_conversation_id() if callable(self._get_ai_conversation_id) else None
        running_convs = self._get_running_convs() if callable(self._get_running_convs) else {}

        st = running_convs.get(conv_id) if conv_id else None
        if not st or not st.get("streaming", False):
            return

        current_req_id = self._get_ai_request_id() if callable(self._get_ai_request_id) else 0
        if st.get("req_id") != req_id:
            return

        msg_id = f"msg-{req_id}"
        messages = self._get_ai_messages() if callable(self._get_ai_messages) else []

        # Get turn messages (from last user msg onward)
        last_user_idx = -1
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "user":
                last_user_idx = idx
                break
        turn_msgs = messages[last_user_idx + 1:] if last_user_idx != -1 else messages

        if not st.get("response_div_added", False):
            js = f"appendMessageContainer('{msg_id}');"
            self._ai_webview.run_javascript(js, None, None)
            st["response_div_added"] = True

        show_details = self._get_show_tool_details() if callable(self._get_show_tool_details) else True
        output = render_turn(TurnRenderInput(
            turn_messages=turn_msgs,
            all_messages=messages,
            streaming_reasoning=st.get("current_reasoning_text", ""),
            streaming_content=st.get("current_assistant_text", ""),
            is_streaming=True,
            show_tool_details=show_details,
        ))
        js_update = build_update_js(msg_id, output)
        self._ai_webview.run_javascript(js_update, None, None)

    def finalize_streaming_render(self, ai_messages: List[Dict], ai_markdown_text: str,
                                   ai_request_id: int, ai_conversation_id: str,
                                   assistant_text: str, reasoning_text: str,
                                   show_tool_details: bool) -> str:
        """流结束时 flush 剩余 buffer，触发前端最终 HTML 渲染。

        Returns:
            rebuilt HTML string for caching.
        """
        # 0. 取消所有排期的 60ms 定时器
        if self._reasoning_flush_source_id:
            GLib.source_remove(self._reasoning_flush_source_id)
            self._reasoning_flush_source_id = 0
            self._reasoning_flush_scheduled = False
        if self._flush_source_id:
            GLib.source_remove(self._flush_source_id)
            self._flush_source_id = 0
            self._flush_scheduled = False

        # 1. flush 剩余 buffer
        if self._reasoning_buffer:
            js_code = f"_appendReasoningCacheOnly({json.dumps(self._reasoning_buffer)});"
            if self._ai_webview:
                self._ai_webview.run_javascript(js_code, None, None)
            self._reasoning_buffer = ""
        if self._token_buffer:
            self._flush_token_buffer()

        # 2. 构建最终 HTML
        msg_id = f"msg-{ai_request_id}"
        last_user_idx = -1
        for idx in range(len(ai_messages) - 1, -1, -1):
            if ai_messages[idx].get("role") == "user":
                last_user_idx = idx
                break
        turn_msgs = ai_messages[last_user_idx + 1:] if last_user_idx != -1 else ai_messages

        output = render_turn(TurnRenderInput(
            turn_messages=turn_msgs,
            all_messages=ai_messages,
            streaming_reasoning="",
            streaming_content=assistant_text,
            is_streaming=False,
            show_tool_details=show_tool_details,
        ))

        # 3. 使用 build_update_js + updateMessageContainer
        js_final = build_update_js(msg_id, output)
        if self._ai_webview:
            self._ai_webview.run_javascript(js_final, None, None)

        # 4. JS 同步
        start_idx = last_user_idx + 1
        js_sync = (
            f"finishReasoning();"
            f"window._isStreaming = false;"
            f"(function(){{"
            f"var m=document.getElementById('{msg_id}')?.querySelector('copy-marker');"
            f"if(m&&!m.dataset.msgIndex)m.dataset.msgIndex='{start_idx}';"
            f"addCopyButtons();"
            f"}})();"
            f"_scrollToBottom();"
            f"_throttledWindowing();"
            f"_initRoundNav();"
        )
        if self._ai_webview:
            self._ai_webview.run_javascript(js_sync, None, None)

        # 5. 清理与 HTML 缓存更新
        self._token_buffer = ""
        self._flush_scheduled = False
        self._flush_source_id = 0
        self._reasoning_buffer = ""
        self._reasoning_flush_scheduled = False
        self._reasoning_flush_source_id = 0
        self._streaming_container_created = False

        if ai_messages:
            rebuilt_md = _rebuild_markdown_from_messages(ai_messages, show_details=show_tool_details)
            self._last_rendered_html = _markdown_to_html_safe(rebuilt_md, fallback_content="")

        return self._last_rendered_html

    # ── WebView 导航策略 ─────────────────────────────────────────

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
                        messages = self._get_ai_messages() if callable(self._get_ai_messages) else []
                        if 0 <= index < len(messages) and messages[index].get("role") in ("assistant", "tool"):
                            turn_msgs = []
                            temp_idx = index
                            while temp_idx < len(messages) and messages[temp_idx].get("role") in ("assistant", "tool"):
                                turn_msgs.append(messages[temp_idx])
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
                                if self._on_copy_started_cb:
                                    self._on_copy_started_cb()
                                if self._on_copy_to_clipboard_cb:
                                    self._on_copy_to_clipboard_cb(content)
                                if self._on_copy_finished_cb:
                                    GLib.idle_add(self._on_copy_finished_cb)
                    except (ValueError, IndexError):
                        pass
                return True
            if uri.startswith("opencode://copy-input"):
                qs = parse_qs(urlparse(uri).query)
                index_str = qs.get("index", [None])[0]
                if index_str is not None:
                    try:
                        index = int(index_str)
                        messages = self._get_ai_messages() if callable(self._get_ai_messages) else []
                        if 0 <= index < len(messages) and messages[index].get("role") == "user":
                            content = messages[index].get("content", "")
                            if content:
                                if isinstance(content, list):
                                    text_parts = []
                                    for p in content:
                                        if isinstance(p, dict) and p.get("type") == "text":
                                            text_parts.append(p["text"])
                                    content = "\n".join(text_parts)
                                if content:
                                    if self._on_copy_started_cb:
                                        self._on_copy_started_cb()
                                    if self._on_copy_to_clipboard_cb:
                                        self._on_copy_to_clipboard_cb(content)
                                    if self._on_copy_finished_cb:
                                        GLib.idle_add(self._on_copy_finished_cb)
                    except (ValueError, IndexError):
                        pass
                return True
            if uri.startswith("opencode://retry"):
                qs = parse_qs(urlparse(uri).query)
                index_str = qs.get("index", [None])[0]
                if index_str is not None:
                    try:
                        # Delegate retry to parent panel via callback
                        if hasattr(self, '_on_retry_requested') and self._on_retry_requested:
                            self._on_retry_requested(int(index_str))
                    except (ValueError, IndexError):
                        pass
                return True
            if uri.startswith("opencode://rollback-round"):
                qs = parse_qs(urlparse(uri).query)
                round_str = qs.get("round", [None])[0]
                if round_str is not None:
                    try:
                        if hasattr(self, '_on_rollback_requested') and self._on_rollback_requested:
                            self._on_rollback_requested(int(round_str))
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

    # ── WebView 进程崩溃恢复 ─────────────────────────────────────

    def _on_webview_crashed(self, webview, event):
        """WebView 进程崩溃时自动重建。"""
        if self._webview_suspended:
            return
        print(f"[opencode-switcher] WebView process crashed, rebuilding...", flush=True)

        current_html = self._last_rendered_html or ""
        old_webview = self._ai_webview
        parent = old_webview.get_parent()

        # 复用已有的 web context
        self._ai_webview = WebKit2.WebView.new_with_context(self._ai_web_context)

        settings = self._ai_webview.get_settings()
        settings.enable_webgl = False
        settings.enable_html5_database = False
        settings.enable_html5_local_storage = False

        self._ai_webview.load_html(
            self.get_html_template(self._theme_name, current_html if current_html else ""),
            "file:///"
        )
        self._streaming_container_created = False

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

    # ── 挂起/恢复 ─────────────────────────────────────────────────

    def suspend(self, cache_html: str = ""):
        """挂起 WebView，终止 web 进程以释放资源。"""
        if cache_html:
            self._last_rendered_html = cache_html
        if not self._webview_suspended:
            self._webview_suspended = True
            if self._ai_webview:
                self._ai_webview.terminate_web_process()
            print("[AI] WebView suspended, web process terminated.", flush=True)

    def restore(self, theme_name: str = "", cached_html: str = ""):
        """恢复 WebView，重新加载 HTML。"""
        self._webview_suspended = False
        theme = theme_name or self._theme_name
        html_content = self.get_html_template(theme, cached_html or self._last_rendered_html or "")
        if self._ai_webview:
            self._ai_webview.load_html(html_content, "file:///")
        self._streaming_container_created = False
        print("[AI] WebView restored from suspension.", flush=True)

    @property
    def is_suspended(self) -> bool:
        return self._webview_suspended

    @property
    def webview(self):
        return self._ai_webview

    @property
    def last_rendered_html(self) -> str:
        return self._last_rendered_html

    @last_rendered_html.setter
    def last_rendered_html(self, value: str):
        self._last_rendered_html = value

    # ── 主题 ──────────────────────────────────────────────────────

    def apply_theme(self, theme_name: str, markdown_text: str = ""):
        """切换主题，重新加载 WebView。"""
        self._theme_name = theme_name
        pygments_css = self.get_pygments_css(theme_name)
        html_content = ""
        if markdown_text:
            html_content = _markdown_to_html_safe(markdown_text)
        html = get_html_template(theme_name, html_content, pygments_css)
        if self._ai_webview:
            self._ai_webview.load_html(html, "file:///")
