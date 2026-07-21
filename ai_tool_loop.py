"""AI 工具循环 — ReAct 风格的多轮工具调用编排。

职责：
  1. 协调 LLM API 调用与工具执行的多轮循环
  2. 处理流式事件的回调和 UI 更新
  3. 路由 MCP 工具与内置工具的执行
"""

import json
import logging
from dataclasses import dataclass, field, replace
from threading import Event
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from gi.repository import GLib

import tool_registry
from event_types import StreamEventType, ToolCallData, tool_call_to_dict
from llm_client import _LLMHttpError
# clean_messages_for_llm 已移至 llm_client 模块
from llm_client import clean_messages_for_llm

if TYPE_CHECKING:
    from mcp_integration.client_manager import MCPClientManager

logger = logging.getLogger(__name__)

# lazily initialized from AISettingsStore
_MAX_TOOL_ITERATIONS: Optional[int] = None


def _get_max_tool_iterations() -> int:
    global _MAX_TOOL_ITERATIONS
    if _MAX_TOOL_ITERATIONS is None:
        from clipboard_store import AISettingsStore
        _MAX_TOOL_ITERATIONS = AISettingsStore().max_tool_iterations
    return _MAX_TOOL_ITERATIONS


# ═══════════════════════════════════════════════════════════════════
#  上下文数据模型
# ═══════════════════════════════════════════════════════════════════


@dataclass
class ToolLoopContext:
    """工具循环上下文：集中管理 20+ 个回调参数。

    .. code-block:: python

        ctx = ToolLoopContext(
            req_id=req_id,
            cancel_event=cancel_event,
            get_current_request_id_fn=lambda: req_id,
            append_message_fn=append_message_callback,
            ...
        )
        run_llm_react_loop(llm_client, config, ctx, messages)
    """
    req_id: int
    cancel_event: Event
    get_current_request_id_fn: Callable[[], int]
    append_message_fn: Callable[[dict], None]
    append_html_to_webview_fn: Callable[[str], None]
    handle_ask_user_question_fn: Callable[..., str]
    on_llm_api_finished_fn: Callable[[int], None]
    finalize_after_tool_loop_fn: Callable[[int], None]
    set_tool_iteration_fn: Callable[[int], None]
    reset_iteration_state_fn: Callable[[], None]
    set_reasoning_text_fn: Optional[Callable[[str], None]] = None
    set_assistant_text_fn: Optional[Callable[[str], None]] = None
    on_token_delta_fn: Optional[Callable[[str], None]] = None
    on_reasoning_delta_fn: Optional[Callable[[str], None]] = None
    on_tool_result_fn: Optional[Callable[[str, str, str], None]] = None
    on_tool_calls_started_fn: Optional[Callable[[int], None]] = None
    conv_id: Optional[str] = None
    mcp_tool_definitions: Optional[list] = None
    mcp_client_manager: Optional['MCPClientManager'] = None
    disabled_tools: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════
#  工具执行路由
# ═══════════════════════════════════════════════════════════════════


def _execute_tool_call(tc: ToolCallData, ctx: ToolLoopContext) -> str:
    """执行单个工具调用，返回结果文本。

    路由优先级：
    1. ``ask_user_question`` → 用户提问对话框
    2. MCP 工具（带 ``server__tool`` 命名空间前缀）→ 通过 mcp_client_manager 调用
    3. MCP 工具（无前缀，模糊匹配）→ 遍历所有 Server 查找
    4. 其他 → 内置工具注册表
    """
    tc_name = tc.name

    # 1. 用户提问（特殊内置工具）
    if tc_name == "ask_user_question":
        return ctx.handle_ask_user_question_fn(tool_call_to_dict(tc))

    # 2. 尝试路由为 MCP 工具
    from mcp_integration import parse_mcp_tool_name
    mcp_server, mcp_tool = parse_mcp_tool_name(tc_name)

    if mcp_server != "builtin" and ctx.mcp_client_manager is not None:
        # 带命名空间前缀：server__tool
        try:
            args = json.loads(tc.arguments)
            return ctx.mcp_client_manager.bridge.run_coroutine(
                ctx.mcp_client_manager.call_tool(mcp_server, mcp_tool, args)
            )
        except Exception as e:
            return f"❌ MCP 工具 '{tc_name}' 执行异常: {e}"

    if ctx.mcp_client_manager is not None and ctx.mcp_tool_definitions is not None:
        # 无前缀：遍历所有 MCP 工具定义进行模糊匹配
        mcp_names = [
            s["function"]["name"].split("__", 1)[-1]
            for s in ctx.mcp_tool_definitions
            if "__" in s.get("function", {}).get("name", "")
        ]
        mcp_servers = {
            s["function"]["name"].split("__", 1)[-1]: s["function"]["name"].split("__", 1)[0]
            for s in ctx.mcp_tool_definitions
            if "__" in s.get("function", {}).get("name", "")
        }
        if tc_name in mcp_names:
            try:
                args = json.loads(tc.arguments)
                return ctx.mcp_client_manager.bridge.run_coroutine(
                    ctx.mcp_client_manager.call_tool(mcp_servers[tc_name], tc_name, args)
                )
            except Exception as e:
                return f"❌ MCP 工具 '{tc_name}' 执行异常: {e}"

    # 3. 内置工具
    return tool_registry.execute_tool_call(
        tool_call_to_dict(tc),
        cancel_event=ctx.cancel_event,
        disabled_list=ctx.disabled_tools,
    )

# ═══════════════════════════════════════════════════════════════════
#  主循环
# ═══════════════════════════════════════════════════════════════════


def run_llm_react_loop(
    llm_client,
    config: Any,  # LLMRequestConfig
    ctx: ToolLoopContext,
    messages: list,
) -> None:
    """ReAct 风格的多轮工具调用主循环。

    Parameters
    ----------
    llm_client : _LLMHttpClient
        LLM HTTP 客户端实例。
    config : LLMRequestConfig
        模型请求配置。
    ctx : ToolLoopContext
        回调与状态上下文。
    messages : list
        对话消息列表（会被追加 tool call 结果）。
    """
    ctx.set_tool_iteration_fn(0)
    tool_registry.set_current_conversation_id(ctx.conv_id)
    try:
        iteration = 0
        max_iter = _get_max_tool_iterations()

        while iteration < max_iter:
            if ctx.cancel_event and ctx.cancel_event.is_set():
                GLib.idle_add(ctx.on_llm_api_finished_fn, ctx.req_id)
                break

            # 在每轮 LLM 调用前检查后台子代理完成情况
            bg_info = tool_registry.check_background_subagents()
            if bg_info:
                messages.append({
                    "role": "system",
                    "content": f"[Background sub-agent completed]\n{bg_info}",
                })

            should_continue = _perform_llm_call(
                llm_client, config, ctx, messages, iteration,
            )
            if not should_continue:
                break

            iteration += 1
            ctx.set_tool_iteration_fn(iteration)
    finally:
        tool_registry.set_current_conversation_id(None)


def _perform_llm_call(
    llm_client,
    config: Any,  # LLMRequestConfig
    ctx: ToolLoopContext,
    messages: list,
    iteration: int,
) -> bool:
    """执行一轮 LLM 调用并处理流式事件。

    Returns
    -------
    bool
        True 表示应继续下一轮迭代，False 表示本轮已完结。
    """
    assistant_text = ""
    reasoning_text = ""
    tool_calls_found: List[ToolCallData] = []

    ctx.reset_iteration_state_fn()

    try:
        cleaned_msgs = clean_messages_for_llm(messages)
        # 合并内置工具（过滤已禁用的）与 MCP 工具定义
        all_tools = tool_registry.get_enabled_tool_definitions(ctx.disabled_tools)
        if ctx.mcp_tool_definitions:
            all_tools.extend(ctx.mcp_tool_definitions)

        # 构建请求配置（合并 MCP 工具，不污染原 config 对象）
        call_config = replace(
            config,
            tools=all_tools,
            tool_choice=tool_registry.TOOL_CHOICE_AUTO,
        )

        for event in llm_client.stream_chat_completion(
            call_config, cleaned_msgs,
            cancel_event=ctx.cancel_event,
        ):
            if ctx.get_current_request_id_fn() != ctx.req_id:
                return False

            if event.type == StreamEventType.TOOL_CALLS:
                if event.tool_calls:
                    if ctx.on_tool_calls_started_fn is not None:
                        GLib.idle_add(ctx.on_tool_calls_started_fn, ctx.req_id)
                    tool_calls_found.extend(event.tool_calls)
                continue

            if event.type == StreamEventType.REASONING_DELTA:
                if event.reasoning_delta:
                    if ctx.on_reasoning_delta_fn is not None:
                        ctx.on_reasoning_delta_fn(event.reasoning_delta)
                    reasoning_text += event.reasoning_delta
                    if ctx.set_reasoning_text_fn is not None:
                        ctx.set_reasoning_text_fn(reasoning_text)
                continue

            if event.type == StreamEventType.TEXT_DELTA:
                if event.text_delta:
                    if ctx.on_token_delta_fn is not None:
                        ctx.on_token_delta_fn(event.text_delta)
                    assistant_text += event.text_delta
                    if ctx.set_assistant_text_fn is not None:
                        ctx.set_assistant_text_fn(assistant_text)
                continue

            if event.type == StreamEventType.STREAM_END:
                break

        # ── 处理本轮产生的工具调用 ──
        if not tool_calls_found:
            GLib.idle_add(ctx.on_llm_api_finished_fn, ctx.req_id)
            return False

        # 在开始执行工具前检查取消（流式解析结束但可能已取消）
        if ctx.cancel_event and ctx.cancel_event.is_set():
            GLib.idle_add(ctx.on_llm_api_finished_fn, ctx.req_id)
            return False

        # 追加 assistant 消息（含 tool_calls 定义）
        tool_call_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": assistant_text,
            "tool_calls": [tool_call_to_dict(tc) for tc in tool_calls_found],
        }
        if reasoning_text:
            tool_call_msg["reasoning_content"] = reasoning_text
        ctx.append_message_fn(tool_call_msg)

        # assistant 文本已随 tool_call_msg 追加到 _ai_messages，
        # 清空 state 中的缓存避免 _render_current_assistant_message
        # 在 turn_msgs 和 streaming_content 中重复渲染同一段文本
        if ctx.set_assistant_text_fn:
            ctx.set_assistant_text_fn("")

        # 逐个执行工具调用
        for tc_idx, tc in enumerate(tool_calls_found):
            if ctx.get_current_request_id_fn() != ctx.req_id:
                return False

            result = _execute_tool_call(tc, ctx)

            if ctx.get_current_request_id_fn() != ctx.req_id:
                return False

            # 增量工具结果通知（v3 特性）
            if ctx.on_tool_result_fn is not None:
                status = ("cancelled" if ctx.cancel_event and ctx.cancel_event.is_set()
                          else "error" if result.strip().startswith(tool_registry.ERROR_PREFIXES)
                          else "success")
                ctx.on_tool_result_fn(tc.id, result, status)

            # 用户取消 → 追加已取消后缀
            if ctx.cancel_event and ctx.cancel_event.is_set():
                ctx.append_message_fn({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": result,
                })
                # 剩余未执行的工具标记为已取消
                for remaining_tc in tool_calls_found[tc_idx + 1:]:
                    if ctx.on_tool_result_fn is not None:
                        ctx.on_tool_result_fn(
                            remaining_tc.id, tool_registry.TOOL_CANCELLED, "cancelled",
                        )
                    ctx.append_message_fn({
                        "role": "tool",
                        "tool_call_id": remaining_tc.id,
                        "name": remaining_tc.name,
                        "content": tool_registry.TOOL_CANCELLED,
                    })
                GLib.idle_add(ctx.on_llm_api_finished_fn, ctx.req_id)
                return False

            ctx.append_message_fn({
                "role": "tool",
                "tool_call_id": tc.id,
                "name": tc.name,
                "content": result,
            })

        if ctx.cancel_event and ctx.cancel_event.is_set():
            GLib.idle_add(ctx.on_llm_api_finished_fn, ctx.req_id)
            return False

        # 检查是否达到最大迭代次数
        max_iter = _get_max_tool_iterations()
        if iteration + 1 >= max_iter:
            ctx.append_message_fn({
                "role": "assistant",
                "content": f"⚠️ 已达到最大迭代次数（{max_iter}），请简化请求或重试。",
            })
            GLib.idle_add(ctx.finalize_after_tool_loop_fn, ctx.req_id)
            return False

        return True

    except _LLMHttpError as e:
        print(f"[ToolLoop] LLM HTTP error: {e}", flush=True)
        GLib.idle_add(ctx.on_llm_api_finished_fn, ctx.req_id)
        return False
    except Exception as e:
        print(f"[ToolLoop] Unhandled exception: {e}", flush=True)
        import traceback
        traceback.print_exc()
        GLib.idle_add(ctx.on_llm_api_finished_fn, ctx.req_id)
        return False
