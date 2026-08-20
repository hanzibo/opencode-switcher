"""WebKit WebView controller, lifecycle, memory management and rendering mixin for AIChatPanel."""

import json
import re
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
try:
    gi.require_version("WebKit2", "4.1")
except ValueError:
    try:
        gi.require_version("WebKit2", "4.0")
    except ValueError:
        pass
from gi.repository import Gtk, Gdk, GLib, Gio, WebKit2

from stores.theme_config import get_ai_gtk_colors
from ai_text_utils import (
    _markdown_to_html_safe,
    _strip_ai_markup,
    _vision_content_to_text,
    _clean_history_title,
)
from ai_engine.render_pipeline import render_turn, TurnRenderInput, build_update_js
from ai_engine.ai_html_template import get_html_template, _get_pygments_css
from .constants import (
    _webview_shell_fingerprint,
    _should_full_reload_webview,
    _AI_HEADER_TITLE,
)


class WebViewMixin:
    """WebKit WebView 生命周期、内存控制、JS 通信与 Markdown 渲染 Mixin。"""

    def _get_gtk_colors(self, theme_name: str) -> dict:
        """Return dict with 'bg', 'header_bg', 'input_bg' as Gdk.RGBA."""
        raw = get_ai_gtk_colors(theme_name)
        return {k: Gdk.RGBA(*v) for k, v in raw.items()}

    def _apply_webview_gtk_background(self):
        """Paint the WebView widget with the opaque theme background."""
        try:
            webview = getattr(self, "_ai_webview", None)
            if webview is None:
                return
            c = self._get_gtk_colors(self._theme)
            webview.override_background_color(Gtk.StateFlags.NORMAL, c["bg"])
        except Exception as e:
            print(f"[opencode-switcher] Failed to set WebView GdkWindow background: {e}", flush=True)

    def _get_pygments_css(self, theme: str) -> str:
        return _get_pygments_css(theme, getattr(self, "_pygments_css_cache", None))

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
        active_st = self._ai_running_convs.get(self._ai_conversation_id) if getattr(self, "_ai_conversation_id", None) else None
        if active_st:
            active_st["response_div_added"] = False
        self._reseed_reasoning_on_container = True

    def _rebind_active_stream(self, st: dict) -> None:
        """A→B→A 切回后重新绑定当前可见的流式会话。"""
        self._streaming_container_created = False
        self._ai_response_div_added = False
        st["response_div_added"] = False
        self._reseed_reasoning_on_container = True
        req_id = st.get("req_id")
        if req_id is not None:
            GLib.idle_add(self._render_current_assistant_message, req_id)

    def _load_webview_html(self, initial_html: str = "", *, force: bool = False) -> None:
        """将 ``initial_html`` 装载进 WebView。"""
        fingerprint = _webview_shell_fingerprint(self._theme, self._get_pygments_css(self._theme))
        if not force and not _should_full_reload_webview(
                getattr(self, "_loaded_shell_fingerprint", None), fingerprint,
                self._webview_dom_live(),
                getattr(self, "_webview_suspended", False),
                getattr(self, "_webview_ready", False)):
            js_code = f"updateContent({json.dumps(initial_html)});"
            self._ai_webview.run_javascript(js_code, None, None)
            self._reset_streaming_dom_state()
            return
        if getattr(self, "_webview_suspended", False):
            self._webview_suspended = False
        self._loaded_shell_fingerprint = fingerprint
        self._webview_ready = False
        self._ai_webview.load_html(self.get_html_template(self._theme, initial_html), "file:///")
        self._reset_streaming_dom_state()

    def _on_webview_load_changed(self, webview, event):
        """跟踪文档装载状态：FINISHED → 就绪；其余事件（PROVISIONAL/COMMITTED/失败）→ 装载中。"""
        if event == WebKit2.LoadEvent.FINISHED:
            self._webview_ready = True
            lbl = getattr(self, "_ai_lbl", None)
            if lbl is not None and getattr(lbl, "_pending_header_reapply", False):
                lbl.set_markup(lbl._ai_last_header_markup)
                lbl._pending_header_reapply = False
        else:
            self._webview_ready = False

    def _render_markdown(self, text: str):
        if not text:
            js_code = "updateContent('');"
            if hasattr(self, "_ai_webview") and self._ai_webview:
                self._ai_webview.run_javascript(js_code, None, None)
            return

        fallback_msg = (
            "<p style='color: #f43f5e; font-weight: bold;'>❌ [错误] 缺少运行时依赖库。</p>"
            "<p>请在终端中运行以下命令安装所需依赖，并重启服务：</p>"
            "<pre><code>~/.local/share/opencode-switcher/venv/bin/pip install markdown pygments</code></pre>"
            f"<hr><pre><code>{text}</code></pre>"
        )
        rendered_html = _markdown_to_html_safe(text, fallback_content=fallback_msg)
        self._last_rendered_html = rendered_html
        if getattr(self, "_ai_conversation_id", None):
            self._ai_html_cache[self._ai_conversation_id] = rendered_html
        
        js_code = f"updateContent({json.dumps(rendered_html)});"
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(js_code, None, None)

    def append_html_to_webview(self, html_snippet: str):
        """Insert HTML snippet before end of content div and scroll to bottom."""
        escaped = json.dumps(html_snippet)
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(
                f"document.getElementById('content').insertAdjacentHTML('beforeend', {escaped});"
                f"_wrapTables(document.getElementById('content'));"
                f"_scrollToBottom();",
                None, None
            )

    def _get_turn_messages(self) -> List[Dict]:
        """Get messages for the current active turn (from last user msg onward)."""
        last_user_idx = -1
        messages = getattr(self, "_ai_messages", [])
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "user":
                last_user_idx = idx
                break
        return messages[last_user_idx + 1:] if last_user_idx != -1 else messages

    def _turn_has_tool_phase(self, turn_msgs: List[Dict]) -> bool:
        """当前轮是否已进入工具阶段（未解决的 tool_calls 或已有 tool 结果消息）。"""
        for msg in turn_msgs:
            if msg.get("role") == "tool":
                return True
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                return True
        return False

    def _render_current_assistant_message(self, req_id: int):
        """Render the current assistant message for the given request ID."""
        conv_id = None
        for cid, st in list(getattr(self, "_ai_running_convs", {}).items()):
            if st.get("req_id") == req_id:
                conv_id = cid
                break
        if not conv_id or getattr(self, "_ai_conversation_id", None) != conv_id:
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
            all_messages=getattr(self, "_ai_messages", []),
            streaming_reasoning=st.get("current_reasoning_text", ""),
            streaming_content=st.get("current_assistant_text", ""),
            is_streaming=True,
            show_tool_details=getattr(self, "_show_tool_details", True),
        ))
        js_update = build_update_js(msg_id, output)
        self._ai_webview.run_javascript(js_update, None, None)

    def _history_summaries_json(self) -> str:
        """生成 WebView header 历史下拉的 JSON 列表（对齐原 HistoryPopover 格式）。"""
        try:
            items = []
            for s in self._get_sorted_conversations():
                sid = s.get("id", "")
                raw_title = s.get("title", "(untitled)")
                title = _clean_history_title(raw_title)
                if len(title) > 25:
                    title = title[:22] + "..."
                count = s.get("message_count", 0)
                items.append({
                    "id": sid,
                    "label": f"{title} ({count}条)",
                    "ts": s.get("updated_at", 0),
                })
            return json.dumps(items, ensure_ascii=False)
        except Exception:
            return "[]"

    def _on_webview_crashed(self, webview, event):
        """WebView 进程崩溃时自动重建。"""
        if getattr(self, "_webview_suspended", False):
            return
        print(f"[opencode-switcher] WebView process crashed, rebuilding...", flush=True)

        current_html = getattr(self, "_last_rendered_html", "") or ""
        old_webview = self._ai_webview
        parent = old_webview.get_parent()

        self._ai_webview = WebKit2.WebView.new_with_context(self._ai_web_context)

        settings = self._ai_webview.get_settings()
        settings.enable_webgl = False
        settings.enable_html5_database = False
        settings.enable_html5_local_storage = False

        self._ai_webview.connect("realize", lambda *_: self._apply_webview_gtk_background())
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

    def _suspend_webview_cb(self) -> bool:
        """定时器回调：空闲 60s 后终止 WebProcess 节省内存。"""
        if not getattr(self, "_ai_has_shown", False):
            self._suspend_timeout_id = 0
            return False

        running_states = list(getattr(self, "_ai_running_convs", {}).values())
        any_running = any(st.get("streaming", False) for st in running_states)
        if any_running:
            print(f"[AI] suspend deferred: {sum(1 for st in running_states if st.get('streaming'))} convs still streaming", flush=True)
            return True

        self._suspend_timeout_id = 0

        if not getattr(self, "_webview_suspended", False):
            if getattr(self, "_ai_conversation_id", None):
                self._ai_html_cache[self._ai_conversation_id] = getattr(self, "_last_rendered_html", "")
            
            self._webview_suspended = True
            if hasattr(self, "_ai_webview") and self._ai_webview:
                self._ai_webview.terminate_web_process()
            print("[AI] WebView suspended, web process terminated.", flush=True)
            
        return False

    def _on_decide_policy(self, webview, decision, decision_type):
        """处理 WebView 导航策略：opencode:// URI 和外部链接。"""
        if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            nav_action = decision.get_navigation_action()
            uri = nav_action.get_request().get_uri()
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
                        msgs = getattr(self, "_ai_messages", [])
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
                                if getattr(self, "on_ai_copy_started", None):
                                    self.on_ai_copy_started()
                                self._copy_to_clipboard(content)
                                if getattr(self, "on_ai_copy_finished", None):
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
                        msgs = getattr(self, "_ai_messages", [])
                        if 0 <= index < len(msgs) and msgs[index].get("role") == "user":
                            content = msgs[index].get("content", "")
                            if content:
                                if isinstance(content, list):
                                    content = _vision_content_to_text(content)
                                if content:
                                    if getattr(self, "on_ai_copy_started", None):
                                        self.on_ai_copy_started()
                                    self._copy_to_clipboard(content)
                                    if getattr(self, "on_ai_copy_finished", None):
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
            if uri.startswith("opencode://close-panel"):
                self.set_no_show_all(True)
                self.hide()
                if getattr(self, "separator", None) is not None:
                    self.separator.set_no_show_all(True)
                    self.separator.hide()
                self._ai_panel_visible_saved = False
                self.queue_resize()
                return True
            if uri.startswith("opencode://history-open"):
                if getattr(self, "_ai_history_popover", None) is not None:
                    self._ai_history_popover.refresh_dropdown()
                try:
                    if getattr(self, "_ai_webview", None) is not None:
                        self._ai_webview.run_javascript("showHistoryDropdown();", None, None)
                except Exception:
                    pass
                return True
            if uri.startswith("opencode://history-close"):
                GLib.idle_add(self._safe_grab_ai_entry_focus)
                return True
            if uri.startswith("opencode://history-select"):
                qs = parse_qs(urlparse(uri).query)
                cid = qs.get("id", [None])[0]
                if cid is not None:
                    self._switch_to_conversation(cid)
                    if getattr(self, "_ai_history_popover", None) is not None:
                        self._ai_history_popover.popdown()
                return True
            if uri.startswith("opencode://history-delete-multi"):
                qs = parse_qs(urlparse(uri).query)
                ids_str = qs.get("ids", [None])[0]
                if ids_str:
                    for cid in ids_str.split(","):
                        if cid:
                            self._delete_conversation_cleanup(cid)
                            if cid == getattr(self, "_ai_conversation_id", None):
                                self._reset_ai_panel_silent()
                    if getattr(self, "_ai_history_popover", None) is not None:
                        self._ai_history_popover.refresh_dropdown()
                return True
            if uri.startswith("opencode://history-delete"):
                qs = parse_qs(urlparse(uri).query)
                cid = qs.get("id", [None])[0]
                if cid is not None:
                    self._delete_conversation_cleanup(cid)
                    if cid == getattr(self, "_ai_conversation_id", None):
                        self._reset_ai_panel_silent()
                    if getattr(self, "_ai_history_popover", None) is not None:
                        self._ai_history_popover.refresh_dropdown()
                return True
            if uri.startswith("opencode://history-edit"):
                return True
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
            except Exception as e:
                print(f"Error launching external link {uri}: {e}", flush=True)
            return True
        return False
