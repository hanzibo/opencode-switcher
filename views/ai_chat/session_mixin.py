"""Session state management, conversation branching, switching, rollback, and commands mixin for AIChatPanel."""

import os
import json
import time
import re
import html
import threading
from uuid import uuid4
from typing import Optional, List, Dict, Any, Tuple

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

from stores.clipboard_store import (
    Conversation,
    CustomPrompt,
    AISettingsStore,
    _DEFAULT_POLISH_TEMPLATE,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TOP_P,
)
from ai_text_utils import (
    _dict_to_chat_message,
    _rebuild_markdown_from_messages,
    _extract_local_title,
    _close_unclosed_code_blocks,
    _preserve_newlines,
    _resolve_vision_image_src,
    _vision_content_to_text,
    _markdown_to_html_safe,
    USER_AVATAR_HTML,
)
from .constants import _AI_HEADER_TITLE


class SessionMixin:
    """会话生命周期管理、模型快照、历史切换、分支 (Fork)、回滚与 Slash 指令处理 Mixin。"""

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

    def _snapshot_system_prompt(self) -> None:
        """从 Settings 快照系统提示词（仅新会话入口调用；旧对话由 _switch_to_conversation 加载自身快照）。"""
        self._ai_system_prompt = AISettingsStore().system_prompt

    def _start_new_conversation(self, prompt_text: str):
        self._ai_messages = [{"role": "user", "content": prompt_text}]
        self._snapshot_system_prompt()
        self._ai_conversation_id = uuid4().hex[:12]
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
        self._last_rendered_html = user_html
        self._load_webview_html(user_html)

    def _build_llm_messages(self) -> tuple:
        """构建发送给 LLM 的消息列表和额外 system 消息。"""
        extra = []
        if getattr(self, "_ai_system_prompt", None):
            extra.append({
                "role": "system",
                "content": self._ai_system_prompt
            })
        if getattr(self, "_ai_summary", None):
            extra.append({
                "role": "system",
                "content": f"【历史摘要】\n{self._ai_summary}"
            })
        return list(getattr(self, "_ai_messages", [])), extra

    def _send_user_message(self, text: str):
        self._sanitize_tool_calls_schema(self._ai_messages)
        self._init_streaming_state()
        self._init_mcp()
        if not self._ai_messages:
            self._snapshot_system_prompt()
        if getattr(self, "_ai_pending_image_hash", None):
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
        if hasattr(self, "_ai_webview") and self._ai_webview:
            self._ai_webview.run_javascript("_autoScroll = true;", None, None)
        self._render_markdown(self._ai_markdown_text)

        self._ai_spinner.show()
        self._ai_spinner.start()

        base_url, api_key, model_name, _, temperature, max_tokens, top_p, thinking_enabled, reasoning_effort = self._read_model_config(
            getattr(self, "_ai_last_prompt_obj", None),
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
        if getattr(self, "_ai_streaming", False):
            active_state = getattr(self, "_ai_running_convs", {}).get(self._ai_conversation_id)
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

        user_index = assistant_index
        while user_index >= 0 and msgs[user_index].get("role") != "user":
            user_index -= 1

        if user_index < 0:
            return

        self._ai_messages = msgs[:user_index + 1]

        self._ai_markdown_text = self._rebuild_markdown_from_messages(self._ai_messages)
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
            getattr(self, "_ai_last_prompt_obj", None),
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
        self._ai_has_shown = True
        self._init_mcp()

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
        self._ai_lbl.set_markup(f"<b>{_AI_HEADER_TITLE}</b>\n<span size='small' foreground='#888888'>({display_name})</span>")

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
            html_text = _markdown_to_html_safe(
                error_msg,
                fallback_content=f"<p style='color: #f43f5e; font-weight: bold;'>{error_msg}</p>"
            )
            self._last_rendered_html = html_text
            self._load_webview_html(html_text)
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

    def _build_model_snapshot(self) -> Dict[str, Any]:
        """Build a model_config_snapshot from active model info or resolved config."""
        active = getattr(self, "_ai_active_model_info", None)
        if active:
            return dict(active)
        base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = self._read_model_config(
            getattr(self, "_ai_last_prompt_obj", None), None
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
        if getattr(self, "_ai_conversation_id", None) in getattr(self, "_deleted_conversation_ids", set()):
            return
        local_title = "New Conversation"
        if getattr(self, "_ai_messages", []):
            local_title = _extract_local_title(
                self._ai_messages[0].get("content", "")
            )

        if not getattr(self, "_ai_conversation_id", None):
            now = int(time.time() * 1000)
            self._ai_conversation_created_at = now
            conv = self._conversation_store.create_conversation(
                title=local_title,
                system_prompt=getattr(self, "_ai_system_prompt", ""),
                model_config=model_snapshot
            )
            self._ai_conversation_id = conv.id
            conv.messages = [_dict_to_chat_message(m) for m in self._ai_messages]
            conv.summary = getattr(self, "_ai_summary", "")
            self._conversation_store.save_conversation(conv, bump_updated_at=not preserve_updated_at)
        else:
            conv = self._conversation_store.load_conversation(self._ai_conversation_id)
            if conv:
                conv.messages = [_dict_to_chat_message(m) for m in self._ai_messages]
                conv.model_config_snapshot = model_snapshot
                conv.system_prompt = getattr(self, "_ai_system_prompt", "")
                if not getattr(self, "_ai_summary_generating", False):
                    conv.summary = getattr(self, "_ai_summary", "")
            else:
                if not getattr(self, "_ai_conversation_created_at", None):
                    self._ai_conversation_created_at = int(time.time() * 1000)
                conv = Conversation(
                    id=self._ai_conversation_id,
                    title=local_title,
                    system_prompt=getattr(self, "_ai_system_prompt", ""),
                    messages=[_dict_to_chat_message(m) for m in self._ai_messages],
                    model_config_snapshot=model_snapshot,
                    created_at=self._ai_conversation_created_at,
                    updated_at=int(time.time() * 1000),
                )
            self._conversation_store.save_conversation(conv, bump_updated_at=not preserve_updated_at)

        if self._ai_conversation_id:
            self._ai_html_cache[self._ai_conversation_id] = getattr(self, "_last_rendered_html", "")

    def _delete_conversation_cleanup(self, conv_id: str):
        try:
            self._clear_subagent_bar_instantly()
            from tool_registry import cleanup_subagents_for_conversation
            cleanup_subagents_for_conversation(conv_id)
        except Exception:
            pass
        self._conversation_store.delete_conversation(conv_id)
        getattr(self, "_deleted_conversation_ids", set()).add(conv_id)
        self._ai_html_cache.pop(conv_id, None)

    def _switch_to_conversation(self, conv_id: str, save_current: bool = True):
        """Switch AI panel to display a different conversation by ID."""
        self._ai_render_async = False
        if not hasattr(self, "_ai_request_id"):
            self._ai_request_id = 0
        self._ai_request_id += 1

        if save_current and getattr(self, "_ai_messages", []) and getattr(self, "_ai_conversation_id", None):
            is_currently_running = getattr(self, "_ai_running_convs", {}).get(self._ai_conversation_id, {}).get("streaming", False)
            if not is_currently_running:
                try:
                    model_snapshot = self._build_model_snapshot()
                    self._save_current_conversation(model_snapshot, preserve_updated_at=True)
                except Exception as e:
                    print(f"Error saving before switch: {e}", flush=True)

        self._clear_subagent_bar_instantly()

        if getattr(self, "_ai_render_timeout_id", 0) != 0:
            GLib.source_remove(self._ai_render_timeout_id)
            self._ai_render_timeout_id = 0

        st = getattr(self, "_ai_running_convs", {}).get(conv_id)
        conv = self._conversation_store.load_conversation(conv_id)
        if not conv and not (st and st.get("streaming")):
            return

        if st and st.get("streaming"):
            self._ai_messages = st["messages"]
            self._ai_conversation_id = conv_id
            if conv:
                self._ai_conversation_created_at = conv.created_at
                self._ai_summary = conv.summary
                self._ai_system_prompt = conv.system_prompt
            else:
                self._ai_summary = ""
                self._ai_conversation_created_at = st.get("created_at", getattr(self, "_ai_conversation_created_at", 0))
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
            self._ai_system_prompt = conv.system_prompt if conv else ""
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
                self._prune_messages(defer_render=True)
                self._ai_render_async = True
                self._async_render_conversation(conv_id)
        
        self._refresh_subagent_bar()

        _, _, _, display_name, _, _, _, _, _ = self._read_model_config(None, getattr(self, "_ai_active_model_info", None))
        if hasattr(self, "_ai_lbl") and self._ai_lbl:
            self._ai_lbl.set_markup(f"<b>{_AI_HEADER_TITLE}</b>\n<span size='small' foreground='#888888'>({display_name})</span>")

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
            if getattr(self, "_ai_history_popover", None) is not None:
                self._ai_history_popover.refresh_dropdown()
        except Exception as e:
            print(f"Failed to refresh dropdown in switch: {e}", flush=True)
        if not getattr(self, "_ai_render_async", False):
            self._update_token_display()

    def _async_render_conversation(self, conv_id: str) -> None:
        """后台渲染会话 HTML 并统计 token，完成后 idle 回主线程应用。"""
        messages = list(getattr(self, "_ai_messages", []))
        show_details = getattr(self, "_show_tool_details", True)
        snapshot_len = len(messages)

        def _worker():
            try:
                text = _rebuild_markdown_from_messages(
                    messages, show_details=show_details,
                )
                rendered_html = _markdown_to_html_safe(text, fallback_content="")
                tokens = self._estimate_token_count(messages)
                GLib.idle_add(self._apply_async_render, conv_id, rendered_html, text, tokens, snapshot_len)
            except Exception as e:
                print(f"[AI] async render error: {e}", flush=True)
                GLib.idle_add(self._fallback_sync_render, conv_id, messages, show_details)

        threading.Thread(target=_worker, daemon=True).start()

    def _fallback_sync_render(self, conv_id: str, messages: list, show_details: bool) -> bool:
        self._ai_render_async = False
        try:
            if getattr(self, "_ai_conversation_id", None) != conv_id:
                return False
            text = _rebuild_markdown_from_messages(messages, show_details=show_details)
            rendered_html = _markdown_to_html_safe(text, fallback_content="")
            self._last_rendered_html = rendered_html
            self._ai_html_cache[conv_id] = rendered_html
            self._ai_markdown_text = text
            js_code = f"updateContent({json.dumps(rendered_html)});"
            if hasattr(self, "_ai_webview") and self._ai_webview:
                self._ai_webview.run_javascript(js_code, None, None)
            self._update_token_display()
        except Exception as e:
            print(f"[AI] fallback sync render error: {e}", flush=True)
        return False

    def _apply_async_render(self, conv_id: str, rendered_html: str, markdown_text: str, tokens: int, snapshot_len: int) -> bool:
        try:
            if getattr(self, "_ai_conversation_id", None) != conv_id:
                return False
            if len(getattr(self, "_ai_messages", [])) != snapshot_len:
                return False
            self._ai_render_async = False
            self._last_rendered_html = rendered_html
            self._ai_html_cache[conv_id] = rendered_html
            self._ai_markdown_text = markdown_text
            js_code = f"updateContent({json.dumps(rendered_html)});"
            if hasattr(self, "_ai_webview") and self._ai_webview:
                self._ai_webview.run_javascript(js_code, None, None)
            self._update_token_display(tokens)
        except Exception as e:
            print(f"[AI] apply async render error: {e}", flush=True)
        return False

    def _get_sorted_conversations(self) -> List[Dict[str, Any]]:
        """Return all conversations sorted by updated_at descending (newest first)."""
        summaries = self._conversation_store.list_conversations()
        existing_ids = {s.get("id") for s in summaries}

        active_id = getattr(self, "_ai_conversation_id", None)
        if active_id and active_id not in existing_ids:
            if getattr(self, "_ai_messages", []):
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

        for cid, st in list(getattr(self, "_ai_running_convs", {}).items()):
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
        """Navigate conversation history via keyboard shortcut."""
        summaries = self._get_sorted_conversations()
        if not summaries:
            return

        if getattr(self, "_ai_conversation_id", None) is None:
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
        if target_id and target_id != getattr(self, "_ai_conversation_id", None):
            self._switch_to_conversation(target_id)

    def _rebuild_markdown_from_messages(
        self,
        messages: List[Dict],
        streaming_reasoning: str = "",
        streaming_content: str = "",
        is_streaming: bool = False
    ) -> str:
        """Convert OpenAI-format message list back to rendered markdown text."""
        show_details = getattr(self, "_show_tool_details", True)
        return _rebuild_markdown_from_messages(
            messages,
            streaming_reasoning=streaming_reasoning,
            streaming_content=streaming_content,
            is_streaming=is_streaming,
            show_details=show_details,
        )

    def _estimate_token_count(self, messages: Optional[List[Dict]] = None) -> int:
        """估算消息列表的 token 数。"""
        if messages is None:
            messages = getattr(self, "_ai_messages", [])
        if not messages:
            return 0
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            n = 3
            for msg in messages:
                n += 4
                for key, value in msg.items():
                    n += len(enc.encode(str(value)))
                    if key == "name":
                        n -= 1
            return int(n * getattr(self, "_TOKEN_CALIBRATION_FACTOR", 0.89))
        except ImportError:
            total_chars = 0
            for msg in messages:
                for key, value in msg.items():
                    total_chars += len(str(value))
                total_chars += getattr(self, "_ESTIMATED_OVERHEAD_PER_MSG", 20)
            return int(total_chars / 2.5)

    def _update_token_display(self, tokens: Optional[int] = None):
        """更新输入框下方的 token 计数显示。"""
        if tokens is None:
            tokens = self._estimate_token_count()
        label = f"Shift+Enter \u21b5 \u00b7 Enter \u53d1\u9001"
        if tokens > 0:
            label = f"\U0001f4dd {tokens:,} tokens  |  " + label
        if hasattr(self, "_ai_hint_label"):
            self._ai_hint_label.set_text(label)

    def _build_conversation_rounds(self, msgs: list) -> list:
        """将消息列表聚合为以 user 提问为起点的轮次结构列表。"""
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

    def _rollback_to_round(self, round_index: int):
        msgs = getattr(self, "_ai_messages", [])
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

    def _handle_retry_command(self):
        self._cancel_streaming_if_active()
        msgs = getattr(self, "_ai_messages", [])
        if not msgs:
            return

        user_index = len(msgs) - 1
        while user_index >= 0 and msgs[user_index].get("role") != "user":
            user_index -= 1

        if user_index < 0:
            return

        user_content = msgs[user_index].get("content", "")
        if isinstance(user_content, list):
            last_user_content = next(
                (p["text"] for p in user_content if isinstance(p, dict) and p.get("type") == "text"),
                ""
            )
        else:
            last_user_content = user_content

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
        msgs = getattr(self, "_ai_messages", [])
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
        if not getattr(self, "_ai_conversation_id", None) or not getattr(self, "_ai_messages", []):
            self.append_html_to_webview(
                '<div class="chat-simple-error">⚠️ 当前没有活跃且包含消息的对话可供 Fork 分支。</div>'
            )
            return

        if getattr(self, "_ai_streaming", False) or getattr(self, "_ai_cancelling", False):
            self.append_html_to_webview(
                '<div class="chat-simple-error">⚠️ 当前正处于回复生成状态，请在当前轮次完成后再执行 /fork 分支。</div>'
            )
            return

        try:
            model_snapshot = self._build_model_snapshot()
            self._save_current_conversation(model_snapshot, preserve_updated_at=True)
        except Exception as e:
            print(f"Error saving conversation before fork: {e}", flush=True)
            self.append_html_to_webview(
                f'<div class="chat-simple-error">❌ 分支建立失败：无法保存当前对话状态 ({html.escape(str(e))})。</div>'
            )
            return

        try:
            current_conv = self._conversation_store.load_conversation(self._ai_conversation_id)
            if not current_conv:
                first_msg = self._ai_messages[0].get("content", "")
                current_title = _extract_local_title(first_msg) if first_msg else "New Conversation"
                sys_prompt = getattr(self, "_ai_system_prompt", "")
            else:
                current_title = current_conv.title
                sys_prompt = current_conv.system_prompt

            if custom_title and custom_title.strip():
                new_title = custom_title.strip()
            else:
                new_title = f"{current_title} (Fork)"

            model_snapshot = self._build_model_snapshot()
            new_conv = self._conversation_store.create_conversation(
                title=new_title,
                system_prompt=sys_prompt,
                model_config=model_snapshot
            )

            new_conv.messages = [_dict_to_chat_message(m) for m in self._ai_messages]
            new_conv.summary = getattr(self, "_ai_summary", "")
            self._conversation_store.save_conversation(new_conv, bump_updated_at=False)

            self._switch_to_conversation(new_conv.id, save_current=False)

            escaped_title = html.escape(new_title)
            self.append_html_to_webview(
                f'<div class="chat-status-notice">'
                f'🌿 <strong>已成功创建分支对话</strong>：「{escaped_title}」'
                f'<span style="opacity: 0.7; font-size: 11px; margin-left: 6px;">(ID: {new_conv.id})</span>'
                f'</div>'
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.append_html_to_webview(
                f'<div class="chat-simple-error">❌ 分支建立失败：{html.escape(str(e))}</div>'
            )

    def _handle_title_command(self, title_text: str):
        if not getattr(self, "_ai_conversation_id", None):
            self.append_html_to_webview(
                '<div class="chat-simple-error">当前没有活跃对话，无法设置标题。</div>'
            )
            return

        if title_text:
            conv = self._conversation_store.load_conversation(self._ai_conversation_id)
            if conv:
                conv.title = title_text
                self._conversation_store.save_conversation(conv, bump_updated_at=False)
            if getattr(self, "_ai_history_popover", None) is not None:
                self._ai_history_popover.refresh_dropdown()
            escaped = html.escape(title_text)
            self.append_html_to_webview(
                f'<div class="chat-simple-info">标题已更新: {escaped}</div>'
            )
        else:
            if not getattr(self, "_ai_messages", []):
                self.append_html_to_webview(
                    '<div class="chat-simple-error">当前对话没有消息，无法生成标题。</div>'
                )
                return

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
                        getattr(self, "_ai_last_prompt_obj", None),
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
        """Handle /summary command: summarize old messages and trim to keep N."""
        if getattr(self, "_ai_settings_store", None) is not None:
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

        if not getattr(self, "_ai_messages", []):
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

        if getattr(self, "_ai_summary_generating", False):
            self.append_html_to_webview(
                '<div class="chat-simple-error">已在生成摘要中，请等待完成后再试。</div>'
            )
            return

        if getattr(self, "_ai_settings_store", None) and not self._ai_settings_store.enable_summary:
            self.append_html_to_webview(
                '<div class="chat-simple-error">摘要功能未启用（设置中 enable_summary=False），请在设置中开启。</div>'
            )
            return

        pruned = self._ai_messages[:-keep]
        trim_target = keep + 1
        self._ai_summary_generating = True
        self._show_summary_status()

        threading.Thread(
            target=self._generate_summary_async,
            args=(list(pruned), trim_target),
            daemon=True
        ).start()

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
        cwd = get_bash_cwd(session_key=getattr(self, "_ai_conversation_id", None))
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
        """处理 /ai-polish <raw_text> 命令。"""
        last_asst_text = ""
        for msg in reversed(getattr(self, "_ai_messages", [])):
            if msg.get("role") == "assistant" and isinstance(msg.get("content"), str) and msg.get("content").strip():
                last_asst_text = msg.get("content").strip()
                break

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
            prompt = f"{template}\n\n{raw_input}"

        buf = self._ai_entry.get_buffer()
        buf.set_text("")
        old_placeholder = getattr(self._ai_entry, "placeholder_text", "给 AI 助手发送消息...")
        self._ai_entry.placeholder_text = "✨ 等待 AI 润色中..."
        self._ai_entry.set_sensitive(False)
        self._update_send_button(False, sensitive=False)
        target_conv_id = getattr(self, "_ai_conversation_id", None)

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

    def _switch_model_by_alias(self, alias: str):
        """Switch AI model by alias. Updates active model info and header label."""
        model = next((m for m in self._llm_settings_store.models if m.alias.lower() == alias.lower()), None)
        if not model:
            lines = [f"❌ 未找到模型别名 **\"{alias}\"**。\n", "可用模型:\n"]
            for m in self._llm_settings_store.models:
                lines.append(f"- **{m.alias}**" + (" (默认)" if m.is_default else "") + f" — `{m.model_name}`")
            lines.append("\n前往 **Prompts Config → ⚙️ API Settings** 管理模型配置。")
            self.append_html_to_webview(
                f'<div class="chat-model-info" style="color: #f43f5e; border-color: #f43f5e;">'
                f'{_markdown_to_html_safe("".join(lines))}</div>'
            )
            return

        self._ai_active_model_info = {
            "alias": model.alias,
            "base_url": model.base_url.strip(),
            "model_name": model.model_name.strip(),
            "temperature": model.temperature,
            "max_tokens": model.max_tokens,
            "top_p": model.top_p,
            "thinking_enabled": getattr(model, "thinking_enabled", False),
            "reasoning_effort": getattr(model, "reasoning_effort", "high"),
        }
        if hasattr(self, "_ai_lbl") and self._ai_lbl:
            self._ai_lbl.set_markup(f"<b>{_AI_HEADER_TITLE}</b>\n<span size='small' foreground='#888888'>({model.alias} ({model.model_name}))</span>")
        notice_html = (
            f'<div class="chat-status-notice">'
            f'🔄 已切换至 <strong>{model.alias}</strong> ({model.model_name})</div>'
        )
        self.append_html_to_webview(notice_html)

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

        if getattr(self, "on_dialog_shown", None):
            dialog.connect("show", lambda *_: self.on_dialog_shown())
        if getattr(self, "on_dialog_hidden", None):
            dialog.connect("destroy", lambda *_: self.on_dialog_hidden())

        from tool_registry import get_bash_cwd
        current_cwd = get_bash_cwd(session_key=getattr(self, "_ai_conversation_id", None))
        if os.path.isdir(current_cwd):
            dialog.set_current_folder(current_cwd)

        def _on_dialog_response(dlg, response):
            if response == Gtk.ResponseType.ACCEPT:
                chosen = dlg.get_filename()
                dlg.destroy()
                if chosen:
                    from tool_registry import set_bash_cwd
                    result = set_bash_cwd(chosen, session_key=getattr(self, "_ai_conversation_id", None))
                    self.append_html_to_webview(
                        f'<div class="chat-status-notice">{html.escape(result)}</div>'
                    )
            else:
                dlg.destroy()

        dialog.connect("response", _on_dialog_response)
        dialog.show_all()

    def _load_recent_conversation_deferred(self) -> bool:
        """首帧绘制后加载最近会话（原 open_ai_and_load_recent 的加载部分）。"""
        if not getattr(self, "_ai_recent_load_pending", False):
            return False
        self._ai_recent_load_pending = False
        try:
            if not self.get_visible():
                return False
            summaries = self._get_sorted_conversations()
            if summaries:
                latest_id = summaries[0].get("id")
                if latest_id:
                    if latest_id == getattr(self, "_ai_conversation_id", None) and getattr(self, "_ai_messages", []):
                        if getattr(self, "_ai_history_popover", None) is not None:
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
