"""Conversation turn rendering utilities.

Pure functions for rendering AI conversation turns into HTML.
Handles three content regions (reasoning, tool calls, answer)
and full conversation markdown rebuild.

Zero GTK dependency. Depends on markdown and cleanup modules,
plus project-level tool_registry and clipboard_store.
"""

import re
import html
import json
from typing import Optional, List, Dict

import tool_registry
from .markdown import _markdown_to_html_safe
from .cleanup import (
    _strip_ai_markup, _preserve_newlines,
    USER_AVATAR_HTML, ASSISTANT_AVATAR_HTML,
)
from .image import _vision_content_to_markdown
from .cleanup import _close_unclosed_code_blocks


# Per-tool: maps tool name to the argument field shown in summary line
_TOOL_DISPLAY_FIELD = {
    "read_file": "path",
    "write_file": "path",
    "edit_file": "path",
    "delete_file": "path",
    "list_directory": "path",
    "file_info": "path",
    "get_code_metrics": "path",
    "find_project_dependencies": "path",
    "parse_file_ast": "path",
    "grep_search": "path",
    "glob_find": "path",
    "web_search": "query",
    "web_fetch": "url",
}


def _render_tool_step(tool_call: dict, tool_result_msg: Optional[dict] = None,
                       show_details: bool = True) -> str:
    func = tool_call.get("function", {})
    name = func.get("name", "unknown")
    arguments_str = func.get("arguments", "{}")
    tc_id = tool_call.get("id", "")

    display_field = _TOOL_DISPLAY_FIELD.get(name)

    # Try to parse arguments for prettier display
    try:
        args = json.loads(arguments_str)
        # Filter out fields shown in summary line
        filter_keys = {"purpose"}
        if display_field:
            filter_keys.add(display_field)
        display_args = {k: v for k, v in args.items() if k not in filter_keys}
        args_display = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in display_args.items())
    except Exception:
        args = {}
        args_display = arguments_str

    # Generic: extract purpose from any tool's arguments for display
    purpose = args.get("purpose", "")

    if name == "ask_user_question":
        question = args.get("question", "") if isinstance(args, dict) else ""
        if not question:
            question = purpose or arguments_str
        rendered_question = _markdown_to_html_safe(question)
        if tool_result_msg:
            footer_text = "✏️ 已回答"
        else:
            footer_text = "✏️ 在下方输入框中回答，或输入 /cancel 取消"

        return (
            f'<div class="tool-ask-user">\n'
            f'<div class="tool-ask-user-header">💬 Agent 需要确认</div>\n'
            f'<div class="tool-ask-user-body">{rendered_question}</div>\n'
            f'<div class="tool-ask-user-footer">{footer_text}</div>\n'
            f'</div>\n'
            f'<!-- tool-step-marker -->\n'
        )

    # Extract per-tool display value (e.g., file path for read_file)
    display_value = args.get(display_field, "") if display_field else ""

    tool_icons = {
        "bash": "🖥️",
        "web_search": "🌐",
        "web_fetch": "📖",
        "read_file": "📄",
        "write_file": "✍️",
        "edit_file": "✏️",
        "delete_file": "🗑️",
        "list_directory": "📁",
        "glob_find": "🔍",
        "grep_search": "🔎",
        "get_current_time": "⏰",
        "todo_create": "📋",
        "todo_update": "📋",
        "todo_list": "📋",
        "sub_agent": "🤖",
        "get_subagent_status": "🤖",
        "read_qq_mail": "📧",
        "get_code_metrics": "📊",
        "find_project_dependencies": "🔗",
        "parse_file_ast": "📦"
    }
    icon = tool_icons.get(name, "⚙️")

    # Status and result
    status_icon = "✅"
    result_html = ""

    if tool_result_msg:
        content = tool_result_msg.get("content", "")
        if content.strip() == tool_registry.TOOL_CANCELLED:
            status_icon = "⚠️"
        elif content.strip().startswith(tool_registry.ERROR_PREFIXES):
            status_icon = "❌"
    else:
        status_icon = '<span class="tool-step-status running">🔄</span>'

    purpose_html = f'<span class="tool-step-purpose">{html.escape(purpose)}</span>\n' if purpose else ""
    display_html = f'<span class="tool-step-purpose">{html.escape(display_value)}</span>\n' if display_value else ""

    # ── 简化模式：无详情，不可展开 ──
    if not show_details:
        return (
            f'<div class="tool-step-simple" data-tool-call-id="{html.escape(tc_id)}">\n'
            f'<span class="tool-step-status">{status_icon}</span>\n'
            f'<strong>调用工具: {icon} {name}</strong>\n'
            f'{purpose_html}'
            f'{display_html}'
            f'</div>\n'
        )

    # ── 完整模式：可展开的 details ──
    if tool_result_msg:
        content = tool_result_msg.get("content", "")
        MAX_DISPLAY = 4000
        display_content = content[:MAX_DISPLAY]
        if len(content) > MAX_DISPLAY:
            display_content += f"\n\n...（结果已截断，共 {len(content)} 字符）"
        safe_content = html.escape(display_content)
        result_html = (
            f'<div class="tool-step-result">\n'
            f'<pre><code>{safe_content}</code></pre>\n'
            f'</div>\n'
        )
    else:
        result_html = '<div class="tool-step-result"><em>正在运行中...</em></div>\n'

    return (
        f'<details class="tool-step-details" data-tool-call-id="{html.escape(tc_id)}">\n'
        f'<summary class="tool-step-summary">\n'
        f'<span class="tool-step-status">{status_icon}</span>\n'
        f'<strong>调用工具: {icon} {name}</strong>\n'
        f'{purpose_html}'
        f'{display_html}'
        f'</summary>\n'
        f'<div class="tool-step-content">\n'
        f'<div class="tool-step-args"><strong>参数:</strong> <code>{html.escape(args_display)}</code></div>\n'
        f'{result_html}'
        f'</div>\n'
        f'</details>\n'
        f'<!-- tool-step-marker -->\n'
    )


def _render_tool_card_standalone(tool_call: dict, result_text: str, status: str = "running",
                                  show_details: bool = True) -> str:
    """渲染单张工具卡片的 HTML（不依赖 turn_messages 上下文）。

    用于增量更新场景：在工具结果到达时，只渲染这一张卡片的新状态。

    Args:
        tool_call: 工具调用的完整 dict（含 id, function.name, function.arguments）
        result_text: 工具执行结果文本
        status: "running" | "success" | "error" | "cancelled"

    Returns:
        工具卡片的完整 HTML 字符串（含 details 结构）
    """
    # _render_tool_step 已根据 tool_result_msg 是否为 None 决定显示"🔄 正在运行中..."还是结果
    tool_result_msg = {"content": result_text} if result_text else None
    return _render_tool_step(tool_call, tool_result_msg, show_details=show_details)


def _render_reasoning_html(
    turn_messages: List[Dict],
    streaming_reasoning: str = "",
    is_streaming: bool = False
) -> str:
    """渲染思考过程区域，输出带 .bubble-region 包裹。"""
    reasoning_parts = []
    for msg in turn_messages:
        if msg.get("role") == "assistant" and msg.get("reasoning_content"):
            reasoning_parts.append(msg["reasoning_content"])
    if streaming_reasoning:
        reasoning_parts.append(streaming_reasoning)

    reasoning_text = "\n".join(reasoning_parts).strip()
    if not reasoning_text:
        return ""
    reasoning_text = _close_unclosed_code_blocks(reasoning_text)
    open_attr = ' open' if is_streaming and streaming_reasoning else ''
    escaped = html.escape(reasoning_text)
    return (
        f'<div class="bubble-region reasoning-region">\n'
        f'<details class="thinking-details"{open_attr}>\n'
        f'<summary class="thinking-summary">💭 Thinking Process</summary>\n'
        f'<div class="thinking-content">{escaped}</div>\n'
        f'</details>\n'
        f'</div>\n\n'
    )


def _render_tool_steps_html(turn_messages: List[Dict], all_messages: Optional[List[Dict]] = None,
                             show_details: bool = True) -> str:
    """渲染工具调用步骤区域，输出带 .bubble-region 包裹。"""
    search_messages = all_messages if all_messages is not None else turn_messages
    tool_results_by_id = {}
    # legacy_tool_results 仅从 turn_messages 收集，避免跨轮次索引错位
    legacy_tool_results = []
    for msg in turn_messages:
        if msg.get("role") == "tool" and not msg.get("tool_call_id"):
            legacy_tool_results.append(msg)
    # ID 匹配集仍从全量消息构建（工具结果可能在后续消息中）
    for msg in search_messages:
        if msg.get("role") == "tool":
            cid = msg.get("tool_call_id")
            if cid:
                tool_results_by_id[cid] = msg

    tool_calls_list = []
    for msg in turn_messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tool_calls_list.append(tc)

    if not tool_calls_list:
        return ""

    steps_list = []
    for i, tc in enumerate(tool_calls_list):
        cid = tc.get("id")
        result_msg = None
        if cid and cid in tool_results_by_id:
            result_msg = tool_results_by_id[cid]
        elif i < len(legacy_tool_results):
            result_msg = legacy_tool_results[i]
        steps_list.append(_render_tool_step(tc, result_msg, show_details=show_details))

    return (
        f'<div class="bubble-region tool-region">\n'
        f'<div class="tool-steps-container">\n'
        f'{"".join(steps_list)}'
        f'</div>\n'
        f'</div>\n\n'
    )


def _render_answer_html(
    turn_messages: List[Dict],
    streaming_content: str = "",
) -> str:
    """渲染 AI 回答内容区域，输出带 .bubble-region 包裹。"""
    content_parts = []
    for msg in turn_messages:
        if msg.get("role") == "assistant" and msg.get("content"):
            content_str = msg["content"]
            content_str = _strip_ai_markup(content_str)
            if content_str.strip():
                content_parts.append(content_str)

    if streaming_content:
        content_parts.append(streaming_content)

    final_content = "\n".join(content_parts).strip()
    if not final_content:
        return ""
    final_content = _close_unclosed_code_blocks(final_content)
    rendered_md = _markdown_to_html_safe(final_content)
    return (
        f'<div class="bubble-region answer-region">\n'
        f'<div class="answer-header">💡 Answer:</div>\n'
        f'{rendered_md}\n'
        f'</div>\n\n'
    )


def _render_active_turn_to_html(
    turn_messages: List[Dict],
    streaming_reasoning: str = "",
    streaming_content: str = "",
    is_streaming: bool = False,
    all_messages: Optional[List[Dict]] = None,
    show_details: bool = True,
) -> str:
    """包装器：组装三个子区域 HTML（向后兼容，返回格式不变）。"""
    reasoning_html = _render_reasoning_html(turn_messages, streaming_reasoning, is_streaming)
    tool_html = _render_tool_steps_html(turn_messages, all_messages, show_details=show_details)
    answer_html = _render_answer_html(turn_messages, streaming_content)
    return f'{reasoning_html}{tool_html}{answer_html}'


def _rebuild_markdown_from_messages(
    messages: List[Dict],
    streaming_reasoning: str = "",
    streaming_content: str = "",
    is_streaming: bool = False,
    show_details: bool = True,
) -> str:
    """Convert OpenAI-format message list back to rendered markdown text with ask_user_question split turn support."""
    if not messages:
        return ""
    parts = []
    i = 0
    while i < len(messages):
        m = messages[i]
        role = m.get("role", "")
        content = m.get("content", "")

        if role == "user":
            if isinstance(content, list):
                rendered_content = _vision_content_to_markdown(content)
            else:
                rendered_content = _close_unclosed_code_blocks(content)
            if rendered_content:
                rendered_content = _preserve_newlines(rendered_content)
            parts.append(
                f'<div class="msg-row user" markdown="1">\n'
                f'{USER_AVATAR_HTML}\n'
                f'<div class="msg-bubble user" markdown="1">\n'
                f'{rendered_content}\n'
                f'<copy-marker data-msg-index="{i}" class="user-copy-marker"></copy-marker>\n'
                f'</div>\n'
                f'</div>\n\n'
            )
            i += 1
            continue

        elif role == "assistant" or role == "tool":
            # If this is specifically the tool response of ask_user_question, render it as a user bubble!
            if role == "tool" and m.get("name") == "ask_user_question":
                rendered_content = html.escape(content or "")
                parts.append(
                    f'<div class="msg-row user" markdown="1">\n'
                    f'{USER_AVATAR_HTML}\n'
                    f'<div class="msg-bubble user" markdown="1">\n'
                    f'{rendered_content}\n'
                    f'</div>\n'
                    f'</div>\n\n'
                )
                i += 1
                continue

            # Otherwise, gather messages in this turn.
            # We stop gathering if we encounter an ask_user_question tool response,
            # so that it will be rendered as a user bubble in the next loop iteration.
            turn_msgs = []
            start_idx = i

            while i < len(messages):
                next_msg = messages[i]
                next_role = next_msg.get("role", "")
                if next_role not in ("assistant", "tool"):
                    break
                if next_role == "tool" and next_msg.get("name") == "ask_user_question":
                    break
                turn_msgs.append(next_msg)
                i += 1

            is_active_streaming_turn = False
            if is_streaming and i == len(messages):
                is_active_streaming_turn = True

            s_reas = streaming_reasoning if is_active_streaming_turn else ""
            s_cont = streaming_content if is_active_streaming_turn else ""
            s_active = is_streaming if is_active_streaming_turn else False

            turn_html = _render_active_turn_to_html(turn_msgs, s_reas, s_cont, s_active, all_messages=messages, show_details=show_details)
            if turn_html.strip():
                parts.append(
                    f'<div class="msg-row assistant" markdown="1">\n'
                    f'{ASSISTANT_AVATAR_HTML}\n'
                    f'<div class="msg-bubble assistant" markdown="1">\n'
                    f'{turn_html}\n'
                    f'<copy-marker data-msg-index="{start_idx}"></copy-marker>\n'
                    f'</div>\n'
                    f'</div>\n\n'
                )
            continue

        i += 1

    if is_streaming:
        # Dangling Stream Case:
        # 如果当前正在流式输出，但消息列表以 user 或 ask_user_question 工具响应（代表用户的回答）结尾，
        # 则说明当前的流式内容属于正在孕育的全新 Assistant 回答，尚未被正式添加进 messages 列表中。
        # 此时需要单独在最末尾追加渲染一个全新的 Assistant 气泡来显示当前正在流式生成的文本/思考过程。
        has_rendered_stream = False
        if messages:
            last_msg = messages[-1]
            if last_msg.get("role") == "user" or (last_msg.get("role") == "tool" and last_msg.get("name") == "ask_user_question"):
                has_rendered_stream = False
            else:
                has_rendered_stream = True
        else:
            has_rendered_stream = False

        if not has_rendered_stream:
            turn_html = _render_active_turn_to_html([], streaming_reasoning, streaming_content, is_streaming, all_messages=messages, show_details=show_details)
            if turn_html.strip():
                parts.append(
                    f'<div class="msg-row assistant" markdown="1">\n'
                    f'{ASSISTANT_AVATAR_HTML}\n'
                    f'<div class="msg-bubble assistant" markdown="1">\n'
                    f'{turn_html}\n'
                    f'<copy-marker data-msg-index="{len(messages)}"></copy-marker>\n'
                    f'</div>\n'
                    f'</div>\n\n'
                )

    return "".join(parts)
