import json
import threading
import requests
from typing import Optional, List, Dict, Any

from clipboard_store import (
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TOP_P,
)
from ai_text_utils import (
    _model_supports_vision,
    _vision_content_to_text,
    _cached_image_to_data_uri,
)
import tool_registry
from event_types import (
    StreamEvent, StreamEventType, ToolCallData,
    text_delta, reasoning_delta, tool_calls_event, stream_end,
    parse_tool_call_from_dict,
)


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


class _LLMHttpError(Exception):
    pass


class _LLMHttpClient:
    def __init__(self):
        self._session = requests.Session()
        self._active_response = None
        self._connect_timeout = 4
        self._init_session_retry()

    def _init_session_retry(self):
        """Configure the session with retry strategy and mount adapters."""
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

    def _build_request(self, base_url: str, api_key: str, model_name: str, messages: list,
                       stream: bool, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS,
                       top_p: float = DEFAULT_TOP_P,
                       tools: Optional[list] = None,
                       tool_choice: Optional[str] = None,
                       extra_system_messages: Optional[list] = None):
        url = base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        cleaned_messages = []
        # 注入额外的 system 消息（如历史摘要），仅在 HTTP 请求层生效，不污染原始消息列表
        if extra_system_messages:
            for extra_msg in extra_system_messages:
                cleaned_messages.append({
                    "role": "system",
                    "content": extra_msg.get("content", ""),
                })
        for m in messages:
            role = m.get("role")
            content = m.get("content")
            msg = {"role": role}

            # Multimodal content (list of content parts for vision/audio) → resolve hash / downcast if model has no vision
            if isinstance(content, list):
                if not _model_supports_vision(model_name):
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
                    if content:
                        msg["content"] = content
                    else:
                        msg["content"] = None
                    # 思考模式下工具调用轮次必须回传 reasoning_content，否则 API 返回 400
                    rc = m.get("reasoning_content")
                    if rc:
                        msg["reasoning_content"] = rc
                else:
                    msg["content"] = content or ""
            elif role == "tool":
                msg["content"] = content or ""
                msg["tool_call_id"] = m.get("tool_call_id") or ""
            else:
                msg["content"] = content or ""
            cleaned_messages.append(msg)

        body = {
            "model": model_name,
            "messages": cleaned_messages,
            "stream": stream,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or tool_registry.TOOL_CHOICE_AUTO
        return url, headers, body

    def _active_response_check_cancel(self, cancel_event) -> bool:
        """Clean up active response and check if user requested cancellation.

        Returns True if caller should return silently (user cancelled).
        Returns False if caller should continue (raise as normal error).
        """
        self._active_response = None
        return bool(cancel_event and cancel_event.is_set())

    def stream_chat_completion(
        self,
        base_url: str,
        api_key: str,
        model_name: str,
        messages: list,
        timeout: int = 30,
        cancel_event: Optional[threading.Event] = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        top_p: float = DEFAULT_TOP_P,
        tools: Optional[list] = None,
        tool_choice: Optional[str] = None,
        extra_system_messages: Optional[list] = None,
    ):
        """SSE streaming. Yields StreamEvent instances.

        Each yielded event has a ``type`` field (StreamEventType enum) and
        exactly one of ``text_delta``, ``reasoning_delta``, or ``tool_calls``
        populated (determined by ``type``).

        For tool_calls, this method accumulates SSE chunks internally via
        ``_ToolCallAccumulator`` and yields a single ``TOOL_CALLS`` event
        with all accumulated calls when the stream signals tool_calls finish.
        """
        url, headers, body = self._build_request(
            base_url, api_key, model_name, messages, stream=True,
            temperature=temperature, max_tokens=max_tokens, top_p=top_p,
            tools=tools, tool_choice=tool_choice,
            extra_system_messages=extra_system_messages,
        )

        try:
            with self._session.post(
                url,
                json=body,
                headers=headers,
                stream=True,
                timeout=(self._connect_timeout, timeout),
            ) as response:
                self._active_response = response
                response.raise_for_status()
                response.encoding = "utf-8"

                # Accumulator for incremental tool_calls delta
                tc_accum = _ToolCallAccumulator()

                for line in response.iter_lines(decode_unicode=True):
                    if cancel_event and cancel_event.is_set():
                        response.close()
                        return
                    if not line:
                        continue
                    if line.startswith("data:"):
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
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                        except (json.JSONDecodeError, IndexError, KeyError):
                            continue

                        tc_delta = delta.get("tool_calls")
                        if tc_delta:
                            for tcd in tc_delta:
                                tc_accum.add_delta(tcd)
                            finish_reason = chunk.get("choices", [{}])[0].get("finish_reason")
                            if finish_reason == "tool_calls":
                                calls = tc_accum.get_calls()
                                if calls:
                                    tc_accum.clear()
                                    typed_calls = [parse_tool_call_from_dict(c) for c in calls]
                                    yield tool_calls_event(typed_calls)
                            continue

                        finish_reason = chunk.get("choices", [{}])[0].get("finish_reason")
                        if finish_reason == "tool_calls":
                            calls = tc_accum.get_calls()
                            if calls:
                                tc_accum.clear()
                                typed_calls = [parse_tool_call_from_dict(c) for c in calls]
                                yield tool_calls_event(typed_calls)
                            continue

                        content = delta.get("content")
                        reasoning = delta.get("reasoning_content")
                        if content is not None:
                            yield text_delta(content)
                        if reasoning is not None:
                            yield reasoning_delta(reasoning)

                # Fallback: if loop exits naturally without [DONE] message, yield remaining tool_calls
                calls = tc_accum.get_calls()
                if calls:
                    typed_calls = [parse_tool_call_from_dict(c) for c in calls]
                    yield tool_calls_event(typed_calls)

            self._active_response = None

        except requests.exceptions.Timeout:
            if self._active_response_check_cancel(cancel_event):
                return
            raise _LLMHttpError(f"请求超时（{timeout}秒）")
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

    def sync_chat_completion(
        self,
        base_url: str,
        api_key: str,
        model_name: str,
        messages: list,
        timeout: int = 15,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        top_p: float = DEFAULT_TOP_P,
        tools: Optional[list] = None,
        tool_choice: Optional[str] = None,
        extra_system_messages: Optional[list] = None,
    ) -> dict:
        """Non-streaming chat completion. Returns the full assistant message dict.

        The returned dict contains at least:
          - "content": str or None (text response)
          - "role": "assistant"
        If the model responds with tool calls, the dict also contains:
          - "tool_calls": list of ToolCall dicts

        Pass ``tools`` and ``tool_choice`` to enable function calling.
        """
        url, headers, body = self._build_request(
            base_url, api_key, model_name, messages, stream=False,
            temperature=temperature, max_tokens=max_tokens, top_p=top_p,
            tools=tools, tool_choice=tool_choice,
            extra_system_messages=extra_system_messages,
        )

        try:
            resp = self._session.post(url, json=body, headers=headers, timeout=timeout)
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
