"""Sub-agent tools — spawn isolated sub-agents for parallel task execution."""

import hashlib
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

_MAX_SUBAGENT_TURNS = 20
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
    """Read the default LLM model config for sub-agent use."""
    from clipboard_store import LLMSettingsStore, LLMModelConfig
    store = LLMSettingsStore()
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
            has_glib = True
        except ImportError:
            has_glib = False
        for cb in list(_subagent_status_listeners):
            if has_glib:
                GLib.idle_add(cb, subagent_id, status_info)
            else:
                cb(subagent_id, status_info)
    except Exception as e:
        import sys
        print(f"[opencode-switcher] Error notifying subagent status change: {e}", file=sys.stderr)


def get_subagent_status_map() -> Dict[str, Dict[str, Any]]:
    return dict(_background_subagent_status)


def remove_subagent_status(subagent_id: str):
    global _background_subagent_status
    _background_subagent_status.pop(subagent_id, None)
    _notify_subagent_status_change(subagent_id, None)


def check_background_subagents(conv_id: Optional[str] = None) -> str:
    global _background_subagent_results
    if not _background_subagent_results:
        return ""

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


def _run_subagent_background(task: str, max_turns: int, agent_type: str,
                             subagent_id: str, max_tokens: Optional[int] = None):
    def _run():
        global _background_subagent_results, _background_subagent_status
        raw_result = _execute_subagent_sync(task, max_turns, agent_type, max_tokens)
        result = html.unescape(raw_result)
        _background_subagent_status[subagent_id] = {
            "task": task[:100],
            "started_at": _background_subagent_status.get(subagent_id, {}).get("started_at", 0),
            "status": "completed",
            "completed_at": time.time(),
            "conv_id": _background_subagent_status.get(subagent_id, {}).get("conv_id"),
        }
        _notify_subagent_status_change(subagent_id, _background_subagent_status[subagent_id])
        result_path = f"/tmp/opencode_subagent_{subagent_id}_result.txt"
        try:
            with open(result_path, "w", encoding="utf-8") as f:
                f.write(result)
        except OSError:
            pass
        _background_subagent_results[subagent_id] = result
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


def _execute_subagent_sync(task: str, max_turns: int, agent_type: str,
                           max_tokens: Optional[int] = None) -> str:
    """Synchronous sub-agent execution (internal)."""
    sub_tools = _build_subagent_tools(agent_type)
    type_info = _SUBAGENT_TYPES[agent_type]

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
        return f"错误：{e}"
    if not config.api_key:
        _bash_state.cwd = saved_bash_cwd
        _bash_state.session = saved_bash_session
        local_session.stop()
        return "错误：未配置 LLM API key。请在 AI 设置中配置。"
    if not config.base_url:
        _bash_state.cwd = saved_bash_cwd
        _bash_state.session = saved_bash_session
        local_session.stop()
        return "错误：未配置 LLM base URL。请在 AI 设置中配置。"

    clamped_turns = max(1, min(max_turns, _MAX_SUBAGENT_TURNS))
    messages = [
        {"role": "system", "content": type_info["system_prompt"]},
        {"role": "user", "content": task},
    ]

    try:
        subagent_max_tokens = int(max_tokens) if max_tokens is not None else config.max_tokens
        if subagent_max_tokens is not None and subagent_max_tokens <= 0:
            subagent_max_tokens = 4096
    except (ValueError, TypeError):
        subagent_max_tokens = 4096

    try:
        from llm_client import _LLMHttpClient, _LLMHttpError, LLMRequestConfig
        llm = _LLMHttpClient()
        final_text = ""

        for turn in range(clamped_turns):
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
                return f"子代理 LLM 请求失败：{e}"
            except Exception as e:
                return f"子代理 LLM 请求异常：{e}"

            content = response.get("content") or ""
            tool_calls = response.get("tool_calls")

            if not tool_calls:
                final_text = content
                break

            assistant_msg = {"role": "assistant", "content": content or None}
            assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            for tc in tool_calls:
                tc_name = tc.get("function", {}).get("name", "")
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


def execute_sub_agent(task: str, max_turns: int = 10,
                      agent_type: str = "general",
                      run_in_background: bool = True,
                      max_tokens: Optional[int] = None) -> str:
    """Spawn an isolated sub-agent to complete a task independently."""
    if agent_type not in _SUBAGENT_TYPES:
        return f"错误：无效的子代理类型「{agent_type}」。有效值：general, explore, bash"

    if run_in_background:
        global _background_subagent_id
        _background_subagent_id += 1
        conv_id = get_current_conversation_id()
        short_hash = get_conv_short_hash(conv_id)
        subagent_id = f"{short_hash}-{_background_subagent_id}"
        _background_subagent_status[subagent_id] = {
            "task": task[:100],
            "started_at": time.time(),
            "status": "running",
            "conv_id": conv_id,
        }
        _notify_subagent_status_change(subagent_id, _background_subagent_status[subagent_id])
        _run_subagent_background(task, max_turns, agent_type, subagent_id, max_tokens)
        return (f"⏳ 子代理已启动（任务ID: {subagent_id}，类型: {agent_type}）。"
                f"完成后结果将保存至 /tmp/opencode_subagent_{subagent_id}_result.txt，"
                f"可让主代理使用 read_file 读取。")

    sync_result = _execute_subagent_sync(task, max_turns, agent_type, max_tokens)
    unescaped = html.unescape(sync_result)
    if len(unescaped) > _MAX_TOOL_RESULT_CHARS:
        return (unescaped[:_MAX_TOOL_RESULT_CHARS]
                + f"\n\n...（结果已截断，共 {len(unescaped)} 字符，详细内容建议使用后台运行查看）")
    return unescaped


def execute_get_subagent_status(id: Optional[Any] = None,
                                clear_completed: bool = False) -> str:
    """查询后台子代理的执行状态。"""
    global _background_subagent_status

    now = time.time()
    to_remove = []
    for sid, info in list(_background_subagent_status.items()):
        if info.get("status") == "completed":
            completed_at = info.get("completed_at", 0)
            if completed_at and (now - completed_at) > _SUBAGENT_CLEANUP_AGE:
                to_remove.append(sid)
    for sid in to_remove:
        del _background_subagent_status[sid]
        _notify_subagent_status_change(sid, None)

    if clear_completed:
        to_clear = [sid for sid, info in _background_subagent_status.items()
                    if info.get("status") == "completed"]
        for sid in to_clear:
            del _background_subagent_status[sid]
            _notify_subagent_status_change(sid, None)
        if not _background_subagent_status:
            return "✅ 已清除所有已完成的后台子代理记录。"
        return f"✅ 已清除 {len(to_clear)} 个已完成的后台子代理记录。"

    if not _background_subagent_status:
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
    return "\n".join(lines).strip()


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
                    "max_turns": {"type": "integer", "description": "最大工具调用轮数（1-15，默认 10）", "default": 10},
                    "agent_type": {"type": "string", "description": "代理类型：general（全工具）、explore（只读探索）、bash（仅命令执行）", "enum": ["general", "explore", "bash"], "default": "general"},
                    "run_in_background": {"type": "boolean", "description": "是否在后台运行（默认 true，后台运行）", "default": True},
                    "max_tokens": {"type": "integer", "description": "子代理响应的最大 token 数（可选）"}
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
