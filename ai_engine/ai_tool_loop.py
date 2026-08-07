"""AI 工具循环 — ReAct 风格的多轮工具调用编排。

职责：
  1. 协调 LLM API 调用与工具执行的多轮循环
  2. 处理流式事件的回调和 UI 更新
  3. 路由 MCP 工具与内置工具的执行
"""

import json
import logging
from dataclasses import dataclass, field, replace
from threading import Event, Timer
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from gi.repository import GLib

import tool_registry
from system.event_types import StreamEventType, ToolCallData, tool_call_to_dict
from ai_engine.llm_client import _LLMHttpError, clean_messages_for_llm

if TYPE_CHECKING:
    from mcp_integration.client_manager import MCPClientManager

logger = logging.getLogger(__name__)

# lazily initialized from AISettingsStore
_MAX_TOOL_ITERATIONS: Optional[int] = None

# LLM 流式调用超时看门狗（与 _generate_summary_async 的双层超时策略一致）：
# - 首 token 总超时：从流开始算，收到首个事件后撤销
# - 流式停顿超时：收到事件后重置；仅覆盖 LLM 流式阶段，不打断工具执行
_LLM_FIRST_TOKEN_TIMEOUT_SEC = 120
_LLM_STREAM_IDLE_TIMEOUT_SEC = 120


def _get_max_tool_iterations() -> int:
    global _MAX_TOOL_ITERATIONS
    if _MAX_TOOL_ITERATIONS is None:
        from stores.clipboard_store import AISettingsStore
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
    on_llm_error_fn: Optional[Callable[[str], None]] = None
    conv_id: Optional[str] = None
    mcp_tool_definitions: Optional[list] = None
    mcp_client_manager: Optional['MCPClientManager'] = None
    disabled_tools: list[str] = field(default_factory=list)
    # 本循环所有 LLM 流共用的稳定请求键（跨迭代复用同一个键，避免每次调用
    # 自动生成新键导致看门狗/外部取消无法定位本会话）。省略时回退到旧行为：
    # stream_chat_completion 自动键 + 看门狗无键取消全部活动流。
    request_key: Optional[Any] = None


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
    from mcp_integration.gtk_asyncio_bridge import CoroutineCancelledError
    mcp_server, mcp_tool = parse_mcp_tool_name(tc_name)

    if mcp_server != "builtin" and ctx.mcp_client_manager is not None:
        # 带命名空间前缀：server__tool
        try:
            args = json.loads(tc.arguments)
            return ctx.mcp_client_manager.bridge.run_coroutine(
                ctx.mcp_client_manager.call_tool(mcp_server, mcp_tool, args),
                cancel_event=ctx.cancel_event,
            )
        except CoroutineCancelledError:
            return tool_registry.TOOL_CANCELLED
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
                    ctx.mcp_client_manager.call_tool(mcp_servers[tc_name], tc_name, args),
                    cancel_event=ctx.cancel_event,
                )
            except CoroutineCancelledError:
                return tool_registry.TOOL_CANCELLED
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

        # 在首轮注入当前工作区可用的 Skills 摘要（进行排重检查，避免重复追加）
        try:
            from stores.skill_store import SkillStore
            cwd = tool_registry.get_bash_cwd()
            skills_prompt = SkillStore().get_skills_prompt_summary(cwd=cwd)
            if skills_prompt:
                has_skills_msg = any(
                    isinstance(m, dict) and m.get("role") == "system" and "<available_skills>" in str(m.get("content", ""))
                    for m in messages
                )
                if not has_skills_msg:
                    messages.append({
                        "role": "system",
                        "content": skills_prompt
                    })
        except Exception as e:
            logger.warning("Failed to inject skills summary: %s", e)

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

    # ── LLM 流式调用超时看门狗（建议 1：首 token 总超时 + 流式停顿超时）──
    # 与 _generate_summary_async 的双层 Timer 策略一致；超时通过 cancel_event +
    # cancel_active_request() 双通道中断，复用现有取消清理链。
    timeout_reason = None
    call_active = True
    total_timer: Optional[Timer] = None
    idle_timer: Optional[Timer] = None

    def _fire_timeout(reason: str):
        nonlocal timeout_reason
        if not call_active:
            # 本调用已结束（finally 已清理），防止迟到定时器误关新请求
            return
        timeout_reason = reason
        logger.warning("LLM 流式调用超时中止: %s (req_id=%s)", reason, ctx.req_id)
        if ctx.cancel_event:
            ctx.cancel_event.set()          # ① 协作式取消标志
        if ctx.request_key is not None:
            # ② 按 request_key 强关本会话响应，解除 iter_lines 阻塞；
            #    只影响本会话，不误伤并行会话的其他流（Wave2 接线）
            llm_client.cancel_active_request(ctx.request_key)
        else:
            llm_client.cancel_active_request()  # ② 无键回退：旧语义，取消全部活动流

    def _on_activity():
        """收到任何流事件（文本/推理/工具调用）即视为模型在工作：撤总超时、重置停顿超时。"""
        nonlocal idle_timer
        if total_timer:
            total_timer.cancel()
        if idle_timer:
            idle_timer.cancel()
        idle_timer = Timer(
            _LLM_STREAM_IDLE_TIMEOUT_SEC,
            lambda: _fire_timeout(
                f"模型响应停顿超过 {_LLM_STREAM_IDLE_TIMEOUT_SEC} 秒，已中止"
            ),
        )
        idle_timer.daemon = True
        idle_timer.start()

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

        total_timer = Timer(
            _LLM_FIRST_TOKEN_TIMEOUT_SEC,
            lambda: _fire_timeout(
                f"{_LLM_FIRST_TOKEN_TIMEOUT_SEC} 秒内未收到模型响应，已中止"
            ),
        )
        total_timer.daemon = True
        total_timer.start()

        for event in llm_client.stream_chat_completion(
            call_config, cleaned_msgs,
            cancel_event=ctx.cancel_event,
            request_key=ctx.request_key,
        ):
            if ctx.get_current_request_id_fn() != ctx.req_id:
                # ⚠️ 死代码（既有问题，未修）：get_current_request_id_fn 恒返回本请求的
                # req_id（见 ai_chat_panel 接线 lambda: req_id），该检查永不成立。
                # 本意是"用户已重试/新请求启动则放弃本请求"，实际未生效——
                # 旧线程只能靠 cancel_event 感知取消。重构时改为读取面板当前 req_id。
                return False

            # 任何事件都代表模型在工作：撤首 token 总超时、重置停顿超时
            _on_activity()

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

        # ── 流阶段结束：立即撤除看门狗（H-1）──
        # 工具执行阶段可能远超流式停顿阈值（bash 最长 120s、ask_user_question 300s），
        # 若不在此撤除，idle_timer 会在长工具执行期间误触发取消（bash 进程组被杀）。
        # finally 中的清理仍保留作为异常/return 路径的兜底。
        if total_timer:
            total_timer.cancel()
        if idle_timer:
            idle_timer.cancel()

        # ── 处理本轮产生的工具调用 ──
        # 超时中止（非用户主动暂停）→ 先上报错误气泡，再走公共收尾。
        # 独立于 tool_calls_found：已累积部分工具调用后停顿同样需要上报（H-2）。
        if timeout_reason and ctx.on_llm_error_fn is not None:
            ctx.on_llm_error_fn(timeout_reason)

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
        if ctx.on_llm_error_fn is not None:
            ctx.on_llm_error_fn(f"LLM 请求失败：{e}")
        GLib.idle_add(ctx.on_llm_api_finished_fn, ctx.req_id)
        return False
    except Exception as e:
        print(f"[ToolLoop] Unhandled exception: {e}", flush=True)
        import traceback
        traceback.print_exc()
        GLib.idle_add(ctx.on_llm_api_finished_fn, ctx.req_id)
        return False
    finally:
        # 任何路径（正常/异常/取消）均撤除超时看门狗，防止迟到定时器误动作
        call_active = False
        if total_timer:
            total_timer.cancel()
        if idle_timer:
            idle_timer.cancel()
