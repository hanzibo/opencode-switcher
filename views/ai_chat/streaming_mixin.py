"""Streaming state and token batching mixin for AIChatPanel."""

import json
from typing import Optional, List, Dict

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib

from ai_text_utils.render import _render_tool_card_standalone


class StreamingMixin:
    """流式输出状态机、Token/Reasoning 批处理缓冲与工具卡片增量更新 Mixin。"""

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
        if getattr(self, "_ai_conversation_id", None):
            active_st = getattr(self, "_ai_running_convs", {}).get(self._ai_conversation_id)
            if active_st and active_st.get("req_id") is not None:
                return active_st["req_id"]
        return getattr(self, "_ai_request_id", 0)

    def _ensure_streaming_container(self, req_id: Optional[int] = None) -> bool:
        """确保流式消息容器已创建，若未创建则发送 appendMessageContainer JS。

        ``req_id`` 显式传入时优先使用（finalize 阶段流状态已弹出、
        ``_active_stream_req_id`` 会回退到全局 id），缺省时沿用
        ``_active_stream_req_id()`` 的既有单流/切回语义。
        """
        if not getattr(self, "_streaming_container_created", False) and hasattr(self, "_ai_webview") and self._ai_webview:
            if req_id is None:
                req_id = self._active_stream_req_id()
            msg_id = f"msg-{req_id}"
            self._ai_webview.run_javascript(f"appendMessageContainer('{msg_id}');", None, None)
            self._streaming_container_created = True
            return True
        return getattr(self, "_streaming_container_created", False)

    def _on_token_delta(self, text: str):
        """收到 LLM 文本增量，累积到 buffer 并安排 60ms flush（主线程调用）。"""
        if not getattr(self, "_ai_streaming", False):
            return  # 流已结束，忽略延迟回调防止重复渲染
        if getattr(self, "_STREAM_PERF_LOG", False):
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
        if not getattr(self, "_ai_streaming", False):
            self._token_buffer = ""
            self._flush_scheduled = False
            self._flush_source_id = 0
            return False  # 流已结束，丢弃残留 buffer
        if getattr(self, "_STREAM_PERF_LOG", False):
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

    def _on_reasoning_delta(self, text: str):
        """收到 LLM 推理增量，累积到 buffer 并安排 60ms flush。"""
        if not getattr(self, "_ai_streaming", False):
            return  # 流已结束，忽略延迟回调
        if getattr(self, "_STREAM_PERF_LOG", False):
            print(f"[perf] reasoning_delta: +{len(text)}ch, buffer={len(self._reasoning_buffer)}ch", flush=True)
        self._reasoning_buffer += text
        if not self._reasoning_flush_scheduled:
            self._reasoning_flush_scheduled = True
            self._reasoning_flush_source_id = GLib.timeout_add(self._BATCH_FLUSH_MS, self._flush_reasoning_buffer)

    def _flush_reasoning_buffer(self) -> bool:
        """60ms 定时器回调：将累积的推理文本批量 flush 到 WebView。"""
        if not getattr(self, "_ai_streaming", False):
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
        并发送 ``finishReasoning()``。
        """
        if getattr(self, "_STREAM_PERF_LOG", False):
            print(f"[perf] tool_calls_started: req={req_id}", flush=True)

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
        for msg in getattr(self, "_ai_messages", []):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if tc.get("id") == tool_call_id:
                        return tc
        return None

    @property
    def _show_tool_details(self) -> bool:
        return getattr(self._ai_settings_store, 'show_tool_details', True) if getattr(self, "_ai_settings_store", None) else True

    def _on_tool_result(self, tool_call_id: str, result_text: str, status: str, req_id: int):
        if (not getattr(self, "_ai_settings_store", None)
                or not self._ai_settings_store.enable_incremental_tools):
            return

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

    def _append_tool_calls_incremental(self, tool_calls: List[dict], req_id: int):
        """增量插入 assistant tool_calls 卡片（P0-1B），避免全量 render_turn."""
        if not tool_calls or not hasattr(self, "_ai_webview") or not self._ai_webview:
            GLib.idle_add(self._render_current_assistant_message, req_id)
            return
        conv_id = None
        for cid, st in list(getattr(self, "_ai_running_convs", {}).items()):
            if st.get("req_id") == req_id:
                conv_id = cid
                break
        if not conv_id or conv_id != getattr(self, "_ai_conversation_id", None):
            return
        st = self._ai_running_convs.get(conv_id)
        if not st or not st.get("streaming", False):
            GLib.idle_add(self._render_current_assistant_message, req_id)
            return
        if not getattr(self, "_streaming_container_created", False) or not st.get("response_div_added", False):
            GLib.idle_add(self._render_current_assistant_message, req_id)
            return
        try:
            cards = []
            for tc in tool_calls:
                # running 状态，无结果
                card_html = _render_tool_card_standalone(tc, "", "running",
                                                          show_details=self._show_tool_details)
                cards.append(card_html)
            combined = "".join(cards)
            msg_id = f"msg-{req_id}"
            js_code = f"appendToolCalls({json.dumps(msg_id)}, {json.dumps(combined)});"
            self._ai_webview.run_javascript(js_code, None, None)
        except Exception:
            GLib.idle_add(self._render_current_assistant_message, req_id)

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
