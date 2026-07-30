"""多会话状态与持久化 — AI 聊天历史管理。

职责：
- 管理 _ai_running_convs 多会话并发状态
- ConversationStore 读写（保存/加载）
- 历史下钻与滚动分段加载
- 标题自动生成
- 上下文自动裁剪（prune）与摘要生成
- Token 计数
"""

import threading
import time
import re
import html
import json
import os
from typing import Optional, List, Dict, Any, Tuple
from uuid import uuid4
from gi.repository import GLib

from stores.clipboard_store import (
    ConversationStore, Conversation, ChatMessage, AISettingsStore,
    DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS, DEFAULT_TOP_P,
    CONFIG_DIR, _DEFAULT_SUMMARY_TEMPLATE,
)
from ai_engine.llm_client import LLMRequestConfig, _LLMHttpClient, _LLMHttpError
from ai_text_utils import (
    _dict_to_chat_message, _extract_local_title, _rebuild_markdown_from_messages,
    _vision_content_to_text, _close_unclosed_code_blocks, _preserve_newlines,
    _resolve_vision_image_src,
    USER_AVATAR_HTML,
)
from ai_engine.render_pipeline import render_turn, TurnRenderInput
from ai_engine.llm_client import LLMRequestConfig as LLMConfig
from system.event_types import StreamEventType


def _to_chat_messages(msgs: List[Dict]) -> List[ChatMessage]:
    """将 dict 列表转为 ChatMessage 对象列表。"""
    return [ChatMessage(role=m["role"], content=m["content"],
                        tool_call_id=m.get("tool_call_id"),
                        name=m.get("name"),
                        tool_calls=m.get("tool_calls"),
                        reasoning_content=m.get("reasoning_content")) for m in msgs]


class AIChatHistoryManager:
    """管理多会话并发状态、持久化、标题生成和上下文裁剪。

    无 GTK 耦合，状态访问需通过 GLib.idle_add 确保主线程安全。
    """

    # ── Token 计数常量 ──
    _TOKEN_CALIBRATION_FACTOR = 0.89
    _ESTIMATED_OVERHEAD_PER_MSG = 20

    def __init__(
        self,
        conversation_store: ConversationStore,
        llm_settings_store,
        ai_settings_store=None,
        on_save_callback=None,
        on_title_generated_callback=None,
        on_markdown_updated_callback=None,
        on_rebuild_needed_callback=None,
        on_render_markdown_callback=None,
        on_append_html_callback=None,
        on_update_token_display_callback=None,
        on_prune_complete_callback=None,
        read_model_config_fn=None,
        get_title_model_config_fn=None,
        build_model_snapshot_fn=None,
        get_active_model_info_fn=None,
        get_last_prompt_obj_fn=None,
        get_ai_request_id_fn=None,
        get_ai_conversation_id_fn=None,
        set_ai_request_id_fn=None,
        set_ai_messages_fn=None,
        get_ai_messages_fn=None,
        set_ai_markdown_text_fn=None,
        get_ai_markdown_text_fn=None,
        set_ai_streaming_fn=None,
        set_ai_conversation_id_fn=None,
        set_ai_title_generated_fn=None,
        set_ai_summary_fn=None,
        get_ai_summary_fn=None,
        set_ai_summary_generating_fn=None,
        set_ai_cancelling_fn=None,
        get_ai_cancelling_fn=None,
        set_streaming_state_fn=None,
        get_running_convs_fn=None,
        set_running_convs_fn=None,
        get_html_cache_fn=None,
        set_html_cache_fn=None,
        get_llm_client_fn=None,
        get_mcp_state_fn=None,
        clear_subagent_bar_fn=None,
        refresh_subagent_bar_fn=None,
        get_update_send_button_fn=None,
        get_ai_spinner_fn=None,
    ):
        self._conversation_store = conversation_store
        self._llm_settings_store = llm_settings_store
        self._ai_settings_store = ai_settings_store
        self._llm_client = _LLMHttpClient()

        # Callbacks
        self._on_save_callback = on_save_callback
        self._on_title_generated_callback = on_title_generated_callback
        self._on_markdown_updated_callback = on_markdown_updated_callback
        self._on_rebuild_needed_callback = on_rebuild_needed_callback
        self._on_render_markdown_callback = on_render_markdown_callback
        self._on_append_html_callback = on_append_html_callback
        self._on_update_token_display_callback = on_update_token_display_callback
        self._on_prune_complete_callback = on_prune_complete_callback
        self._clear_subagent_bar_fn = clear_subagent_bar_fn
        self._refresh_subagent_bar_fn = refresh_subagent_bar_fn
        self._get_update_send_button_fn = get_update_send_button_fn
        self._get_ai_spinner_fn = get_ai_spinner_fn

        # State accessor functions (injected by parent panel)
        self._read_model_config_fn = read_model_config_fn
        self._get_title_model_config_fn = get_title_model_config_fn
        self._build_model_snapshot_fn = build_model_snapshot_fn
        self._get_active_model_info_fn = get_active_model_info_fn
        self._get_last_prompt_obj_fn = get_last_prompt_obj_fn
        self._get_ai_request_id_fn = get_ai_request_id_fn
        self._get_ai_conversation_id_fn = get_ai_conversation_id_fn
        self._set_ai_request_id_fn = set_ai_request_id_fn
        self._set_ai_messages_fn = set_ai_messages_fn
        self._get_ai_messages_fn = get_ai_messages_fn
        self._set_ai_markdown_text_fn = set_ai_markdown_text_fn
        self._get_ai_markdown_text_fn = get_ai_markdown_text_fn
        self._set_ai_streaming_fn = set_ai_streaming_fn
        self._set_ai_conversation_id_fn = set_ai_conversation_id_fn
        self._set_ai_title_generated_fn = set_ai_title_generated_fn
        self._set_ai_summary_fn = set_ai_summary_fn
        self._get_ai_summary_fn = get_ai_summary_fn
        self._set_ai_summary_generating_fn = set_ai_summary_generating_fn
        self._set_ai_cancelling_fn = set_ai_cancelling_fn
        self._get_ai_cancelling_fn = get_ai_cancelling_fn
        self._set_streaming_state_fn = set_streaming_state_fn
        self._get_running_convs_fn = get_running_convs_fn
        self._set_running_convs_fn = set_running_convs_fn
        self._get_html_cache_fn = get_html_cache_fn
        self._set_html_cache_fn = set_html_cache_fn
        self._get_llm_client_fn = get_llm_client_fn
        self._get_mcp_state_fn = get_mcp_state_fn

    # ── 便捷状态访问 ─────────────────────────────────────────────

    def _get_messages(self) -> List[Dict]:
        return self._get_ai_messages_fn() if self._get_ai_messages_fn else []

    def _set_messages(self, msgs):
        if self._set_ai_messages_fn:
            self._set_ai_messages_fn(msgs)

    def _get_markdown_text(self) -> str:
        return self._get_ai_markdown_text_fn() if self._get_ai_markdown_text_fn else ""

    def _set_markdown_text(self, text: str):
        if self._set_ai_markdown_text_fn:
            self._set_ai_markdown_text_fn(text)

    def _get_conv_id(self) -> str:
        return self._get_ai_conversation_id_fn() if self._get_ai_conversation_id_fn else ""

    def _set_conv_id(self, cid: str):
        if self._set_ai_conversation_id_fn:
            self._set_ai_conversation_id_fn(cid)

    def _get_request_id(self) -> int:
        return self._get_ai_request_id_fn() if self._get_ai_request_id_fn else 0

    def _set_request_id(self, rid: int):
        if self._set_ai_request_id_fn:
            self._set_ai_request_id_fn(rid)

    def _get_summary(self) -> str:
        return self._get_ai_summary_fn() if self._get_ai_summary_fn else ""

    def _set_summary(self, s: str):
        if self._set_ai_summary_fn:
            self._set_ai_summary_fn(s)

    def _get_running_convs(self) -> Dict:
        return self._get_running_convs_fn() if self._get_running_convs_fn else {}

    def _set_running_convs(self, d: Dict):
        if self._set_running_convs_fn:
            self._set_running_convs_fn(d)

    # ── Builder: 消息构建 ────────────────────────────────────────

    def build_llm_messages(self, ai_messages: List[Dict], ai_summary: str) -> tuple:
        """构建发送给 LLM 的消息列表和额外 system 消息。

        Returns:
            tuple: (messages_list, extra_system_messages)
        """
        extra = []
        if ai_summary:
            extra.append({
                "role": "system",
                "content": f"【历史摘要】\n{ai_summary}"
            })
        return list(ai_messages), extra

    # ── 对话保存/加载 ────────────────────────────────────────────

    def save_current_conversation(
        self,
        ai_messages: List[Dict],
        ai_conversation_id: str,
        ai_conversation_created_at: int,
        ai_summary: str,
        ai_summary_generating: bool,
        last_rendered_html: str,
        model_snapshot: Dict[str, Any],
        preserve_updated_at: bool = False,
    ):
        """Save or update the current active conversation to the store."""
        local_title = "New Conversation"
        if ai_messages:
            local_title = _extract_local_title(
                ai_messages[0].get("content", "")
            )

        if not ai_conversation_id:
            now = int(time.time() * 1000)
            conv = self._conversation_store.create_conversation(
                title=local_title,
                model_config=model_snapshot
            )
            ai_conversation_id = conv.id
            conv.messages = [_dict_to_chat_message(m) for m in ai_messages]
            conv.summary = ai_summary
            self._conversation_store.save_conversation(conv, bump_updated_at=not preserve_updated_at)
            # Update parent state
            if self._set_ai_conversation_id_fn:
                self._set_ai_conversation_id_fn(ai_conversation_id)
        else:
            conv = self._conversation_store.load_conversation(ai_conversation_id)
            if conv:
                conv.messages = [_dict_to_chat_message(m) for m in ai_messages]
                conv.model_config_snapshot = model_snapshot
                if not ai_summary_generating:
                    conv.summary = ai_summary
            else:
                conv = Conversation(
                    id=ai_conversation_id,
                    title=local_title,
                    system_prompt="",
                    messages=[_dict_to_chat_message(m) for m in ai_messages],
                    model_config_snapshot=model_snapshot,
                    created_at=ai_conversation_created_at,
                    updated_at=int(time.time() * 1000),
                )
            self._conversation_store.save_conversation(conv, bump_updated_at=not preserve_updated_at)

        # Update HTML cache
        if self._set_html_cache_fn and ai_conversation_id:
            self._set_html_cache_fn(ai_conversation_id, last_rendered_html)

        return ai_conversation_id

    def build_model_snapshot(self, active_model_info: Optional[Dict] = None,
                              read_model_config_fn=None) -> Dict[str, Any]:
        """Build a model_config_snapshot from active model info or resolved config."""
        if active_model_config_fn := read_model_config_fn or self._read_model_config_fn:
            if active_model_info:
                return dict(active_model_info)
            base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = (
                active_model_config_fn(None, None)
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
        return {}

    # ── 对话切换 ─────────────────────────────────────────────────

    def switch_to_conversation(
        self,
        conv_id: str,
        ai_messages: List[Dict],
        ai_conversation_id: str,
        ai_conversation_created_at: int,
        ai_summary: str,
        last_rendered_html: str,
        ai_html_cache: Dict[str, str],
        ai_streaming: bool,
        ai_current_assistant_text: str,
        ai_current_reasoning_text: str,
        ai_response_div_added: bool,
        on_switch_complete=None,
    ):
        """Switch AI panel to display a different conversation by ID.

        Returns a dict with the new state to apply, or None if conversation not found.
        """
        # Save current conversation if it has content
        if ai_messages and ai_conversation_id:
            running_convs = self._get_running_convs()
            is_currently_running = running_convs.get(ai_conversation_id, {}).get("streaming", False)
            if not is_currently_running:
                try:
                    model_snapshot = self.build_model_snapshot(
                        None, self._read_model_config_fn
                    )
                    self.save_current_conversation(
                        ai_messages, ai_conversation_id, ai_conversation_created_at,
                        ai_summary, False, last_rendered_html, model_snapshot,
                        preserve_updated_at=True,
                    )
                except Exception as e:
                    print(f"Error saving before switch: {e}", flush=True)

        # Load target conversation
        conv = self._conversation_store.load_conversation(conv_id)
        if not conv:
            return None

        return self._build_conv_state_from_loaded(conv)

    def _build_conv_state_from_loaded(self, conv: Conversation) -> Dict:
        """Build state dict from a loaded Conversation object."""
        messages = []
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
            messages.append(msg)

        return {
            "messages": messages,
            "conversation_id": conv.id,
            "created_at": conv.created_at,
            "summary": conv.summary or "",
            "title": conv.title,
        }

    # ── 排序 ─────────────────────────────────────────────────────

    def get_sorted_conversations(
        self,
        active_conv_id: str,
        ai_messages: List[Dict],
        running_convs: Dict,
    ) -> List[Dict[str, Any]]:
        """Return all conversations sorted by updated_at descending (newest first)."""
        summaries = self._conversation_store.list_conversations()
        existing_ids = {s.get("id") for s in summaries}

        # Add active conversation if not on disk
        if active_conv_id and active_conv_id not in existing_ids:
            if ai_messages:
                first_msg = ai_messages[0].get("content", "")
                if isinstance(first_msg, list):
                    first_msg = next(
                        (p["text"] for p in first_msg if isinstance(p, dict) and p.get("type") == "text"), ""
                    )
                title = first_msg[:30] if first_msg else "New Conversation"
                summaries.append({
                    "id": active_conv_id,
                    "title": title,
                    "message_count": len(ai_messages),
                    "updated_at": int(time.time() * 1000),
                })
                existing_ids.add(active_conv_id)

        # Add running background conversations not on disk
        for cid, st in list(running_convs.items()):
            if cid not in existing_ids:
                msgs = st.get("messages", [])
                if msgs:
                    first_msg = msgs[0].get("content", "")
                    if isinstance(first_msg, list):
                        first_msg = next(
                            (p["text"] for p in first_msg if isinstance(p, dict) and p.get("type") == "text"), ""
                        )
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

    def navigate_conversation(self, direction: int, active_conv_id: str,
                               ai_messages: List[Dict], running_convs: Dict,
                               on_switch_fn) -> bool:
        """Navigate conversation history.

        Returns True if navigation occurred.
        """
        summaries = self.get_sorted_conversations(active_conv_id, ai_messages, running_convs)
        if not summaries:
            return False

        if active_conv_id is None:
            target_idx = len(summaries) - 1 if direction < 0 else 0
        else:
            current_idx = -1
            for i, s in enumerate(summaries):
                if s.get("id") == active_conv_id:
                    current_idx = i
                    break
            if current_idx == -1:
                return False
            target_idx = current_idx + direction
            if target_idx < 0 or target_idx >= len(summaries):
                return False

        target_id = summaries[target_idx].get("id")
        if target_id and target_id != active_conv_id and on_switch_fn:
            on_switch_fn(target_id)
            return True
        return False

    # ── 标题生成 ─────────────────────────────────────────────────

    def call_llm_sync(self, messages: list, base_url: str, api_key: str,
                       model_name: str, timeout: int = 15,
                       temperature: float = DEFAULT_TEMPERATURE,
                       max_tokens: int = DEFAULT_MAX_TOKENS,
                       top_p: float = DEFAULT_TOP_P) -> Optional[str]:
        """同步调用 LLM 并返回 content。"""
        config = LLMConfig(
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
        """Call LLM with a title-generation prompt, parse <title> and update conversation."""
        try:
            content = self.call_llm_sync(
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

    def generate_conversation_title(self, first_message: str, conv_id: str,
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

    def generate_title_from_context(self, context_text: str, conv_id: str,
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
        """Idle callback: update conversation title in store."""
        conv = self._conversation_store.load_conversation(conv_id)
        if conv:
            conv.title = title
            self._conversation_store.save_conversation(conv, bump_updated_at=False)
        if self._on_title_generated_callback:
            self._on_title_generated_callback(conv_id, title)

    # ── Token 计数 ───────────────────────────────────────────────

    def estimate_token_count(self, messages: Optional[List[Dict]] = None) -> int:
        """估算消息列表的 token 数。"""
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
            return int(n * self._TOKEN_CALIBRATION_FACTOR)
        except ImportError:
            total_chars = 0
            for msg in messages:
                for key, value in msg.items():
                    total_chars += len(str(value))
                total_chars += self._ESTIMATED_OVERHEAD_PER_MSG
            return int(total_chars / 2.5)

    def update_token_display(self, ai_messages: List[Dict]):
        """触发 token 计数显示更新。"""
        if self._on_update_token_display_callback:
            n = self.estimate_token_count(ai_messages)
            self._on_update_token_display_callback(n)

    # ── 上下文裁剪（Prune） ─────────────────────────────────────

    def prune_messages(self, ai_messages: List[Dict], ai_summary: str,
                        ai_summary_generating: bool, ai_conversation_id: str,
                        read_model_config_fn=None, get_title_model_config_fn=None,
                        get_active_model_info_fn=None, get_last_prompt_obj_fn=None,
                        llm_client=None):
        """自动裁剪消息列表，超出 soft_limit 时触发。

        Returns actions dict or None if no pruning needed.
        """
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

        if len(ai_messages) <= soft_limit:
            return None

        if enable_summary and not ai_summary_generating:
            first = ai_messages[:1]
            rest = ai_messages[1:]
            target_len = trim_target - 1
            start_idx = len(rest) - target_len
            if start_idx < 0:
                start_idx = 0
            pruned = rest[:start_idx]
            if pruned and len(pruned) >= summary_threshold:
                # Start async summary generation
                self._start_summary_generation(
                    list(pruned), trim_target,
                    ai_messages, ai_summary, ai_conversation_id,
                )
                return {"action": "summarizing", "trim_target": trim_target}

        return self.apply_prune(ai_messages, trim_target, ai_summary, ai_conversation_id, save_summary=False)

    def apply_prune(self, ai_messages: List[Dict], trim_target: int,
                     ai_summary: str, ai_conversation_id: str,
                     save_summary: bool = False) -> Dict:
        """根据 trim_target 裁剪消息列表。

        Returns dict with the new state to apply.
        """
        if len(ai_messages) <= 1:
            return {"messages": ai_messages, "clear_summary_status": True}

        first = ai_messages[:1]
        rest = ai_messages[1:]
        target_len = trim_target - 1
        start_idx = len(rest) - target_len
        if start_idx < 0:
            start_idx = 0

        # Adjust start_idx backward if it lands on a "tool" message
        while start_idx > 0 and rest[start_idx].get("role") == "tool":
            start_idx -= 1

        if start_idx == 0 and rest and rest[0].get("role") == "tool":
            while start_idx < len(rest) and rest[start_idx].get("role") == "tool":
                start_idx += 1

        new_messages = first + rest[start_idx:]

        if save_summary:
            self._save_summary_to_conversation(ai_conversation_id, ai_summary)

        return {
            "messages": new_messages,
            "save_summary": save_summary,
            "clear_summary_status": True,
        }

    def _save_summary_to_conversation(self, ai_conversation_id: str, ai_summary: str):
        """保存摘要到对话文件。"""
        try:
            if not ai_conversation_id:
                return
            conv = self._conversation_store.load_conversation(ai_conversation_id)
            if conv:
                conv.summary = ai_summary
                self._conversation_store.save_conversation(conv, bump_updated_at=False)
        except Exception as e:
            print(f"Error saving summary to conversation: {e}", flush=True)

    def _start_summary_generation(self, pruned_messages: list, trim_target: int,
                                   ai_messages: List[Dict], ai_summary: str,
                                   ai_conversation_id: str):
        """启动后台摘要生成线程。"""
        if self._set_ai_summary_generating_fn:
            self._set_ai_summary_generating_fn(True)

        # Show summary status in UI
        if self._on_append_html_callback:
            self._on_append_html_callback(
                '<div id="summary-display" class="summary-display">'
                '<div class="summary-header">📝 摘要压缩中</div>'
                '<div class="summary-content"></div>'
                '</div>'
            )

        threading.Thread(
            target=self._generate_summary_async,
            args=(pruned_messages, trim_target, ai_messages, ai_summary, ai_conversation_id),
            daemon=True
        ).start()

    def _generate_summary_async(self, pruned_messages: list, trim_target: int,
                                 ai_messages: List[Dict], ai_summary: str,
                                 ai_conversation_id: str):
        """在后台线程中调用 LLM（流式），将即将丢弃的消息压缩为摘要。"""
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

            prev_summary = f"已有摘要：\n{ai_summary}\n\n" if ai_summary else ""
            template = (self._ai_settings_store.summary_prompt_template
                        if self._ai_settings_store else _DEFAULT_SUMMARY_TEMPLATE)
            try:
                prompt = template.format(
                    prev_summary=prev_summary,
                    conversation_text=convo_text,
                    max_chars=max_chars,
                )
            except (KeyError, ValueError) as e:
                failure_reason = f"模板格式错误：{e}"
                return

            # Read model config
            read_fn = self._read_model_config_fn
            if read_fn:
                last_prompt_obj = self._get_last_prompt_obj_fn() if self._get_last_prompt_obj_fn else None
                active_info = self._get_active_model_info_fn() if self._get_active_model_info_fn else None
                base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = \
                    read_fn(last_prompt_obj, active_info)
            else:
                base_url, api_key, model_name, temperature, max_tokens, top_p = "", "", "", 0.7, 4096, 0.9

            if not base_url or not api_key or not model_name:
                failure_reason = "当前模型配置不完整（缺少 Base URL / API Key / Model Name）"
                return

            result_parts = []
            summary_config = LLMConfig(
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
                    # Update summary display via callback
                    if self._on_append_html_callback:
                        GLib.idle_add(self._update_summary_display, event.text_delta)
                elif event.type == StreamEventType.STREAM_END:
                    break

            if cancel_event.is_set():
                if has_received_token:
                    failure_reason = f"摘要生成超时（流式停顿超过{idle_timeout_sec}秒），请更换模型重试"
                else:
                    failure_reason = f"摘要生成超时（{total_timeout_sec}秒内未收到首个token），请更换模型重试"
            else:
                result = "".join(result_parts).strip()
                if result:
                    new_summary = (
                        f"{ai_summary}\n后续对话摘要：{result}" if ai_summary else result
                    )
                    if len(new_summary) > max_chars * 3:
                        new_summary = new_summary[-max_chars * 3:]
                    # Update parent state
                    if self._set_ai_summary_fn:
                        self._set_ai_summary_fn(new_summary)
                    save_summary = True

        except _LLMHttpError as e:
            failure_reason = f"摘要生成失败（{e}），请更换模型重试"
        except Exception as e:
            failure_reason = f"摘要生成异常：{e}"
            import traceback
            traceback.print_exc()
        finally:
            total_timer.cancel()
            if idle_timer:
                idle_timer.cancel()
            if self._set_ai_summary_generating_fn:
                self._set_ai_summary_generating_fn(False)

            if failure_reason:
                GLib.idle_add(self._show_summary_failure, failure_reason)
            else:
                GLib.idle_add(self._on_prune_complete, trim_target, save_summary)

    def _update_summary_display(self, text: str):
        """后台线程调用，通过 GLib.idle_add 推送摘要流式文本到 WebView。"""
        if not text:
            return
        escaped = json.dumps(text)
        if self._on_append_html_callback:
            self._on_append_html_callback(
                f"<script>"
                f"(function(){{"
                f"var e=document.getElementById('summary-display');"
                f"if(e){{"
                f"var c=e.querySelector('.summary-content');"
                f"if(c)c.textContent+={escaped};"
                f"_scrollToBottom();"
                f"}}}})();"
                f"</script>"
            )

    def _show_summary_failure(self, reason: str):
        """在对话中插入一条系统消息说明摘要生成失败的原因。"""
        html_content = (
            '<div class="chat-message system-message" style="margin:8px 0;padding:8px 12px;'
            'background:var(--notice-bg,#fff3cd);border-left:4px solid var(--notice-border,#ffc107);'
            'border-radius:4px;font-size:13px;color:var(--notice-text,#856404);">'
            '⚠️ <b>上下文压缩失败</b><br>'
            f'{reason}'
            '</div>'
        )
        if self._on_append_html_callback:
            self._on_append_html_callback(html_content)
        if self._on_rebuild_needed_callback:
            self._on_rebuild_needed_callback()

    def _on_prune_complete(self, trim_target: int, save_summary: bool):
        """Prune complete callback (main thread)."""
        if self._on_prune_complete_callback:
            self._on_prune_complete_callback(trim_target, save_summary)

    # ── 轮次结构 ─────────────────────────────────────────────────

    @staticmethod
    def build_conversation_rounds(msgs: list) -> list:
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

    @staticmethod
    def build_round_cards_html(rounds) -> str:
        """Build HTML displaying conversation rounds as clickable cards."""
        def _strip_html(text):
            return re.sub(r'<[^>]+>', '', text).strip()

        cards_html = []
        total_rounds = len(rounds)
        for i, rd in enumerate(rounds):
            user_msg = rd["user_msg"]
            asst_msg = rd["asst_msg"]
            if isinstance(user_msg, list):
                text_parts = []
                for p in user_msg:
                    if isinstance(p, dict) and p.get("type") == "text":
                        text_parts.append(p["text"])
                user_msg = "\n".join(text_parts)
            if isinstance(asst_msg, list):
                text_parts = []
                for p in asst_msg:
                    if isinstance(p, dict) and p.get("type") == "text":
                        text_parts.append(p["text"])
                asst_msg = "\n".join(text_parts)
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

    # ── 背景对话渲染 ─────────────────────────────────────────────

    def render_background_conversation(self, conv_id: str, target_messages: list, state: dict,
                                        show_tool_details: bool, ai_html_cache: Dict[str, str],
                                        get_title_model_config_fn=None,
                                        read_model_config_fn=None,
                                        get_active_model_info_fn=None):
        """渲染背景对话（非当前可见），只更新 cache 不操作 WebView。"""
        output = render_turn(TurnRenderInput(
            turn_messages=target_messages,
            all_messages=target_messages,
            is_streaming=False,
            show_tool_details=show_tool_details,
        ))

        rebuilt_markdown = _rebuild_markdown_from_messages(target_messages, show_details=show_tool_details)
        from ai_text_utils import _markdown_to_html_safe
        html_content = _markdown_to_html_safe(rebuilt_markdown, fallback_content="")
        ai_html_cache[conv_id] = html_content
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
                model_snapshot = self.build_model_snapshot(
                    get_active_model_info_fn() if get_active_model_info_fn else None,
                    read_model_config_fn,
                )
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
                    title_cfg = None
                    if get_title_model_config_fn:
                        title_cfg = get_title_model_config_fn()
                    if not title_cfg and read_model_config_fn:
                        base_url, api_key, model_name, _, temperature, max_tokens, top_p, _, _ = \
                            read_model_config_fn(None, get_active_model_info_fn() if get_active_model_info_fn else None)
                        title_cfg = (base_url, api_key, model_name, temperature, max_tokens, top_p)
                    if title_cfg and title_cfg[1]:
                        threading.Thread(
                            target=self.generate_conversation_title,
                            args=(first_msg, conv_id, *title_cfg),
                            daemon=True
                        ).start()
        except Exception as e:
            print(f"Error saving background finished conversation: {e}", flush=True)

        return ai_html_cache
