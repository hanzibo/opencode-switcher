"""
Tool Registry package — 24 AI tool executors for the AI panel.

Replaces the monolithic tool_registry.py. Each tool group is a separate module.
This __init__.py assembles TOOL_DEFINITIONS (from per-module TOOL_SCHEMAS),
TOOL_EXECUTORS (dispatch dict), and re-exports all public symbols.
"""

import json
from typing import Any, Callable, Dict, List

from . import common
from . import todo
from . import filesystem
from . import search
from . import web
from . import bash
from . import notification
from . import mail
from . import display
from . import subagent

from . import code_analysis as _code_analysis


# ── Public constants ────────────────────────────────────────────────

TOOL_CHOICE_AUTO = "auto"

ERROR_PREFIXES = ("❌", "错误：", "执行工具「", "搜索失败", "获取页面失败", "子代理")


# ── Assemble TOOL_DEFINITIONS from per-module TOOL_SCHEMAS ──────────

TOOL_DEFINITIONS: List[Dict[str, Any]] = []
for _mod in [common, todo, filesystem, search, web, bash,
             notification, mail, display, subagent, _code_analysis]:
    for _schema in getattr(_mod, "TOOL_SCHEMAS", []):
        TOOL_DEFINITIONS.append(_schema)


# ── Assemble TOOL_EXECUTORS dispatch dict ───────────────────────────

TOOL_EXECUTORS: Dict[str, Callable] = {
    # common
    "get_current_time": common.execute_get_current_time,
    "ask_user_question": common.execute_ask_user_question,
    # todo
    "todo_create": todo.execute_todo_create,
    "todo_update": todo.execute_todo_update,
    "todo_list": todo.execute_todo_list,
    # filesystem
    "list_directory": filesystem.execute_list_directory,
    "read_file": filesystem.execute_read_file,
    "write_file": filesystem.execute_write_file,
    "edit_file": filesystem.execute_edit_file,
    "delete_file": filesystem.execute_delete_file,
    "rename_file": filesystem.execute_rename_file,
    "file_info": filesystem.execute_file_info,
    # search
    "grep_search": search.execute_grep_search,
    "glob_find": search.execute_glob_find,
    # web
    "web_search": web.execute_web_search,
    "web_fetch": web.execute_web_fetch,
    # bash
    "bash": bash.execute_bash,
    "bash_get_session_info": bash.execute_bash_get_session_info,
    # notification
    "send_notification": notification.execute_send_notification,
    # mail
    "read_qq_mail": mail.execute_read_qq_mail,
    # subagent
    "sub_agent": subagent.execute_sub_agent,
    "get_subagent_status": subagent.execute_get_subagent_status,
    # code_analysis
    "get_code_metrics": _code_analysis.execute_get_code_metrics,
    "find_project_dependencies": _code_analysis.execute_find_dependencies,
    "parse_file_ast": _code_analysis.execute_parse_file_ast,
}


# ── Tool dispatcher ─────────────────────────────────────────────────

def execute_tool_call(tool_call: dict, cancel_event=None) -> str:
    """Execute a single tool call and return the result as a string.

    Args:
        tool_call: OpenAI-format tool_call dict with "id", "type", "function" keys.
        cancel_event: Optional threading.Event for cancellation signaling.

    Returns:
        String result to be sent back as tool role content.
    """
    name = tool_call.get("function", {}).get("name", "")
    arguments_raw = tool_call.get("function", {}).get("arguments", "{}")

    try:
        arguments = json.loads(arguments_raw)
    except json.JSONDecodeError:
        return f"工具调用参数解析失败：无效的 JSON ({arguments_raw[:200]})"

    executor = TOOL_EXECUTORS.get(name)
    if not executor:
        return f"错误：未知工具「{name}」"

    try:
        import inspect
        sig = inspect.signature(executor)
        if 'cancel_event' in sig.parameters:
            return executor(**arguments, cancel_event=cancel_event)
        return executor(**arguments)
    except Exception as e:
        return f"执行工具「{name}」时出错：{e}"


# ── Re-export display formatters ────────────────────────────────────

format_tool_calls_for_display = display.format_tool_calls_for_display
render_collapsible_tool_result = display.render_collapsible_tool_result
format_tool_result_for_display = display.format_tool_result_for_display


# ── Re-export bash cwd functions ────────────────────────────────────

set_bash_cwd = bash.set_bash_cwd
get_bash_cwd = bash.get_bash_cwd


# ── Re-export subagent status functions ─────────────────────────────

set_current_conversation_id = subagent.set_current_conversation_id
get_current_conversation_id = subagent.get_current_conversation_id
check_background_subagents = subagent.check_background_subagents
register_subagent_status_listener = subagent.register_subagent_status_listener
unregister_subagent_status_listener = subagent.unregister_subagent_status_listener
get_subagent_status_map = subagent.get_subagent_status_map
remove_subagent_status = subagent.remove_subagent_status


# ── Re-export web constants ─────────────────────────────────────────

MAX_TOOL_RESULT_CHARS = 20000
