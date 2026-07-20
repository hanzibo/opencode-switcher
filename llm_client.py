import json
import logging
import threading
import requests
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Generator

from clipboard_store import (
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TOP_P,
)

logger = logging.getLogger(__name__)
from ai_text_utils import (
    _model_supports_vision,
    _vision_content_to_text,
    _cached_image_to_data_uri,
    _strip_ai_markup,
)
import tool_registry
from event_types import (
    StreamEvent, StreamEventType, ToolCallData,
    text_delta, reasoning_delta, tool_calls_event, stream_end,
    parse_tool_call_from_dict,
)


# ═══════════════════════════════════════════════════════════════════
#  数据模型
# ═══════════════════════════════════════════════════════════════════


@dataclass
class LLMRequestConfig:
    """LLM API 请求参数聚合。

    替代 stream_chat_completion / sync_chat_completion 中重复出现的
    base_url / api_key / model_name / temperature / max_tokens / top_p / tools 等参数。
    """
    base_url: str
    api_key: str
    model_name: str
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    top_p: float = DEFAULT_TOP_P
    timeout: int = 30
    tools: Optional[list] = None
    tool_choice: Optional[str] = None
    extra_system_messages: Optional[list] = None
    thinking_enabled: bool = False
    reasoning_effort: str = "high"


# ═══════════════════════════════════════════════════════════════════
#  消息预处理
# ═══════════════════════════════════════════════════════════════════


def clean_messages_for_llm(messages: list) -> list:
    """清理消息列表：去除 AI 回复的 HTML/Markdown 标记等。

    从 ai_tool_loop 提取至此以统一消息预处理入口。
    """
    cleaned = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "assistant" and isinstance(content, str):
            cleaned_content = _strip_ai_markup(content)
            msg_copy = dict(msg)
            msg_copy["content"] = cleaned_content
            cleaned.append(msg_copy)
        else:
            cleaned.append(msg)
    return cleaned


# ═══════════════════════════════════════════════════════════════════
#  SSE 流解析
# ═══════════════════════════════════════════════════════════════════


class _ToolCallAccumulator:
    """Accumulate SSE streamed tool_calls deltas into complete ToolCall dicts.

    OpenAI SSE streams send tool_calls in multiple chunks:
      chunk 1: {index:0, id:"call_xxx", type:"function", function:{name:"web_search", arguments:""}}
      chunk 2: {index:0, function:{arguments:"{\\"query\\":\\"hello\\""}}

    This accumulator merges chunks by index. Tool calls are NOT yielded
    incrementally — they are only extracted when the stream ends
    (finish_reason: "tool_calls" or [DONE]).
    """
    def __init__(self):
        self._calls: Dict[int, dict] = {}

    def add_delta(self, delta: dict) -> None:
        """Accumulate a tool_calls delta chunk from the SSE stream."""
        index = delta.get("index")
        if index is None:
            return
        if index not in self._calls:
            self._calls[index] = {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            }
        call = self._calls[index]
        if "id" in delta:
            call["id"] += delta["id"]
        if "function" in delta:
            fn = delta["function"]
            if "name" in fn:
                call["function"]["name"] += fn["name"]
            if "arguments" in fn:
                call["function"]["arguments"] += fn["arguments"]

    def get_calls(self) -> List[dict]:
        """Return all accumulated tool calls, ordered by index, filtering out incomplete ones."""
        return [self._calls[k] for k in sorted(self._calls.keys())
                if self._calls[k]["id"] and self._calls[k]["function"]["name"]]

    def clear(self) -> None:
        """Clear all accumulated tool calls."""
        self._calls.clear()

    @property
    def has_calls(self) -> bool:
        """True if any tool calls have been accumulated."""
        return any(c["id"] and c["function"]["name"] for c in self._calls.values())


def parse_sse_events(
    response,
    cancel_event: Optional[threading.Event] = None,
) -> Generator[StreamEvent, None, None]:
    """从 requests 流式响应中解析 SSE 事件，产出 StreamEvent。

    将 SSE 行解析逻辑从 stream_chat_completion 中剥离，
    使其可独立测试和复用。

    Parameters
    ----------
    response : requests.Response
        已开始流式读取的 HTTP 响应。
    cancel_event : threading.Event, optional
        取消事件，设置后停止读取。

    Yields
    ------
    StreamEvent
        文本增量、推理增量或工具调用事件。
    """
    tc_accum = _ToolCallAccumulator()

    try:
        lines_iter = response.iter_lines(decode_unicode=True)
    except Exception as e:
        logger.error("SSE 无法开始读取响应流: %s", e)
        return

    try:
        for line in lines_iter:
            if cancel_event and cancel_event.is_set():
                return
            if line is None:
                continue
            if not line:
                continue
            if not line.startswith("data:"):
                continue

            data_str = line[5:].strip()
            if data_str == "[DONE]":
                calls = tc_accum.get_calls()
                if calls:
                    typed_calls = [parse_tool_call_from_dict(c) for c in calls]
                    yield tool_calls_event(typed_calls)
                return
            if not data_str:
                continue

            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            choices = chunk.get("choices", [{}])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            finish_reason = choices[0].get("finish_reason")

            # 处理工具调用增量
            tc_delta = delta.get("tool_calls")
            if tc_delta:
                for tcd in tc_delta:
                    tc_accum.add_delta(tcd)
                if finish_reason == "tool_calls":
                    calls = tc_accum.get_calls()
                    if calls:
                        tc_accum.clear()
                        typed_calls = [parse_tool_call_from_dict(c) for c in calls]
                        yield tool_calls_event(typed_calls)
                continue

            # finish_reason == "tool_calls" 但无 tc_delta（罕见情况）
            if finish_reason == "tool_calls":
                calls = tc_accum.get_calls()
                if calls:
                    tc_accum.clear()
                    typed_calls = [parse_tool_call_from_dict(c) for c in calls]
                    yield tool_calls_event(typed_calls)
                continue

            content = delta.get("content")
            if content:
                yield text_delta(content)

            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                yield reasoning_delta(reasoning)
    except Exception as e:
        logger.error("SSE 流读取异常: %s", e)
        return

    # Fallback: 流自然结束时产出残留工具调用
    calls = tc_accum.get_calls()
    if calls:
        typed_calls = [parse_tool_call_from_dict(c) for c in calls]
        yield tool_calls_event(typed_calls)


# ═══════════════════════════════════════════════════════════════════
#  异常
# ═══════════════════════════════════════════════════════════════════


class _LLMHttpError(Exception):
    pass


# ═══════════════════════════════════════════════════════════════════
#  HTTP 客户端
# ═══════════════════════════════════════════════════════════════════


class _LLMHttpClient:
    """LLM HTTP 客户端。

    封装与 OpenAI-compatible API 的 HTTP 通信。
    支持流式与非流式两种模式。
    """

    def __init__(self):
        self._session = requests.Session()
        self._active_response = None
        self._connect_timeout = 4
        self._init_session_retry()

    def _init_session_retry(self):
        """Configure retry strategy for transient failures."""
        retry_strategy = requests.packages.urllib3.util.retry.Retry(
            total=3,
            connect=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST"],
        )
        adapter = requests.adapters.HTTPAdapter(max_retries=retry_strategy)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _build_request(self, config: LLMRequestConfig, messages: list, stream: bool):
        """构建 HTTP 请求的 url、headers 和 body。

        Parameters
        ----------
        config : LLMRequestConfig
            请求配置（模型名、温度等）。
        messages : list
            已预处理的 messages（入参 messages 已通过 apply_message_template 处理）。
        stream : bool
            是否流式请求。

        Returns
        -------
        tuple
            (url, headers, body)
        """
        url = config.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
        }

        cleaned_messages = []
        # 注入额外的 system 消息（如历史摘要）
        if config.extra_system_messages:
            for extra_msg in config.extra_system_messages:
                cleaned_messages.append({
                    "role": "system",
                    "content": extra_msg.get("content", ""),
                })
        for m in messages:
            role = m.get("role")
            content = m.get("content")
            msg = {"role": role}

            # Multimodal content → resolve hash / downcast if model has no vision
            if isinstance(content, list):
                if not _model_supports_vision(config.model_name):
                    msg["content"] = _vision_content_to_text(content)
                else:
                    resolved_parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "image_url":
                            iu = part.get("image_url", {})
                            h = iu.get("hash")
                            if h and not iu.get("url", "").startswith("data:"):
                                du = _cached_image_to_data_uri(h)
                                if du:
                                    part = {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": du,
                                            "detail": iu.get("detail", "high"),
                                        },
                                    }
                                else:
                                    continue
                        resolved_parts.append(part)
                    msg["content"] = resolved_parts if resolved_parts else "[图片未就绪或已失效]"
            elif role == "assistant":
                tool_calls = m.get("tool_calls")
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                    msg["content"] = content if content else None
                    # 兼容 DeepSeek (reasoning_content) 和 MiMo (reasoning)
                    rc = m.get("reasoning_content") or m.get("reasoning")
                    if rc:
                        msg["reasoning_content"] = rc
                else:
                    msg["content"] = content or ""
            elif role == "tool":
                msg["content"] = content or ""
                msg["tool_call_id"] = m.get("tool_call_id") or ""
                name_val = m.get("name")
                if name_val:
                    msg["name"] = name_val
            else:
                msg["content"] = content or ""
            cleaned_messages.append(msg)

        body: Dict[str, Any] = {
            "model": config.model_name,
            "messages": cleaned_messages,
            "stream": stream,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "top_p": config.top_p,
        }
        if config.tools:
            body["tools"] = config.tools
            body["tool_choice"] = config.tool_choice or tool_registry.TOOL_CHOICE_AUTO
        if config.thinking_enabled:
            body["reasoning_effort"] = config.reasoning_effort
            # DeepSeek 兼容：需额外传递 thinking 开关（OpenAI 标准模型会忽略此字段）
            body["thinking"] = {"type": "enabled"}
        return url, headers, body

    def _active_response_check_cancel(self, cancel_event) -> bool:
        """Clean up active response and check if user requested cancellation.

        Returns True if caller should return silently (user cancelled).
        Returns False if caller should continue (raise as normal error).
        """
        self._active_response = None
        return bool(cancel_event and cancel_event.is_set())

    # ── 流式请求 ────────────────────────────────────────────────

    def stream_chat_completion(
        self,
        config: LLMRequestConfig,
        messages: list,
        cancel_event: Optional[threading.Event] = None,
    ):
        """SSE streaming. Yields StreamEvent instances.

        Parameters
        ----------
        config : LLMRequestConfig
            请求配置（base_url、api_key、model 等）。
        messages : list
            对话消息列表。
        cancel_event : threading.Event, optional
            取消事件。

        Yields
        ------
        StreamEvent
            文本增量、推理增量或工具调用事件。
        """
        url, headers, body = self._build_request(config, messages, stream=True)

        try:
            with self._session.post(
                url,
                json=body,
                headers=headers,
                stream=True,
                timeout=(self._connect_timeout, config.timeout),
            ) as response:
                self._active_response = response
                response.raise_for_status()
                response.encoding = "utf-8"

                for event in parse_sse_events(response, cancel_event):
                    yield event

            self._active_response = None

        except requests.exceptions.Timeout:
            if self._active_response_check_cancel(cancel_event):
                return
            raise _LLMHttpError(f"请求超时（{config.timeout}秒）")
        except requests.exceptions.ConnectionError as e:
            if self._active_response_check_cancel(cancel_event):
                return
            raise _LLMHttpError(f"网络连接失败：{e}")
        except requests.exceptions.HTTPError as e:
            if self._active_response_check_cancel(cancel_event):
                return
            status = e.response.status_code if e.response is not None else "?"
            try:
                err_body = e.response.json() if e.response is not None else {}
                err_msg = err_body.get("error", {}).get("message", str(e))
            except Exception:
                err_msg = str(e)
            raise _LLMHttpError(f"HTTP {status}: {err_msg}")
        except requests.exceptions.RequestException as e:
            if self._active_response_check_cancel(cancel_event):
                return
            raise _LLMHttpError(f"请求异常：{e}")

    def cancel_active_request(self):
        """Close the active HTTP response to unblock the SSE streaming thread.
        If no response exists yet (blocked in connect phase), close the
        entire connection pool to interrupt the connect attempt."""
        if self._active_response is not None:
            self._active_response.close()
            self._active_response = None
        else:
            self._session.close()
            self._session = requests.Session()
            self._init_session_retry()

    # ── 同步请求 ────────────────────────────────────────────────

    def sync_chat_completion(
        self,
        config: LLMRequestConfig,
        messages: list,
    ) -> dict:
        """Non-streaming chat completion. Returns the full assistant message dict.

        The returned dict contains at least:
          - "content": str or None (text response)
          - "role": "assistant"
        If the model responds with tool calls, the dict also contains:
          - "tool_calls": list of ToolCall dicts

        Pass ``tools`` and ``tool_choice`` in ``config`` to enable function calling.

        Parameters
        ----------
        config : LLMRequestConfig
            请求配置。
        messages : list
            对话消息列表。

        Returns
        -------
        dict
            完整的 assistant message dict，可直接追加到 messages 列表。
        """
        url, headers, body = self._build_request(config, messages, stream=False)

        try:
            resp = self._session.post(url, json=body, headers=headers, timeout=config.timeout)
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content")
            if not content:
                fr = data["choices"][0].get("finish_reason", "?")
                print(f"[sync_chat] API 返回 content 为空 (finish_reason={fr})", flush=True)
                if data.get("usage"):
                    print(f"[sync_chat] usage: {data['usage']}", flush=True)
            return msg
        except requests.exceptions.RequestException as e:
            raise _LLMHttpError(f"请求异常：{e}")
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise _LLMHttpError(f"同步请求解析失败：{e}")
