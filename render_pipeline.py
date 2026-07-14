"""
render_pipeline.py — AI 对话三区渲染编排引擎

职责：
  将"一轮 AI 响应"中的三种内容（推理过程、工具调用、最终回答）
  编排为前端可消费的 HTML，消除 ai_chat_panel.py 中的 5 处重复。

依赖：
  - ai_text_utils 中的纯渲染函数（_render_reasoning_html 等）
  - clipboard_store 中的 ChatMessage 类型

使用方式：
  from render_pipeline import render_turn, TurnRenderInput, TurnRenderOutput

  output = render_turn(TurnRenderInput(
      turn_messages=turn_msgs,
      all_messages=self._ai_messages,
      streaming_reasoning=st.get("current_reasoning_text", ""),
      streaming_content=st.get("current_assistant_text", ""),
      is_streaming=True,
  ))
  # output.combined_html  → 可直接传给 updateMessageContainer
  # output.is_split       → 控制 JS 渲染模式
"""

from dataclasses import dataclass
from typing import List, Dict, Optional
import json

# ── 依赖 ai_text_utils 中的渲染函数 ──────────────────────────────
from ai_text_utils.markdown import _markdown_to_html_safe
from ai_text_utils.render import (
    _rebuild_markdown_from_messages,
    _render_reasoning_html,
    _render_tool_steps_html,
    _render_answer_html,
)


# ═══════════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════════


@dataclass
class TurnRenderInput:
    """渲染"一轮"AI 对话所需的所有数据。

    一轮 = 从最后一条 user 消息到当前最新消息之间的所有消息。
    包含助理的推理过程、工具调用和最终回答。

    Attributes:
        turn_messages: 当前轮的消息列表（不含 user 消息本身）。
        all_messages:  完整的对话消息列表（用于跨消息查找 tool result）。
        streaming_reasoning: 流式推理文本的当前累积值（非流式时为空字符串）。
        streaming_content:   流式回答文本的当前累积值（非流式时为空字符串）。
        is_streaming:  是否为流式渲染（影响推理区域的 open 属性和渲染行为）。
    """
    turn_messages: List[Dict]
    all_messages: Optional[List[Dict]] = None
    streaming_reasoning: str = ""
    streaming_content: str = ""
    is_streaming: bool = False

    def __post_init__(self):
        """自动兜底：如果没有传入 all_messages，则复用 turn_messages。"""
        if self.all_messages is None:
            self.all_messages = self.turn_messages


@dataclass
class TurnRenderOutput:
    """渲染输出：三个子区域的 HTML 及元数据。

    Attributes:
        combined_html: 三区拼接后的完整 HTML（可直接传给 JS updateMessageContainer）。
        reasoning_html: 推理区域的独立 HTML。
        tool_html:      工具调用区域的独立 HTML。
        answer_html:    回答区域的独立 HTML。
        is_split:      控制 JS 端渲染模式。
                       - True:  全量替换容器内容（ask_user_question 路径）。
                       - False: 三区增量更新（标准路径）。
        has_ask_question: 本轮是否包含 ask_user_question。
    """
    combined_html: str
    reasoning_html: str = ""
    tool_html: str = ""
    answer_html: str = ""
    is_split: bool = False
    has_ask_question: bool = False
    raw_markdown: str = ""


# ═══════════════════════════════════════════════════════════════════
# 核心判断逻辑（从 ai_chat_panel.py 提取，保持语义一致）
# ═══════════════════════════════════════════════════════════════════


def contains_ask_user_question(turn_msgs: List[Dict]) -> bool:
    """检查给定的消息列表中是否包含 ask_user_question 相关的工具调用或响应。

    语义完全同 ai_chat_panel.py 中的 _contains_ask_user_question()。
    这是一个纯函数，不依赖 self。
    """
    for msg in turn_msgs:
        if msg.get("role") == "tool" and msg.get("name") == "ask_user_question":
            return True
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []) or []:
                if tc.get("function", {}).get("name") == "ask_user_question":
                    return True
    return False


# ═══════════════════════════════════════════════════════════════════
# 三区渲染引擎
# ═══════════════════════════════════════════════════════════════════


def render_turn(input_data: TurnRenderInput) -> TurnRenderOutput:
    """主入口：根据输入数据选择渲染路径，返回三区 HTML。

    两条路径：
      1. ask_user_question 路径 → 全量 Markdown 渲染（is_split=True）
      2. 标准路径              → 三区独立渲染（is_split=False）

    参数:
        input_data: 渲染需要的所有输入数据（封装为 dataclass）。

    返回:
        TurnRenderOutput 包含拼接好的 HTML 和元数据。
    """
    if contains_ask_user_question(input_data.turn_messages):
        return _render_ask_question_mode(input_data)
    return _render_standard_mode(input_data)


def _render_standard_mode(input_data: TurnRenderInput) -> TurnRenderOutput:
    """标准三区渲染模式。

    - reasoning 区域：渲染推理内容 + 流式文本
    - tool 区域：渲染工具调用步骤（含结果）
    - answer 区域：渲染最终回答 + 流式文本

    JS 端会分别更新三个 .bubble-region，无需全量替换。
    """
    all_msgs = input_data.all_messages
    turn_msgs = input_data.turn_messages

    reasoning_html = _render_reasoning_html(
        turn_msgs,
        streaming_reasoning=input_data.streaming_reasoning,
        is_streaming=input_data.is_streaming,
    )
    tool_html = _render_tool_steps_html(turn_msgs, all_msgs)
    answer_html = _render_answer_html(
        turn_msgs,
        streaming_content=input_data.streaming_content,
    )

    combined = f"{reasoning_html}{tool_html}{answer_html}"

    return TurnRenderOutput(
        combined_html=combined,
        reasoning_html=reasoning_html,
        tool_html=tool_html,
        answer_html=answer_html,
        is_split=False,
        has_ask_question=False,
    )


def _render_ask_question_mode(input_data: TurnRenderInput) -> TurnRenderOutput:
    """ask_user_question 渲染模式。

    当 LLM 调用 ask_user_question 工具时，需要将整个轮次的内容
    作为全量 Markdown 渲染（因为工具调用的格式在 Markdown 中更易处理）。

    JS 端会直接替换整个容器的 innerHTML。
    """
    rebuilt = _rebuild_markdown_from_messages(
        input_data.turn_messages,
        streaming_reasoning=input_data.streaming_reasoning,
        streaming_content=input_data.streaming_content,
        is_streaming=input_data.is_streaming,
    )
    html = _markdown_to_html_safe(rebuilt, fallback_content="")

    return TurnRenderOutput(
        combined_html=html,
        is_split=True,
        has_ask_question=True,
        raw_markdown=rebuilt,
    )


# ═══════════════════════════════════════════════════════════════════
# 辅助函数：生成 JS 调用字符串（减少 ai_chat_panel.py 中的样板代码）
# ═══════════════════════════════════════════════════════════════════


def build_update_js(msg_id: str, output: TurnRenderOutput) -> str:
    """生成调用 JS updateMessageContainer 的代码字符串。

    参数:
        msg_id:  消息容器的 DOM ID（格式 "msg-{req_id}"）。
        output:  render_turn() 的返回结果。

    返回:
        可直接传给 webview.run_javascript() 的 JS 字符串。
    """
    return (
        f"updateMessageContainer("
        f"'{msg_id}', "
        f"{json.dumps(output.combined_html)}, "
        f"{json.dumps(output.is_split)}"
        f");"
    )


# ═══════════════════════════════════════════════════════════════════
# 调试辅助
# ═══════════════════════════════════════════════════════════════════

# 全局开关：设置为 True 时，每次渲染都会打印摘要到 stderr
_DEBUG_RENDER_PIPELINE = False


def set_debug(enabled: bool):
    """启用/禁用渲染管线的调试输出。

    启用后，每次 render_turn() 调用都会在 stderr 打印：
      - 输入摘要（turn_messages 数量、streaming 长度）
      - 渲染路径（standard / ask_question）
      - 输出统计（各区域 HTML 长度）
    """
    global _DEBUG_RENDER_PIPELINE
    _DEBUG_RENDER_PIPELINE = enabled


def _debug_log(input_data: TurnRenderInput, output: TurnRenderOutput):
    """打印渲染调试信息到 stderr。"""
    if not _DEBUG_RENDER_PIPELINE:
        return
    import sys
    mode = "ask_question" if output.has_ask_question else "standard"
    print(
        f"[render_pipeline] mode={mode} "
        f"msgs={len(input_data.turn_messages)} "
        f"reasoning={len(input_data.streaming_reasoning)}ch "
        f"content={len(input_data.streaming_content)}ch "
        f"streaming={input_data.is_streaming} "
        f"html_out={len(output.combined_html)}ch "
        f"reasoning_html={len(output.reasoning_html)}ch "
        f"tool_html={len(output.tool_html)}ch "
        f"answer_html={len(output.answer_html)}ch",
        file=sys.stderr, flush=True,
    )


def render_turn_with_debug(input_data: TurnRenderInput) -> TurnRenderOutput:
    """带调试输出的 render_turn 版本（可通过 set_debug() 控制）。"""
    output = render_turn(input_data)
    _debug_log(input_data, output)
    return output
