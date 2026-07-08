"""
Tool Registry — Function Calling 工具定义与执行器

为 AI 面板提供 OpenAI-compatible 工具注册、调度和执行能力。
首期工具：web_search（网络搜索）

所有工具使用零新外部依赖（requests 和 stdlib only）。
"""

import ast
import datetime
import difflib
import fnmatch
import json
import os
import pathlib
import select
import stat
import subprocess
import tempfile
import time
import urllib.parse
import re
import shutil
import sys
import inspect
import html
import uuid
from html.parser import HTMLParser
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime, parsedate_tz, mktime_tz
from typing import Any, Dict, Final, List, Optional, Callable, Tuple

import requests

from llm_client import _LLMHttpClient, _LLMHttpError


_IGNORE_DIRS: Final = {"node_modules", "venv", ".venv", "env", "__pycache__", "build", "dist", "target", "cache", ".cache"}

# ── Status Emoji Mapping (unified for todo tools) ───────────────────────────

STATUS_EMOJI: Final[Dict[str, str]] = {
    "pending": "⏳",
    "blocked": "🔴",
    "in_progress": "🔄",
    "completed": "✅",
    "failed": "❌",
    "cancelled": "⭕",
}


def _status_emoji(status: str) -> str:
    """Return emoji for a given task status."""
    return STATUS_EMOJI.get(status, "❓")


def _get_ignore_dirs() -> set:
    config_path = os.path.expanduser("~/.config/opencode-switcher/config.json")
    ignore_dirs = set(_IGNORE_DIRS)
    if os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
            custom_ignores = config.get("search_ignore_dirs", [])
            if isinstance(custom_ignores, list):
                for d in custom_ignores:
                    if isinstance(d, str) and d.strip():
                        ignore_dirs.add(d.strip())
        except Exception:
            pass
    return ignore_dirs


# ── Sub-Agent Constants ──────────────────────────────────────────

_SUBAGENT_BLOCKED_TOOLS: Final[frozenset] = frozenset([
    "ask_user_question",
    "read_qq_mail",
    "sub_agent",
])


# ── Read State Tracking (for edit_file staleness check) ─────────────────────

_READ_FILE_STATE: Dict[str, Dict[str, Any]] = {}
"""Tracks full file reads for edit_file staleness validation.
Key: resolved absolute path.
Value: {"content": str, "mtime": float, "full_read": bool, "encoding": str, "line_ending": str}
"""


def _check_file_stale(path: str) -> Optional[str]:
    """Check if file has been modified since read_file was called.
    Returns None if OK, error message string if stale/missing.
    """
    resolved = os.path.realpath(path)
    state = _READ_FILE_STATE.get(resolved)
    if state is None:
        return f"错误：文件「{path}」尚未被读取。请先使用 read_file 工具读取该文件。"
    if not state.get("full_read", False):
        return f"错误：文件「{path}」之前只读取了部分内容。请使用 read_file 完整读取后再编辑。"
    try:
        current_mtime = os.path.getmtime(resolved)
    except OSError:
        return f"错误：无法访问文件「{path}」"
    if current_mtime > state["mtime"]:
        try:
            with open(resolved, "rb") as f:
                raw = f.read()
            current_content = raw.decode(state.get("encoding", "utf-8"), errors="replace")
        except Exception:
            return f"错误：文件「{path}」自读取后已被修改，请重新读取。"
        if current_content != state["content"]:
            return f"错误：文件「{path}」自读取后已被外部修改，请重新使用 read_file 读取。"
        # False positive (cloud sync, antivirus), update mtime
        state["mtime"] = current_mtime
    return None


# ── Todo Storage (persistent task management) ───────────────────────────────

_TODO_DIR: Final[str] = os.path.expanduser("~/.config/opencode-switcher")
_TODO_PATH: Final[str] = os.path.join(_TODO_DIR, "todos.json")


def _load_todos() -> Dict[str, Any]:
    """Load all todos from disk. Returns {"version": 1, "todos": [...], "next_id": int}."""
    default = {"version": 1, "todos": [], "next_id": 1}
    if not os.path.isfile(_TODO_PATH):
        return default
    try:
        with open(_TODO_PATH, "r", encoding="utf-8") as f:
            data: Dict[str, Any] = json.load(f)
        if "todos" not in data:
            return default
        return data
    except (json.JSONDecodeError, TypeError, KeyError):
        return default


def _save_todos(data: Dict[str, Any]) -> None:
    """Save todos to disk."""
    os.makedirs(_TODO_DIR, exist_ok=True)
    with open(_TODO_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _find_todo(todos: List[Dict[str, Any]], todo_id: str) -> Optional[Dict[str, Any]]:
    """Find a todo by ID in the list."""
    for t in todos:
        if t["id"] == todo_id:
            return t
    return None


def _check_cycle(todos: List[Dict[str, Any]], todo_id: str, blocked_by: List[str],
                 seen: Optional[set] = None) -> bool:
    """Detect circular dependencies. Returns True if cycle found."""
    if seen is None:
        seen = set()
    if todo_id in seen:
        return True
    seen.add(todo_id)
    for dep_id in blocked_by:
        dep = _find_todo(todos, dep_id)
        if dep is None:
            continue
        # Check if dep is blocked_by (or transitively) back to todo_id
        if _check_cycle(todos, dep_id, dep.get("blocked_by", []), seen.copy()):
            return True
    return False


def _update_dependents(todos: List[Dict[str, Any]], completed_id: str) -> None:
    """When a task completes, check if any tasks blocked by it become unblocked.
    A blocked task becomes pending only when ALL its blocked_by tasks are completed.
    """
    completed_ids = {t["id"] for t in todos if t["status"] == "completed"}
    completed_ids.add(completed_id)

    for t in todos:
        if t.get("status") == "blocked":
            blocked_by = t.get("blocked_by", [])
            if blocked_by and all(dep in completed_ids for dep in blocked_by):
                t["status"] = "pending"
                t["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()


def execute_todo_create(title: str, description: str = "",
                        priority: str = "medium",
                        blocked_by: Optional[List[str]] = None,
                        active_form: str = "",
                        verification: str = "") -> str:
    """Create a new todo item."""
    if not title.strip():
        return "错误：任务标题不能为空。"

    data = _load_todos()
    todos = data["todos"]

    todo_id = "todo_" + uuid.uuid4().hex[:8]

    if blocked_by:
        for dep_id in blocked_by:
            if _find_todo(todos, dep_id) is None:
                return f"错误：依赖任务「{dep_id}」不存在。"

        if _check_cycle(todos, todo_id, blocked_by):
            return "错误：检测到循环依赖，请检查 blocked_by 设置。"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    initial_status = "blocked" if blocked_by else "pending"

    new_todo: Dict[str, Any] = {
        "id": todo_id,
        "title": title,
        "description": description,
        "active_form": active_form,
        "verification": verification,
        "status": initial_status,
        "priority": priority,
        "blocked_by": blocked_by or [],
        "created_at": now,
        "updated_at": now,
    }

    todos.append(new_todo)
    data["next_id"] += 1
    _save_todos(data)

    emoji = _status_emoji(initial_status)
    return f"{emoji} 已创建任务「{title}」（ID: {todo_id}，状态: {initial_status}）"


def execute_todo_update(id: str, status: Optional[str] = None,
                        title: Optional[str] = None,
                        description: Optional[str] = None,
                        priority: Optional[str] = None,
                        add_blocked_by: Optional[List[str]] = None,
                        active_form: Optional[str] = None) -> str:
    """Update an existing todo item."""
    data = _load_todos()
    todos = data["todos"]
    todo = _find_todo(todos, id)

    if todo is None:
        return f"错误：未找到 ID 为「{id}」的任务。"

    if title is not None:
        todo["title"] = title
    if description is not None:
        todo["description"] = description
    if priority is not None:
        todo["priority"] = priority
    if active_form is not None:
        todo["active_form"] = active_form

    if add_blocked_by:
        for dep_id in add_blocked_by:
            if _find_todo(todos, dep_id) is None:
                return f"错误：依赖任务「{dep_id}」不存在。"
        # Cycle check: would adding these blocked_by create a cycle?
        new_blocked_by = todo.get("blocked_by", []) + add_blocked_by
        if _check_cycle(todos, id, new_blocked_by):
            return "错误：检测到循环依赖，请检查 add_blocked_by 设置。"
        todo["blocked_by"] = new_blocked_by
        # If currently pending/in_progress, re-evaluate status
        if todo["status"] in ("pending", "in_progress"):
            todo["status"] = "blocked"

    if status is not None:
        valid_statuses = ("pending", "in_progress", "completed", "failed", "cancelled")
        if status not in valid_statuses:
            return f"错误：无效的状态「{status}」。有效值：{', '.join(valid_statuses)}"
        todo["status"] = status

        # If completed, unlock dependents
        if status == "completed":
            _update_dependents(todos, id)

    todo["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _save_todos(data)

    emoji = _status_emoji(todo.get("status", "pending"))
    return f"{emoji} 已更新任务「{todo['title']}」状态为 {todo['status']}"


def execute_todo_list(id: Optional[str] = None,
                      status_filter: Optional[str] = None,
                      sort_by: str = "created_at") -> str:
    """List or query todo items."""
    data = _load_todos()
    todos = data["todos"]

    if not todos:
        return "📋 暂无任务。"

    # Single task detail mode
    if id is not None:
        todo = _find_todo(todos, id)
        if todo is None:
            return f"错误：未找到 ID 为「{id}」的任务。"
        emoji = _status_emoji(todo.get("status", "pending"))
        blocked_by_str = ", ".join(todo.get("blocked_by", [])) or "无"
        desc = todo.get("description", "")
        desc_block = f"描述:\n{desc}\n---\n" if desc else ""
        active_form = todo.get("active_form", "")
        active_form_block = f"当前动作: {active_form}\n" if active_form else ""
        verification = todo.get("verification", "")
        verification_block = f"验证标准: {verification}\n" if verification else ""
        return (
            f"📋 任务详情\n"
            f"---\n"
            f"ID:          {todo['id']}\n"
            f"标题:        {todo['title']}\n"
            f"状态:        {emoji} {todo['status']}\n"
            f"优先级:      {todo.get('priority', 'medium')}\n"
            f"依赖:        {blocked_by_str}\n"
            f"{active_form_block}{verification_block}"
            f"创建时间:    {todo.get('created_at', '')}\n"
            f"更新时间:    {todo.get('updated_at', '')}\n"
            f"---\n"
            f"{desc_block}"
        )

    # List mode
    if status_filter:
        filtered = [t for t in todos if t.get("status") == status_filter]
        if not filtered:
            return f"📋 没有状态为「{status_filter}」的任务。"
        todos_to_show = filtered
    else:
        todos_to_show = todos

    # Sort
    if sort_by == "priority":
        priority_order = {"high": 0, "medium": 1, "low": 2}
        todos_to_show = sorted(todos_to_show, key=lambda t: priority_order.get(t.get("priority", "medium"), 1))
    elif sort_by == "updated_at":
        todos_to_show = sorted(todos_to_show, key=lambda t: t.get(sort_by, ""), reverse=True)
    else:
        todos_to_show = sorted(todos_to_show, key=lambda t: t.get(sort_by, ""))

    # Statistics
    total = len(todos)
    completed = sum(1 for t in todos if t.get("status") == "completed")
    in_progress = sum(1 for t in todos if t.get("status") == "in_progress")
    pending = sum(1 for t in todos if t.get("status") == "pending")
    blocked = sum(1 for t in todos if t.get("status") == "blocked")

    lines = [
        f"📋 任务清单（共 {len(todos_to_show)} 项）",
        f"   统计: {completed}✅ {in_progress}🔄 {pending}⏳ {blocked}🔴 / {total}总计",
        ""
    ]
    for t in todos_to_show:
        emoji = _status_emoji(t.get("status", "pending"))
        blocked_str = ""
        if t.get("blocked_by"):
            blocked_str = f" ⚠ 依赖: {', '.join(t['blocked_by'])}"
        lines.append(f"{emoji} [{t['status']}]  {t['title']}（ID: {t['id']}）{blocked_str}")

    return "\n".join(lines)


# ── Tool Definitions (OpenAI function calling schema) ──────────────────────

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索互联网获取实时信息。当用户问及实时新闻、最新动态、技术文档、或不确认的知识时使用。返回搜索结果列表（标题、URL、摘要）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，使用中文或英文关键词均可"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "返回结果数量（1-10，默认 5）",
                        "default": 5
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "获取指定 URL 的页面内容并转换为纯文本。用于阅读文章、文档、新闻等具体页面。支持缓存（5分钟）和超时配置。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要获取的页面完整 URL（以 http:// 或 https:// 开头）"
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最多返回字符数（默认 5000，范围 500-20000）",
                        "default": 5000
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "请求超时秒数（默认 20，范围 5-60）。对慢页面或大文件可适当增大。",
                        "default": 20
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出指定目录的内容。仅接受绝对路径。返回文件和子目录列表，每条显示类型标记、大小和修改时间（[DIR] 目录、[FILE] 文件、[LINK] 符号链接）。支持按名称/大小/时间排序。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要列出的目录的绝对路径"
                    },
                    "include_hidden": {
                        "type": "boolean",
                        "description": "是否包含隐藏文件（以 . 开头），默认不包含",
                        "default": False
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["name", "size", "time"],
                        "description": "排序方式：name（按名称字母序，默认）、size（按文件大小，目录排后）、time（按修改时间，最新在前）",
                        "default": "name"
                    },
                    "reverse": {
                        "type": "boolean",
                        "description": "是否反向排序（默认 false）。例如 sort_by=time + reverse=true 则最早修改的排最前。",
                        "default": False
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取指定文件的内容。仅接受绝对路径，自动检测并拒绝二进制文件。返回文件的纯文本内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要读取的文件的绝对路径"
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最多返回字符数（默认 5000，最大 50000）",
                        "default": 5000
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（1-indexed，包含，可选，默认为 1）",
                        "default": 1
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（1-indexed，包含，可选，默认读取至文件末尾）"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间。支持可选时区参数。默认返回系统本地时间。用于回答当前时间、日期、星期几等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "时区名称，如「Asia/Shanghai」「America/New_York」「UTC」。留空则使用系统本地时区。",
                        "default": ""
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "在目录中按正则表达式或关键词全文搜索文件内容。支持大小写控制、纯文本搜索和上下文行。自动跳过隐藏目录和二进制文件。优先使用 ripgrep（如已安装），否则回退至内置搜索。仅接受绝对路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "搜索的正则表达式或关键词（支持 Python re 语法）。如 literal=true 则视为纯文本。"
                    },
                    "path": {
                        "type": "string",
                        "description": "要搜索的根目录的绝对路径"
                    },
                    "include": {
                        "type": "string",
                        "description": "文件通配过滤模式，如「*.py」「*.{ts,js}」。留空则搜索所有文本文件。",
                        "default": ""
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回的匹配行数（1-500，默认 50）",
                        "default": 50
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "搜索结果总字符数上限（500-50000，默认 8000）。超出后截断并提示。与 max_results 双上限，任一先达即截断。",
                        "default": 8000
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "忽略大小写搜索（默认 false）",
                        "default": False
                    },
                    "literal": {
                        "type": "boolean",
                        "description": "将 pattern 视为纯文本而非正则表达式（默认 false，搜索函数名等字符串时有用）",
                        "default": False
                    },
                    "context": {
                        "type": "integer",
                        "description": "匹配行前后各显示的上下文行数（0-10，默认 0）",
                        "default": 0
                    }
                },
                "required": ["pattern", "path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob_find",
            "description": "按通配模式递归查找文件。仅接受绝对路径。自动跳过隐藏目录和常见忽略目录。返回每行包含类型标记、大小和修改时间的列式列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "通配模式，如「**/*.py」「config*.json」「src/**/*.ts」"
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索的根目录绝对路径"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回的文件数（1-500，默认 100）",
                        "default": 100
                    },
                    "exclude": {
                        "type": "string",
                        "description": "要排除的通配模式，如「*__pycache__*」「*.min.js」。留空不额外排除。",
                        "default": ""
                    }
                },
                "required": ["pattern", "path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_info",
            "description": "获取文件或目录的元信息：大小、修改时间、访问时间、Unix 权限、类型（文件/目录/符号链接）、所有者等。仅接受绝对路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件或目录的绝对路径"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user_question",
            "description": "向用户提问以获取澄清信息。当你需要更多信息、确认或用户决策才能继续执行时使用。问题应当清晰具体，避免模糊提问。",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "要向用户提出的问题，应当清晰具体"
                    }
                },
                "required": ["question"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "创建新文件、完全覆盖已有文件、或追加内容。仅接受绝对路径。默认不覆盖已有文件（需设置 force=True 覆盖）。"
                           "对已有文件的局部修改请优先使用 edit_file，它只发送差异部分，更安全且不易误覆盖。"
                           "当父目录不存在时将自动创建。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要写入的文件的绝对路径"
                    },
                    "content": {
                        "type": "string",
                        "description": "文件内容（文本格式）"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["write", "append"],
                        "description": "写入模式：write（覆盖写入，默认）、append（追加到文件末尾）。append 模式下 force 参数被忽略。",
                        "default": "write"
                    },
                    "force": {
                        "type": "boolean",
                        "description": "当目标文件已存在时是否覆盖，默认 false",
                        "default": False
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "对已读取过的文件执行替换操作。支持两种互斥模式："
                           "mode=string（默认）用 old_string 精确匹配原文替换为 new_string；"
                           "mode=line 按行号范围替换。"
                           "修改前会校验文件自读取后是否被外部修改——若已过期则拒绝并提示重新读取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要编辑的文件的绝对路径"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["string", "line"],
                        "description": "替换模式：string（精确字符串替换，默认）、line（行号范围替换）。两种模式互斥。",
                        "default": "string"
                    },
                    "old_string": {
                        "type": "string",
                        "description": "【string 模式】要被替换的原文。必须精确匹配文件中的内容，包括空格和缩进。"
                    },
                    "new_string": {
                        "type": "string",
                        "description": "替换后的新内容（两种模式均适用）"
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "【string 模式】是否替换所有匹配项。默认 false（只替换第一个匹配）。设为 true 替换所有。",
                        "default": False
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "【line 模式】起始行号（1-indexed，包含）。替换从此行开始。"
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "【line 模式】结束行号（1-indexed，包含）。替换到此行结束。省略则只替换 start_line 一行。"
                    }
                },
                "required": ["path", "new_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "todo_create",
            "description": "创建新任务。返回任务的唯一 ID。任务创建后默认为 pending 状态。如果指定了 blocked_by（依赖任务列表），则状态自动设为 blocked，直到所有依赖任务完成。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "任务标题，简短描述任务内容（建议 ≤60 字）"
                    },
                    "description": {
                        "type": "string",
                        "description": "任务详细描述，包含需求、验收标准、实现思路等"
                    },
                    "active_form": {
                        "type": "string",
                        "description": "当前执行动作描述（例如「正在搜索文档」「正在编写测试」），用于进度展示"
                    },
                    "verification": {
                        "type": "string",
                        "description": "验证标准（例如「测试全部通过」「代码编译无错误」），明确任务完成的判定条件"
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "优先级（默认 medium）",
                        "default": "medium"
                    },
                    "blocked_by": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "依赖的任务 ID 列表。未完成时，此任务自动标记为 blocked。"
                    }
                },
                "required": ["title"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "todo_update",
            "description": "更新任务的状态或字段。最常用操作：标记任务为 in_progress 或 completed。"
                       "当标记为 completed 时，自动检查并解除依赖该任务的其他 blocked 任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "任务 ID"
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "failed", "cancelled"],
                        "description": "新状态。pending→in_progress→completed 为主流程。"
                    },
                    "title": {
                        "type": "string",
                        "description": "更新任务标题"
                    },
                    "description": {
                        "type": "string",
                        "description": "更新任务描述"
                    },
                    "active_form": {
                        "type": "string",
                        "description": "更新当前执行动作描述（例如「正在分析代码」「正在运行测试」）"
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "更新优先级"
                    },
                    "add_blocked_by": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "添加依赖的任务 ID 列表。会自动检测循环依赖。"
                    }
                },
                "required": ["id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "todo_list",
            "description": "列出任务摘要或查询单个任务详情。不指定 id 时返回所有任务摘要（含状态、优先级、依赖）。指定 id 时返回该任务的完整信息（含描述）。可选 status_filter 仅列出特定状态的任务。返回统计信息（完成数/进行中/待处理/阻塞）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "可选。指定后返回该任务的完整详情（含 description、active_form、verification）。省略则返回所有任务摘要。"
                    },
                    "status_filter": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "failed", "cancelled", "blocked"],
                        "description": "可选。仅列出指定状态的任务。只在列表模式（id 为空时）有效。"
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["created_at", "updated_at", "priority"],
                        "description": "可选。排序方式：created_at（默认，按创建时间）、updated_at（最近更新优先）、priority（高优先级在前）。",
                        "default": "created_at"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "执行 shell 命令。在一个持久化的 bash session 中运行，环境变量和工作目录跨命令保持。支持命令链（&&、||、;）。不支持交互式命令（vim、less、top 等需要 TTY 的程序）。命令执行结果包含 stdout、stderr 和退出码。非零退出码不会导致工具崩溃——错误信息会作为正常反馈返回。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 shell 命令。支持多行和命令链。"
                    },
                    "restart": {
                        "type": "boolean",
                        "description": "设为 true 以重启 bash session。当 session 卡死、超时或状态异常时使用。",
                        "default": False
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "命令超时秒数（1 到 120，默认 60）。超时后 session 将进入异常状态，需重启。",
                        "default": 60
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除文件或空目录。可设置 recursive=True 递归删除非空目录（相当于 rm -r）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要删除的文件或目录的绝对路径"
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "是否递归删除目录。对非空目录必须设为 true（默认 false）",
                        "default": False
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rename_file",
            "description": "重命名或移动文件/目录。源路径必须存在，目标路径不能已存在（除非设置 force=True）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "源文件或目录的绝对路径"
                    },
                    "destination": {
                        "type": "string",
                        "description": "目标路径的绝对路径"
                    },
                    "force": {
                        "type": "boolean",
                        "description": "是否覆盖已存在的目标路径（默认 false）",
                        "default": False
                    }
                },
                "required": ["source", "destination"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_notification",
            "description": "发送桌面通知给用户。当后台任务完成、长时间操作结束、或需要异步提醒用户时使用。支持设置标题、正文、紧急程度和显示时长。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "通知标题（简短总结，例如「编译完成」「任务完成」「有新的AI消息」）"
                    },
                    "body": {
                        "type": "string",
                        "description": "通知正文（详细信息，可选）"
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["low", "normal", "critical"],
                        "description": "紧急程度：low（低）/ normal（普通）/ critical（紧急），默认 normal",
                        "default": "normal"
                    },
                    "expire_time": {
                        "type": "integer",
                        "description": "显示时长（毫秒），默认 5000ms。注意部分通知服务（GNOME Shell）可能忽略此参数。",
                        "default": 5000
                    },
                    "icon": {
                        "type": "string",
                        "description": "图标名称或路径，例如「dialog-information」「dialog-warning」「firefox」等 freedesktop 图标名",
                    }
                },
                "required": ["summary"]
            }
        }
    },
    {
        "type": "function",
        "function": {
             "name": "sub_agent",
            "description": "创建一个独立子代理来完成指定任务。子代理拥有独立的上下文窗口和工具集，"
                           "与当前对话完全隔离。支持多种代理类型（general/explore/bash）和后台运行。"
                           "任务完成后返回最终结果摘要。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "子代理要完成的任务描述。应包含明确的目标和可验证的交付物。"
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": "子代理最大工具调用轮数（1-15，默认 10）",
                        "default": 10
                    },
                    "agent_type": {
                        "type": "string",
                        "enum": ["general", "explore", "bash"],
                        "description": "子代理类型：general（通用全能，默认）、explore（只读探索）、bash（仅命令执行）",
                        "default": "general"
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": "设为 true 在后台运行，不阻塞当前对话。完成后将通过通知提醒。",
                        "default": False
                    }
                },
                "required": ["task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_subagent_status",
            "description": "查询后台子代理的执行状态。当已启动后台子代理（run_in_background=true）后，"
                           "可以用此工具检查其是否完成。返回所有后台子代理的 ID、状态和任务描述。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_qq_mail",
            "description": "读取QQ邮箱收件箱中的邮件。需要先在设置中配置QQ邮箱地址和授权码（IMAP密码）。"
                           "支持按数量、文件夹和搜索条件筛选邮件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "要获取的邮件数量（1-20，默认 5）",
                        "default": 5
                    },
                    "folder": {
                        "type": "string",
                        "description": "邮箱文件夹名称（默认「INBOX」收件箱）",
                        "default": "INBOX"
                    },
                    "search_criteria": {
                        "type": "string",
                        "description": "IMAP 搜索条件（默认「ALL」全部）。"
                                       "常用值：ALL（全部）、UNSEEN（未读）、FROM xxx（来自某人）、"
                                       "SUBJECT xxx（主题包含）、SINCE 01-Jul-2026（指定日期后）",
                        "default": "ALL"
                    },
                    "include_body": {
                        "type": "boolean",
                        "description": "是否包含邮件正文内容（默认 true）",
                        "default": True
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_code_metrics",
            "description": "分析文件或目录的代码度量指标：总行数、代码行数、注释行数、空行数、"
                           "函数/类数量。支持任何文本文件。对于 Python 文件额外提供函数和类计数。"
                           "可用 include 过滤文件类型（如 *.py），exclude 排除文件或目录，"
                           "sort_by 控制排序方式。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件或目录的绝对路径。如果是目录，汇总其中所有文件的度量。"
                    },
                    "include": {
                        "type": "string",
                        "description": "文件通配过滤（逗号分隔多值），例如「*.py」只统计 Python 文件。默认包含常见代码文件类型。",
                        "default": ""
                    },
                    "exclude": {
                        "type": "string",
                        "description": "排除的文件或目录（逗号分隔多值，支持通配），例如「*test*,node_modules」。默认已排除 .git、__pycache__、venv 等。",
                        "default": ""
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "目录模式的排序方式：total（按总行数降序）、code（按代码行降序）、name（按文件名升序）。默认 total。",
                        "default": "total"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_project_dependencies",
            "description": "分析文件或项目的依赖关系。Python 文件使用 ast 模块精确提取 import 语句，"
                           "JS/TS/Go 使用正则提取。将依赖分类为标准库、第三方包和本地模块，"
                           "并检测循环依赖和孤立模块。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件或目录的绝对路径。如果是目录，分析项目中所有文件间的依赖关系。"
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "是否递归扫描子目录（默认 true）",
                        "default": True
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "parse_file_ast",
            "description": "解析代码文件的语法结构，提取类、函数、导入语句、全局变量等结构化信息。"
                           "Python 项目使用 language=python（基于标准库 ast，推荐）。"
                           "JS/TS/Go/Rust/Java/C++ 等项目请传入对应语言名称，将使用 Tree-sitter 解析"
                           "（需安装 tree-sitter 及对应 grammar）。"
                           "不指定 language 时自动从文件后缀检测。"
                           "可通过 exclude_private 过滤私有函数，include_docstrings 显示文档摘要，"
                           "include_imports 控制是否列出导入语句。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件绝对路径"
                    },
                    "language": {
                        "type": "string",
                        "description": "语言类型：python / javascript / typescript / go / rust / java / cpp / auto（自动检测）",
                        "default": "auto"
                    },
                    "include_body": {
                        "type": "boolean",
                        "description": "是否包含函数/方法体源码（仅 Python 有效，默认 false）",
                        "default": False
                    },
                    "include_imports": {
                        "type": "boolean",
                        "description": "是否列出导入语句（默认 true）",
                        "default": True
                    },
                    "include_docstrings": {
                        "type": "boolean",
                        "description": "是否在函数/类旁显示文档字符串摘要（仅 Python，默认 false）",
                        "default": False
                    },
                    "exclude_private": {
                        "type": "boolean",
                        "description": "是否过滤以 _ 开头的私有函数/方法（默认 false）",
                        "default": False
                    }
                },
                "required": ["path"]
            }
        }
    },
]

TOOL_CHOICE_AUTO = "auto"

ERROR_PREFIXES = ("❌", "错误：", "执行工具「", "搜索失败", "获取页面失败", "子代理")

# ── Sub-Agent Helpers ────────────────────────────────────────────


def _get_llm_config() -> "LLMModelConfig":
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
    if agent_type == "explore":
        allowed = {"read_file", "grep_search", "glob_find", "list_directory", "file_info",
                    "get_current_time"}
        return [t for t in TOOL_DEFINITIONS
                if t.get("function", {}).get("name") in allowed]
    if agent_type == "bash":
        allowed = {"bash", "get_current_time", "ask_user_question"}
        return [t for t in TOOL_DEFINITIONS
                if t.get("function", {}).get("name") in allowed]
    # general: all tools except blocked
    return [
        t for t in TOOL_DEFINITIONS
        if t.get("function", {}).get("name") not in _SUBAGENT_BLOCKED_TOOLS
    ]


# ── Utility: strip HTML tags ───────────────────────────────────────────────

class _HTMLTagStripper(HTMLParser):
    """Strip HTML tags, extracting clean text."""
    def __init__(self):
        super().__init__()
        self._text_parts: List[str] = []
        self._skip_tags = {"script", "style"}

    def handle_data(self, data):
        if not hasattr(self, "_in_skip") or not self._in_skip:
            stripped = data.strip()
            if stripped:
                self._text_parts.append(stripped)

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._in_skip = True

    def handle_endtag(self, tag):
        if tag in self._skip_tags:
            self._in_skip = False
        elif tag in ("p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._text_parts.append("\n")

def strip_html(html_text: str) -> str:
    """Convert HTML to plain text."""
    stripper = _HTMLTagStripper()
    stripper.feed(html_text)
    return " ".join(stripper._text_parts)


# ── Web Search Parser ──────────────────────────────────────────────────────

class _DuckDuckGoResultParser(HTMLParser):
    """Parse DuckDuckGo HTML search results page."""
    def __init__(self, max_results: int):
        super().__init__()
        self.max_results = max_results
        self.results: List[Dict[str, str]] = []
        self._in_result_body = False
        self._in_title = False
        self._in_url = False
        self._in_snippet = False
        self._current: Dict[str, str] = {}
        self._div_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")

        if "result__body" in cls:
            self._in_result_body = True
            self._current = {}
            self._div_depth = 1
            return
        if not self._in_result_body:
            return

        if tag == "div":
            self._div_depth += 1

        if "result__title" in cls:
            self._in_title = True
        elif "result__url" in cls:
            self._in_url = True
        elif "result__snippet" in cls:
            self._in_snippet = True

    def handle_data(self, data):
        if self._in_title:
            self._current["title"] = self._current.get("title", "") + data
        elif self._in_url:
            self._current["url"] = self._current.get("url", "") + data
        elif self._in_snippet:
            self._current["snippet"] = self._current.get("snippet", "") + data

    def handle_endtag(self, tag):
        if self._in_result_body:
            if self._in_title and tag == "a":
                self._in_title = False
            elif self._in_url and tag == "a":
                self._in_url = False
            elif self._in_snippet and tag == "a":
                self._in_snippet = False

            if tag == "div":
                self._div_depth -= 1
                if self._div_depth <= 0 and self._current.get("title"):
                    if self.results is not None and len(self.results) < self.max_results:
                        # Deduplicate by title
                        title = self._current.get("title", "").strip()
                        url = self._current.get("url", "").strip()
                        snippet = self._current.get("snippet", "").strip()
                        if title and not any(r["title"] == title for r in self.results):
                            self.results.append({
                                "title": title,
                                "url": url,
                                "snippet": snippet,
                            })
                    self._in_result_body = False
                    self._current = {}


# ── Path Safety ────────────────────────────────────────────────────────────

def _resolve_safe_path(path: str) -> Optional[str]:
    """Resolve a path safely.

    Returns the resolved absolute path if it exists, None otherwise.
    Requires absolute paths to prevent directory traversal attacks.
    """
    if not path or not isinstance(path, str) or not os.path.isabs(path):
        return None
    resolved = os.path.realpath(path)
    if not os.path.exists(resolved):
        return None
    return resolved



def _resolve_write_path(path: str, force: bool = False) -> Optional[str]:
    """Resolve a path for write operations.

    Unlike _resolve_safe_path, this does NOT require the file to exist.
    Requires absolute paths to prevent directory traversal attacks.
    If the file already exists, force must be True to allow overwriting.

    Returns the resolved absolute path if valid, None otherwise.
    """
    if not path or not isinstance(path, str) or not os.path.isabs(path):
        return None
    resolved = os.path.realpath(path)
    parent = os.path.dirname(resolved)
    if not os.path.isdir(parent):
        return None
    if os.path.exists(resolved) and not force:
        return None
    return resolved


_MAX_DIRECTORY_LISTING = 200


def execute_list_directory(path: str, include_hidden: bool = False,
                           sort_by: str = "name", reverse: bool = False) -> str:
    """List contents of a directory. Accepts absolute paths only."""
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：目录不存在或路径无效「{path}」"
    if not os.path.isdir(resolved):
        return f"错误：路径不是目录「{resolved}」"
    try:
        raw_names = os.listdir(resolved)
    except PermissionError:
        return f"错误：无权访问目录「{resolved}」"
    except OSError as e:
        return f"错误：访问目录时出错「{resolved}」: {e}"

    # Collect entries with metadata for sorting
    entries = []
    for name in raw_names:
        if not include_hidden and name.startswith("."):
            continue
        full = os.path.join(resolved, name)
        try:
            st = os.lstat(full)
            is_dir = stat.S_ISDIR(st.st_mode)
            is_link = os.path.islink(full)
            entries.append((name, full, st, is_dir, is_link))
        except OSError:
            entries.append((name, full, None, False, False))

    # Sort by requested field
    if sort_by == "name":
        entries.sort(key=lambda e: (0 if e[3] else 1, e[0].lower()))
    elif sort_by == "size":
        entries.sort(key=lambda e: (
            0 if e[3] else 1,  # dirs before files
            -(e[2].st_size if e[2] is not None and not e[3] else 0)
        ))
    elif sort_by == "time":
        entries.sort(key=lambda e: (
            0 if e[3] else 1,
            -(e[2].st_mtime if e[2] is not None else 0)
        ))

    if reverse:
        entries.reverse()

    # Format output
    lines = []
    for name, full, st, is_dir, is_link in entries:
        try:
            if is_link:
                marker = "LINK"
                target = os.readlink(full)
                name_display = f"{name} → {target}"
            elif is_dir:
                marker = "DIR"
                name_display = name
            else:
                marker = "FILE"
                name_display = name
            if st is not None and not is_dir:
                size = _format_file_size(st.st_size)
            else:
                size = "—"
            if st is not None:
                mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M")
            else:
                mtime = "?"
        except OSError:
            marker = "?"
            size = "?"
            mtime = "?"
            name_display = name

        lines.append(f"[{marker:4s}] {size:>8s}  {mtime}  {name_display}")

    if not lines:
        return f"目录「{resolved}」为空。"

    total = len(lines)
    if total > _MAX_DIRECTORY_LISTING:
        lines = lines[:_MAX_DIRECTORY_LISTING]
        lines.append(f"\n...（已截断，仅显示前 {_MAX_DIRECTORY_LISTING} 项，共 {total} 项）")

    return f"📁 目录列表: {resolved}\n\n" + "\n".join(lines)


def execute_read_file(path: str, max_chars: int = 5000, start_line: int = 1, end_line: Optional[int] = None) -> str:
    """Read a text file's content. Accepts absolute paths only, with optional line range selection."""
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：文件不存在或路径无效「{path}」"
    if not os.path.isfile(resolved):
        return f"错误：路径不是文件「{resolved}」"

    max_chars = max(500, min(50000, max_chars))

    if start_line < 1:
        start_line = 1

    if end_line is not None and end_line < start_line:
        return f"错误：结束行号「{end_line}」不能小于起始行号「{start_line}」"

    try:
        with open(resolved, "rb") as f:
            header = f.read(8192)
    except PermissionError:
        return f"错误：无权读取文件「{resolved}」"
    except OSError as e:
        return f"错误：读取文件时出错「{resolved}」: {e}"

    if b"\x00" in header:
        return f"错误：文件「{resolved}」是二进制文件（或包含 null 字节），不支持读取。"

    try:
        with open(resolved, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        return f"错误：文件「{resolved}」不是有效的 UTF-8 文本文件。"
    except PermissionError:
        return f"错误：无权读取文件「{resolved}」"
    except OSError as e:
        return f"错误：读取文件时出错「{resolved}」: {e}"

    total_lines = len(lines)
    start_idx = start_line - 1
    if start_idx >= total_lines:
        return f"提示：文件「{resolved}」共有 {total_lines} 行，起始行号「{start_line}」超出了文件范围。"

    end_idx = total_lines if end_line is None else min(end_line, total_lines)
    sliced_lines = lines[start_idx:end_idx]

    content = "".join(sliced_lines)
    truncated_by_chars = False

    if len(content) > max_chars:
        content = content[:max_chars]
        truncated_by_chars = True

    # Determine whether this is a full read (for edit_file staleness tracking)
    is_full_read = (start_line == 1 and end_line is None and not truncated_by_chars)

    if truncated_by_chars:
        content += f"\n\n...（内容因超出 max_chars={max_chars} 字符而被截断）"
    elif end_line is not None and end_line < total_lines:
        content += f"\n\n...（已截断，仅显示第 {start_line} 至 {end_line} 行，文件共 {total_lines} 行）"
    elif start_line > 1:
        content += f"\n\n...（已截断，仅显示第 {start_line} 行至文件末尾，文件共 {total_lines} 行）"

    # Prepend file metadata header when reading from the start
    if start_line == 1:
        try:
            full_content_for_header = "".join(lines)
            line_ending = "CRLF" if "\r\n" in full_content_for_header else "LF"
            file_size_str = _format_file_size(os.path.getsize(resolved))
            header = (
                f"--- {os.path.basename(resolved)} ({file_size_str}, {total_lines} 行, utf-8, {line_ending})\n"
                f"--- {resolved}\n"
                + ("-" * 44) + "\n\n"
            )
            content = header + content
        except OSError:
            pass

    # Save read state for edit_file staleness check (full reads only)
    if is_full_read:
        try:
            full_content = "".join(lines)
            line_ending = "CRLF" if "\r\n" in full_content else "LF"
            _READ_FILE_STATE[resolved] = {
                "content": full_content,
                "mtime": os.path.getmtime(resolved),
                "full_read": True,
                "encoding": "utf-8",
                "line_ending": line_ending,
            }
        except OSError:
            pass

    return content


# ── Edit File Helpers ────────────────────────────────────────────────────────


def _atomic_write(path: str, content: str, line_ending: str = "LF") -> None:
    """Atomically write content to file via temp file + rename."""
    resolved = os.path.realpath(path)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(resolved),
        prefix=f'.{os.path.basename(resolved)}.',
        suffix='.tmp'
    )
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8',
                       newline=('\r\n' if line_ending == 'CRLF' else '\n')) as f:
            f.write(content)
        os.replace(tmp_path, resolved)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _generate_diff(old_content: str, new_content: str, path: str, n: int = 2) -> str:
    """Generate unified diff preview, max 30 lines."""
    basename = os.path.basename(path)
    diff_lines = list(difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f'a/{basename}',
        tofile=f'b/{basename}',
        n=n,
    ))
    if not diff_lines:
        return ""
    if len(diff_lines) > 30:
        diff_lines = diff_lines[:30] + [f"... (共 {len(diff_lines) - 30} 行已省略)\n"]
    return ''.join(diff_lines)


def _find_line_numbers(content: str, old_string: str) -> List[int]:
    """Find all line numbers where old_string appears."""
    lines = []
    start = 0
    while True:
        idx = content.find(old_string, start)
        if idx == -1:
            break
        line_num = content[:idx].count('\n') + 1
        lines.append(line_num)
        start = idx + 1
    return lines


def execute_edit_file(path: str, old_string: str = "", new_string: str = "",
                      replace_all: bool = False, mode: str = "string",
                      start_line: Optional[int] = None,
                      end_line: Optional[int] = None) -> str:
    """Edit a file via string replacement or line-range replacement.

    Two exclusive modes:
      - "string": Replace old_string with new_string (default).
      - "line": Replace lines [start_line, end_line] with new_string.

    Requires prior full read_file call (staleness check).
    """
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：文件不存在或路径无效「{path}」"
    if not os.path.isfile(resolved):
        return f"错误：路径不是文件「{resolved}」"

    stale_err = _check_file_stale(resolved)
    if stale_err is not None:
        return stale_err

    if mode not in ("string", "line"):
        return "错误：mode 必须是 'string' 或 'line'。"

    state = _READ_FILE_STATE.get(os.path.realpath(resolved))
    line_ending = state.get("line_ending", "LF") if state else "LF"

    try:
        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return f"错误：文件「{resolved}」不是有效的 UTF-8 文本文件。"
    except PermissionError:
        return f"错误：无权读取文件「{resolved}」"
    except OSError as e:
        return f"错误：读取文件时出错「{resolved}」: {e}"

    if mode == "line":
        # Line-based replacement mode
        lines = content.splitlines(keepends=True)
        if start_line is None:
            return "错误：line 模式下必须提供 start_line。"
        if start_line < 1 or start_line > len(lines):
            return f"错误：start_line {start_line} 超出文件范围（共 {len(lines)} 行）。"
        if end_line is not None:
            if end_line < start_line:
                return f"错误：end_line（{end_line}）不能小于 start_line（{start_line}）。"
            if end_line > len(lines):
                return f"错误：end_line {end_line} 超出文件范围（共 {len(lines)} 行）。"
        else:
            end_line = start_line

        # Preserve the original line ending of the last removed line
        removed_lines = lines[start_line - 1:end_line]
        trailing_ending = removed_lines[-1] if removed_lines else "\n"
        if trailing_ending and not trailing_ending.endswith("\n"):
            trailing_ending += "\n"

        # Insert the new content (ensure it ends with the same line ending)
        if new_string and not new_string.endswith("\n"):
            new_string += "\n"

        new_lines = lines[:start_line - 1] + [new_string] + lines[end_line:]
        new_content = "".join(new_lines)
        actual_changes = 1

        diff = _generate_diff(content, new_content, path)
        diff_block = f"\n{diff}" if diff else ""
        try:
            _atomic_write(resolved, new_content, line_ending)
        except PermissionError:
            return f"错误：无权写入文件「{path}」"
        except OSError as e:
            return f"错误：写入文件时出错「{path}」: {e}"

        _READ_FILE_STATE[os.path.realpath(resolved)] = {
            "content": new_content,
            "mtime": os.path.getmtime(resolved),
            "full_read": True,
            "encoding": "utf-8",
            "line_ending": line_ending,
        }
        return f"✅ 已编辑文件「{path}」\n   模式: line（L{start_line}-L{end_line}）\n   变更: {actual_changes} 处替换{diff_block}"

    # ── String mode (default) ──
    if not old_string:
        return "错误：string 模式下 old_string 不能为空。"

    if old_string not in content:
        return (
            f"错误：未能在文件「{path}」中找到指定的 old_string。\n\n"
            f"请确保 old_string 与文件内容完全匹配（包括空格和缩进）。\n"
            f"如需查看文件当前内容，请使用 read_file 读取。"
        )

    occurrence_count = content.count(old_string)
    if occurrence_count > 1 and not replace_all:
        line_nums = _find_line_numbers(content, old_string)
        lines_str = ", ".join(f"第 {n} 行" for n in line_nums)
        return (
            f"错误：old_string 在文件中出现了 {occurrence_count} 次\n"
            f"   位置: {lines_str}\n"
            f"   建议: 设置 replace_all=True 替换全部，或提供更多上下文以唯一匹配"
        )

    new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    actual_changes = occurrence_count if replace_all else 1

    try:
        _atomic_write(resolved, new_content, line_ending)
    except PermissionError:
        return f"错误：无权写入文件「{path}」"
    except OSError as e:
        return f"错误：写入文件时出错「{path}」: {e}"

    _READ_FILE_STATE[os.path.realpath(resolved)] = {
        "content": new_content,
        "mtime": os.path.getmtime(resolved),
        "full_read": True,
        "encoding": "utf-8",
        "line_ending": line_ending,
    }

    diff = _generate_diff(content, new_content, path)
    diff_block = f"\n{diff}" if diff else ""
    return f"✅ 已编辑文件「{path}」\n   变更: {actual_changes} 处替换{diff_block}"


# ── Delete / Rename ─────────────────────────────────────────────────────────


def execute_delete_file(path: str, recursive: bool = False) -> str:
    """Delete a file or empty directory.

    Uses _resolve_safe_path for safety validation.
    For non-empty directories, set recursive=True to delete recursively.
    """
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：文件或目录不存在「{path}」"

    try:
        if os.path.isdir(resolved):
            if recursive:
                item_count = sum(1 for _ in os.scandir(resolved))
                shutil.rmtree(resolved)
                return f"✅ 已递归删除目录: {path}（含 {item_count} 个子项）"
            else:
                os.rmdir(resolved)
                return f"✅ 已删除空目录: {path}"
        else:
            size_str = _format_file_size(os.path.getsize(resolved))
            os.remove(resolved)
            _READ_FILE_STATE.pop(os.path.realpath(resolved), None)
            return f"✅ 已删除文件: {path}（{size_str}）"
    except PermissionError:
        return f"错误：无权删除「{path}」"
    except OSError as e:
        return f"错误：删除「{path}」时出错: {e}"


def execute_rename_file(source: str, destination: str, force: bool = False) -> str:
    """Rename or move a file/directory.

    Source must exist (checked via _resolve_safe_path).
    Destination is checked via _resolve_write_path.
    If destination already exists, set force=True to overwrite.
    """
    resolved_src = _resolve_safe_path(source)
    if resolved_src is None:
        return f"错误：源文件或目录不存在「{source}」"

    is_file = os.path.isfile(resolved_src)
    src_size = _format_file_size(os.path.getsize(resolved_src)) if is_file else None

    resolved_dst = _resolve_write_path(destination, force=force)
    if resolved_dst is None:
        if os.path.exists(os.path.realpath(destination)) and not force:
            return f"错误：目标路径已存在。如需覆盖请设置 force=True。"
        return f"错误：目标路径无效「{destination}」"

    try:
        shutil.move(resolved_src, resolved_dst)
        # Invalidate any cached read state for the old path
        _READ_FILE_STATE.pop(os.path.realpath(resolved_src), None)
        parts = [f"✅ 已重命名: {source} → {destination}"]
        if src_size:
            parts.append(f"  大小: {src_size}")
        return "\n".join(parts)
    except OSError as e:
        return f"错误：重命名「{source}」→「{destination}」时出错: {e}"


# ── Time Query ─────────────────────────────────────────────────────────────

_WEEKDAYS_CN: Final[Tuple[str, ...]] = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def execute_get_current_time(timezone: str = "") -> str:
    """Get current date, time, weekday and timezone info.

    Args:
        timezone: IANA timezone name (e.g. "Asia/Shanghai", "UTC").
                  Empty string means system local time.

    Returns:
        Formatted string with current time info.
    """
    if timezone:
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(timezone)
            now = datetime.datetime.now(tz)
        except (ImportError, ModuleNotFoundError):
            return f"错误：当前 Python 版本不支持 zoneinfo，无法使用时区参数「{timezone}」。请留空 timezone 使用本地时间。"
        except (KeyError, TypeError, OSError):
            return f"错误：无效的时区名称「{timezone}」"
    else:
        now = datetime.datetime.now().astimezone()

    tz_name = now.tzname() or "?"

    weekday = _WEEKDAYS_CN[now.weekday()]
    ts = int(now.timestamp())

    # Compute UTC offset
    offset = now.utcoffset()
    if offset is not None:
        total_minutes = int(offset.total_seconds() // 60)
        offset_hours = total_minutes // 60
        offset_minutes = abs(total_minutes) % 60
        offset_str = f"UTC{offset_hours:+d}" + (f":{offset_minutes:02d}" if offset_minutes else "")
    else:
        offset_str = "?"

    return (
        f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"ISO 8601：{now.isoformat()}\n"
        f"星期：{weekday}\n"
        f"时区：{tz_name} ({offset_str})\n"
        f"时间戳：{ts}"
    )


# ── Grep Search ─────────────────────────────────────────────────────────────

_MAX_GREP_RESULTS = 500


def _glob_match(filename: str, pattern: str) -> bool:
    """Check if filename matches a glob pattern, handling {a,b} brace expansion."""
    if "{" in pattern and "}" in pattern:
        import re as _re
        m = _re.search(r"\{([^}]+)\}", pattern)
        if m:
            alts = m.group(1).split(",")
            prefix = pattern[:m.start()]
            suffix = pattern[m.end():]
            return any(fnmatch.fnmatch(filename, prefix + a + suffix) for a in alts)
    return fnmatch.fnmatch(filename, pattern)

_MAX_LINES_PER_FILE = 50


def _grep_with_ripgrep(pattern: str, resolved: str, max_results: int,
                       include: str = "", ignore_case: bool = False,
                       literal: bool = False, context: int = 0,
                       max_chars: int = 8000) -> str:
    import subprocess as _sp
    import json as _json

    max_lines_per_file = _MAX_LINES_PER_FILE

    cmd = ["rg", "--json", "--line-number", "--no-heading", "--color=never",
           "--hidden", "--max-columns", "500", "--max-count",
           str(max_lines_per_file)]
    if ignore_case:
        cmd.append("--ignore-case")
    if literal:
        cmd.append("--fixed-strings")
    if include:
        cmd.extend(["--glob", include])
    if context > 0:
        cmd.extend(["-C", str(min(context, 10))])
    cmd.extend(["--", pattern, str(resolved)])

    try:
        proc = _sp.run(cmd, capture_output=True, text=True, timeout=30)
    except _sp.TimeoutExpired:
        return f"错误：ripgrep 搜索超时（30s），请缩小搜索范围。"
    except OSError as e:
        return f"错误：ripgrep 执行失败: {e}"

    if proc.returncode not in (0, 1):
        stderr = proc.stderr.strip()
        if stderr:
            return f"错误：ripgrep 搜索出错: {stderr}"

    file_matches_map: Dict[str, List[str]] = {}
    file_total: Dict[str, int] = {}
    last_file = ""
    char_count = 0
    hit_char_limit = False

    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = _json.loads(line)
        except _json.JSONDecodeError:
            continue

        if event.get("type") == "begin":
            data = event.get("data", {})
            last_file = (data.get("path", {}).get("text", "") or "")
        elif event.get("type") == "match":
            data = event.get("data", {})
            fpath = data.get("path", {}).get("text", "") or last_file
            lineno = data.get("line_number", 0)
            text = data.get("lines", {}).get("text", "").rstrip("\n\r")
            if len(text) > 500:
                text = text[:500] + "..."
            if fpath not in file_matches_map:
                file_matches_map[fpath] = []
                file_total[fpath] = 0
            if file_total[fpath] < max_lines_per_file:
                line_str = f"    L{lineno}: {text}"
                char_count += len(line_str)
                if char_count > max_chars and not hit_char_limit:
                    hit_char_limit = True
                if not hit_char_limit or char_count <= max_chars:
                    file_matches_map[fpath].append(line_str)
                    file_total[fpath] += 1
                else:
                    if file_total[fpath] == 0 and not file_matches_map[fpath]:
                        file_matches_map[fpath].append(line_str)
                        file_total[fpath] += 1
            elif file_total[fpath] == max_lines_per_file:
                file_matches_map[fpath].append(
                    f"    ...（该文件匹配超过 {max_lines_per_file} 行，已截断）")
                file_total[fpath] += 1
        elif event.get("type") == "context":
            data = event.get("data", {})
            fpath = data.get("path", {}).get("text", "") or last_file
            lineno = data.get("line_number", 0)
            text = data.get("lines", {}).get("text", "").rstrip("\n\r")
            if fpath in file_matches_map:
                ctx_line = f"    -{lineno}- {text}"
                char_count += len(ctx_line)
                if char_count > max_chars and not hit_char_limit:
                    hit_char_limit = True
                if not hit_char_limit or char_count <= max_chars:
                    file_matches_map[fpath].append(ctx_line)

    if not file_matches_map:
        return f"在目录「{resolved}」中没有找到匹配「{pattern}」的内容。"

    all_files = sorted(file_matches_map.keys())
    total_matches = sum(file_total.values())
    lines_out: List[str] = []
    for fpath in all_files:
        relpath = os.path.relpath(fpath, resolved)
        lines_out.append(f"📄 {relpath}")
        lines_out.extend(file_matches_map[fpath])

    result = (f"🔍 搜索「{pattern}」在 {resolved}\n"
              f"共 {len(all_files)} 个文件，{total_matches} 行匹配\n\n" +
              "\n".join(lines_out))

    reasons = []
    if total_matches >= max_results:
        reasons.append(f"行数上限 {max_results}")
    if hit_char_limit:
        reasons.append(f"字符数上限 {max_chars}")
    if reasons:
        result += f"\n\n⚠️ 结果已截断（触达上限：{'，'.join(reasons)}），存在更多匹配"

    return result


def _grep_with_python(pattern: str, resolved: str, max_results: int,
                      include: str = "", ignore_case: bool = False,
                      literal: bool = False, context: int = 0,
                      max_chars: int = 8000) -> str:
    max_lines_per_file = _MAX_LINES_PER_FILE

    try:
        if literal:
            compiled = re.compile(re.escape(pattern))
        elif ignore_case:
            compiled = re.compile(pattern, re.IGNORECASE)
        else:
            compiled = re.compile(pattern)
    except re.error as e:
        return f"错误：无效的正则表达式「{pattern}」: {e}"

    matches: List[str] = []
    total_matches = 0
    file_count = 0
    char_count = 0
    hit_char_limit = False

    ignore_dirs = _get_ignore_dirs()
    for root, dirs, files in os.walk(resolved, topdown=True):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ignore_dirs]

        if total_matches >= max_results:
            break

        for fname in files:
            if total_matches >= max_results:
                break

            if fname.startswith("."):
                continue

            if include:
                if not _glob_match(fname, include):
                    continue

            fpath = os.path.join(root, fname)
            file_matches: List[str] = []

            try:
                with open(fpath, "rb") as f:
                    header = f.read(8192)
                if b"\x00" in header:
                    continue
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
            except (PermissionError, OSError):
                continue

            for idx, line in enumerate(all_lines):
                if len(file_matches) >= max_lines_per_file:
                    file_matches.append(
                        f"    ...（该文件匹配超过 {max_lines_per_file} 行，已截断）")
                    break
                if compiled.search(line):
                    stripped = line.rstrip("\n\r")
                    if len(stripped) > 500:
                        stripped = stripped[:500] + "..."
                    line_str = f"    L{idx + 1}: {stripped}"
                    char_count += len(line_str)
                    if char_count > max_chars and not hit_char_limit:
                        hit_char_limit = True
                    if not hit_char_limit or char_count <= max_chars:
                        file_matches.append(line_str)

            if file_matches:
                relpath = os.path.relpath(fpath, resolved)
                matches.append(f"📄 {relpath}")
                matches.extend(file_matches)
                total_matches += sum(1 for m in file_matches
                                     if not m.startswith("    ..."))
                file_count += 1

        if hit_char_limit and char_count > max_chars:
            break

    if not matches:
        return f"在目录「{resolved}」中没有找到匹配「{pattern}」的内容。"

    result = (f"🔍 搜索「{pattern}」在 {resolved}\n"
              f"共 {file_count} 个文件，{total_matches} 行匹配\n\n" +
              "\n".join(matches))

    reasons = []
    if total_matches >= max_results:
        reasons.append(f"行数上限 {max_results}")
    if hit_char_limit:
        reasons.append(f"字符数上限 {max_chars}")
    if reasons:
        result += f"\n\n⚠️ 结果已截断（触达上限：{'，'.join(reasons)}），存在更多匹配"

    return result


def execute_grep_search(pattern: str, path: str, include: str = "",
                        max_results: int = 50, ignore_case: bool = False,
                        literal: bool = False, context: int = 0,
                        max_chars: int = 8000) -> str:
    """Search file contents by regex/keyword in a directory tree.

    Auto-detects ripgrep for fast search; falls back to pure-Python impl.

    Args:
        pattern: Regex or keyword to search for.
        path: Absolute path of the root directory to search.
        include: Glob pattern to filter files (e.g. "*.py", "*.{ts,js}").
        max_results: Maximum number of matching lines to return (1-500).
        max_chars: Total character limit for search results (500-50000).
        ignore_case: Case-insensitive search (default: False).
        literal: Treat pattern as literal string (default: False).
        context: Lines of context before/after each match (0-10, default: 0).

    Returns:
        Formatted string with matches per file.
    """
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：目录不存在或路径无效「{path}」"
    if not os.path.isdir(resolved):
        return f"错误：路径不是目录「{resolved}」"

    max_results = max(1, min(_MAX_GREP_RESULTS, max_results))
    max_chars = max(500, min(50000, max_chars))

    import shutil as _shutil
    if _shutil.which("rg"):
        return _grep_with_ripgrep(
            pattern, resolved, max_results,
            include=include, ignore_case=ignore_case,
            literal=literal, context=context,
            max_chars=max_chars)
    return _grep_with_python(
        pattern, resolved, max_results,
        include=include, ignore_case=ignore_case,
        literal=literal, context=context,
        max_chars=max_chars)


# ── Glob Find ───────────────────────────────────────────────────────────────

_MAX_GLOB_RESULTS = 500


def execute_glob_find(pattern: str, path: str, max_results: int = 100,
                      exclude: str = "") -> str:
    """Recursively find files matching a glob pattern, skipping blacklisted directories.

    Args:
        pattern: Glob pattern such as "**/*.py", "config*.json".
        path: Absolute path of the root directory to search.
        max_results: Maximum number of files to return (1-500).
        exclude: Glob pattern for files to exclude (e.g. "*__pycache__*").

    Returns:
        Formatted string with matched file paths, sizes, and modification times.
    """
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：目录不存在或路径无效「{path}」"
    if not os.path.isdir(resolved):
        return f"错误：路径不是目录「{resolved}」"

    max_results = max(1, min(_MAX_GLOB_RESULTS, max_results))

    entries: List[tuple] = []

    def _is_match(relpath: str, fname: str, pat: str) -> bool:
        if fnmatch.fnmatch(fname, pat):
            return True
        if fnmatch.fnmatch(relpath, pat):
            return True
        if pat.startswith("**/"):
            clean_pat = pat[3:]
            if fnmatch.fnmatch(relpath, clean_pat) or fnmatch.fnmatch(fname, clean_pat):
                return True
        return False

    try:
        ignore_dirs = _get_ignore_dirs()
        for root_dir, dirs, files in os.walk(resolved, topdown=True):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ignore_dirs]

            for fname in files:
                if fname.startswith("."):
                    continue
                if exclude and fnmatch.fnmatch(fname, exclude):
                    continue
                if exclude and fnmatch.fnmatch(os.path.join(root_dir, fname), exclude):
                    continue
                fpath = os.path.join(root_dir, fname)
                relpath = os.path.relpath(fpath, resolved)
                if _is_match(relpath, fname, pattern):
                    try:
                        st = os.lstat(fpath)
                        entries.append((st.st_mtime, relpath, fname, fpath, st))
                    except OSError:
                        entries.append((0, relpath, fname, fpath, None))
    except (PermissionError, OSError) as e:
        return f"错误：搜索文件时出错「{resolved}」: {e}"

    if not entries:
        return f"在目录「{resolved}」中没有找到匹配「{pattern}」的文件。"

    # Sort by mtime descending (most recently modified first)
    entries.sort(key=lambda e: e[0], reverse=True)

    total = len(entries)
    if total > max_results:
        entries = entries[:max_results]

    lines: List[str] = []
    for _, relpath, fname, fpath, st in entries:
        if st is not None:
            if stat.S_ISDIR(st.st_mode):
                marker = "DIR"
                size = "—"
            elif os.path.islink(fpath):
                marker = "LINK"
                size = _format_file_size(st.st_size)
            else:
                marker = "FILE"
                size = _format_file_size(st.st_size)
            mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M")
        else:
            marker = "?"
            size = "?"
            mtime = "?"

        lines.append(f"[{marker:4s}] {size:>8s}  {mtime}  {relpath}")

    result = f"📂 搜索模式「{pattern}」在 {resolved}\n共 {total} 个匹配" + (f"（显示前 {len(entries)} 个）" if total > max_results else "") + "\n\n"
    result += "\n".join(lines)

    if total > max_results:
        result += f"\n\n...（已截断，仅显示前 {len(entries)} 个，共 {total} 个）"

    return result


# ── File Info ───────────────────────────────────────────────────────────────

_FILE_SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"]


def _format_file_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
    import math
    unit_idx = int(math.floor(math.log(size_bytes, 1024)))
    unit_idx = min(unit_idx, len(_FILE_SIZE_UNITS) - 1)
    value = size_bytes / (1024 ** unit_idx)
    if unit_idx == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {_FILE_SIZE_UNITS[unit_idx]}"


def execute_file_info(path: str) -> str:
    """Get file/directory metadata: size, mtime, atime, permissions, type, owner.

    Args:
        path: Absolute path of the file or directory.

    Returns:
        Formatted string with file metadata.
    """
    # Check symlink BEFORE path resolution
    raw_path = os.path.expanduser(path)
    is_symlink = os.path.islink(raw_path)
    link_target = os.readlink(raw_path) if is_symlink else None

    resolved = _resolve_safe_path(path)
    if resolved is None:
        if is_symlink:
            return (
                f"📋 文件信息: {raw_path}\n"
                f"  类型: 符号链接（破损）→ {link_target}\n"
                f"  大小: —（目标不存在）"
            )
        return f"错误：文件或目录不存在或路径无效「{path}」"

    try:
        st = os.stat(resolved)
    except PermissionError:
        return f"错误：无权访问「{resolved}」"
    except OSError as e:
        return f"错误：访问「{resolved}」时出错: {e}"

    if is_symlink:
        file_type = f"符号链接 → {link_target}"
    elif os.path.isdir(resolved):
        file_type = "目录"
    elif os.path.isfile(resolved):
        file_type = "文件"
    else:
        file_type = "其他"

    mode = st.st_mode
    perm_octal = oct(stat.S_IMODE(mode))[2:]  # e.g. "644", "755"
    perm_str = stat.filemode(mode)             # e.g. "-rw-r--r--"

    try:
        import pwd
        import grp
        owner = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name
    except (ImportError, KeyError):
        owner = str(st.st_uid)
        group = str(st.st_gid)

    if os.path.isdir(resolved) and not is_symlink:
        size_str = "—（目录）"
    else:
        size_str = _format_file_size(st.st_size)

    local_tz = datetime.datetime.now().astimezone().tzinfo
    mtime = datetime.datetime.fromtimestamp(st.st_mtime, tz=local_tz).strftime("%Y-%m-%d %H:%M:%S")
    atime = datetime.datetime.fromtimestamp(st.st_atime, tz=local_tz).strftime("%Y-%m-%d %H:%M:%S")
    ctime = datetime.datetime.fromtimestamp(st.st_ctime, tz=local_tz).strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"📋 文件信息: {resolved}",
        f"  类型: {file_type}",
        f"  大小: {size_str}",
        f"  权限: {perm_octal} ({perm_str})",
        f"  所有者: {owner}:{group}",
        f"  修改时间: {mtime}",
        f"  访问时间: {atime}",
        f"  创建时间: {ctime}",
    ]

    return "\n".join(lines)


def execute_ask_user_question(question: str) -> str:
    """Ask the user a question and return their response.

    Note: This is a fallback — the actual blocking user interaction
    is handled by clipboard_panel.py which intercepts this tool in
    ai_tool_loop.py before calling execute_tool_call().

    If this function is reached (interception failed), it returns an error
    to prevent the agent from receiving a fake success response.
    """
    return "错误：ask_user_question 未被拦截，用户提问已丢失。请使用其他方式获取所需信息。"


# ── Write File ──────────────────────────────────────────────────────────────

_MAX_WRITE_CHARS = 100000


def execute_write_file(path: str, content: str, force: bool = False,
                       mode: str = "write") -> str:
    """Create a new file or overwrite an existing file's content.

    Only accepts absolute paths. By default, will NOT overwrite an existing
    file — set force=True to allow overwriting. Supports append mode.

    Args:
        path: Absolute path of the file to write.
        content: Text content to write to the file.
        force: If True, overwrite existing file without warning.
               Defaults to False (safer). Ignored when mode="append".
        mode: "write" (overwrite, default) or "append" (append to end).

    Returns:
        Formatted string with result summary.
    """
    if not path or not isinstance(path, str) or not os.path.isabs(path):
        return "错误：必须使用绝对路径！"

    if mode == "append":
        # Append mode: skip exists/force checks, just append
        resolved = os.path.realpath(path)
        parent_dir = os.path.dirname(resolved)
        if not os.path.isdir(parent_dir):
            try:
                os.makedirs(parent_dir, exist_ok=True)
            except OSError as e:
                return f"错误：无法创建父目录「{parent_dir}」: {e}"
        if not os.access(parent_dir, os.W_OK):
            return f"错误：目录不可写「{parent_dir}」"

        if len(content) > _MAX_WRITE_CHARS:
            content = content[:_MAX_WRITE_CHARS]

        try:
            with open(resolved, "a", encoding="utf-8") as f:
                f.write(content)
        except PermissionError:
            return f"错误：无权写入文件「{resolved}」"
        except OSError as e:
            return f"错误：追加写入时出错「{resolved}」: {e}"

        size_bytes = len(content.encode("utf-8"))
        size_str = _format_file_size(size_bytes)
        line_count = content.count("\n") + 1 if content else 0
        return (
            f"✅ 已追加到文件末尾: {resolved}\n"
            f"  写入: {size_str}\n"
            f"  行数: {line_count}\n"
            f"  字符数: {len(content)}"
        )

    # Write (overwrite) mode — original logic
    resolved = _resolve_write_path(path, force)
    if resolved is None:
        real_path = os.path.realpath(path)
        parent_dir = os.path.dirname(real_path)
        if not os.path.isdir(parent_dir):
            try:
                os.makedirs(parent_dir, exist_ok=True)
            except OSError as e:
                return f"错误：无法创建父目录「{parent_dir}」: {e}"
            resolved = real_path
        else:
            if os.path.exists(real_path):
                return f"错误：文件已存在「{path}」。如需覆盖请设置 force=True。"
            return f"错误：无法写入文件「{path}」"

    parent = os.path.dirname(resolved)
    if not os.access(parent, os.W_OK):
        return f"错误：目录不可写「{parent}」"

    if len(content) > _MAX_WRITE_CHARS:
        content = content[:_MAX_WRITE_CHARS]

    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
    except PermissionError:
        return f"错误：无权写入文件「{resolved}」"
    except OSError as e:
        return f"错误：写入文件时出错「{resolved}」: {e}"

    size_bytes = len(content.encode("utf-8"))
    size_str = _format_file_size(size_bytes)
    line_count = content.count("\n") + 1 if content else 0
    return (
        f"✅ 文件已写入: {resolved}\n"
        f"  大小: {size_str}\n"
        f"  行数: {line_count}\n"
        f"  字符数: {len(content)}"
    )


# ── Tool Functions ─────────────────────────────────────────────────────────

# Max characters in a single tool result (to prevent token overflow)
MAX_TOOL_RESULT_CHARS = 5000

# Obscura headless browser binary (pre-installed)
_OBSCURA_BIN = os.environ.get("OBSCURA_BIN") or os.path.expanduser("~/.local/bin/obscura")

_OBSCURA_EVAL_TPL = """JSON.stringify(
    Array.from(document.querySelectorAll('.result__body')).slice(0, %d).map(el => ({
        title: el.querySelector('.result__title')?.textContent?.trim() || '',
        url: el.querySelector('.result__url')?.textContent?.trim() || '',
        snippet: el.querySelector('.result__snippet')?.textContent?.trim() || ''
    }))
)"""

# Web fetch response cache (5 min TTL, LRU eviction at 50 entries)
_FETCH_CACHE: Dict[str, Tuple[float, str]] = {}
_FETCH_CACHE_TTL = 300
_FETCH_CACHE_MAX = 50


def _get_cached_fetch(url: str) -> Optional[str]:
    """Return cached fetch result if not expired."""
    entry = _FETCH_CACHE.get(url)
    if entry is not None and time.monotonic() - entry[0] < _FETCH_CACHE_TTL:
        return entry[1]
    _FETCH_CACHE.pop(url, None)
    return None


def _set_cached_fetch(url: str, content: str):
    """Store fetch result in cache, evict LRU when over capacity."""
    while len(_FETCH_CACHE) >= _FETCH_CACHE_MAX:
        _FETCH_CACHE.pop(next(iter(_FETCH_CACHE)), None)
    _FETCH_CACHE[url] = (time.monotonic(), content)


def _format_search_results(results: List[Dict[str, str]], query: str) -> str:
    if not results:
        return f"没有找到关于「{query}」的搜索结果。"
    parts = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "").strip()
        url_str = r.get("url", "").strip()
        snippet = r.get("snippet", "").strip()
        parts.append(f"{i}. {title}")
        if url_str:
            parts.append(f"   URL: {url_str}")
        if snippet:
            parts.append(f"   {snippet}")
        parts.append("")
    result_text = "\n".join(parts).strip()
    if len(result_text) > MAX_TOOL_RESULT_CHARS:
        result_text = result_text[:MAX_TOOL_RESULT_CHARS] + "\n\n...（结果已截断）"
    return result_text


def _execute_obscura_search(query: str, max_results: int) -> Optional[str]:
    """Execute search via Obscura headless browser + DuckDuckGo HTML endpoint.
    Returns None if Obscura is unavailable or fails."""
    if not os.path.isfile(_OBSCURA_BIN):
        return None
    encoded = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    js = _OBSCURA_EVAL_TPL % max_results
    try:
        result = subprocess.run(
            [_OBSCURA_BIN, "fetch", url, "--quiet", "--eval", js, "--timeout", "20"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        output = result.stdout.strip()
        if not output:
            return None
        parsed = json.loads(output)
        return _format_search_results(parsed, query)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
            json.JSONDecodeError, FileNotFoundError):
        return None


def _is_duckduckgo_captcha(html_text: str) -> bool:
    return "anomaly-modal" in html_text or "challenge-form" in html_text


def _execute_duckduckgo_search(query: str, max_results: int) -> str:
    """Execute search via DuckDuckGo HTML endpoint (requests, no JS)."""
    encoded = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"搜索失败：{e}"
    if _is_duckduckgo_captcha(resp.text):
        return (
            f"DuckDuckGo 搜索暂时被拦截（CAPTCHA 验证）。\n"
            f"当前为直连请求回退路径，搜索会被 CAPTCHA 拦截。"
        )
    parser = _DuckDuckGoResultParser(max_results)
    try:
        parser.feed(resp.text)
    except Exception:
        return f"解析搜索结果时出错"
    results = parser.results[:max_results]
    return _format_search_results(results, query)


def execute_web_search(query: str, max_results: int = 5) -> str:
    """Execute a web search. Uses direct DuckDuckGo HTTP as primary path (fast),
    falls back to Obscura headless browser if DuckDuckGo is blocked."""
    max_results = max(1, min(10, max_results))
    # Fast path: direct DuckDuckGo HTTP (0.5-2s)
    result = _execute_duckduckgo_search(query, max_results)
    # If DuckDuckGo returned actual results (not CAPTCHA/error), use them
    if not result.startswith(("DuckDuckGo", "搜索失败")):
        return result
    # Fallback: Obscura headless browser (~2.5s, handles CAPTCHA)
    result = _execute_obscura_search(query, max_results)
    if result is not None:
        return result
    return f"搜索失败：所有搜索路径均不可用。"


_SUSPICIOUS_PATTERNS = re.compile(
    r"(Access Denied|Please enable JavaScript|请启用 JavaScript|"
    r"Your browser does not support JavaScript|Just a moment|"
    r"Checking your browser|DDoS protection|captcha|challenge)",
    re.IGNORECASE,
)


def _is_cjk(ch: str) -> bool:
    """Check if character is in CJK ideograph, punctuation or fullwidth range."""
    val = ord(ch)
    return (
        (0x4E00 <= val <= 0x9FFF) or      # CJK Unified Ideographs
        (0x3400 <= val <= 0x4DBF) or      # CJK Ext A
        (0x3000 <= val <= 0x303F) or      # CJK Symbols & Punctuation
        (0xFF00 <= val <= 0xFFEF) or      # Halfwidth/Fullwidth Forms
        (0x3040 <= val <= 0x309F) or      # Hiragana
        (0x30A0 <= val <= 0x30FF) or      # Katakana
        (0xAC00 <= val <= 0xD7AF)         # Hangul Syllables
    )


# ── Web Fetch ────────────────────────────────────────────────────────────────


def _extract_with_trafilatura(html_text: str) -> Optional[str]:
    """Extract main content from HTML using Trafilatura.
    Returns cleaned text, or None if extraction fails/unavailable."""
    try:
        import trafilatura
    except ImportError:
        return None
    try:
        result = trafilatura.extract(
            html_text,
            output_format='markdown',
            include_comments=False,
            include_tables=True,
            no_fallback=False,
        )
        if result and result.strip():
            return result.strip()
    except Exception:
        pass
    return None


def _try_requests_fetch(url: str, max_chars: int, timeout: int = 20) -> Optional[str]:
    """Try fetching a page via plain HTTP requests. Returns None on failure
    or if the result looks suspicious (too short, garbled, JS-required page)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    # Primary path: Trafilatura main-content extraction (clean, no chrome)
    trafilatura_text = _extract_with_trafilatura(resp.text)
    if trafilatura_text:
        text = trafilatura_text
    else:
        # Fallback: strip_html (more content but includes page chrome)
        text = strip_html(resp.text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.strip()

    # Suspicious: too short (JS-rendered page got only shell HTML)
    if len(text) < 200:
        return None

    # Suspicious: contains known JS-required / captcha / error patterns
    if _SUSPICIOUS_PATTERNS.search(text):
        return None

    # Check for excessive garbled content: >15% non-ASCII (excluding CJK) characters
    if text:
        non_ascii_non_cjk = sum(1 for ch in text if ord(ch) > 127 and not _is_cjk(ch))
        if non_ascii_non_cjk / len(text) > 0.15:
            return None

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n...（内容已截断）"
    return text



def _try_obscura_fetch(url: str, max_chars: int, timeout: int = 20) -> str:
    """Fetch a page via Obscura headless browser with --dump markdown."""
    if not os.path.isfile(_OBSCURA_BIN):
        return f"获取页面失败：Obscura 不可用"
    obs_timeout = max(10, timeout)
    try:
        result = subprocess.run(
            [_OBSCURA_BIN, "fetch", url, "--dump", "markdown", "--quiet", "--timeout", str(obs_timeout)],
            capture_output=True, text=True, timeout=obs_timeout + 10,
        )
        if result.returncode != 0:
            return f"获取页面失败：Obscura 返回错误码 {result.returncode}"
        text = result.stdout.strip()
        if not text:
            return f"获取页面失败：Obscura 返回空内容"
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
            FileNotFoundError) as e:
        return f"获取页面失败：{e}"

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n...（内容已截断）"
    return text


def execute_web_fetch(url: str, max_chars: int = 5000, timeout: int = 20) -> str:
    """Fetch a page's content as plain text.

    Tries plain HTTP requests (fast, zero overhead) first. If the result
    is suspicious — too short (JS-rendered shell), contains JS-required /
    captcha patterns, or garbled non-ASCII — falls back to Obscura headless
    browser (--dump markdown) which executes JavaScript and renders the
    real page content. Caches results for 5 minutes.
    """
    max_chars = max(500, min(20000, max_chars))
    timeout = max(5, min(60, timeout))

    # Check cache first
    cached = _get_cached_fetch(url)
    if cached is not None:
        return cached

    result = _try_requests_fetch(url, max_chars, timeout)
    if result is not None:
        _set_cached_fetch(url, result)
        return result

    result = _try_obscura_fetch(url, max_chars, timeout)
    _set_cached_fetch(url, result)
    return result


# ── Bash Tool ───────────────────────────────────────────────────────────────

_MAX_BASH_OUTPUT_CHARS = 5000
_BASH_TIMEOUT_DEFAULT = 60
_BASH_SHELL = "/bin/bash"
_BASH_DEFAULT_CWD = os.path.dirname(os.path.abspath(__file__))
_bash_cwd = _BASH_DEFAULT_CWD

# Interactive commands that would hang in a non-TTY pipe session
_ALWAYS_INTERACTIVE: Final[frozenset] = frozenset({
    "vi", "vim", "nvim", "nano", "emacs", "vimdiff",
    "less", "more", "most",
    "top", "htop", "btop", "iftop", "iotop",
})

_DUAL_MODE: Final[frozenset] = frozenset({
    "python", "python3", "ipython",
    "node", "irb",
    "bash", "zsh", "sh", "dash", "fish",
})


def _check_interactive(command: str) -> Optional[str]:
    """Return error message if command starts with an interactive program."""
    if not command or not command.strip():
        return None
    parts = command.strip().split(maxsplit=1)
    first_word = parts[0].strip()
    has_args = len(parts) > 1 and parts[1].strip()

    if first_word in _ALWAYS_INTERACTIVE:
        return (
            f"错误：不支持交互式命令「{first_word}」。\n"
            f"   该命令需要 TTY 终端，无法在后台管道模式下执行。"
        )

    if first_word in _DUAL_MODE and not has_args:
        return (
            f"错误：裸启动「{first_word}」会进入交互模式。\n"
            f"   如需执行脚本，请提供参数，例如: {first_word} script.py"
        )

    return None


def _save_truncated_output(output: str, command: str) -> str:
    """Save truncated output to a temp file, return a user-facing message."""
    tmp_dir = tempfile.gettempdir()
    _clean_old_temp_files("bash_out_", tmp_dir)
    cmd_hash = hash(command) & 0xFFFFFFFF
    prefix = f"bash_out_{cmd_hash:08x}_"
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', dir=tmp_dir, prefix=prefix, suffix='.txt',
            delete=False, encoding='utf-8',
        ) as f:
            f.write(output)
            return f"\n完整输出已保存至: {f.name}"
    except OSError:
        return ""


def _clean_old_temp_files(prefix: str, tmp_dir: str, max_age: int = 86400):
    """Remove temp files older than max_age seconds from tmp_dir."""
    now = time.time()
    for entry in os.listdir(tmp_dir):
        if entry.startswith(prefix):
            path = os.path.join(tmp_dir, entry)
            try:
                if now - os.path.getmtime(path) > max_age:
                    os.remove(path)
            except OSError:
                pass


class _BashSession:
    """Persistent bash session that maintains state across command executions.

    Uses binary pipe I/O (bypassing Python's TextIOWrapper buffering) with a
    sentinel protocol to reliably detect command completion and capture exit
    codes. On timeout the session enters an error state and must be restarted.
    """

    _SENTINEL_B = b",,,,bash-exit-"
    _SENTINEL_END_B = b"-banner,,,,"

    def __init__(self):
        self.process: Optional["subprocess.Popen[bytes]"] = None
        self._timed_out = False
        self._started = False

    def start(self):
        """Spawn a new persistent bash subprocess (binary pipe mode)."""
        global _bash_cwd
        self.process = subprocess.Popen(
            [_BASH_SHELL],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            cwd=_bash_cwd,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
        self._started = True

    def execute(self, command: str, timeout: int = _BASH_TIMEOUT_DEFAULT, cancel_event=None) -> dict:
        """Execute a command and return ``{output, exit_code, timed_out}``.

        Raises ``RuntimeError`` if the session is in a timed-out state and
        needs to be restarted.
        """
        if self._timed_out:
            raise RuntimeError("Bash session has timed out and must be restarted (restart=True).")
        if not self._started or self.process is None:
            self.start()
        if self.process is None:
            return {"output": "错误：Bash 进程未能启动", "exit_code": -1, "timed_out": False}

        process = self.process

        if process.returncode is not None:
            return {"output": "错误：Bash 进程已意外退出", "exit_code": process.returncode, "timed_out": False}

        # Use binary mode to bypass Python TextIOWrapper buffering.
        # TextIOWrapper.read() reads large chunks → poll() sees empty pipe.
        sentinel_cmd_b = (
            b"{ "
            + command.encode("utf-8", errors="replace")
            + b"; } 2>&1; echo "
            + self._SENTINEL_B
            + b"$?"
            + self._SENTINEL_END_B
            + b"\n"
        )

        try:
            process.stdin.write(sentinel_cmd_b)
            process.stdin.flush()
        except BrokenPipeError:
            return {"output": "错误：Bash 进程已关闭（stdin 写入失败）", "exit_code": -1, "timed_out": False}

        output_buf = bytearray()
        sentinel_found = False
        exit_code = -1
        fd = process.stdout.fileno()

        poll = select.poll()
        poll.register(fd, select.POLLIN)

        deadline = time.monotonic() + timeout if timeout > 0 else float("inf")

        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                self._kill_process_group()
                output = output_buf.decode("utf-8", errors="replace").strip()
                full_len = len(output)
                if len(output) > _MAX_BASH_OUTPUT_CHARS:
                    truncated = output[:_MAX_BASH_OUTPUT_CHARS]
                    saved_msg = _save_truncated_output(output, command)
                    output = truncated + f"\n...（输出已截断，共 {full_len} 字符）{saved_msg}"
                return {"output": output, "exit_code": -1, "timed_out": False}

            if process.poll() is not None and not sentinel_found:
                remaining = os.read(fd, 65536)
                if remaining:
                    output_buf.extend(remaining)
                break

            events = poll.poll(50)
            if not events:
                continue

            chunk = os.read(fd, 65536)
            if not chunk:
                break

            output_buf.extend(chunk)

            sidx = output_buf.find(self._SENTINEL_B)
            if sidx != -1:
                sentinel_found = True
                # Extract exit code between sentinel and sentinel_end
                after = output_buf[sidx:]
                eidx = after.find(self._SENTINEL_END_B)
                if eidx != -1:
                    code_bytes = after[len(self._SENTINEL_B):eidx]
                    try:
                        exit_code = int(code_bytes.decode("ascii"))
                    except (ValueError, UnicodeDecodeError):
                        exit_code = -1
                output_buf = output_buf[:sidx]
                break

        if not sentinel_found:
            self._timed_out = True
            self._kill_process_group()
            output = output_buf.decode("utf-8", errors="replace").strip()
            full_len = len(output)
            if len(output) > _MAX_BASH_OUTPUT_CHARS:
                truncated = output[:_MAX_BASH_OUTPUT_CHARS]
                saved_msg = _save_truncated_output(output, command)
                output = truncated + f"\n...（输出已截断，共 {full_len} 字符）{saved_msg}"
            return {
                "output": f"命令执行超时（{timeout}秒），session 已终止。\n最后输出：{output[:600]}",
                "exit_code": -1,
                "timed_out": True,
            }

        output = output_buf.decode("utf-8", errors="replace").strip()
        full_len = len(output)
        if len(output) > _MAX_BASH_OUTPUT_CHARS:
            truncated = output[:_MAX_BASH_OUTPUT_CHARS]
            saved_msg = _save_truncated_output(output, command)
            output = truncated + f"\n...（输出已截断，共 {full_len} 字符）{saved_msg}"

        return {"output": output, "exit_code": exit_code, "timed_out": False}

    def _kill_process_group(self):
        """Kill the entire process group to clean up children."""
        if self.process is not None and self.process.pid is not None:
            try:
                os.killpg(os.getpgid(self.process.pid), 9)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    self.process.kill()
                except OSError:
                    pass

    def stop(self):
        """Gracefully stop the bash session, with force-kill fallback."""
        if self.process is None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
            self._kill_process_group()
        self._started = False
        self.process = None

    def restart(self):
        """Restart the bash session (stop + start)."""
        self.stop()
        self._timed_out = False
        self.start()


def set_bash_cwd(path: str) -> str:
    """Set the working directory for the bash session and update active process if running."""
    global _bash_cwd, _bash_session
    import shlex
    path = os.path.abspath(os.path.expanduser(path.strip()))
    if not os.path.exists(path):
        return f"❌ 路径不存在：{path}"
    if not os.path.isdir(path):
        return f"❌ 路径不是一个目录：{path}"

    _bash_cwd = path
    if _bash_session is not None and _bash_session._started and _bash_session.process is not None:
        if _bash_session.process.poll() is None:
            # Persistent shell is active. Execute cd to switch directory.
            try:
                cmd = f"cd {shlex.quote(path)}"
                res = _bash_session.execute(cmd, timeout=5)
                if res.get("timed_out", False):
                    return f"⚠️ 目录切换命令超时，已更新全局配置。新路径：{path}"
            except Exception as e:
                return f"⚠️ 现有 Bash 会话异常（{e}），已更新工作目录配置。新路径：{path}"
    return f"✅ Bash 工作路径已切换至：{path}"


def get_bash_cwd() -> str:
    """Get the current working directory of the bash session."""
    global _bash_cwd
    return _bash_cwd


# Global bash session (module-level singleton)
_bash_session: Optional[_BashSession] = None


def execute_bash(command: str, restart: bool = False, timeout: int = _BASH_TIMEOUT_DEFAULT, cancel_event=None) -> str:
    """Execute a shell command in a persistent bash session.

    Args:
        command: Shell command to execute.
        restart: If True, restart the session before executing.
        timeout: Command timeout in seconds (1-120).
        cancel_event: Optional threading.Event for cancellation signaling.

    Returns:
        Formatted result string with output, exit code, and status.
    """
    global _bash_session

    if not command or not command.strip():
        return "错误：命令不能为空。"

    # Interactive command check
    interactive_err = _check_interactive(command)
    if interactive_err is not None:
        return interactive_err

    if restart:
        if _bash_session is not None:
            _bash_session.stop()
        _bash_session = _BashSession()
        _bash_session.start()
        if not command or not command.strip():
            return "🔄 Bash session 已重启。"

    timeout = max(1, min(120, timeout))

    if _bash_session is None:
        _bash_session = _BashSession()
        _bash_session.start()

    timed_out = False
    try:
        result = _bash_session.execute(command, timeout=timeout, cancel_event=cancel_event)
    except RuntimeError:
        # Session in timed-out state from prior call — auto-restart and retry once
        if _bash_session is not None:
            _bash_session.stop()
        _bash_session = _BashSession()
        _bash_session.start()
        try:
            result = _bash_session.execute(command, timeout=timeout, cancel_event=cancel_event)
        except RuntimeError as e:
            return f"错误：{e}"

    output = result.get("output", "")
    exit_code = result.get("exit_code", -1)
    timed_out = result.get("timed_out", False)

    if timed_out:
        # Auto-restart: old session is dead, create fresh one
        if _bash_session is not None:
            _bash_session.stop()
            _bash_session = _BashSession()
            _bash_session.start()
        parts = ["⚠️ 命令执行超时，已自动重启 bash session"]
        if output:
            parts.append("")
            parts.append(output)
        return "\n".join(parts)

    status_icon = "✅" if exit_code == 0 else "⚠️" if exit_code == -1 else "❌"
    parts = [f"{status_icon} 命令执行完成（退出码：{exit_code}）"]
    if output:
        parts.append("")
        parts.append(output)

    return "\n".join(parts)


# ── Notification ──────────────────────────────────────────────────────────

def execute_send_notification(
    summary: str,
    body: str = "",
    urgency: str = "normal",
    expire_time: int = 5000,
    icon: str = "",
) -> str:
    """Send a desktop notification via notify-send.

    Args:
        summary: Notification title/summary.
        body: Optional notification body text.
        urgency: Urgency level — "low", "normal", or "critical".
        expire_time: Display duration in milliseconds (default 5000).
        icon: Icon name or path (freedesktop icon name like "dialog-information").

    Returns:
        Structured result with status, summary, and details.
    """
    try:
        cmd = ["notify-send", "-a", "OpenCode Switcher"]

        if urgency in ("low", "normal", "critical"):
            cmd.extend(["-u", urgency])

        if expire_time > 0:
            cmd.extend(["-t", str(expire_time)])

        if icon:
            cmd.extend(["-i", icon])

        cmd.append(summary)
        if body:
            cmd.append(body)

        result = subprocess.run(
            cmd,
            timeout=10,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            err = result.stderr.strip()
            error_msg = err if err else f"返回码 {result.returncode}"
            return f"❌ 通知发送失败\n   原因: {error_msg}\n   标题: {summary}"

        return f"✅ 通知已发送\n   标题: {summary}\n   正文: {body or '(无)'}\n   紧急程度: {urgency}"

    except FileNotFoundError:
        return "❌ 通知发送失败\n   原因: 系统中未找到 notify-send\n   解决方案: sudo apt install libnotify-bin"
    except subprocess.TimeoutExpired:
        return "❌ 通知发送失败\n   原因: notify-send 无响应（超时）\n   标题: {summary}"
    except Exception as e:
        return f"❌ 通知发送失败\n   原因: {e}\n   标题: {summary}"


# ── QQ Mail Reader ──────────────────────────────────────────────────────────

_QQMAIL_IMAP_SERVER = "imap.qq.com"
_QQMAIL_IMAP_PORT = 993


def _sort_ids_by_internaldate(mail, total, max_results):
    """Fetch INTERNALDATE for all messages and return the newest N IDs.

    QQ Mail's IMAP sequence numbers are NOT in chronological order, so
    sorting by sequence number is unreliable.  INTERNALDATE is the server-
    side arrival timestamp — a lightweight metadata fetch without body data.
    """
    status, idata = mail.fetch(f'1:{total}', '(INTERNALDATE)')
    if status != 'OK':
        return None

    date_pool = []
    for token in idata:
        if not isinstance(token, bytes):
            continue
        m = re.search(rb'(\d+)\s+\(INTERNALDATE\s+"([^"]+)"\)', token)
        if not m:
            continue
        eid = int(m.group(1))
        date_str = m.group(2).decode()
        try:
            dt = parsedate_tz(date_str)
        except Exception:
            continue
        if dt:
            ts = mktime_tz(dt)
            date_pool.append((ts, eid))

    if not date_pool:
        return None

    date_pool.sort(key=lambda x: x[0], reverse=True)
    n = min(max_results, len(date_pool))
    return [str(eid).encode() for _, eid in date_pool[:n]]


def execute_read_qq_mail(max_results: int = 5, folder: str = "INBOX",
                         search_criteria: str = "ALL",
                         include_body: bool = True) -> str:
    """Read emails from QQ mailbox via IMAP over SSL.

    Requires QQ mail IMAP authorization code configured in
    qq_mail_credentials.json or QQ_MAIL_AUTH_CODE env var.
    Uses stdlib imaplib + email — zero external dependencies."""
    from clipboard_store import QQMailCredentialsStore

    max_results = max(1, min(20, max_results))

    store = QQMailCredentialsStore()
    email_addr = store.email
    auth_code = store.auth_code
    if not email_addr:
        email_addr = os.environ.get("QQ_MAIL_EMAIL", "").strip()
    if not auth_code:
        auth_code = os.environ.get("QQ_MAIL_AUTH_CODE", "").strip()

    if not email_addr or not auth_code:
        return (
            "❌ QQ邮箱未配置。请先配置邮箱地址和授权码。\n\n"
            "配置步骤：\n"
            "1. 登录 QQ邮箱网页版 → 设置 → 账号与安全\n"
            "2. 开启「POP3/SMTP/IMAP 服务」\n"
            "3. 短信验证后获取 16 位授权码\n"
            "4. 编辑 ~/.config/opencode-switcher/qq_mail_credentials.json：\n"
            '   {\n'
            '       "version": 1,\n'
            '       "email": "yourname@qq.com",\n'
            '       "auth_code": "16位授权码"\n'
            '   }\n\n'
            "也可通过环境变量配置：\n"
            "  export QQ_MAIL_EMAIL=yourname@qq.com\n"
            "  export QQ_MAIL_AUTH_CODE=你的16位授权码"
        )

    try:
        mail = imaplib.IMAP4_SSL(_QQMAIL_IMAP_SERVER, _QQMAIL_IMAP_PORT)
        mail.login(email_addr, auth_code)
    except imaplib.IMAP4.error as e:
        return f"❌ QQ邮箱登录失败：{e}\n请检查邮箱地址和授权码是否正确。"
    except Exception as e:
        return f"❌ 连接 QQ邮箱失败：{e}\n请检查网络连接。"

    try:
        try:
            status, folder_data = mail.select(folder)
            if status != "OK":
                return f"❌ 无法打开文件夹「{folder}」"
        except imaplib.IMAP4.error:
            return f"❌ 文件夹「{folder}」不存在。"

        try:
            result, data = mail.search(None, search_criteria)
            if result != "OK" or not data[0]:
                return f"📭 收件箱无匹配邮件（条件：{search_criteria}）"
        except imaplib.IMAP4.error:
            return f"❌ 搜索条件无效：{search_criteria}"

        all_ids = data[0].split()
        total = len(all_ids)
        fetch_count = min(max_results, total)

        sorted_ids = _sort_ids_by_internaldate(mail, total, fetch_count)
        if sorted_ids is None:
            return f"❌ 无法获取邮件时间信息，共 {total} 封"

        result_parts = [f"📧 共 {total} 封匹配邮件，显示最新 {fetch_count} 封\n"]

        for eid in sorted_ids:
            try:
                _, fetch_data = mail.fetch(eid, "(RFC822)")
                raw_email = fetch_data[0][1]
                msg = email.message_from_bytes(raw_email)

                subject = _decode_email_header(msg["Subject"])
                from_ = str(msg.get("From", "(未知发件人)"))
                date_str = _format_email_date(msg.get("Date", ""))

                result_parts.append(f"📩 发件人: {from_}")
                result_parts.append(f"📎 主题: {subject}")
                result_parts.append(f"🕐 时间: {date_str}")

                if include_body:
                    body_text = _extract_email_body(msg)
                    if body_text:
                        if len(body_text) > 500:
                            body_text = body_text[:500] + (
                                f"\n...（全文共 {len(body_text)} 字符，已截断）")
                        result_parts.append(f"📋 内容:\n{body_text}")

                result_parts.append("─" * 40)

            except Exception as e:
                result_parts.append(f"⚠️ 读取邮件时出错：{e}")
                result_parts.append("─" * 40)

        return "\n".join(result_parts).strip()

    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _decode_email_header(header_value: str) -> str:
    """Decode an email header that may be RFC 2047 encoded."""
    if not header_value:
        return "(无主题)"
    try:
        decoded_parts = decode_header(header_value)
        result = ""
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                result += part.decode(charset or "utf-8", errors="replace")
            else:
                result += part
        return result
    except Exception:
        return str(header_value)


def _format_email_date(date_str: str) -> str:
    """Parse and format an email date string."""
    if not date_str:
        return ""
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return date_str


def _extract_email_body(msg: email.message.Message) -> str:
    """Extract plain text body from an email, preferring text/plain over text/html."""
    import html as html_mod

    if msg.is_multipart():
        plain_parts = []
        html_parts_hack = []
        for part in msg.walk():
            ct = part.get_content_type()
            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
                if ct == "text/plain":
                    plain_parts.append(text)
                elif ct == "text/html":
                    html_parts_hack.append(text)
            except Exception:
                pass
        if plain_parts:
            return "\n".join(plain_parts).strip()
        elif html_parts_hack:
            text = re.sub(r"<[^>]+>", "", "\n".join(html_parts_hack))
            return html_mod.unescape(text).strip()
        return ""
    else:
        try:
            payload = msg.get_payload(decode=True)
            if not payload:
                return ""
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                text = re.sub(r"<[^>]+>", "", text)
                text = html_mod.unescape(text)
            return text.strip()
        except Exception:
            return ""


# ── Code Metrics ────────────────────────────────────────────────────────────

_METRICS_BINARY_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot",
    ".pyc", ".o", ".so", ".dll", ".dylib",
    ".zip", ".tar", ".gz", ".bz2", ".xz",
})

_METRICS_IGNORE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", "venv", ".venv",
    "env", "build", "dist", "target", ".cache", ".omo", ".hzb-agents",
})


def _is_binary(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in _METRICS_BINARY_EXTS


def _count_file_lines(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        total = 0
        code = 0
        comments = 0
        blank = 0
        is_python = path.endswith(".py")
        for line in source.split("\n"):
            total += 1
            stripped = line.strip()
            if not stripped:
                blank += 1
            elif stripped.startswith("#"):
                comments += 1
            else:
                code += 1
        result = {
            "file": os.path.basename(path),
            "total_lines": total,
            "code_lines": code,
            "comment_lines": comments,
            "blank_lines": blank,
        }
        if is_python:
            try:
                tree = ast.parse(source)
                funcs = sum(1 for n in ast.walk(tree)
                           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
                classes = sum(1 for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
                result["num_functions"] = funcs
                result["num_classes"] = classes
            except SyntaxError:
                result["num_functions"] = 0
                result["num_classes"] = 0
        return result
    except Exception:
        return None


def _format_metrics_table(results: list, total: dict, sort_by: str = "total") -> str:
    if not results:
        return "没有找到可分析的文件。"
    if sort_by == "name":
        sorted_results = sorted(results, key=lambda x: x.get("file", ""))
    elif sort_by == "code":
        sorted_results = sorted(results, key=lambda x: x["code_lines"], reverse=True)
    else:
        sorted_results = sorted(results, key=lambda x: x["total_lines"], reverse=True)
    lines = []
    if len(results) > 1:
        lines.append("📊 代码度量汇总")
        lines.append(f"{'文件':40s} {'总行数':>8s} {'代码行':>8s} {'注释行':>8s} {'空行':>8s}{' 函数':>6s}{' 类':>6s}")
        lines.append("─" * 90)
        for r in sorted_results:
            fn = r.get("file", "?")
            fn_trunc = fn[:38] + ".." if len(fn) > 38 else fn
            funcs = r.get("num_functions", "-")
            classes = r.get("num_classes", "-")
            f_str = str(funcs) if funcs != "-" else "-"
            c_str = str(classes) if classes != "-" else "-"
            lines.append(
                f"{fn_trunc:40s} {r['total_lines']:>8d} {r['code_lines']:>8d} "
                f"{r['comment_lines']:>8d} {r['blank_lines']:>8d} {f_str:>6s} {c_str:>6s}"
            )
        lines.append("─" * 90)
        lines.append(
            f"{'总计':40s} {total['total_lines']:>8d} {total['code_lines']:>8d} "
            f"{total['comment_lines']:>8d} {total['blank_lines']:>8d} "
            f"{str(total.get('num_functions', '-')):>6s} {str(total.get('num_classes', '-')):>6s}"
        )
    else:
        r = results[0]
        lines.append(f"📊 代码度量: {r['file']}")
        lines.append(f"   总行数:     {r['total_lines']}")
        lines.append(f"   代码行:     {r['code_lines']}")
        lines.append(f"   注释行:     {r['comment_lines']}")
        if r["total_lines"] > 0:
            pct = r["comment_lines"] / r["total_lines"] * 100
            lines[-1] += f"  ({pct:.1f}%)"
        lines.append(f"   空行:       {r['blank_lines']}")
        if "num_functions" in r:
            lines.append(f"   函数:       {r['num_functions']}")
            lines.append(f"   类:         {r['num_classes']}")
    return "\n".join(lines)


def execute_get_code_metrics(path: str, include: str = "",
                              exclude: str = "",
                              sort_by: str = "total") -> str:
    """Analyze code metrics for a file or directory.

    Supports Python (with ast-based function/class counting) and
    common text-based source files. Binary files are skipped automatically.

    Args:
        path: Absolute path to file or directory.
        include: Glob pattern to filter files (e.g. "*.py"). Empty = all supported types.
        exclude: Extra directory names to skip (comma-separated).

    Returns:
        Formatted metrics report with line counts and structure info.
    """
    if not path or not os.path.isabs(path):
        return "❌ 错误：必须使用绝对路径！"

    resolved = os.path.realpath(path)

    exclude_patterns = set()
    if exclude:
        for d in exclude.split(","):
            d = d.strip()
            if d:
                exclude_patterns.add(d)

    def _match_any(fname: str, patterns) -> bool:
        for p in patterns:
            if p == fname or fnmatch.fnmatch(fname, p):
                return True
        return False

    if os.path.isfile(resolved):
        if _is_binary(resolved):
            return f"❌ 跳过二进制文件: {os.path.basename(resolved)}"
        if include and not _match_any(os.path.basename(resolved), include.split(",")):
            return f"❌ 文件不匹配过滤条件: {include}"
        result = _count_file_lines(resolved)
        if result is None:
            return f"❌ 无法读取文件: {path}"
        return _format_metrics_table([result], result, sort_by)

    elif os.path.isdir(resolved):
        all_results = []
        totals = {"total_lines": 0, "code_lines": 0,
                  "comment_lines": 0, "blank_lines": 0,
                  "num_functions": 0, "num_classes": 0}
        for root, dirs, files in os.walk(resolved):
            dirs[:] = [d for d in dirs if not _match_any(d, exclude_patterns)
                       and d not in _METRICS_IGNORE_DIRS]
            for fname in sorted(files):
                fpath = os.path.join(root, fname)
                if _is_binary(fpath):
                    continue
                if include and not _match_any(fname, include.split(",")):
                    continue
                if _match_any(fname, exclude_patterns):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext not in (".py", ".js", ".ts", ".rs", ".go", ".java",
                               ".c", ".cpp", ".h", ".hpp", ".cs", ".rb",
                               ".sh", ".bash", ".zsh", ".yaml", ".yml",
                               ".json", ".xml", ".html", ".css", ".scss",
                               ".md", ".rst", ".txt", ".cfg", ".ini",
                               ".conf", ".toml"):
                    continue
                result = _count_file_lines(fpath)
                if result:
                    all_results.append(result)
                    totals["total_lines"] += result["total_lines"]
                    totals["code_lines"] += result["code_lines"]
                    totals["comment_lines"] += result["comment_lines"]
                    totals["blank_lines"] += result["blank_lines"]
                    totals["num_functions"] += result.get("num_functions", 0)
                    totals["num_classes"] += result.get("num_classes", 0)
        if not all_results:
            return f"在目录中未找到可分析的代码文件: {path}"
        return _format_metrics_table(all_results, totals, sort_by)
    else:
        return f"❌ 路径不存在: {path}"


# ── Project Dependencies ────────────────────────────────────────────────────

_DEP_IGNORE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", "venv", ".venv",
    "env", "build", "dist", "target", ".cache", ".omo", ".hzb-agents",
})

# Python stdlib module names (Python 3.10+)
try:
    _STDLIB_MODULES = frozenset(sys.stdlib_module_names)
except AttributeError:
    _STDLIB_MODULES = frozenset()


def _extract_python_imports(source: str) -> List[Dict[str, str]]:
    """Extract imports from Python source using ast module."""
    imports = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "type": "import",
                    "module": alias.name,
                    "alias": alias.asname or "",
                    "line": node.lineno,
                })
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                full = f"{module}.{alias.name}" if module else alias.name
                imports.append({
                    "type": "from_import",
                    "module": full,
                    "alias": alias.asname or "",
                    "line": node.lineno,
                })
    return imports


def _extract_js_imports(source: str) -> List[Dict[str, str]]:
    """Extract imports from JS/TS source using regex."""
    imports = []
    # ES6 static imports
    for m in re.finditer(
        r'(?:import\s+(?:(?:\{[^}]*\}|[^;{]+)\s+from\s+)?["\']([^"\']+)["\']|require\s*\(\s*["\']([^"\']+)["\']\s*\))',
        source
    ):
        module = m.group(1) or m.group(2)
        imports.append({"type": "import", "module": module, "alias": "", "line": 0})
    return imports


def _extract_go_imports(source: str) -> List[Dict[str, str]]:
    """Extract imports from Go source using regex."""
    imports = []
    # Single imports: import "fmt"
    for m in re.finditer(r'import\s+"([^"]+)"', source):
        imports.append({"type": "import", "module": m.group(1), "alias": "", "line": 0})
    # Grouped imports: import ( "fmt" "os" )
    in_group = False
    for line in source.split("\n"):
        stripped = line.strip()
        if stripped.startswith("import ("):
            in_group = True
            continue
        if in_group and stripped.startswith(")"):
            in_group = False
            continue
        if in_group and (stripped.startswith('"') and stripped.endswith('"')):
            imports.append({"type": "import", "module": stripped.strip('"'), "alias": "", "line": 0})
    return imports


def _categorize_dependency(module_name: str, project_root: str) -> str:
    """Categorize a module as stdlib, third_party, or local."""
    top_level = module_name.split(".")[0]
    # Check stdlib
    if top_level in _STDLIB_MODULES:
        return "stdlib"
    # Check local (relative import or file exists in project)
    if module_name.startswith("."):
        return "local"
    local_path = os.path.join(project_root, top_level.replace(".", os.sep))
    if os.path.isdir(local_path) or os.path.isfile(local_path + ".py"):
        return "local"
    return "third_party"


def _find_python_files(root: str, recursive: bool = True) -> List[str]:
    """Find all Python files in a directory tree."""
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        if not recursive:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if d not in _DEP_IGNORE_DIRS]
        for f in filenames:
            if f.endswith(".py"):
                files.append(os.path.join(dirpath, f))
    return sorted(files)


def execute_find_dependencies(path: str, recursive: bool = True) -> str:
    """Analyze dependencies of a file or project directory.

    For Python files, uses ast module for precise import detection.
    For JS/TS/Go files, uses regex-based extraction.
    Categorizes dependencies as stdlib / third-party / local.
    Detects circular dependencies and potentially unused exports.

    Args:
        path: Absolute path to file or directory.
        recursive: Whether to scan subdirectories (default True).

    Returns:
        Formatted dependency report with imports, direction, layering,
        unused exports, and circular dependency analysis.
    """
    if not path or not os.path.isabs(path):
        return "❌ 错误：必须使用绝对路径！"

    resolved = os.path.realpath(path)
    project_root = resolved if os.path.isdir(resolved) else os.path.dirname(resolved)

    py_files = []
    js_files = []
    go_files = []

    if os.path.isfile(resolved):
        if resolved.endswith(".py"):
            py_files = [resolved]
        elif resolved.endswith((".js", ".ts", ".jsx", ".tsx")):
            js_files = [resolved]
        elif resolved.endswith(".go"):
            go_files = [resolved]
        else:
            return f"❌ 不支持的文件类型: {resolved}"
    elif os.path.isdir(resolved):
        for dirpath, dirnames, filenames in os.walk(resolved):
            if not recursive:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if d not in _DEP_IGNORE_DIRS]
            for f in filenames:
                fpath = os.path.join(dirpath, f)
                if f.endswith(".py"):
                    py_files.append(fpath)
                elif f.endswith((".js", ".ts", ".jsx", ".tsx")):
                    js_files.append(fpath)
                elif f.endswith(".go"):
                    go_files.append(fpath)
    else:
        return f"❌ 路径不存在: {path}"

    if not (py_files or js_files or go_files):
        return "未找到可分析的代码文件（Python/JS/TS/Go）。"

    # Phase 1: extract imports + defined names from all files
    file_info = {}  # rel_path -> {"imports": [...], "defined": set(), "categories": {stdlib, 3rd, local}}

    for fpath in py_files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except Exception:
            continue
        rel = os.path.relpath(fpath, project_root)
        imports = _extract_python_imports(source)
        funcs, classes = _extract_defined_names(source)
        cats = {"stdlib": set(), "third_party": set(), "local": set()}
        for imp in imports:
            cat = _categorize_dependency(imp["module"], project_root)
            cats[cat].add(imp["module"])
        file_info[rel] = {"imports": imports, "defined": funcs | classes,
                          "categories": cats, "is_python": True}

    for fpath in js_files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except Exception:
            continue
        rel = os.path.relpath(fpath, project_root)
        imports = _extract_js_imports(source)
        cats = {"stdlib": set(), "third_party": set(), "local": set()}
        for imp in imports:
            mod = imp["module"]
            if mod.startswith(".") or mod.startswith("/"):
                cats["local"].add(mod)
            else:
                cats["third_party"].add(mod)
        file_info[rel] = {"imports": imports, "defined": set(),
                          "categories": cats, "is_python": False}

    for fpath in go_files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except Exception:
            continue
        rel = os.path.relpath(fpath, project_root)
        imports = _extract_go_imports(source)
        cats = {"stdlib": set(), "third_party": set(), "local": set()}
        for imp in imports:
            mod = imp["module"]
            parts = mod.split("/")
            if len(parts) >= 3 and "." in parts[0]:
                cats["third_party"].add(mod)
            else:
                cats["stdlib"].add(mod)
        file_info[rel] = {"imports": imports, "defined": set(),
                          "categories": cats, "is_python": False}

    if not file_info:
        return "未找到可分析的文件。"

    # Phase 2: build forward + reverse dependency graphs
    forward = {}  # rel -> {stdlib: [], 3rd: [], local: [rel2]}
    reverse = {}  # rel -> [rel_of_importer]
    for rel, info in file_info.items():
        forward[rel] = {
            "stdlib": sorted(info["categories"]["stdlib"]),
            "third_party": sorted(info["categories"]["third_party"]),
            "local": sorted(info["categories"]["local"]),
        }
    # Build reverse: for each file's local deps, add it to the reverse index of the dep
    for rel, info in file_info.items():
        local_deps = info["categories"]["local"]
        for dep_module in local_deps:
            top_mod = dep_module.split(".")[0]
            dep_rel = top_mod + ".py"
            if dep_rel not in reverse:
                reverse[dep_rel] = []
            if rel not in reverse[dep_rel]:
                reverse[dep_rel].append(rel)

    # Phase 3: module layering
    # Bottom: files that import 0-1 local modules
    # Middle: files that both import and are imported
    # Top: files that are imported by no one but import local modules
    bottom = []
    middle = []
    top = []
    for rel, info in file_info.items():
        local_imports = len(info["categories"]["local"])
        imported_by = len(reverse.get(rel, []))
        if local_imports <= 1 and imported_by == 0:
            bottom.append(rel)
        elif imported_by == 0 and local_imports > 0:
            top.append(rel)
        else:
            middle.append(rel)

    # Phase 4: unused exports detection (Python only)
    unused_exports = {}  # rel -> [name, ...]
    for rel, info in file_info.items():
        if not info.get("is_python"):
            continue
        defined = info.get("defined", set())
        if not defined:
            continue
        # Collect all names imported FROM this module
        imported_names = set()
        for other_rel, other_info in file_info.items():
            if other_rel == rel:
                continue
            for imp in other_info.get("imports", []):
                if imp.get("type") == "from_import":
                    full = imp["module"]
                    # module is "clipboard_store.ClipboardStore" -> module="clipboard_store", name="ClipboardStore"
                    parts = full.split(".")
                    if len(parts) >= 2:
                        mod_part = parts[0]
                        name_part = ".".join(parts[1:])
                        # Check if the module part matches this file's module name
                        # (e.g., clipboard_store.ClipboardStore → module=clipboard_store)
                        mod_file = mod_part + ".py"
                        if mod_file == rel:
                            imported_names.add(name_part)
                    elif full == rel.replace(".py", ""):
                        # import module directly, all defs are accessible
                        pass
        # Check for unused
        unused = set()
        for name in defined:
            if name.startswith("_"):
                continue  # private by convention
            if name not in imported_names:
                unused.add(name)
        if unused:
            unused_exports[rel] = sorted(unused)

    # Phase 5: circular dependency
    all_modules = {}
    for rel, info in file_info.items():
        all_modules[rel] = list(info["categories"]["local"])
    circles = _find_circular_deps(all_modules)

    # ── Format Output ──────────────────────────────────────────

    total_stdlib = sum(len(f["categories"]["stdlib"]) for f in file_info.values())
    total_third = sum(len(f["categories"]["third_party"]) for f in file_info.values())
    total_local = sum(len(f["categories"]["local"]) for f in file_info.values())
    total_imports = total_stdlib + total_third + total_local

    lines = [
        f"🔗 项目依赖分析: {os.path.basename(project_root)}",
        "═" * 50,
        "",
        f"📊 概览",
        f"   · 总文件数:      {len(file_info)}",
        f"   · 总导入语句:     {total_imports}",
        f"   · 标准库:        {total_stdlib}",
        f"   · 第三方包:      {total_third}",
        f"   · 本地模块依赖:   {total_local}",
        f"   · 循环依赖:      {'⚠️ 发现 ' + str(len(circles)) + ' 个' if circles else '✅ 无'}",
        "",
    ]

    # Module layering
    lines.append("📦 模块分层")
    if bottom:
        lines.append(f"   底层 (不依赖其他本地模块):")
        lines.append(f"     {', '.join(sorted(bottom))}")
    if middle:
        lines.append(f"   中间层 (互相依赖):")
        # Show top 8 middle files
        mid_show = sorted(middle)[:8]
        lines.append(f"     {', '.join(mid_show)}")
        if len(middle) > 8:
            lines.append(f"     ... 及其他 {len(middle) - 8} 个")
    if top:
        lines.append(f"   顶层 (不被其他模块依赖):")
        lines.append(f"     {', '.join(sorted(top))}")
    lines.append("")

    # Per-file detail (show ALL files, no truncation)
    # Sort by total imports descending
    sorted_rels = sorted(file_info.items(),
                         key=lambda x: len(x[1]["categories"]["stdlib"]) + len(x[1]["categories"]["third_party"]) + len(x[1]["categories"]["local"]),
                         reverse=True)

    lines.append("─" * 55)
    lines.append(f"📄 各文件依赖详情（按依赖数降序，共 {len(sorted_rels)} 个文件）")
    lines.append("")

    for idx, (rel, info) in enumerate(sorted_rels, 1):
        cats = info["categories"]
        imp_count = len(cats["stdlib"]) + len(cats["third_party"]) + len(cats["local"])
        imported_by = reverse.get(rel, [])
        lines.append(f"【{idx}】{rel} ({imp_count} 导入)")
        lines.append(f"  📥 导入:")
        # Show non-empty categories
        if cats["stdlib"]:
            stdlib_list = ", ".join(sorted(cats["stdlib"])[:12])
            extra = f" +{len(cats['stdlib']) - 12}" if len(cats['stdlib']) > 12 else ""
            lines.append(f"    stdlib: {stdlib_list}{extra}")
        if cats["third_party"]:
            third_list = ", ".join(sorted(cats["third_party"])[:8])
            extra = f" +{len(cats['third_party']) - 8}" if len(cats['third_party']) > 8 else ""
            lines.append(f"    3rd:   {third_list}{extra}")
        if cats["local"]:
            local_files_shown = set()
            for local_mod in sorted(cats["local"]):
                local_mod_clean = local_mod.split(".")[0]
                local_rel = local_mod_clean + ".py"
                display = local_mod_clean + (".py" if local_rel in file_info else "")
                if display not in local_files_shown:
                    local_files_shown.add(display)
                    lines.append(f"    → {display}")
        if imported_by:
            lines.append(f"  📤 被引用: {', '.join(sorted(imported_by)[:6])}"
                         + (f" +{len(imported_by)-6}" if len(imported_by) > 6 else ""))
        else:
            lines.append(f"  📤 被引用: (无)")
        lines.append("")

    # Circular deps
    lines.append("─" * 55)
    lines.append("⚠️  循环依赖检测")
    if circles:
        for a, b in circles:
            lines.append(f"   {a} ↔ {b}")
    else:
        lines.append("   ✅ 无")

    # Unused exports
    lines.append("")
    lines.append("─" * 55)
    lines.append("🕳️  未使用的导出（定义了但未被其他文件引用）")
    if unused_exports:
        total_unused = sum(len(v) for v in unused_exports.values())
        lines.append(f"   检测到 {total_unused} 个（可能包含误报：动态注册、`__main__` 入口、或仅内部使用的符号）")
        for rel, names in sorted(unused_exports.items()):
            for name in sorted(names):
                lines.append(f"   · {rel}: {name}()")
    else:
        lines.append("   ✅ 无 — 所有定义的函数/类均被引用")

    return "\n".join(lines)


def _extract_defined_names(source: str) -> tuple:
    """Extract top-level function and class names from Python source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(), set()
    funcs = set()
    classes = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.add(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.add(node.name)
    return funcs, classes


def _find_circular_deps(modules: Dict[str, List[str]]) -> List[tuple]:
    """Simple pairwise circular dependency detection."""
    circles = []
    names = list(modules.keys())
    for i, name_a in enumerate(names):
        for name_b in names[i + 1:]:
            if name_a in modules.get(name_b, []) and name_b in modules.get(name_a, []):
                circles.append((name_a, name_b))
    return circles


# ── Parse File AST ──────────────────────────────────────────────────────────

_EXT_TO_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "javascript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java",
    ".cpp": "cpp", ".c": "cpp", ".h": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".kt": "kotlin", ".swift": "swift",
}


def _parse_python_ast(path: str, include_body: bool = False,
                      include_imports: bool = True,
                      include_docstrings: bool = False,
                      exclude_private: bool = False) -> str:
    """Parse Python file using stdlib ast module."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except Exception as e:
        return f"❌ 无法读取文件: {e}"

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return f"❌ Python 语法错误: {e}"

    source_lines = source.split("\n")
    total_lines = len(source_lines)
    code_lines = sum(1 for l in source_lines if l.strip())
    blank_lines = total_lines - code_lines
    filename = os.path.basename(path)

    imports = []
    classes = []
    functions = []
    constants = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else ""))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = []
            for alias in node.names:
                n = alias.name + (f" as {alias.asname}" if alias.asname else "")
                names.append(n)
            imports.append(f"from {module} import {', '.join(names)}")
        elif isinstance(node, ast.ClassDef):
            if exclude_private and node.name.startswith("_"):
                continue
            bases = []
            for b in node.bases:
                try:
                    bases.append(ast.unparse(b))
                except Exception:
                    bases.append("...")
            cls_info = {
                "name": node.name,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "bases": bases,
                "decorators": [],
                "methods": [],
            }
            for dec in node.decorator_list:
                try:
                    cls_info["decorators"].append(ast.unparse(dec))
                except Exception:
                    cls_info["decorators"].append("...")
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if exclude_private and child.name.startswith("_"):
                        continue
                    args = []
                    for arg in child.args.args:
                        arg_str = arg.arg
                        if arg.annotation:
                            try:
                                arg_str += f": {ast.unparse(arg.annotation)}"
                            except Exception:
                                arg_str += ": ?"
                        args.append(arg_str)
                    ret = ""
                    if child.returns:
                        try:
                            ret = f" -> {ast.unparse(child.returns)}"
                        except Exception:
                            ret = " -> ?"
                    m_info = {
                        "name": child.name,
                        "line": child.lineno,
                        "end_line": getattr(child, "end_lineno", child.lineno),
                        "args": args,
                        "returns": ret,
                        "is_async": isinstance(child, ast.AsyncFunctionDef),
                        "decorators": [],
                        "docstring": ast.get_docstring(child) or "" if (include_docstrings or include_body) else "",
                    }
                    if include_body:
                        m_info["body"] = ast.get_source_segment(source, child) or ""
                    for dec in child.decorator_list:
                        try:
                            m_info["decorators"].append(ast.unparse(dec))
                        except Exception:
                            m_info["decorators"].append("...")
                    cls_info["methods"].append(m_info)
            classes.append(cls_info)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if exclude_private and node.name.startswith("_"):
                continue
            args = []
            for arg in node.args.args:
                arg_str = arg.arg
                if arg.annotation:
                    try:
                        arg_str += f": {ast.unparse(arg.annotation)}"
                    except Exception:
                        arg_str += ": ?"
                args.append(arg_str)
            ret = ""
            if node.returns:
                try:
                    ret = f" -> {ast.unparse(node.returns)}"
                except Exception:
                    ret = " -> ?"
            fn_info = {
                "name": node.name,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "args": args,
                "returns": ret,
                "is_async": isinstance(node, ast.AsyncFunctionDef),
                "decorators": [],
                "docstring": ast.get_docstring(node) or "" if (include_docstrings or include_body) else "",
            }
            for dec in node.decorator_list:
                try:
                    fn_info["decorators"].append(ast.unparse(dec))
                except Exception:
                    fn_info["decorators"].append("...")
            if include_body:
                fn_info["body"] = ast.get_source_segment(source, node) or ""
            functions.append(fn_info)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                try:
                    name = ast.unparse(target)
                    if name.isupper() and name.isidentifier():
                        constants.append(name)
                except Exception:
                    pass

    # Meta summary line (P0)
    func_count = len([f for f in functions if not (exclude_private and f["name"].startswith("_"))])
    cls_count = len([c for c in classes if not (exclude_private and c["name"].startswith("_"))])
    out = [
        f"📦 {filename}  [Python | {total_lines} 行 | {func_count} 函数 | {cls_count} 类 | {len(imports)} 导入]",
        "",
    ]

    if include_imports and imports:
        out.append(f"📥 导入 ({len(imports)}):")
        for imp in imports:
            out.append(f"  · {imp}")
        out.append("")

    if classes:
        out.append(f"📚 类 ({len(classes)}):")
        for cls in classes:
            base_str = f"({', '.join(cls['bases'])})" if cls['bases'] else ""
            dec_str = f"@{' @'.join(cls['decorators'])} " if cls['decorators'] else ""
            line_range = f"L{cls['line']}-{cls['end_line']}" if cls['end_line'] != cls['line'] else f"L{cls['line']}"
            out.append(f"  📦 {dec_str}{cls['name']}{base_str}  ({line_range})")
            for m in cls["methods"]:
                if exclude_private and m["name"].startswith("_"):
                    continue
                async_str = "async " if m["is_async"] else ""
                dec_str_m = f"@{' @'.join(m['decorators'])} " if m['decorators'] else ""
                args_str = ", ".join(m["args"])
                m_line_range = f"L{m['line']}-{m['end_line']}" if m['end_line'] != m['line'] else f"L{m['line']}"
                sig = f"{dec_str_m}{async_str}def {m['name']}({args_str}){m['returns']}  ({m_line_range})"
                if include_docstrings and m["docstring"]:
                    doc_first = m["docstring"].split("\n")[0][:60]
                    sig += f"  # {doc_first}"
                out.append(f"    └ {sig}")
                if include_body and m.get("body"):
                    for body_line in m["body"].split("\n"):
                        out.append(f"       {body_line}")
        out.append("")

    if functions:
        out.append(f"📋 函数 ({len(functions)}):")
        for fn in functions:
            if exclude_private and fn["name"].startswith("_"):
                continue
            async_str = "async " if fn["is_async"] else ""
            dec_str = f"@{' @'.join(fn['decorators'])} " if fn['decorators'] else ""
            args_str = ", ".join(fn["args"])
            line_range = f"L{fn['line']}-{fn['end_line']}" if fn['end_line'] != fn['line'] else f"L{fn['line']}"
            sig = f"{dec_str}{async_str}def {fn['name']}({args_str}){fn['returns']}  ({line_range})"
            if include_docstrings and fn["docstring"]:
                doc_first = fn["docstring"].split("\n")[0][:60]
                sig += f"  # {doc_first}"
            out.append(f"  · {sig}")
            if include_body and fn.get("body"):
                for body_line in fn["body"].split("\n"):
                    out.append(f"     {body_line}")
        out.append("")

    if constants:
        out.append(f"📌 全局常量 ({len(constants)}):")
        for c in constants:
            out.append(f"  · {c}")
        out.append("")

    return "\n".join(out)


def _parse_tree_sitter(path: str, language: str, include_body: bool) -> str:
    """Parse non-Python file using Tree-sitter."""
    try:
        import tree_sitter
    except ImportError:
        msg = f"❌ 当前环境未安装 Tree-sitter，仅支持 Python 语言分析。\n\n如需分析"
        if language == "unknown":
            msg += "非 Python 文件，请安装 Tree-sitter：\n  pip install tree-sitter"
        else:
            msg += f" .{language} 文件，请安装：\n  pip install tree-sitter tree-sitter-{language}"
        msg += "\n\n解析当前文件时请使用 language=python（仅对 Python 项目）。"
        return msg
    return f"❌ Tree-sitter 解析尚未实现（language={language}）"


def execute_parse_file_ast(path: str, language: str = "auto",
                           include_body: bool = False,
                           include_imports: bool = True,
                           include_docstrings: bool = False,
                           exclude_private: bool = False) -> str:
    """Parse a code file and extract its structure: classes, functions,
    imports, constants, and other structural elements.

    Uses Python's stdlib ast module for Python files (zero extra dependencies).
    For other languages, requires tree-sitter to be installed.

    Args:
        path: Absolute path to the file to parse.
        language: Language hint ("python", "javascript", "go", etc. or "auto").
        include_body: If True, include function/method body source.
        include_imports: If True, list import statements.
        include_docstrings: If True, show docstring summary after signatures.
        exclude_private: If True, filter out _-prefixed private functions.

    Returns:
        Formatted structural overview of the file.
    """
    if not path or not os.path.isabs(path):
        return "❌ 错误：必须使用绝对路径！"
    if not os.path.isfile(path):
        return f"❌ 文件不存在: {path}"

    if language == "auto":
        ext = os.path.splitext(path)[1].lower()
        language = _EXT_TO_LANG.get(ext, "unknown")

    if language == "python":
        return _parse_python_ast(path, include_body, include_imports,
                                 include_docstrings, exclude_private)
    else:
        return _parse_tree_sitter(path, language, include_body)


# ── Sub-Agent Tool ─────────────────────────────────────────────────────────

_MAX_SUBAGENT_TURNS = 15
_SUBAGENT_TIMEOUT_PER_TURN = 60

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


def execute_sub_agent(task: str, max_turns: int = 10,
                      agent_type: str = "general",
                      run_in_background: bool = False) -> str:
    """Spawn an isolated sub-agent to complete a task independently.

    The sub-agent gets its own bash session, a clean file-read state,
    and a fresh LLM context. All parent state is restored after completion.

    Args:
        task: Description of the task for the sub-agent.
        max_turns: Maximum tool-calling iterations (1-15, default 10).
        agent_type: "general" (full tools), "explore" (read-only), or "bash" (shell only).
        run_in_background: If True, run in background thread and return immediately.

    Returns:
        The sub-agent's final text response, or an error message.
    """
    if agent_type not in _SUBAGENT_TYPES:
        return f"错误：无效的子代理类型「{agent_type}」。有效值：general, explore, bash"

    if run_in_background:
        global _background_subagent_id
        _background_subagent_id += 1
        subagent_id = _background_subagent_id
        _background_subagent_status[subagent_id] = {
            "task": task[:100],
            "started_at": time.time(),
            "status": "running",
        }
        _run_subagent_background(task, max_turns, agent_type, subagent_id)
        return f"⏳ 子代理已启动（任务ID: {subagent_id}，类型: {agent_type}）。" \
               f"完成后结果将保存至 /tmp/opencode_subagent_{subagent_id}_result.txt，" \
               f"可让主代理使用 read_file 读取。"

    return _execute_subagent_sync(task, max_turns, agent_type)


_background_subagent_id = 0
_background_subagent_results: Dict[int, str] = {}
"""Stores completed background sub-agent results, keyed by subagent_id.
Cleared once consumed by check_background_subagents()."""
_background_subagent_status: Dict[int, Dict[str, Any]] = {}
"""Tracks running/background sub-agents: {id: {"task": str, "started_at": float, "status": str}}"""


def check_background_subagents() -> str:
    """Check if any background sub-agents have completed since last check.
    Returns a formatted message with results, or empty string if none.
    Clears the consumed entries. Designed to be called before sending a user message."""
    global _background_subagent_results
    if not _background_subagent_results:
        return ""
    parts = []
    for sid in sorted(_background_subagent_results):
        parts.append(
            f"## 后台子代理 {sid} 已完成\n"
            f"结果文件: /tmp/opencode_subagent_{sid}_result.txt\n\n"
            f"请使用 read_file 读取结果文件以获取详细信息。"
        )
    _background_subagent_results.clear()
    return "\n\n---\n\n".join(parts)


def _run_subagent_background(task: str, max_turns: int, agent_type: str, subagent_id: int):
    """Run a sub-agent in a background daemon thread."""
    def _run():
        global _background_subagent_results, _background_subagent_status
        result = _execute_subagent_sync(task, max_turns, agent_type)
        _background_subagent_status[subagent_id] = {
            "task": task[:100],
            "started_at": _background_subagent_status.get(subagent_id, {}).get("started_at", 0),
            "status": "completed",
        }
        result_path = f"/tmp/opencode_subagent_{subagent_id}_result.txt"
        try:
            with open(result_path, "w", encoding="utf-8") as f:
                f.write(result)
        except OSError:
            pass
        _background_subagent_results[subagent_id] = result
        try:
            summary = result[:100] + "..." if len(result) > 100 else result
            execute_send_notification(
                summary=f"子代理 {subagent_id} 已完成",
                body=f"{summary}\n结果已保存至 {result_path}",
                urgency="normal",
                expire_time=8000,
            )
        except Exception:
            pass

    import threading as _threading
    t = _threading.Thread(target=_run, daemon=True)
    t.start()


def _execute_subagent_sync(task: str, max_turns: int, agent_type: str) -> str:
    """Synchronous sub-agent execution (internal)."""
    sub_tools = _build_subagent_tools(agent_type)
    type_info = _SUBAGENT_TYPES[agent_type]

    # Use a local bash session — don't touch global _bash_session
    local_session = _BashSession()
    try:
        local_session.start()
    except Exception as e:
        return f"错误：无法创建子 bash 会话 — {e}"

    # Temporarily override global state for sub-agent execution
    global _bash_cwd, _bash_session
    saved_bash_cwd = _bash_cwd
    saved_bash_session = _bash_session
    saved_read_state = dict(_READ_FILE_STATE)
    _bash_session = local_session
    _bash_cwd = _BASH_DEFAULT_CWD
    _READ_FILE_STATE.clear()

    try:
        config = _get_llm_config()
    except RuntimeError as e:
        _bash_cwd = saved_bash_cwd
        _bash_session = saved_bash_session
        local_session.stop()
        return f"错误：{e}"
    if not config.api_key:
        _bash_cwd = saved_bash_cwd
        _bash_session = saved_bash_session
        local_session.stop()
        return "错误：未配置 LLM API key。请在 AI 设置中配置。"
    if not config.base_url:
        _bash_cwd = saved_bash_cwd
        _bash_session = saved_bash_session
        local_session.stop()
        return "错误：未配置 LLM base URL。请在 AI 设置中配置。"

    clamped_turns = max(1, min(max_turns, _MAX_SUBAGENT_TURNS))
    messages = [
        {"role": "system", "content": type_info["system_prompt"]},
        {"role": "user", "content": task},
    ]

    try:
        llm = _LLMHttpClient()
        final_text = ""

        for turn in range(clamped_turns):
            try:
                response = llm.sync_chat_completion(
                    base_url=config.base_url,
                    api_key=config.api_key,
                    model_name=config.model_name,
                    messages=messages,
                    timeout=_SUBAGENT_TIMEOUT_PER_TURN,
                    tools=sub_tools,
                    tool_choice=TOOL_CHOICE_AUTO,
                    max_tokens=config.max_tokens,
                )
            except _LLMHttpError as e:
                return f"子代理 LLM 请求失败：{e}"
            except Exception as e:
                return f"子代理 LLM 请求异常：{e}"

            content = response.get("content") or ""
            tool_calls = response.get("tool_calls")

            if not tool_calls:
                final_text = content  # may be empty → falls through to rebuild below
                break

            assistant_msg = {"role": "assistant", "content": content or None}
            assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            for tc in tool_calls:
                tc_name = tc.get("function", {}).get("name", "")
                try:
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
            # Rebuild result from all tool results and assistant messages
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

        if len(final_text) > MAX_TOOL_RESULT_CHARS:
            final_text = final_text[:MAX_TOOL_RESULT_CHARS] + f"\n\n...（结果已截断，共 {len(final_text)} 字符）"
        return final_text

    finally:
        local_session.stop()
        _bash_cwd = saved_bash_cwd
        _bash_session = saved_bash_session
        _READ_FILE_STATE.clear()
        _READ_FILE_STATE.update(saved_read_state)


# ── Tool Executor Registry ─────────────────────────────────────────────────

TOOL_EXECUTORS: Dict[str, Callable] = {
    "web_search": execute_web_search,
    "web_fetch": execute_web_fetch,
    "list_directory": execute_list_directory,
    "read_file": execute_read_file,
    "get_current_time": execute_get_current_time,
    "grep_search": execute_grep_search,
    "glob_find": execute_glob_find,
    "file_info": execute_file_info,
    "ask_user_question": execute_ask_user_question,
    "write_file": execute_write_file,
    "edit_file": execute_edit_file,
    "todo_create": execute_todo_create,
    "todo_update": execute_todo_update,
    "todo_list": execute_todo_list,
    "bash": execute_bash,
    "delete_file": execute_delete_file,
    "rename_file": execute_rename_file,
    "send_notification": execute_send_notification,
    "read_qq_mail": execute_read_qq_mail,
    "get_code_metrics": execute_get_code_metrics,
    "find_project_dependencies": execute_find_dependencies,
    "parse_file_ast": execute_parse_file_ast,
    "sub_agent": execute_sub_agent,
}


def execute_get_subagent_status() -> str:
    """查询所有后台子代理的执行状态。"""
    if not _background_subagent_status:
        return "当前没有运行中的后台子代理。"
    lines = ["📋 后台子代理状态:\n"]
    for sid in sorted(_background_subagent_status):
        info = _background_subagent_status[sid]
        status = info.get("status", "unknown")
        task = info.get("task", "?")
        started = info.get("started_at", 0)
        elapsed = int(time.time() - started) if started else 0
        status_emoji = "✅" if status == "completed" else "🔄" if status == "running" else "❌"
        elapsed_str = f"{elapsed // 60}分{elapsed % 60}秒" if elapsed >= 60 else f"{elapsed}秒"
        lines.append(f"{status_emoji} ID={sid}，状态={status}，耗时={elapsed_str}")
        lines.append(f"   任务: {task}")
        if status == "completed":
            lines.append(f"   结果文件: /tmp/opencode_subagent_{sid}_result.txt")
        lines.append("")
    return "\n".join(lines).strip()


TOOL_EXECUTORS["get_subagent_status"] = execute_get_subagent_status


def execute_tool_call(tool_call: dict, cancel_event=None) -> str:
    """Execute a single tool call and return the result as a string.

    Args:
        tool_call: OpenAI-format tool_call dict with "id", "type", "function" keys.
                   tool_call["function"] has "name" and "arguments" (JSON string).
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


def format_tool_calls_for_display(tool_calls: List[dict]) -> str:
    """Format tool calls into an HTML snippet for WebView display.

    Returns a string with HTML like:
        <div class="tool-call-info">🔍 <b>网络搜索：</b>查询内容</div>
    """
    if not tool_calls:
        return ""

    parts = []
    for tc in tool_calls:
        name = tc.get("function", {}).get("name", "")
        try:
            args = json.loads(tc.get("function", {}).get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}

        if name == "web_search":
            query = args.get("query", "")
            safe_query = html.escape(query)
            parts.append(f'<div class="tool-call-info">🔍 <b>网络搜索：</b>{safe_query}</div>')
        elif name == "web_fetch":
            url = args.get("url", "")
            safe_url = html.escape(url)
            parts.append(f'<div class="tool-call-info">📄 <b>获取页面：</b>{safe_url}</div>')
        elif name == "list_directory":
            path = args.get("path", "")
            safe_path = html.escape(path)
            parts.append(f'<div class="tool-call-info">📁 <b>列出目录：</b>{safe_path}</div>')
        elif name == "read_file":
            path = args.get("path", "")
            start = args.get("start_line", 1)
            end = args.get("end_line")
            safe_path = html.escape(path)
            if start > 1 or end is not None:
                range_str = f"第 {start} 行至 {end if end is not None else '末尾'}"
                parts.append(f'<div class="tool-call-info">📝 <b>读取文件：</b>{safe_path} ({range_str})</div>')
            else:
                parts.append(f'<div class="tool-call-info">📝 <b>读取文件：</b>{safe_path}</div>')
        elif name == "get_current_time":
            tz = args.get("timezone", "")
            if tz:
                safe_tz = html.escape(tz)
                parts.append(f'<div class="tool-call-info">🕐 <b>查询时间：</b>{safe_tz}</div>')
            else:
                parts.append(f'<div class="tool-call-info">🕐 <b>查询时间：</b>本地时间</div>')
        elif name == "grep_search":
            pattern = args.get("pattern", "")
            search_path = args.get("path", "")
            safe_pattern = html.escape(pattern)
            safe_path = html.escape(search_path)
            parts.append(f'<div class="tool-call-info">🔍 <b>搜索内容：</b>{safe_pattern} 在 {safe_path}</div>')
        elif name == "glob_find":
            gpattern = args.get("pattern", "")
            gpath = args.get("path", "")
            safe_gpattern = html.escape(gpattern)
            safe_gpath = html.escape(gpath)
            parts.append(f'<div class="tool-call-info">📂 <b>查找文件：</b>{safe_gpattern} 在 {safe_gpath}</div>')
        elif name == "file_info":
            fpath = args.get("path", "")
            safe_fpath = html.escape(fpath)
            parts.append(f'<div class="tool-call-info">📋 <b>文件信息：</b>{safe_fpath}</div>')
        elif name == "ask_user_question":
            parts.append('<div class="tool-call-info">💬 <b>询问用户</b></div>')
        elif name == "write_file":
            wpath = args.get("path", "")
            content = args.get("content", "")
            safe_wpath = html.escape(wpath)
            mode = "覆盖" if args.get("force", False) else "写入"
            preview_label = f"内容预览（{len(content)} 字符）" if len(content) <= 500 else f"内容预览（前500 / 共{len(content)} 字符）"
            parts.append(
                f'<div class="tool-call-info">✏️ <b>{mode}文件：</b>{safe_wpath}</div>'
                + _make_collapsible_preview(content, preview_label)
            )
        elif name == "edit_file":
            epath = args.get("path", "")
            eold = args.get("old_string", "")
            re_all = args.get("replace_all", False)
            safe_epath = html.escape(epath)
            old_preview = html.escape(eold[:200])
            old_label = f"替换原文（{len(eold)} 字符）" if len(eold) <= 200 else f"替换原文（前200 / 共{len(eold)} 字符）"
            plural = "全部" if re_all else "第一处"
            parts.append(
                f'<div class="tool-call-info">✏️ <b>编辑文件（{plural}匹配）：</b>{safe_epath}</div>'
                + _make_collapsible_preview(eold, old_label, max_chars=200)
            )
        elif name == "delete_file":
            dpath = args.get("path", "")
            rec = args.get("recursive", False)
            safe_dpath = html.escape(dpath)
            rec_tag = "（递归）" if rec else ""
            parts.append(
                f'<div class="tool-call-info">🗑️ <b>删除{rec_tag}：</b>{safe_dpath}</div>'
            )
        elif name == "rename_file":
            rsrc = args.get("source", "")
            rdst = args.get("destination", "")
            safe_rsrc = html.escape(rsrc)
            safe_rdst = html.escape(rdst)
            parts.append(
                f'<div class="tool-call-info">📦 <b>重命名：</b>{safe_rsrc} → {safe_rdst}</div>'
            )
        elif name == "todo_create":
            ttitle = args.get("title", "")
            tpriority = args.get("priority", "medium")
            safe_title = html.escape(ttitle)
            parts.append(
                f'<div class="tool-call-info">✅ <b>创建任务：</b>{safe_title}'
                f'<span style="color:#888;font-size:11px;margin-left:8px;">优先级: {tpriority}</span></div>'
            )
        elif name == "todo_update":
            tid = args.get("id", "")
            tstatus = args.get("status", "")
            safe_id = html.escape(tid)
            parts.append(
                f'<div class="tool-call-info">🔄 <b>更新任务：</b>{safe_id}'
                + (f' → {tstatus}' if tstatus else '')
                + '</div>'
            )
        elif name == "todo_list":
            tid = args.get("id", "")
            sfilter = args.get("status_filter", "")
            if tid:
                parts.append(f'<div class="tool-call-info">📋 <b>查询任务：</b>{html.escape(tid)}</div>')
            elif sfilter:
                parts.append(f'<div class="tool-call-info">📋 <b>任务清单：</b>仅 {sfilter}</div>')
            else:
                parts.append('<div class="tool-call-info">📋 <b>任务清单：</b>全部</div>')
        elif name == "send_notification":
            nsummary = args.get("summary", "")
            nurgency = args.get("urgency", "normal")
            safe_nsum = html.escape(nsummary)
            parts.append(
                f'<div class="tool-call-info">🔔 <b>发送通知：</b>{safe_nsum}'
                f'<span style="color:#888;font-size:11px;margin-left:8px;">紧急度: {nurgency}</span></div>'
            )
        elif name == "sub_agent":
            stask = args.get("task", "")
            safe_task = html.escape(stask[:120])
            max_t = args.get("max_turns", 10)
            parts.append(
                f'<div class="tool-call-info">🔄 <b>子代理任务：</b>{safe_task}'
                f'<span style="color:#888;font-size:11px;margin-left:8px;">最多 {max_t} 轮</span></div>'
            )
        elif name == "bash":
            cmd = args.get("command", "")
            cmd_timeout = args.get("timeout", 60)
            safe_cmd = html.escape(cmd)
            first_line = cmd.split("\n")[0].strip() if cmd else ""
            safe_first = html.escape(first_line)
            cmd_label = f"命令预览（{len(cmd)} 字符）" if len(cmd) <= 300 else f"命令预览（前300 / 共{len(cmd)} 字符）"
            parts.append(
                f'<div class="tool-call-info">🖥️ <b>执行命令：</b>{safe_first}</div>'
                f'<div style="margin: 2px 0 4px 16px; font-size: 11px; color: #888;">超时限制：{cmd_timeout}s</div>'
                + _make_collapsible_preview(cmd, cmd_label, max_chars=300, use_pre=True)
            )
        elif name == "read_qq_mail":
            count = args.get("max_results", 5)
            folder = args.get("folder", "INBOX")
            criteria = args.get("search_criteria", "ALL")
            safe_folder = html.escape(folder)
            safe_criteria = html.escape(criteria)
            parts.append(
                f'<div class="tool-call-info">📧 <b>读取QQ邮件：</b>'
                f'{count} 封，文件夹: {safe_folder}，条件: {safe_criteria}</div>'
            )
        elif name == "parse_file_ast":
            fpath = args.get("path", "")
            lang = args.get("language", "auto")
            safe_path = html.escape(fpath)
            safe_lang = html.escape(lang)
            parts.append(
                f'<div class="tool-call-info">📦 <b>解析AST：</b>{safe_path} ({safe_lang})</div>'
            )
        elif name == "get_code_metrics":
            fpath = args.get("path", "")
            safe_path = html.escape(fpath)
            parts.append(
                f'<div class="tool-call-info">📊 <b>代码度量：</b>{safe_path}</div>'
            )
        elif name == "find_project_dependencies":
            fpath = args.get("path", "")
            rec = args.get("recursive", True)
            safe_path = html.escape(fpath)
            rec_str = "递归" if rec else "非递归"
            parts.append(
                f'<div class="tool-call-info">🔗 <b>项目依赖分析：</b>{safe_path} ({rec_str})</div>'
            )
        else:
            safe_name = html.escape(name)
            parts.append(f'<div class="tool-call-info">🔧 <b>工具调用：</b>{safe_name}</div>')

    return "\n".join(parts)


def _make_collapsible_preview(content: str, label: str, max_chars: int = 500,
                              use_pre: bool = False) -> str:
    """Build a collapsible preview HTML block.

    Args:
        content: The text content to preview.
        label: Display label for the collapsed state (e.g. "内容预览（120 字符）").
        max_chars: Truncation limit before the fold.
        use_pre: Ignored, as container is now pre tag.

    Returns:
        HTML string of the collapsible box.
    """
    truncated = len(content) > max_chars
    preview = content[:max_chars]
    if truncated:
        preview += f"\n\n...（已截断，共 {len(content)} 字符）"
    inner = html.escape(preview)
    return (
        f'<div class="tool-result-box">\n'
        f'<div class="tool-result-header">\n'
        f'<span>📄 {html.escape(label)}</span>\n'
        f'<span class="tool-result-toggle" onclick="toggleToolResult(this)">展开</span>\n'
        f'</div>\n'
        f'<pre class="tool-result-content" style="display: none;">\n'
        f'{inner}\n'
        f'</pre>\n'
        f'</div><!-- tool-result-marker -->'
    )


def render_collapsible_tool_result(name: str, content: str) -> str:
    """Render tool result block into collapsible HTML structure."""
    safe_name = html.escape(name)
    MAX_TOOL_DISPLAY = 2000
    display = content[:MAX_TOOL_DISPLAY]
    if len(content) > MAX_TOOL_DISPLAY:
        display += f"\n\n...（结果已截断，共 {len(content)} 字符）"
    safe_display = html.escape(display)

    return (
        f'<div class="tool-result-box">\n'
        f'<div class="tool-result-header">\n'
        f'<span>📎 工具结果 ({safe_name})</span>\n'
        f'<span class="tool-result-toggle" onclick="toggleToolResult(this)">展开</span>\n'
        f'</div>\n'
        f'<pre class="tool-result-content" style="display: none;">\n'
        f'{safe_display}\n'
        f'</pre>\n'
        f'</div><!-- tool-result-marker -->'
    )


def format_tool_result_for_display(name: str, content: str) -> str:
    """Format a tool execution result into an HTML snippet for WebView display."""
    return render_collapsible_tool_result(name, content)
