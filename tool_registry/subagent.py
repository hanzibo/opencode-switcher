"""Sub-agent tools — spawn isolated sub-agents for parallel task execution."""

import hashlib
import copy
import html
import os
import threading
import time
from typing import Any, Dict, List, Optional

from ._state import bash as _bash_state, file_read as _file_read_state
from .bash import _BashSession


_SUBAGENT_BLOCKED_TOOLS = frozenset([
    "ask_user_question",
    "read_qq_mail",
    "sub_agent",
])

_SUBAGENT_TIMEOUT_PER_TURN = 60
_SUBAGENT_CLEANUP_AGE = 300
_MAX_TOOL_RESULT_CHARS = 5000

_SUBAGENT_TYPES = {
    "general": {
        "system_prompt": "You are a sub-agent. Your ONLY job is to execute the EXACT task given to you.\n\n"
                         "CRITICAL RULES:\n"
                         "- Follow the task instructions PRECISELY. Do not deviate, do not add extra work.\n"
                         "- Use the available tools to gather information needed for the task.\n"
                         "- You CANNOT ask the user questions — find answers using tools.\n"
                         "- After completing the EXACT requested work, output a clear result.\n"
                         "- Your output will be returned to the parent agent as-is.",
    },
    "explore": {
        "system_prompt": "You are a code exploration sub-agent. Your ONLY job is to explore the codebase.\n\n"
                         "CRITICAL RULES:\n"
                         "- Follow the exploration task PRECISELY.\n"
                         "- You have READ-ONLY tools: read files, search code, list directories, get file info.\n"
                         "- You CANNOT modify files. You CANNOT ask the user questions.\n"
                         "- After completing the EXACT requested analysis, output the findings.\n"
                         "- Your output will be returned to the parent agent as-is.",
    },
    "bash": {
        "system_prompt": "You are a command execution sub-agent.\n"
                         "Your ONLY job is to execute the EXACT shell commands requested.\n\n"
                         "You have the bash tool only. You CANNOT read/write files directly.\n"
                         "You CANNOT ask the user questions.\n\n"
                         "After executing, output the command results.",
    },
}


def _get_llm_config():
    """读取子代理使用的默认模型配置。

    优先级：① is_subagent_default（API Settings 中指定的子代理默认模型）
            → ② is_default（全局默认模型）→ ③ models 第一个 → 报错
    """
    from stores.clipboard_store import LLMSettingsStore
    store = LLMSettingsStore()
    subagent_default = next((m for m in store.models
                             if getattr(m, "is_subagent_default", False)), None)
    if subagent_default is not None:
        return subagent_default
    default = next((m for m in store.models if m.is_default), None)
    if default is None and store.models:
        default = store.models[0]
    if default is None:
        raise RuntimeError("没有可用的 LLM 模型配置。请在 AI 设置中配置模型。")
    return default


def _build_subagent_tools(agent_type: str = "general") -> list:
    """Build filtered tool definitions list for sub-agent use."""
    from . import TOOL_DEFINITIONS

    if agent_type == "explore":
        allowed = {
            "read_file", "grep_search", "glob_find", "list_directory", "file_info",
            "get_current_time", "get_code_metrics", "find_project_dependencies", "parse_file_ast"
        }
        return [t for t in TOOL_DEFINITIONS
                if t.get("function", {}).get("name") in allowed]
    if agent_type == "bash":
        allowed = {"bash", "get_current_time"}
        return [t for t in TOOL_DEFINITIONS
                if t.get("function", {}).get("name") in allowed]
    return [
        t for t in TOOL_DEFINITIONS
        if t.get("function", {}).get("name") not in _SUBAGENT_BLOCKED_TOOLS
    ]


_thread_local = threading.local()


def set_current_conversation_id(conv_id: Optional[str]):
    _thread_local.conversation_id = conv_id


def get_current_conversation_id() -> Optional[str]:
    return getattr(_thread_local, "conversation_id", None)


def get_conv_short_hash(conv_id: Optional[str]) -> str:
    if not conv_id:
        return "temp"
    return hashlib.md5(conv_id.encode('utf-8')).hexdigest()[:5]


_background_subagent_id = 0
_background_subagent_results: Dict[str, str] = {}
_background_subagent_status: Dict[str, Dict[str, Any]] = {}
_subagent_status_listeners = []
# 保护 _background_subagent_status / _background_subagent_results 的并发访问：
# 后台线程（子代理执行、tool-loop）与 GTK 主线程（UI 读取/删除）无锁共享，
# 深拷贝迭代期间他线程新增键会抛 RuntimeError（dict changed size during iteration），
# 且 remove 与"prev 检查→写回"之间需要原子性（防止状态复活）。RLock 支持重入。
_status_lock = threading.RLock()


def register_subagent_status_listener(callback):
    global _subagent_status_listeners
    if callback not in _subagent_status_listeners:
        _subagent_status_listeners.append(callback)


def unregister_subagent_status_listener(callback):
    global _subagent_status_listeners
    if callback in _subagent_status_listeners:
        _subagent_status_listeners.remove(callback)


def _notify_subagent_status_change(subagent_id: str, status_info: Optional[dict]):
    try:
        try:
            from gi.repository import GLib
        except ImportError:
            GLib = None
        # 先构造快照：主线程同步调用与 idle_add 异步投递统一使用入队/调用时刻的快照，
        # 避免回调读到被后续更新覆盖后的最终值（连续动作事件全部变成最后一次动作）。
        # 快照在循环外构造一次，多监听者共享同一份。
        snapshot = copy.deepcopy(status_info) if status_info is not None else None
        if GLib is None:
            for cb in list(_subagent_status_listeners):
                cb(subagent_id, snapshot)
            return
        # 判断当前线程是否为 GTK 主线程：双条件——
        # ① 是 Python 主线程；② 是 GLib 主上下文所有者（GTK 主循环所在线程）。
        # 不能只用 GLib.main_depth()（后台线程恒 0）；也不能只用 is_owner()（被 push
        # thread-default context 的后台线程可能误判为 True）。
        is_main_thread = (threading.current_thread() is threading.main_thread()
                          and GLib.main_context_default().is_owner())
        for cb in list(_subagent_status_listeners):
            if is_main_thread:
                cb(subagent_id, snapshot)
            else:
                # idle_add 跨线程安全，会调度到主循环，由主线程执行回调
                GLib.idle_add(cb, subagent_id, snapshot)
    except Exception as e:
        import sys
        print(f"[opencode-switcher] Error notifying subagent status change: {e}", file=sys.stderr)


def get_subagent_status_map() -> Dict[str, Dict[str, Any]]:
    # 深拷贝：避免 UI 持有的 info 与后台线程共享内层 dict，
    # 防止 _update_action 原地修改被 UI 侧意外观察到。持锁防止迭代期间 dict 扩容。
    with _status_lock:
        return copy.deepcopy(_background_subagent_status)


def remove_subagent_status(subagent_id: str):
    global _background_subagent_status, _background_subagent_results
    with _status_lock:
        _background_subagent_status.pop(subagent_id, None)
        # 同步清理未消费的结果，避免残留条目永久泄漏
        _background_subagent_results.pop(subagent_id, None)
    _notify_subagent_status_change(subagent_id, None)


def check_background_subagents(conv_id: Optional[str] = None,
                               subagent_ids: Optional[List[str]] = None) -> str:
    """检查后台子代理结果，返回可注入主代理上下文的文本。

    两种消费模式：
    - subagent_ids：按 sid 精确消费（UI 手动发送选中块时使用）。UI 主线程的
      thread-local conv_id 恒为 None，按 conv_id 匹配永远失败，因此必须按 sid 消费，
      否则结果会残留并泄漏。
    - conv_id（默认）：按对话 ID 匹配（tool loop 后台线程使用）。
    """
    global _background_subagent_results
    with _status_lock:
        if not _background_subagent_results:
            return ""

        if subagent_ids:
            matching_sids = [str(s) for s in subagent_ids
                             if str(s) in _background_subagent_results]
        else:
            if conv_id is None:
                conv_id = get_current_conversation_id()
            matching_sids = []
            for sid in list(_background_subagent_results.keys()):
                sid_conv_id = _background_subagent_status.get(sid, {}).get("conv_id")
                if sid_conv_id == conv_id:
                    matching_sids.append(sid)

        if not matching_sids:
            return ""

        parts = []
        for sid in sorted(matching_sids):
            parts.append(
                f"## 后台子代理 {sid} 已完成\n"
                f"结果文件: /tmp/opencode_subagent_{sid}_result.txt\n\n"
                f"请使用 read_file 读取结果文件以获取详细信息。"
            )
            _background_subagent_results.pop(sid, None)

        return "\n\n---\n\n".join(parts)


def _cleanup_expired_subagents():
    """清理超过 _SUBAGENT_CLEANUP_AGE 的已完成/失败子代理状态与结果（防止无限累积）。

    在启动新子代理与查询状态时顺带调用，避免只依赖主代理主动调用
    get_subagent_status 才触发清理。failed 条目以 failed_at 为时间基准，
    避免失败记录永久残留（🔴-1）。
    """
    global _background_subagent_status, _background_subagent_results
    with _status_lock:
        now = time.time()
        to_remove = []
        for sid, info in list(_background_subagent_status.items()):
            status = info.get("status")
            if status == "completed":
                ts = info.get("completed_at", 0)
            elif status == "failed":
                ts = info.get("failed_at", 0)
            else:
                continue
            if ts and (now - ts) > _SUBAGENT_CLEANUP_AGE:
                to_remove.append(sid)
        for sid in to_remove:
            _background_subagent_status.pop(sid, None)
            _background_subagent_results.pop(sid, None)
    for sid in to_remove:
        _notify_subagent_status_change(sid, None)


def _run_subagent_background(task: str, agent_type: str,
                             subagent_id: str):
    def _run():
        global _background_subagent_results, _background_subagent_status
        try:
            # 兜底捕获：任何未预料异常（LLM 内部、html.unescape、竞态等）都不能让
            # 线程静默死亡导致状态永久卡在 running（🔴-2），一律收敛到 failed。
            raw_result = _execute_subagent_sync(task, agent_type,
                                                subagent_id=subagent_id)
        except Exception as e:
            raw_result = f"错误：子代理执行异常 — {e!r}"
            # 标记 failed 并广播（模拟 _mark_failed），确保异常场景也收敛到 failed。
            # 注意：此处的广播发生在 results 写入之前，与 _execute_subagent_sync
            # 内部 _mark_failed 的时序一致（🟠-4：先落盘/写 results 再广播为理想顺序，
            # 失败路径依赖 idle 队列 FIFO 兜底，已加注释固化该约定）。
            _failed_info = None
            with _status_lock:
                _failed_info = _background_subagent_status.get(subagent_id)
                if _failed_info is not None:
                    _failed_info["status"] = "failed"
                    _failed_info["action"] = "失败"
                    _failed_info["failed_at"] = time.time()
            if _failed_info is not None:
                _notify_subagent_status_change(subagent_id, _failed_info)
        result = html.unescape(raw_result)
        result_path = f"/tmp/opencode_subagent_{subagent_id}_result.txt"
        # 先落盘结果文件，再更新状态并通知，避免 UI 收到 completed 时文件尚不存在
        try:
            with open(result_path, "w", encoding="utf-8") as f:
                f.write(result)
        except OSError:
            pass

        with _status_lock:
            # 锁内二次确认：状态可能在运行期间被 remove_subagent_status 移除。
            # "检查 prev + 写回"必须与 remove 互斥（TOCTOU），否则会复活状态并重写 results。
            prev = _background_subagent_status.get(subagent_id)
            if prev is None:
                return
            # 结果始终写入（错误信息也写入 results，供主代理按 sid 消费/查看）
            _background_subagent_results[subagent_id] = result
            if prev.get("status") == "failed":
                # 执行失败：保持 failed 状态，不覆盖为 completed（错误详情已广播并在结果中）
                return
            _background_subagent_status[subagent_id] = {
                "task": prev.get("task", task[:100]),
                "started_at": prev.get("started_at", 0),
                "status": "completed",
                "action": "已完成",
                "completed_at": time.time(),
                "conv_id": prev.get("conv_id"),
            }
            # 先写 results 再通知：UI 收到 completed 事件时结果必定可被消费
            _notify_subagent_status_change(subagent_id, _background_subagent_status[subagent_id])
        try:
            from .notification import execute_send_notification
            summary = result[:100] + "..." if len(result) > 100 else result
            execute_send_notification(
                summary=f"子代理 {subagent_id} 已完成",
                body=f"{summary}\n结果已保存至 {result_path}",
                urgency="normal",
                expire_time=8000,
            )
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def _execute_subagent_sync(task: str, agent_type: str,
                           subagent_id: Optional[str] = None) -> str:
    """Synchronous sub-agent execution (internal)."""
    sub_tools = _build_subagent_tools(agent_type)
    type_info = _SUBAGENT_TYPES[agent_type]

    # 记录上次已广播的 action，用于去重（闭包，仅本次执行内有效）。
    # 初始值 "Thinking" 与 execute_sub_agent 预置状态一致：生产路径下预置 Thinking
    # 已广播过，线程内首个 Thinking 不应重复广播（🟡-1）。
    last_broadcast_action = ["Thinking"]

    def _update_action(action_str: str):
        if subagent_id:
            with _status_lock:
                info = _background_subagent_status.get(subagent_id)
                if info is None:
                    # 状态已被移除（如 remove_subagent_status），不再更新
                    return
                # 基于"上次已广播的 action"去重：连续相同动作不重复广播
                if last_broadcast_action[0] == action_str:
                    return
                last_broadcast_action[0] = action_str
                info["action"] = action_str
            _notify_subagent_status_change(subagent_id, info)

    def _mark_failed():
        """子代理执行失败时，将状态标记为 failed 并广播（有 subagent_id 时）。

        记录 failed_at 作为过期清理的时间基准（🔴-1）。
        """
        if subagent_id:
            with _status_lock:
                info = _background_subagent_status.get(subagent_id)
                if info is None:
                    return
                info["status"] = "failed"
                info["action"] = "失败"
                info["failed_at"] = time.time()
            _notify_subagent_status_change(subagent_id, info)

    local_session = _BashSession()
    try:
        local_session.start()
    except Exception as e:
        return f"错误：无法创建子 bash 会话 — {e}"

    saved_bash_cwd = _bash_state.cwd
    saved_bash_session = _bash_state.session
    saved_read_state = dict(_file_read_state.store)
    _bash_state.session = local_session
    _bash_state.cwd = _bash_state.default_cwd
    _file_read_state.store.clear()

    try:
        config = _get_llm_config()
    except RuntimeError as e:
        _bash_state.cwd = saved_bash_cwd
        _bash_state.session = saved_bash_session
        local_session.stop()
        _mark_failed()
        return f"错误：{e}"
    if not config.api_key:
        _bash_state.cwd = saved_bash_cwd
        _bash_state.session = saved_bash_session
        local_session.stop()
        _mark_failed()
        return "错误：未配置 LLM API key。请在 AI 设置中配置。"
    if not config.base_url:
        _bash_state.cwd = saved_bash_cwd
        _bash_state.session = saved_bash_session
        local_session.stop()
        _mark_failed()
        return "错误：未配置 LLM base URL。请在 AI 设置中配置。"

    from stores.clipboard_store import AISettingsStore, DEFAULT_MAX_TOKENS
    ai_settings = AISettingsStore()
    # 轮次上限仅由用户设置 max_tool_iterations 决定：子代理不再接收主代理的
    # max_turns 参数，执行到模型给出纯文本答案即自然结束，最多跑到设置上限。
    # 对 JSON 手改/损坏的字符串/None 做类型归一（int），失败回退默认 25（M2）。
    try:
        _max_iter = int(ai_settings.max_tool_iterations)
    except (TypeError, ValueError):
        _max_iter = 25
    clamped_turns = max(1, _max_iter)

    messages = [
        {"role": "system", "content": type_info["system_prompt"]},
        {"role": "user", "content": task},
    ]

    # 最大输出 tokens 遵循子代理默认模型（API Settings 中指定，_get_llm_config
    # 返回）的 max_tokens 配置，不再由主代理传递；配置非法（<=0/非数字）时
    # 回退 DEFAULT_MAX_TOKENS 兜底（L2：与 clipboard_store 单一事实来源对齐）。
    try:
        subagent_max_tokens = int(config.max_tokens)
        if subagent_max_tokens <= 0:
            subagent_max_tokens = DEFAULT_MAX_TOKENS
    except (ValueError, TypeError):
        subagent_max_tokens = DEFAULT_MAX_TOKENS

    try:
        from ai_engine.llm_client import _LLMHttpClient, _LLMHttpError, LLMRequestConfig
        llm = _LLMHttpClient()
        final_text = ""

        for turn in range(clamped_turns):
            _update_action("Thinking")
            try:
                sub_config = LLMRequestConfig(
                    base_url=config.base_url,
                    api_key=config.api_key,
                    model_name=config.model_name,
                    timeout=_SUBAGENT_TIMEOUT_PER_TURN,
                    max_tokens=subagent_max_tokens,
                    tools=sub_tools,
                    tool_choice="auto",
                )
                response = llm.sync_chat_completion(
                    sub_config,
                    messages=messages,
                )
            except _LLMHttpError as e:
                _mark_failed()
                return f"子代理 LLM 请求失败：{e}"
            except Exception as e:
                _mark_failed()
                return f"子代理 LLM 请求异常：{e}"

            content = response.get("content") or ""
            tool_calls = response.get("tool_calls")

            if not tool_calls:
                _update_action("Answering")
                final_text = content
                break

            assistant_msg = {"role": "assistant", "content": content or None}
            assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            for tc in tool_calls:
                tc_name = tc.get("function", {}).get("name", "")
                _update_action(f"Tool Call: {tc_name}")
                try:
                    from . import execute_tool_call
                    result = execute_tool_call(tc)
                except Exception as e:
                    result = f"执行工具「{tc_name}」时异常：{e}"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": tc_name,
                    "content": result,
                })

        if not final_text:
            result_parts = []
            for m in messages:
                role = m.get("role", "")
                content = m.get("content", "") or ""
                if role == "tool":
                    name = m.get("name", "")
                    c = content.strip()
                    if c and c != "None":
                        result_parts.append(c[:500])
                elif role == "assistant" and content.strip():
                    result_parts.append(content[:500])
            if result_parts:
                final_text = "\n\n---\n\n".join(dict.fromkeys(result_parts))
            else:
                final_text = "(子代理已完成任务)"

        MAX_SUBAGENT_RESULT_CHARS = 100000
        if len(final_text) > MAX_SUBAGENT_RESULT_CHARS:
            final_text = (final_text[:MAX_SUBAGENT_RESULT_CHARS]
                          + f"\n\n...（结果因超出 100k 字符而被截断，共 {len(final_text)} 字符）")
        return final_text

    finally:
        local_session.stop()
        _bash_state.cwd = saved_bash_cwd
        _bash_state.session = saved_bash_session
        _file_read_state.store.clear()
        _file_read_state.store.update(saved_read_state)


def execute_sub_agent(task: str, agent_type: str = "general",
                      run_in_background: bool = True,
                      **kwargs) -> str:
    """Spawn an isolated sub-agent to complete a task independently.

    轮次机制：子代理不再接收主代理传入的 max_turns，执行到模型给出纯文本
    答案即自然结束；唯一硬上限是用户设置（AISettingsStore.max_tool_iterations，
    默认 25）。最大输出 tokens 遵循子代理默认模型（API Settings 中指定）的
    max_tokens 配置，同样不由主代理传递。**kwargs 仅用于吸收旧版本模型缓存中
    可能携带的 max_turns / max_tokens 冗余参数，避免 TypeError；其他未知参数
    不静默吞掉，告警提示 schema 与实现漂移（M1）。
    """
    # 显式吸收已知旧参数；其余未知参数告警，避免未来新增合法参数被静默忽略
    for _stale in ("max_turns", "max_tokens"):
        kwargs.pop(_stale, None)
    if kwargs:
        import logging
        logging.getLogger(__name__).warning(
            "execute_sub_agent 收到未知参数已忽略: %s", sorted(kwargs)
        )
    if agent_type not in _SUBAGENT_TYPES:
        return f"错误：无效的子代理类型「{agent_type}」。有效值：general, explore, bash"

    if run_in_background:
        global _background_subagent_id
        _cleanup_expired_subagents()  # 顺带清理过期状态，防止无限累积
        _background_subagent_id += 1
        conv_id = get_current_conversation_id()
        short_hash = get_conv_short_hash(conv_id)
        subagent_id = f"{short_hash}-{_background_subagent_id}"
        with _status_lock:
            _background_subagent_status[subagent_id] = {
                "task": task[:100],
                "started_at": time.time(),
                "status": "running",
                "action": "Thinking",
                "conv_id": conv_id,
            }
        _notify_subagent_status_change(subagent_id, _background_subagent_status[subagent_id])
        _run_subagent_background(task, agent_type, subagent_id)
        return (f"⏳ 子代理已启动（任务ID: {subagent_id}，类型: {agent_type}）。"
                f"完成后结果将保存至 /tmp/opencode_subagent_{subagent_id}_result.txt，"
                f"可让主代理使用 read_file 读取。")

    sync_result = _execute_subagent_sync(task, agent_type)
    unescaped = html.unescape(sync_result)
    if len(unescaped) > _MAX_TOOL_RESULT_CHARS:
        return (unescaped[:_MAX_TOOL_RESULT_CHARS]
                + f"\n\n...（结果已截断，共 {len(unescaped)} 字符，详细内容建议使用后台运行查看）")
    return unescaped


def execute_get_subagent_status(id: Optional[Any] = None,
                                clear_completed: bool = False) -> str:
    """查询后台子代理的执行状态。"""
    global _background_subagent_status

    _cleanup_expired_subagents()
    now = time.time()  # 单 ID/列表分支计算耗时使用，勿删除

    to_clear: List[str] = []
    with _status_lock:
        if clear_completed:
            # 同时清除 completed 与 failed（均为终结态），并同步清理对应 results（🟠-3）
            to_clear = [sid for sid, info in _background_subagent_status.items()
                        if info.get("status") in ("completed", "failed")]
            for sid in to_clear:
                _background_subagent_status.pop(sid, None)
                _background_subagent_results.pop(sid, None)

        if not _background_subagent_status and not clear_completed:
            return "当前没有运行中的后台子代理。"

        if id is not None:
            target_sid = None
            id_str = str(id)
            if id_str in _background_subagent_status:
                target_sid = id_str
            else:
                for sid in _background_subagent_status:
                    if isinstance(sid, str) and (sid.endswith(f"-{id}") or sid == id_str):
                        target_sid = sid
                        break

            if target_sid is None:
                return f"错误：未找到 ID 为「{id}」的后台子代理。"

            info = _background_subagent_status.get(target_sid)
            status = info.get("status", "unknown")
            task = info.get("task", "?")
            started = info.get("started_at", 0)
            elapsed = int(now - started) if started else 0
            status_emoji = "✅" if status == "completed" else "🔄" if status == "running" else "❌"
            elapsed_str = f"{elapsed // 60}分{elapsed % 60}秒" if elapsed >= 60 else f"{elapsed}秒"
            lines = [
                f"📋 子代理 {target_sid} 状态:\n",
                f"{status_emoji} ID={target_sid}，状态={status}，耗时={elapsed_str}",
                f"   任务: {task}",
            ]
            if status == "completed":
                lines.append(f"   结果文件: /tmp/opencode_subagent_{target_sid}_result.txt")
            return "\n".join(lines)

        lines = ["📋 后台子代理状态:\n"]
        for sid in sorted(_background_subagent_status):
            info = _background_subagent_status[sid]
            status = info.get("status", "unknown")
            task = info.get("task", "?")
            started = info.get("started_at", 0)
            elapsed = int(now - started) if started else 0
            status_emoji = "✅" if status == "completed" else "🔄" if status == "running" else "❌"
            elapsed_str = f"{elapsed // 60}分{elapsed % 60}秒" if elapsed >= 60 else f"{elapsed}秒"
            lines.append(f"{status_emoji} ID={sid}，状态={status}，耗时={elapsed_str}")
            lines.append(f"   任务: {task}")
            if status == "completed":
                lines.append(f"   结果文件: /tmp/opencode_subagent_{sid}_result.txt")
            lines.append("")
        result_text = "\n".join(lines).strip()

    # 锁外广播删除事件（clear_completed 场景），确保 UI 块被移除
    for sid in to_clear:
        _notify_subagent_status_change(sid, None)
    if clear_completed:
        if not _background_subagent_status:
            return "✅ 已清除所有已完成的后台子代理记录。"
        return f"✅ 已清除 {len(to_clear)} 个已完成的后台子代理记录。"
    return result_text


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "sub_agent",
            "description": "启动一个独立的子代理来完成指定任务。子代理拥有独立的 bash 会话和 LLM 上下文。支持后台运行（默认），完成后结果保存到临时文件。支持 general（全部工具）、explore（只读）、bash（仅 shell）三种模式。不适用于查询已有子代理的状态（应使用 get_subagent_status）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "子代理需要完成的任务描述"},
                    "agent_type": {"type": "string", "description": "代理类型：general（全工具）、explore（只读探索）、bash（仅命令执行）", "enum": ["general", "explore", "bash"], "default": "general"},
                    "run_in_background": {"type": "boolean", "description": "是否在后台运行（默认 true，后台运行）", "default": True},
                },
                "required": ["task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_subagent_status",
            "description": "查询后台子代理的执行状态。可按 ID 查询单个子代理，或列出全部。支持清除已完成子代理记录。不适用于创建新子代理或分配新任务（应使用 sub_agent）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"description": "子代理 ID（可选，省略则列出全部）"},
                    "clear_completed": {"type": "boolean", "description": "是否清除所有已完成的子代理记录", "default": False}
                }
            }
        }
    },
]
