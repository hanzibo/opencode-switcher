"""
event_types.py — Typed event models for the LLM streaming pipeline.

Defines the contract between llm_client → ai_tool_loop → ai_chat_panel.
Aligns conceptually with the AG-UI protocol's event types
(TextMessageContent, ReasoningMessage, ToolCallStart/End).

All events are immutable dataclasses (frozen=True) to prevent
accidental mutation across thread boundaries.
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, List


class StreamEventType(Enum):
    """Types of events emitted by the LLM streaming client."""
    TEXT_DELTA = auto()             # 普通文本 token 增量
    REASONING_DELTA = auto()        # 推理/思考文本 token 增量
    TOOL_CALLS = auto()             # 完整的工具调用（在 finish_reason="tool_calls" 时发出）
    STREAM_END = auto()             # 流结束标记（[DONE] 或 finish_reason="stop"）


@dataclass(frozen=True)
class ToolCallData:
    """A single tool/function call within a StreamEvent.

    Corresponds to one element in OpenAI's ``tool_calls`` array.
    ``id`` and ``name`` are guaranteed non-empty in a complete event.
    ``arguments`` is a JSON string; callers should parse with ``json.loads()``.
    """
    id: str
    name: str
    arguments: str  # JSON string, e.g. '{"query": "hello"}'


@dataclass(frozen=True)
class StreamEvent:
    """A single typed event yielded by ``stream_chat_completion()``.

    Only one of ``text_delta``, ``reasoning_delta``, or ``tool_calls``
    is non-None per event, determined by ``type``.

    Usage::

        for event in client.stream_chat_completion(...):
            if event.type == StreamEventType.TEXT_DELTA:
                assistant_text += event.text_delta
            elif event.type == StreamEventType.REASONING_DELTA:
                reasoning_text += event.reasoning_delta
            elif event.type == StreamEventType.TOOL_CALLS:
                handle_tool_calls(event.tool_calls)
            elif event.type == StreamEventType.STREAM_END:
                break
    """
    type: StreamEventType
    text_delta: Optional[str] = None
    reasoning_delta: Optional[str] = None
    tool_calls: Optional[List[ToolCallData]] = None


# ── 快捷构造器 ──

def text_delta(content: str) -> StreamEvent:
    """创建文本增量事件。"""
    return StreamEvent(type=StreamEventType.TEXT_DELTA, text_delta=content)


def reasoning_delta(content: str) -> StreamEvent:
    """创建推理文本增量事件。"""
    return StreamEvent(type=StreamEventType.REASONING_DELTA, reasoning_delta=content)


def tool_calls_event(calls: List[ToolCallData]) -> StreamEvent:
    """创建工具调用事件。"""
    return StreamEvent(type=StreamEventType.TOOL_CALLS, tool_calls=calls)


def stream_end() -> StreamEvent:
    """创建流结束事件。"""
    return StreamEvent(type=StreamEventType.STREAM_END)


# ── 工具函数 ──

def parse_tool_call_from_dict(raw: dict) -> ToolCallData:
    """从 OpenAI 格式的 tool_call dict 解析为 ToolCallData。

    OpenAI 格式示例::
        {
            "id": "call_abc123",
            "type": "function",
            "function": {"name": "web_search", "arguments": '{"query":"hello"}'}
        }
    """
    func = raw.get("function", {})
    return ToolCallData(
        id=raw.get("id", ""),
        name=func.get("name", ""),
        arguments=func.get("arguments", "{}"),
    )


def tool_call_to_dict(tc: ToolCallData) -> dict:
    """将 ToolCallData 转回 OpenAI 格式 dict（与现有 ChatMessage.tool_calls 兼容）。"""
    return {
        "id": tc.id,
        "type": "function",
        "function": {
            "name": tc.name,
            "arguments": tc.arguments,
        },
    }


# ── 调试辅助 ──

_DEBUG_EVENTS = False


def set_debug_events(enabled: bool):
    """启用/禁用流式事件的 stderr 日志。"""
    global _DEBUG_EVENTS
    _DEBUG_EVENTS = enabled


def log_event(event: StreamEvent, context: str = ""):
    """打印事件摘要到 stderr（仅 _DEBUG_EVENTS=True 时）。"""
    if not _DEBUG_EVENTS:
        return
    import sys
    type_name = event.type.name
    summary = ""
    if event.text_delta:
        summary = f"text={event.text_delta[:50]!r}"
    elif event.reasoning_delta:
        summary = f"reasoning={event.reasoning_delta[:50]!r}"
    elif event.tool_calls:
        names = [tc.name for tc in event.tool_calls]
        summary = f"tools={names}"
    print(f"[StreamEvent] {context} {type_name} {summary}", file=sys.stderr, flush=True)
