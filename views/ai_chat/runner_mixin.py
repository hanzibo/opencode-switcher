"""LLM ReAct loop execution, streaming runner, watchdog and context summary mixin for AIChatPanel."""

import json
import time
import re
import html
import threading
from copy import deepcopy
from typing import Optional, List, Dict, Any

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib

from ai_engine.llm_client import LLMRequestConfig, _LLMHttpError
from ai_engine.ai_tool_loop import run_llm_react_loop, ToolLoopContext
from system.event_types import StreamEventType
from stores.clipboard_store import (
    Conversation,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TOP_P,
    _DEFAULT_SUMMARY_TEMPLATE,
    AISettingsStore,
)
from ai_text_utils import _markdown_to_html_safe, _extract_local_title
from ai_engine.render_pipeline import render_turn, TurnRenderInput, build_update_js
from tool_registry.notification import execute_send_notification
from .constants import (
    _ai_stream_request_key,
    _ai_summary_request_key,
    _to_chat_messages,
    _AI_HEADER_TITLE,
)


class RunnerMixin:
    """LLM ReAct 执行循环、后台线程管理、流式收尾、看门狗与上下文自动压缩 Mixin。"""

    def _run_llm_api_request(self, base_url: str, api_key: str, model_name: str, messages: list,
                              req_id: int, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS,
                              top_p: float = DEFAULT_TOP_P, markdown_text: str = "", conv_id: str = "",
                              extra_system_messages: Optional[list] = None,
                              thinking_enabled: bool = False, reasoning_effort: str = "high"):
        """Start the ReAct loop by delegating execution to the run_llm_react_loop orchestrator."""
        cancel_event = threading.Event()
        request_key = _ai_stream_request_key(conv_id, req_id)

        state = {
            "streaming": True,
            "messages": deepcopy(messages),
            "cancel_event": cancel_event,
            "current_assistant_text": "",
            "current_reasoning_text": "",
            "response_div_added": False,
            "ai_markdown_text": markdown_text,
            "req_id": req_id,
            "request_key": request_key,
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
            if getattr(self, "_ai_conversation_id", None) == conv_id:
                self._ai_assistant_buffer = ""
                self._ai_current_assistant_text = ""
                self._ai_response_div_added = False
                self._ai_assistant_html_base = ""
                self._ai_current_reasoning_text = ""
                self._token_buffer = ""
                self._flush_scheduled = False
                self._reasoning_buffer = ""
                self._reasoning_flush_scheduled = False

        def append_message_callback(msg):
            msg_copy = deepcopy(msg) if isinstance(msg, dict) else msg
            st = self._ai_running_convs.get(conv_id)
            if st:
                st["messages"].append(msg_copy)
            if getattr(self, "_ai_conversation_id", None) == conv_id:
                if st:
                    self._ai_messages = st["messages"]
                else:
                    self._ai_messages.append(msg_copy)
                    if msg.get("role") == "tool" and getattr(self, "_ai_streaming", None) is False:
                        GLib.idle_add(self._re_render_after_tool_cancel)

                enable_inc = (getattr(self, "_ai_settings_store", None)
                              and self._ai_settings_store.enable_incremental_tools)
                is_active_stream = (req_id == getattr(self, "_ai_request_id", 0))
                dom_ready = (getattr(self, "_streaming_container_created", False)
                             and (st.get("response_div_added", False) if st else False))
                if msg.get("role") == "tool" and enable_inc and is_active_stream and dom_ready:
                    pass
                else:
                    GLib.idle_add(self._render_current_assistant_message, req_id)

        def set_reasoning_callback(text):
            st = self._ai_running_convs.get(conv_id)
            if st:
                st["current_reasoning_text"] = text
            if getattr(self, "_ai_conversation_id", None) == conv_id:
                self._ai_current_reasoning_text = text

        def set_assistant_callback(text):
            st = self._ai_running_convs.get(conv_id)
            if st:
                st["current_assistant_text"] = text
            if getattr(self, "_ai_conversation_id", None) == conv_id:
                self._ai_current_assistant_text = text

        def append_html_callback(html_snippet):
            if getattr(self, "_ai_conversation_id", None) == conv_id:
                GLib.idle_add(self.append_html_to_webview, html_snippet)

        def on_llm_error_fn(reason):
            GLib.idle_add(self._render_llm_error, conv_id, reason)

        def on_token_delta_fn(text):
            if getattr(self, "_ai_conversation_id", None) == conv_id:
                GLib.idle_add(self._on_token_delta, text)

        def on_reasoning_delta_fn(text):
            if getattr(self, "_ai_conversation_id", None) == conv_id:
                GLib.idle_add(self._on_reasoning_delta, text)

        def on_tool_result_fn(tool_call_id: str, result_text: str, status: str):
            if getattr(self, "_ai_conversation_id", None) == conv_id:
                GLib.idle_add(self._on_tool_result, tool_call_id, result_text, status, req_id)

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
        """更新当前会话的 Markdown 和 HTML 缓存。"""
        self._ai_markdown_text = self._rebuild_markdown_from_messages(getattr(self, "_ai_messages", []))
        self._last_rendered_html = _markdown_to_html_safe(self._ai_markdown_text, fallback_content="")
        if getattr(self, "_ai_conversation_id", None):
            self._ai_html_cache[self._ai_conversation_id] = self._last_rendered_html

    def _finalize_after_tool_loop(self, req_id: int):
        """Finalize after tool loop ends (used when tool iteration limit hit)."""
        conv_id = None
        for cid, st in list(getattr(self, "_ai_running_convs", {}).items()):
            if st.get("req_id") == req_id:
                conv_id = cid
                break

        if not conv_id:
            conv_id = getattr(self, "_ai_conversation_id", None)
            state = self._ai_running_convs.get(conv_id) if conv_id else None
        else:
            state = self._ai_running_convs.get(conv_id)

        if state is None or state.get("req_id") != req_id:
            return

        if state:
            state["streaming"] = False

        if getattr(self, "_ai_conversation_id", None) == conv_id:
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
            self._ai_ask_user_state = None
            GLib.idle_add(self._safe_grab_ai_entry_focus)
            GLib.idle_add(self._update_send_button, True)
            return "[询问用户超时：用户未在 5 分钟内回答]"

        state = getattr(self, "_ai_ask_user_state", None)
        answer = state.get("answer", "") if state else ""
        self._ai_ask_user_state = None
        GLib.idle_add(self._safe_grab_ai_entry_focus)

        if not answer:
            return "[用户取消了回答]"
        return answer

    def _enable_ask_user_entry(self):
        self._ai_entry.placeholder_text = "请输入回答..."
        self._ai_send_btn.set_label("发送")
        self._ai_send_btn.set_sensitive(True)
        self._safe_grab_ai_entry_focus()

    def _render_background_conversation(self, conv_id: str, target_messages: list, state):
        """渲染背景对话（非当前可见），只更新 cache 不操作 WebView。"""
        if conv_id in getattr(self, "_deleted_conversation_ids", set()):
            return
        output = render_turn(TurnRenderInput(
            turn_messages=target_messages,
            all_messages=target_messages,
            is_streaming=False,
            show_tool_details=getattr(self, "_show_tool_details", True),
        ))

        rebuilt_markdown = self._rebuild_markdown_from_messages(target_messages)
        rendered_html = _markdown_to_html_safe(rebuilt_markdown, fallback_content="")
        self._ai_html_cache[conv_id] = rendered_html
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
        """流结束时 flush 剩余 buffer，触发前端最终 HTML 渲染（仅当前可见对话）。"""
        if getattr(self, "_reasoning_flush_source_id", 0):
            GLib.source_remove(self._reasoning_flush_source_id)
            self._reasoning_flush_source_id = 0
            self._reasoning_flush_scheduled = False
        if getattr(self, "_flush_source_id", 0):
            GLib.source_remove(self._flush_source_id)
            self._flush_source_id = 0
            self._flush_scheduled = False

        if getattr(self, "_reasoning_buffer", ""):
            js_code = f"_appendReasoningCacheOnly({json.dumps(self._reasoning_buffer)});"
            if hasattr(self, "_ai_webview") and self._ai_webview:
                self._ai_webview.run_javascript(js_code, None, None)
            self._reasoning_buffer = ""
        if getattr(self, "_token_buffer", ""):
            self._flush_token_buffer(req_id)

        if req_id is None:
            req_id = getattr(self, "_ai_request_id", 0)
        msg_id = f"msg-{req_id}"
        turn_msgs = self._get_turn_messages()
        output = render_turn(TurnRenderInput(
            turn_messages=turn_msgs,
            all_messages=getattr(self, "_ai_messages", []),
            streaming_reasoning="",
            streaming_content=getattr(self, "_ai_current_assistant_text", ""),
            is_streaming=False,
            show_tool_details=getattr(self, "_show_tool_details", True),
        ))

        js_final = (
            f"window._isStreaming = false;"
            f"{build_update_js(msg_id, output)}"
        )
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript(js_final, None, None)

        last_user_idx = -1
        messages = getattr(self, "_ai_messages", [])
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "user":
                last_user_idx = idx
                break
        start_idx = last_user_idx + 1
        self._append_assistant_turn_to_cache()

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

        self._token_buffer = ""
        self._flush_scheduled = False
        self._flush_source_id = 0
        self._reasoning_buffer = ""
        self._reasoning_flush_scheduled = False
        self._reasoning_flush_source_id = 0
        self._streaming_container_created = False

    def _handle_stream_end(self, req_id: int, conv_id: Optional[str] = None):
        """Common cleanup after a conversation turn ends (save, prune, title gen)."""
        if conv_id in getattr(self, "_deleted_conversation_ids", set()):
            return
        if conv_id is None:
            for cid, st in list(getattr(self, "_ai_running_convs", {}).items()):
                if st.get("req_id") == req_id:
                    conv_id = cid
                    break
        if conv_id is not None and conv_id != getattr(self, "_ai_conversation_id", None):
            return
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
                    getattr(self, "_ai_last_prompt_obj", None),
                    getattr(self, "_ai_active_model_info", None)
                )
            if (not getattr(self, "_ai_title_generated", False)
                    and getattr(self, "_ai_conversation_id", None)
                    and getattr(self, "_ai_messages", [])
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
            if getattr(self, "_ai_history_popover", None) is not None:
                self._ai_history_popover.refresh_dropdown()
        except Exception as e:
            print(f"Dropdown refresh error: {e}", flush=True)
        self._update_token_display()

    def _on_llm_api_finished(self, req_id: int):
        """Called when LLM stream completes with a pure text response (no tool_calls)."""
        conv_id = None
        for cid, st in list(getattr(self, "_ai_running_convs", {}).items()):
            if st.get("req_id") == req_id:
                conv_id = cid
                break

        if not conv_id:
            conv_id = getattr(self, "_ai_conversation_id", None)
            state = self._ai_running_convs.get(conv_id) if conv_id else None
        else:
            state = self._ai_running_convs.get(conv_id)

        if state is None or state.get("req_id") != req_id:
            return

        assistant_text = state["current_assistant_text"] if state else getattr(self, "_ai_current_assistant_text", "")
        reasoning = state["current_reasoning_text"] if state else getattr(self, "_ai_current_reasoning_text", "")
        assistant_msg = {"role": "assistant", "content": assistant_text}
        if reasoning:
            assistant_msg["reasoning_content"] = reasoning

        target_messages = state["messages"] if state else getattr(self, "_ai_messages", [])
        if target_messages and (assistant_text or reasoning):
            target_messages.append(assistant_msg)

        if state:
            state["current_assistant_text"] = ""
            state["current_reasoning_text"] = ""
            state["response_div_added"] = False
            state["streaming"] = False

        if getattr(self, "_ai_conversation_id", None) == conv_id:
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
            if state:
                target_messages = state["messages"]
                self._render_background_conversation(conv_id, target_messages, state)

        was_cancelling = getattr(self, "_ai_cancelling", False)
        self._ai_running_convs.pop(conv_id, None)
        self._ai_cancelling = False

        if getattr(self, "_ai_cancel_watchdog_id", 0) != 0:
            GLib.source_remove(self._ai_cancel_watchdog_id)
            self._ai_cancel_watchdog_id = 0

            model_info = getattr(self, "_ai_active_model_info", None)
            _, _, _, display_name, _, _, _, _, _ = self._read_model_config(None, model_info)
            if hasattr(self, "_ai_lbl") and self._ai_lbl:
                self._ai_lbl.set_markup(f"<b>{_AI_HEADER_TITLE}</b>\n<span size='small' foreground='#888888'>({display_name})</span>")

        error_pending = getattr(self, "_ai_error_pending_conv", None)
        if error_pending == conv_id:
            self._ai_error_pending_conv = None

        if (getattr(self, "_ai_conversation_id", None) == conv_id
                and not was_cancelling
                and (assistant_text or reasoning)
                and error_pending != conv_id
                and getattr(self._ai_settings_store, "enable_answer_notification", True)):
            self._notify_ai_answer_finished(assistant_text or reasoning)

        self._handle_stream_end(req_id, conv_id)

    def _notify_ai_answer_finished(self, answer_text: str) -> None:
        """主对话 AI 正式回答结束后弹桌面通知（best-effort，后台线程不阻塞 UI）。"""
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

    def _cancel_streams_for_conversation(self, conv_id: str) -> bool:
        """定向取消某个会话的活跃流（主 ReAct 流 + 摘要流），不触碰其他会话。"""
        st = getattr(self, "_ai_running_convs", {}).get(conv_id)
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
        if getattr(self, "_ai_streaming", False):
            active_state = getattr(self, "_ai_running_convs", {}).get(self._ai_conversation_id)
            if active_state and active_state.get("cancel_event"):
                self._cancel_streams_for_conversation(self._ai_conversation_id)
                active_state["current_assistant_text"] = ""
                active_state["current_reasoning_text"] = ""
                self._ai_current_assistant_text = ""
                self._ai_current_reasoning_text = ""
            else:
                for st in list(getattr(self, "_ai_running_convs", {}).values()):
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
        if not getattr(self, "_ai_cancelling", False):
            return False
        self._ai_cancelling = False
        self._ai_running_convs.pop(self._ai_conversation_id, None)
        self._ai_streaming = False
        if self._sanitize_tool_calls_schema(getattr(self, "_ai_messages", [])):
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
            self._ai_lbl.set_markup(f"<b>{_AI_HEADER_TITLE}</b>\n<span size='small' foreground='#888888'>({display_name})</span>")
        print("[cancel] 看门狗触发：强制清理取消状态", flush=True)
        return False

    def _re_render_after_tool_cancel(self):
        """Re-render and save conversation after tool result appended post-cancel."""
        if getattr(self, "_ai_streaming", False):
            return
        self._ai_markdown_text = self._rebuild_markdown_from_messages(getattr(self, "_ai_messages", []))
        self._render_markdown(self._ai_markdown_text)
        try:
            self._save_current_conversation(self._build_model_snapshot())
        except Exception:
            pass

    def _render_llm_error(self, conv_id: str, reason: str):
        """主线程：在 WebView 中渲染 LLM 请求失败/超时错误气泡。"""
        if getattr(self, "_ai_conversation_id", None) != conv_id:
            return
        if getattr(self, "_ai_cancelling", False):
            return
        self._ai_error_pending_conv = conv_id
        safe = html.escape(reason)
        self.append_html_to_webview(
            '<div class="chat-system-error">❌ ' + safe + '</div>'
        )

    def _prune_messages(self, defer_render: bool = False):
        """按 soft_limit 裁剪超长会话；``defer_render=True`` 时仅裁剪不渲染（异步路径）。"""
        if getattr(self, "_ai_settings_store", None) is not None:
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

        if len(getattr(self, "_ai_messages", [])) <= soft_limit:
            return

        if enable_summary and not getattr(self, "_ai_summary_generating", False):
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
        """根据 trim_target 从当前 _ai_messages 重新计算裁剪位置。"""
        if len(getattr(self, "_ai_messages", [])) <= 1:
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
        if defer_render:
            self._clear_summary_status()
            return
        self._ai_markdown_text = self._rebuild_markdown_from_messages(self._ai_messages)
        self._render_markdown(self._ai_markdown_text)
        if save_summary:
            self._save_summary_to_conversation()
            try:
                self._save_current_conversation(self._build_model_snapshot(),
                                                preserve_updated_at=True)
            except Exception as e:
                print(f"[prune] 保存裁剪后对话失败: {e}", flush=True)
            try:
                if getattr(self, "_ai_history_popover", None) is not None:
                    self._ai_history_popover.refresh_dropdown()
            except Exception as e:
                print(f"[prune] 刷新历史下拉框失败: {e}", flush=True)
        self._clear_summary_status()
        self._update_token_display()

    def _generate_summary_async(self, pruned_messages: list, trim_target: int):
        """在后台线程中调用 LLM（流式），将即将丢弃的消息压缩为摘要并实时显示。"""
        save_summary = False
        cancel_event = threading.Event()
        summary_key = _ai_summary_request_key(getattr(self, "_ai_conversation_id", ""))
        idle_timeout_sec = 120
        total_timeout_sec = 120
        failure_reason = None
        has_received_token = False

        def _cancel_summary_stream():
            cancel_event.set()
            self._llm_client.cancel_active_request(summary_key)

        total_timer = threading.Timer(total_timeout_sec, _cancel_summary_stream)
        total_timer.daemon = True
        total_timer.start()

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
            prev_summary = f"已有摘要：\n{self._ai_summary}\n\n" if getattr(self, "_ai_summary", None) else ""
            template = (self._ai_settings_store.summary_prompt_template
                        if self._ai_settings_store else _DEFAULT_SUMMARY_TEMPLATE)
            try:
                prompt = template.format(
                    prev_summary=prev_summary,
                    conversation_text=convo_text,
                    max_chars=max_chars,
                )
            except (KeyError, ValueError) as e:
                print(f"[summary] 模板格式错误: {e}", flush=True)
                failure_reason = f"模板格式错误：{e}"
                return

            base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = \
                self._read_model_config(getattr(self, "_ai_last_prompt_obj", None),
                                        getattr(self, "_ai_active_model_info", None))

            if not base_url or not api_key or not model_name:
                print(f"[summary] 模型配置不完整，跳过摘要生成", flush=True)
                failure_reason = "当前模型配置不完整（缺少 Base URL / API Key / Model Name）"
                return

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
                        _reset_idle_timer()
                        total_timer.cancel()
                    else:
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
                    if getattr(self, "_ai_summary", None):
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

            if failure_reason:
                GLib.idle_add(self._show_summary_failure, failure_reason)
            else:
                GLib.idle_add(self._apply_prune, trim_target, save_summary)

    def _show_summary_failure(self, reason: str):
        """在对话中插入一条系统消息说明摘要生成失败的原因。"""
        self._clear_summary_status()
        html_content = (
            '<div class="chat-message system-message" style="margin:8px 0;padding:8px 12px;'
            'background:var(--notice-bg,#fff3cd);border-left:4px solid var(--notice-border,#ffc107);'
            'border-radius:4px;font-size:13px;color:var(--notice-text,#856404);">'
            '⚠️ <b>上下文压缩失败</b><br>'
            f'{reason}'
            '</div>'
        )
        self.append_html_to_webview(html_content)

    def _save_summary_to_conversation(self):
        """在主线程中仅保存摘要到对话文件，不重建消息列表。"""
        try:
            if not getattr(self, "_ai_conversation_id", None):
                return
            conv = self._conversation_store.load_conversation(self._ai_conversation_id)
            if conv:
                conv.summary = getattr(self, "_ai_summary", "")
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
        if getattr(self, "_ai_history_popover", None) is not None:
            self._ai_history_popover.refresh_dropdown()
        if getattr(self, "_ai_pending_title_notification", False):
            self._ai_pending_title_notification = False
            escaped = html.escape(title)
            self.append_html_to_webview(
                f'<div class="chat-simple-info">标题已生成: {escaped}</div>'
            )
