"""
Tool Registry — Function Calling 工具定义与执行器

为 AI 面板提供 OpenAI-compatible 工具注册、调度和执行能力。
首期工具：web_search（网络搜索）

所有工具使用零新外部依赖（requests 和 stdlib only）。
"""

import datetime
import fnmatch
import json
import os
import pathlib
import select
import stat
import subprocess
import time
import urllib.parse
import re
import shutil
import html
import uuid
from html.parser import HTMLParser
from typing import Any, Dict, Final, List, Optional, Callable, Tuple

import requests


_IGNORE_DIRS: Final = {"node_modules", "venv", ".venv", "env", "__pycache__", "build", "dist", "target", "cache", ".cache"}


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
                        blocked_by: Optional[List[str]] = None) -> str:
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
        "status": initial_status,
        "priority": priority,
        "blocked_by": blocked_by or [],
        "created_at": now,
        "updated_at": now,
    }

    todos.append(new_todo)
    data["next_id"] += 1
    _save_todos(data)

    status_emoji = {"pending": "⚪", "blocked": "🔴", "in_progress": "🟡", "completed": "🟢", "failed": "⚫", "cancelled": "⭕"}
    emoji = status_emoji.get(initial_status, "⚪")
    return f"{emoji} 已创建任务「{title}」（ID: {todo_id}，状态: {initial_status}）"


def execute_todo_update(id: str, status: Optional[str] = None,
                        title: Optional[str] = None,
                        description: Optional[str] = None,
                        priority: Optional[str] = None,
                        add_blocked_by: Optional[List[str]] = None) -> str:
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

    status_emoji = {"pending": "⚪", "blocked": "🔴", "in_progress": "🟡", "completed": "🟢", "failed": "⚫", "cancelled": "⭕"}
    emoji = status_emoji.get(todo.get("status", "pending"), "⚪")
    return f"{emoji} 已更新任务「{todo['title']}」状态为 {todo['status']}"


def execute_todo_list(id: Optional[str] = None,
                      status_filter: Optional[str] = None) -> str:
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
        status_emoji = {"pending": "⚪", "blocked": "🔴", "in_progress": "🟡", "completed": "🟢", "failed": "⚫", "cancelled": "⭕"}
        emoji = status_emoji.get(todo.get("status", "pending"), "⚪")
        blocked_by_str = ", ".join(todo.get("blocked_by", [])) or "无"
        desc = todo.get("description", "")
        desc_block = f"描述:\n{desc}\n---\n" if desc else ""
        return (
            f"📋 任务详情\n"
            f"---\n"
            f"ID:          {todo['id']}\n"
            f"标题:        {todo['title']}\n"
            f"状态:        {emoji} {todo['status']}\n"
            f"优先级:      {todo.get('priority', 'medium')}\n"
            f"依赖:        {blocked_by_str}\n"
            f"创建时间:    {todo.get('created_at', '')}\n"
            f"更新时间:    {todo.get('updated_at', '')}\n"
            f"---\n"
            f"{desc_block}"
        )

    # List mode
    status_emoji = {"pending": "⚪", "blocked": "🔴", "in_progress": "🟡", "completed": "🟢", "failed": "⚫", "cancelled": "⭕"}

    if status_filter:
        filtered = [t for t in todos if t.get("status") == status_filter]
        if not filtered:
            return f"📋 没有状态为「{status_filter}」的任务。"
        todos_to_show = filtered
    else:
        todos_to_show = todos

    lines = [f"📋 任务清单（共 {len(todos_to_show)} 项）\n"]
    for t in todos_to_show:
        emoji = status_emoji.get(t.get("status", "pending"), "⚪")
        blocked = ""
        if t.get("blocked_by"):
            blocked = f" ⚠ 依赖: {', '.join(t['blocked_by'])}"
        lines.append(f"{emoji} [{t['status']}]  {t['title']}（ID: {t['id']}）{blocked}")

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
            "description": "获取指定 URL 的页面内容并转换为纯文本。用于阅读文章、文档、新闻等具体页面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要获取的页面完整 URL（以 http:// 或 https:// 开头）"
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最多返回字符数（默认 5000）",
                        "default": 5000
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
            "description": "列出指定目录的内容。仅接受绝对路径。返回文件和子目录列表，每条显示类型标记（[DIR] 目录、[FILE] 文件、[LINK] 符号链接）。",
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
            "description": "在目录中按正则表达式或关键词全文搜索文件内容。支持按文件通配模式过滤（如 *.py、*.{ts,js}）。自动跳过隐藏目录和二进制文件。仅接受绝对路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "搜索的正则表达式或关键词（支持 Python re 语法）"
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
                        "description": "最多返回的匹配行数（1-200，默认 30）",
                        "default": 30
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
            "description": "按通配模式递归查找文件。仅接受绝对路径。自动跳过隐藏目录（以 . 开头）。返回匹配文件的绝对路径列表。",
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
            "description": "创建新文件或覆盖已有文件的内容。仅接受绝对路径。默认不覆盖已有文件（需设置 force=True 覆盖）。",
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
            "description": "对已读取过的文件执行精确字符串替换。用 old_string 精确匹配原文（包括空格和缩进），替换为 new_string。"
                           "old_string 在同一文件中必须唯一（除非设置 replace_all=True）。"
                           "修改前会校验文件自读取后是否被外部修改——若已过期则拒绝并提示重新读取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要编辑的文件的绝对路径"
                    },
                    "old_string": {
                        "type": "string",
                        "description": "要被替换的原文。必须精确匹配文件中的内容，包括空格和缩进。"
                    },
                    "new_string": {
                        "type": "string",
                        "description": "替换后的新内容"
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "是否替换所有匹配项。默认 false（只替换第一个匹配）。设为 true 替换所有。",
                        "default": False
                    }
                },
                "required": ["path", "old_string", "new_string"]
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
            "description": "列出任务摘要或查询单个任务详情。不指定 id 时返回所有任务摘要（含状态、优先级、依赖）。指定 id 时返回该任务的完整信息（含描述）。可选 status_filter 仅列出特定状态的任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "可选。指定后返回该任务的完整详情（含 description）。省略则返回所有任务摘要。"
                    },
                    "status_filter": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "failed", "cancelled", "blocked"],
                        "description": "可选。仅列出指定状态的任务。只在列表模式（id 为空时）有效。"
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
]

TOOL_CHOICE_AUTO = "auto"


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


def execute_list_directory(path: str, include_hidden: bool = False) -> str:
    """List contents of a directory. Accepts absolute paths only."""
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：目录不存在或路径无效「{path}」"
    if not os.path.isdir(resolved):
        return f"错误：路径不是目录「{resolved}」"
    try:
        entries = sorted(os.listdir(resolved))
    except PermissionError:
        return f"错误：无权访问目录「{resolved}」"
    except OSError as e:
        return f"错误：访问目录时出错「{resolved}」: {e}"

    filtered = []
    for name in entries:
        if not include_hidden and name.startswith("."):
            continue
        full = os.path.join(resolved, name)
        try:
            if os.path.islink(full):
                marker = "[LINK]"
            elif os.path.isdir(full):
                marker = "[DIR]"
            else:
                marker = "[FILE]"
        except OSError:
            marker = "[?]"
        marker = marker.ljust(6)
        filtered.append(f"{marker}  {name}")

    if not filtered:
        return f"目录「{resolved}」为空。"

    total = len(filtered)
    if total > _MAX_DIRECTORY_LISTING:
        filtered = filtered[:_MAX_DIRECTORY_LISTING]
        filtered.append(f"\n...（已截断，仅显示前 {_MAX_DIRECTORY_LISTING} 项，共 {total} 项）")

    result = f"📁 目录列表: {resolved}\n\n" + "\n".join(filtered)
    return result


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


def execute_edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Replace old_string with new_string in file. Requires prior full read_file call."""
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：文件不存在或路径无效「{path}」"
    if not os.path.isfile(resolved):
        return f"错误：路径不是文件「{resolved}」"

    stale_err = _check_file_stale(resolved)
    if stale_err is not None:
        return stale_err

    if not old_string:
        return "错误：old_string 不能为空。"

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

    if old_string not in content:
        return (
            f"错误：未能在文件「{resolved}」中找到指定的 old_string。\n\n"
            f"请确保 old_string 与文件内容完全匹配（包括空格和缩进）。\n"
            f"如需查看文件当前内容，请使用 read_file 读取。"
        )

    occurrence_count = content.count(old_string)
    if occurrence_count > 1 and not replace_all:
        return (
            f"错误：old_string 在文件中出现了 {occurrence_count} 次。"
            f"请设置 replace_all=True 以替换所有匹配，或提供更精确的 old_string 以唯一匹配。"
        )

    new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    actual_changes = occurrence_count if replace_all else 1

    try:
        with open(resolved, "w", encoding="utf-8", newline=("\r\n" if line_ending == "CRLF" else "\n")) as f:
            f.write(new_content)
    except PermissionError:
        return f"错误：无权写入文件「{resolved}」"
    except OSError as e:
        return f"错误：写入文件时出错「{resolved}」: {e}"

    _READ_FILE_STATE[os.path.realpath(resolved)] = {
        "content": new_content,
        "mtime": os.path.getmtime(resolved),
        "full_read": True,
        "encoding": "utf-8",
        "line_ending": line_ending,
    }

    return f"已成功对文件「{path}」应用 {actual_changes} 处编辑。"


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
                shutil.rmtree(resolved)
                return f"已成功递归删除目录「{path}」"
            else:
                os.rmdir(resolved)
                return f"已成功删除空目录「{path}」"
        else:
            os.remove(resolved)
            _READ_FILE_STATE.pop(os.path.realpath(resolved), None)
            return f"已成功删除文件「{path}」"
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

    resolved_dst = _resolve_write_path(destination, force=force)
    if resolved_dst is None:
        if os.path.exists(os.path.realpath(destination)) and not force:
            return f"错误：目标路径已存在。如需覆盖请设置 force=True。"
        return f"错误：目标路径无效「{destination}」"

    try:
        shutil.move(resolved_src, resolved_dst)
        # Invalidate any cached read state for the old path
        _READ_FILE_STATE.pop(os.path.realpath(resolved_src), None)
        return f"已成功将「{source}」重命名为「{destination}」"
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
        f"星期：{weekday}\n"
        f"时区：{tz_name} ({offset_str})\n"
        f"时间戳：{ts}"
    )


# ── Grep Search ─────────────────────────────────────────────────────────────

_MAX_GREP_RESULTS = 200


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


def execute_grep_search(pattern: str, path: str, include: str = "",
                        max_results: int = 30) -> str:
    """Search file contents by regex/keyword in a directory tree.

    Args:
        pattern: Regex or keyword to search for (Python re syntax).
        path: Absolute path of the root directory to search.
        include: Glob pattern to filter files (e.g. "*.py", "*.{ts,js}").
                 Empty string means all text files.
        max_results: Maximum number of matching lines to return (1-200).

    Returns:
        Formatted string with matches per file.
    """
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：目录不存在或路径无效「{path}」"
    if not os.path.isdir(resolved):
        return f"错误：路径不是目录「{resolved}」"

    max_results = max(1, min(_MAX_GREP_RESULTS, max_results))
    max_lines_per_file = _MAX_LINES_PER_FILE

    try:
        compiled = re.compile(pattern)
    except re.error as e:
        return f"错误：无效的正则表达式「{pattern}」: {e}"

    matches: List[str] = []
    total_matches = 0
    file_count = 0

    ignore_dirs = _get_ignore_dirs()
    for root, dirs, files in os.walk(resolved, topdown=True):
        # Skip hidden directories and build/dependency directories
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
                    continue  # skip binary
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if len(file_matches) >= max_lines_per_file:
                            file_matches.append(f"    ...（该文件匹配超过 {max_lines_per_file} 行，已截断）")
                            break
                        if compiled.search(line):
                            stripped = line.rstrip("\n\r")
                            if len(stripped) > 500:
                                stripped = stripped[:500] + "..."
                            file_matches.append(f"    L{lineno}: {stripped}")
            except (PermissionError, OSError):
                continue

            if file_matches:
                relpath = os.path.relpath(fpath, resolved)
                matches.append(f"📄 {relpath}")
                matches.extend(file_matches)
                total_matches += sum(1 for m in file_matches if not m.startswith("    ..."))
                file_count += 1

    if not matches:
        return f"在目录「{resolved}」中没有找到匹配「{pattern}」的内容。"

    result = f"🔍 搜索「{pattern}」在 {resolved}\n共 {file_count} 个文件，{total_matches} 行匹配\n\n" + "\n".join(matches)

    if total_matches >= max_results:
        result += f"\n\n...（已达到最大显示行数 {max_results}，可能还有更多匹配）"

    return result


# ── Glob Find ───────────────────────────────────────────────────────────────

_MAX_GLOB_RESULTS = 500


def execute_glob_find(pattern: str, path: str, max_results: int = 100) -> str:
    """Recursively find files matching a glob pattern, skipping blacklisted directories.

    Args:
        pattern: Glob pattern such as "**/*.py", "config*.json".
        path: Absolute path of the root directory to search.
        max_results: Maximum number of files to return (1-500).

    Returns:
        Formatted string with matched file paths.
    """
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：目录不存在或路径无效「{path}」"
    if not os.path.isdir(resolved):
        return f"错误：路径不是目录「{resolved}」"

    max_results = max(1, min(_MAX_GLOB_RESULTS, max_results))

    filtered: List[str] = []

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
            # Skip hidden directories and build/dependency directories
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ignore_dirs]
            
            for fname in files:
                if fname.startswith("."):
                    continue
                fpath = os.path.join(root_dir, fname)
                relpath = os.path.relpath(fpath, resolved)
                if _is_match(relpath, fname, pattern):
                    filtered.append(os.path.abspath(fpath))
    except (PermissionError, OSError) as e:
        return f"错误：搜索文件时出错「{resolved}」: {e}"

    # Sort results for deterministic output
    filtered.sort()

    if not filtered:
        return f"在目录「{resolved}」中没有找到匹配「{pattern}」的文件。"

    total = len(filtered)
    if total > max_results:
        filtered = filtered[:max_results]

    result = f"📂 搜索模式「{pattern}」在 {resolved}\n共 {total} 个匹配" + (f"（显示前 {max_results} 个）" if total > max_results else "") + "\n\n"
    result += "\n".join(filtered)

    if total > max_results:
        result += f"\n\n...（已截断，仅显示前 {max_results} 个，共 {total} 个）"

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
    except Exception:
        owner = str(st.st_uid)
        group = str(st.st_gid)

    if os.path.isdir(resolved) and not is_symlink:
        size_str = "—（目录）"
    else:
        size_str = _format_file_size(st.st_size)

    mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    atime = datetime.datetime.fromtimestamp(st.st_atime).strftime("%Y-%m-%d %H:%M:%S")
    ctime = datetime.datetime.fromtimestamp(st.st_ctime).strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"📋 文件信息: {resolved}",
        f"  类型: {file_type}",
        f"  大小: {size_str}",
        f"  权限: {perm_octal} ({perm_str})",
        f"  所有者: {owner}:{group}",
        f"  修改时间 (mtime): {mtime}",
        f"  访问时间 (atime): {atime}",
        f"  创建/状态变更 (ctime): {ctime}",
        f"  Inode: {st.st_ino}",
        f"  硬链接数: {st.st_nlink}",
    ]

    return "\n".join(lines)


def execute_ask_user_question(question: str) -> str:
    """Ask the user a question and return their response.

    Note: This is a placeholder executor. The actual blocking user interaction
    is handled by clipboard_panel.py which intercepts this tool before calling
    execute_tool_call(). This function exists for registration completeness.

    Args:
        question: The question to ask the user.

    Returns:
        A placeholder string indicating the question was asked.
    """
    return f"请回答: {question}"


# ── Write File ──────────────────────────────────────────────────────────────

_MAX_WRITE_CHARS = 100000


def execute_write_file(path: str, content: str, force: bool = False) -> str:
    """Create a new file or overwrite an existing file's content.

    Only accepts absolute paths. By default, will NOT overwrite an existing
    file — set force=True to allow overwriting.

    Args:
        path: Absolute path of the file to write.
        content: Text content to write to the file.
        force: If True, overwrite existing file without warning.
               Defaults to False (safer).

    Returns:
        Formatted string with result summary.
    """
    resolved = _resolve_write_path(path, force)
    if resolved is None:
        if not os.path.isabs(path):
            return "错误：必须使用绝对路径！"
        parent_dir = os.path.dirname(os.path.realpath(path))
        if not os.path.isdir(parent_dir):
            return f"错误：父目录不存在「{os.path.dirname(path)}」"
        if os.path.exists(os.path.realpath(path)):
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
    return (
        f"✅ 文件已写入: {resolved}\n"
        f"  大小: {size_str}\n"
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
    """Execute a web search. Uses Obscura (headless browser) as primary path,
    falls back to direct DuckDuckGo HTTP endpoint."""
    max_results = max(1, min(10, max_results))
    result = _execute_obscura_search(query, max_results)
    if result is not None:
        return result
    return _execute_duckduckgo_search(query, max_results)


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


def _try_requests_fetch(url: str, max_chars: int) -> Optional[str]:
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
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        return None

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



def _try_obscura_fetch(url: str, max_chars: int) -> str:
    """Fetch a page via Obscura headless browser with --dump markdown."""
    if not os.path.isfile(_OBSCURA_BIN):
        return f"获取页面失败：Obscura 不可用"
    try:
        result = subprocess.run(
            [_OBSCURA_BIN, "fetch", url, "--dump", "markdown", "--quiet", "--timeout", "20"],
            capture_output=True, text=True, timeout=30,
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


def execute_web_fetch(url: str, max_chars: int = 5000) -> str:
    """Fetch a page's content as plain text.

    Tries plain HTTP requests (fast, zero overhead) first. If the result
    is suspicious — too short (JS-rendered shell), contains JS-required /
    captcha patterns, or garbled non-ASCII — falls back to Obscura headless
    browser (--dump markdown) which executes JavaScript and renders the
    real page content.
    """
    max_chars = max(500, min(20000, max_chars))

    result = _try_requests_fetch(url, max_chars)
    if result is not None:
        return result

    return _try_obscura_fetch(url, max_chars)


# ── Bash Tool ───────────────────────────────────────────────────────────────

_MAX_BASH_OUTPUT_CHARS = 5000
_BASH_TIMEOUT_DEFAULT = 60
_BASH_SHELL = "/bin/bash"
_BASH_DEFAULT_CWD = os.path.dirname(os.path.abspath(__file__))
_bash_cwd = _BASH_DEFAULT_CWD


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

    def execute(self, command: str, timeout: int = _BASH_TIMEOUT_DEFAULT) -> dict:
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
            if len(output) > _MAX_BASH_OUTPUT_CHARS:
                output = output[:_MAX_BASH_OUTPUT_CHARS] + "\n...（输出已截断）"
            return {
                "output": f"命令执行超时（{timeout}秒），session 已强制终止。请设置 restart=True 重启。\n最后输出：{output[:500]}",
                "exit_code": -1,
                "timed_out": True,
            }

        output = output_buf.decode("utf-8", errors="replace").strip()
        if len(output) > _MAX_BASH_OUTPUT_CHARS:
            output = output[:_MAX_BASH_OUTPUT_CHARS] + f"\n...（输出已截断，共 {len(output)} 字符）"

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


def execute_bash(command: str, restart: bool = False, timeout: int = _BASH_TIMEOUT_DEFAULT) -> str:
    """Execute a shell command in a persistent bash session.

    Args:
        command: Shell command to execute.
        restart: If True, restart the session before executing.
        timeout: Command timeout in seconds (1-120).

    Returns:
        Formatted result string with output, exit code, and status.
    """
    global _bash_session

    if restart:
        if _bash_session is not None:
            _bash_session.stop()
        _bash_session = _BashSession()
        _bash_session.start()
        if not command or not command.strip():
            return "🔄 Bash session 已重启。"

    if not command or not command.strip():
        return "错误：命令不能为空。"

    timeout = max(1, min(120, timeout))

    if _bash_session is None:
        _bash_session = _BashSession()
        _bash_session.start()

    try:
        result = _bash_session.execute(command, timeout=timeout)
    except RuntimeError as e:
        return f"错误：{e}"

    output = result.get("output", "")
    exit_code = result.get("exit_code", -1)
    timed_out = result.get("timed_out", False)

    status_icon = "✅" if exit_code == 0 else "❌" if exit_code != -1 else "⚠️"
    status_text = f"{status_icon} 命令执行完成（退出码：{exit_code}）"

    parts = [status_text]
    if output:
        parts.append("")
        parts.append(output)

    return "\n".join(parts)


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
}


def execute_tool_call(tool_call: dict) -> str:
    """Execute a single tool call and return the result as a string.

    Args:
        tool_call: OpenAI-format tool_call dict with "id", "type", "function" keys.
                   tool_call["function"] has "name" and "arguments" (JSON string).

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
