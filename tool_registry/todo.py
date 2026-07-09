"""TODO management tools — persistent task management with dependencies and priority."""

import datetime
import json
import os
import uuid
from typing import Any, Dict, Final, List, Optional


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
        if _check_cycle(todos, dep_id, dep.get("blocked_by", []), seen.copy()):
            return True
    return False


def _update_dependents(todos: List[Dict[str, Any]], completed_id: str) -> None:
    """When a task completes, check if any tasks blocked by it become unblocked."""
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
        new_blocked_by = todo.get("blocked_by", []) + add_blocked_by
        if _check_cycle(todos, id, new_blocked_by):
            return "错误：检测到循环依赖，请检查 add_blocked_by 设置。"
        todo["blocked_by"] = new_blocked_by
        if todo["status"] in ("pending", "in_progress"):
            todo["status"] = "blocked"

    if status is not None:
        valid_statuses = ("pending", "in_progress", "completed", "failed", "cancelled")
        if status not in valid_statuses:
            return f"错误：无效的状态「{status}」。有效值：{', '.join(valid_statuses)}"
        todo["status"] = status

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
        todos_to_show = sorted(todos_to_show, key=lambda t: (
            priority_order.get(t.get("priority", "medium"), 1),
            t.get("created_at", "")
        ))
    elif sort_by == "updated_at":
        todos_to_show = sorted(todos_to_show, key=lambda t: t.get(sort_by, ""), reverse=True)
    else:
        todos_to_show = sorted(todos_to_show, key=lambda t: t.get(sort_by, ""))

    total = len(todos)
    completed = sum(1 for t in todos if t.get("status") == "completed")
    in_progress = sum(1 for t in todos if t.get("status") == "in_progress")
    pending = sum(1 for t in todos if t.get("status") == "pending")
    blocked = sum(1 for t in todos if t.get("status") == "blocked")

    priority_label = {"high": "[high]", "medium": "[med]", "low": "[low]"}

    lines = [
        f"📋 任务清单（共 {len(todos_to_show)} 项）",
        f"   统计: {completed}✅ {in_progress}🔄 {pending}⏳ {blocked}🔴 / {total}总计",
        ""
    ]
    for t in todos_to_show:
        emoji = _status_emoji(t.get("status", "pending"))
        prio = t.get("priority", "medium")
        prio_tag = priority_label.get(prio, "")
        blocked_str = ""
        if t.get("blocked_by"):
            blocked_str = f" ⚠ 依赖: {', '.join(t['blocked_by'])}"
        lines.append(f"{emoji} [{t['status']}]  {prio_tag} {t['title']}（ID: {t['id']}）{blocked_str}")

    return "\n".join(lines)


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "todo_create",
            "description": "创建新任务。支持设置优先级、依赖任务（blocked_by）、包含当前动作和验证标准。依赖任务全部完成时，本任务自动解除阻塞。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "任务标题"},
                    "description": {"type": "string", "description": "任务详细描述（可选）", "default": ""},
                    "priority": {"type": "string", "description": "优先级", "enum": ["high", "medium", "low"], "default": "medium"},
                    "blocked_by": {"type": "array", "items": {"type": "string"}, "description": "依赖的任务 ID 列表，这些任务完成后本任务才能开始（可选）"},
                    "active_form": {"type": "string", "description": "当前正在执行的行动/步骤（可选）", "default": ""},
                    "verification": {"type": "string", "description": "验证此任务是否完成的标准（可选）", "default": ""}
                },
                "required": ["title"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "todo_update",
            "description": "更新已有任务。可修改状态、标题、描述、优先级、依赖关系和当前动作。状态变更会自动处理依赖链。",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "任务 ID"},
                    "status": {"type": "string", "description": "新状态", "enum": ["pending", "in_progress", "completed", "failed", "cancelled"]},
                    "title": {"type": "string", "description": "新标题（可选）"},
                    "description": {"type": "string", "description": "新描述（可选）"},
                    "priority": {"type": "string", "description": "优先级", "enum": ["high", "medium", "low"]},
                    "add_blocked_by": {"type": "array", "items": {"type": "string"}, "description": "添加依赖的任务 ID 列表（可选）"},
                    "active_form": {"type": "string", "description": "当前正在执行的行动/步骤（可选）"}
                },
                "required": ["id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "todo_list",
            "description": "查询任务清单。可按状态筛选、按创建时间/更新时间/优先级排序。不传参数时返回所有任务。支持查询单个任务详情。",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "查询单个任务的详情（可选）"},
                    "status_filter": {"type": "string", "description": "按状态筛选（pending/in_progress/completed/failed/cancelled）"},
                    "sort_by": {"type": "string", "description": "排序方式", "enum": ["created_at", "updated_at", "priority"], "default": "created_at"}
                }
            }
        }
    },
]
