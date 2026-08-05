import json
import logging
import re
import threading
import requests
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Generator

from stores.clipboard_store import (
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
from system.event_types import (
    StreamEvent, StreamEventType, ToolCallData,
    text_delta, reasoning_delta, tool_calls_event, stream_end,
    parse_tool_call_from_dict,
)


def _extract_http_error_details(e: Exception) -> str:
    """Extract detailed status code and error message from a requests exception."""
    resp = getattr(e, "response", None)
    status = resp.status_code if resp is not None else "?"

    if resp is None:
        return f"HTTP {status}: {e}"

    # Try parsing JSON error object
    try:
        data = resp.json()
        if isinstance(data, dict):
            err_obj = data.get("error")
            if isinstance(err_obj, dict):
                msg = err_obj.get("message")
                if msg:
                    return f"HTTP {status}: {msg}"
            elif isinstance(err_obj, str) and err_obj:
                return f"HTTP {status}: {err_obj}"

            msg = data.get("message") or data.get("detail")
            if msg:
                return f"HTTP {status}: {msg}"
    except Exception:
        pass

    # Try reading text response (strip HTML tags or limit length)
    if resp.text:
        text = resp.text.strip()
        if text.startswith("<html") or text.startswith("<!DOCTYPE"):
            m = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE)
            if m:
                text = m.group(1).strip()
            else:
                text = re.sub(r"<[^>]+>", " ", text)[:200].strip()
        elif len(text) > 300:
            text = text[:300] + "..."
        return f"HTTP {status}: {text}"

    return f"HTTP {status}: {e}"


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


def extract_reasoning_content(msg: dict) -> Optional[str]:
    """兼容 DeepSeek (reasoning_content) 与 MiMo (reasoning) 的思考内容提取。

    作为 thinking 模式 rc 字段兼容映射的单一事实来源：SSE 解析、请求体构建、
    子代理消息组装均复用本函数，避免键兼容策略在多处重复维护。
    """
    return msg.get("reasoning_content") or msg.get("reasoning")


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
        """Accumulate a tool_calls delta chunk from the SSE stream.

        兼容两种 chunk 风格（修复 Ling-3.0-flash 等兼容实现的流中断）：
        - OpenAI 标准：后续 chunk 省略 id/name/arguments 键
        - 部分兼容实现：显式发送 null 值（{"id": null, "name": null}）
        两种情况下均须跳过 None/非 str 值，避免 str + None 的 TypeError
        中断整个流。非 dict 元素（如 tool_calls 数组中的 null）整体跳过。
        """
        if not isinstance(delta, dict):
            return
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
        # 值判空 + 类型守卫：{"id": null} 或非 str 值均跳过拼接（🟡-1/🟡-2）
        val = delta.get("id")
        if isinstance(val, str) and val:
            call["id"] += val
        fn = delta.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str) and name:
                call["function"]["name"] += name
            args = fn.get("arguments")
            if isinstance(args, str) and args:
                call["function"]["arguments"] += args

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

            # 逐层防御（🟡-1）：chunk / choices / 首元素 / delta 显式 null 均不中断流
            if not isinstance(chunk, dict):
                continue
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            first = choices[0]
            if not isinstance(first, dict):
                continue
            delta = first.get("delta") or {}
            finish_reason = first.get("finish_reason")

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

            reasoning = extract_reasoning_content(delta)
            if reasoning:
                yield reasoning_delta(reasoning)
    except Exception as e:
        # 用户取消导致响应被关闭（cancel_active_request 并发 close）时，
        # 读取中断属预期行为，静默收尾而非误报"流读取中断"（🟡-4）
        if cancel_event and cancel_event.is_set():
            return
        logger.error("SSE 流读取异常: %s", e)
        raise _LLMHttpError(f"SSE 流读取中断: {e}")

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

# 取消逻辑区分"键不存在"与"键存在但值未就绪（连接阶段占位）"的哨兵
_MISSING = object()


class _LLMHttpClient:
    """LLM HTTP 客户端。

    封装与 OpenAI-compatible API 的 HTTP 通信。
    支持流式与非流式两种模式。

    并发安全：活动流式响应按 ``request_key`` 分别登记在
    ``_active_responses`` 字典中（连接阶段暂存 None 占位），取消与清理均
    按键操作，互不覆盖。未传键的旧调用方自动获得唯一键。
    """

    def __init__(self):
        self._session = requests.Session()
        # request_key -> 活动流式响应；连接阶段（response 尚未返回）暂存 None 占位
        self._active_responses: Dict[Any, Any] = {}
        self._lock = threading.Lock()
        self._next_auto_key = 0
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

    def _new_request_key(self) -> Any:
        """为未显式指定 request_key 的流式调用生成唯一键。

        ("auto", n) 元组而非整型，避免与调用方显式传入的字符串键冲突。
        """
        with self._lock:
            self._next_auto_key += 1
            return ("auto", self._next_auto_key)

    def _swap_session_locked(self) -> requests.Session:
        """持有 _lock 时调用：发布全新 session 并返回旧 session（须在锁外关闭）。

        切换与取消决策在同一临界区内完成，保证重建之后注册的任何新请求
        必然引用新 session，不会被旧 session 的关闭误伤。
        """
        old = self._session
        self._session = requests.Session()
        self._init_session_retry()
        return old

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
        base_url = (config.base_url or "").strip().rstrip("/")
        if base_url.endswith("/chat/completions"):
            url = base_url
        else:
            url = base_url + "/chat/completions"

        api_key = (config.api_key or "").strip()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "OpenCodeSwitcher/1.0 (Linux; GTK3)",
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
                else:
                    msg["content"] = content or ""
                # 无论是否带 tool_calls 均回传 rc——thinking 模式下 API 要求
                # 工具调用轮必须全量回传，非工具轮回传会被忽略（官方文档确认无害）
                rc = extract_reasoning_content(m)
                if rc:
                    msg["reasoning_content"] = rc
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
        """Check if user requested cancellation.

        Returns True if caller should return silently (user cancelled).
        Returns False if caller should continue (raise as normal error).

        活动响应清理统一由 ``stream_chat_completion`` 的 finally 按 request_key
        完成；此处不再触碰状态字典，避免并发下误清其他请求。
        """
        return bool(cancel_event and cancel_event.is_set())

    # ── 流式请求 ────────────────────────────────────────────────

    def stream_chat_completion(
        self,
        config: LLMRequestConfig,
        messages: list,
        cancel_event: Optional[threading.Event] = None,
        request_key: Any = None,
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
        request_key : hashable, optional
            本请求的标识键。同一客户端上的并发流必须使用不同键，
            否则取消/清理会互相覆盖。省略时自动生成唯一键，旧调用方无需改动。

        Yields
        ------
        StreamEvent
            文本增量、推理增量或工具调用事件。
        """
        url, headers, body = self._build_request(config, messages, stream=True)
        key = request_key if request_key is not None else self._new_request_key()

        try:
            with self._lock:
                if key in self._active_responses:
                    raise ValueError(
                        f"request_key {key!r} 已被占用：同一键不可并发复用"
                    )
                # 连接阶段占位：response 尚未返回，取消时据此判断是否需重建 session
                self._active_responses[key] = None
                # 锁内捕获 session：session 重建后新注册的请求立即绑定新会话
                session = self._session

            with session.post(
                url,
                json=body,
                headers=headers,
                stream=True,
                timeout=(self._connect_timeout, config.timeout),
            ) as response:
                with self._lock:
                    self._active_responses[key] = response
                response.raise_for_status()
                response.encoding = "utf-8"

                for event in parse_sse_events(response, cancel_event):
                    yield event

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
            err_msg = _extract_http_error_details(e)
            raise _LLMHttpError(err_msg)
        except requests.exceptions.RequestException as e:
            if self._active_response_check_cancel(cancel_event):
                return
            err_msg = _extract_http_error_details(e) if getattr(e, "response", None) is not None else str(e)
            raise _LLMHttpError(f"请求异常：{err_msg}")
        finally:
            # 任何路径（正常结束/异常/GeneratorExit/取消）均按 key 清理本请求的
            # 悬挂引用，绝不误清其他并发请求的响应（🟡-3 并发安全版）
            with self._lock:
                self._active_responses.pop(key, None)

    def cancel_active_request(self, request_key: Any = None):
        """取消一个或多个活动的流式请求。

        Parameters
        ----------
        request_key : hashable, optional
            仅取消该键对应的流式请求：关闭其 HTTP 响应以解除 SSE 读取阻塞。
            若目标请求仍处于连接阶段（尚无响应对象）且没有其他活动请求，
            则重建 session 来中断其连接；存在其他活动请求时不重建，避免误伤
            并发流。session 切换与"无其他活动请求"判定在同一临界区内完成，
            因此重建之后新注册的请求必然拿到新 session。
            省略时取消所有活动流（旧语义的超集，原先只关闭单个响应）。
        """
        to_close = []
        old_session = None

        with self._lock:
            if request_key is not None:
                resp = self._active_responses.pop(request_key, _MISSING)
                if resp is _MISSING:
                    return  # 该请求已结束或键不存在：无操作
                if resp is not None:
                    to_close.append(resp)
                elif not self._active_responses:
                    # 目标仍在连接阶段且无其他活动请求：可安全重建 session
                    old_session = self._swap_session_locked()
            else:
                for k in list(self._active_responses.keys()):
                    resp = self._active_responses.pop(k)
                    if resp is not None:
                        to_close.append(resp)
                if not self._active_responses:
                    old_session = self._swap_session_locked()

        for resp in to_close:
            try:
                resp.close()
            except Exception as e:
                logger.warning("关闭流式响应失败 (request_key=%r): %s", request_key, e)
        if old_session is not None:
            try:
                old_session.close()
            except Exception as e:
                logger.warning("关闭旧 HTTP session 失败: %s", e)

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
            if not content and not msg.get("tool_calls"):
                fr = data["choices"][0].get("finish_reason", "?")
                print(f"[sync_chat] API 返回 content 为空 (finish_reason={fr})", flush=True)
                if data.get("usage"):
                    print(f"[sync_chat] usage: {data['usage']}", flush=True)
            return msg
        except requests.exceptions.RequestException as e:
            err_msg = _extract_http_error_details(e) if getattr(e, "response", None) is not None else str(e)
            raise _LLMHttpError(f"请求异常：{err_msg}")
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise _LLMHttpError(f"同步请求解析失败：{e}")
