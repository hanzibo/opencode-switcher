"""Agent memory tools — conversation search + semantic memory CRUD."""

import json
import os
import glob
from typing import Optional
from clipboard_store import CONVERSATIONS_DIR, ChatMessage
from clipboard_store import MemStore, ConversationStore

_MEM_STORE: Optional[MemStore] = None


def _get_mem_store() -> MemStore:
    global _MEM_STORE
    if _MEM_STORE is None:
        _MEM_STORE = MemStore()
    return _MEM_STORE


def execute_conversation_search(query: str, max_results: int = 5) -> str:
    """搜索历史对话记录。返回匹配的对话 ID、消息位置和简短片段。"""
    results = []
    pattern = os.path.join(CONVERSATIONS_DIR, "*.json")
    for path in sorted(glob.glob(pattern), reverse=True)[:50]:
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        conv_id = data.get("id", "")
        title = data.get("title", "")
        conv_summary = data.get("summary", "")
        messages = data.get("messages", [])

        # 摘要匹配
        if query.lower() in conv_summary.lower():
            snippet = conv_summary[:200]
            results.append(
                f"📄 对话 [{conv_id}] {title or '(无标题)'}\n"
                f"   匹配类型: 摘要\n"
                f"   片段: {snippet}"
            )
            if len(results) >= max_results:
                break
            continue

        # 消息内容匹配 — 返回所有匹配的消息记录
        for idx, m in enumerate(messages):
            content = m.get("content", "")
            if isinstance(content, list):
                content = str(content)
            if not isinstance(content, str):
                continue
            if query.lower() in content.lower():
                role = m.get("role", "?")
                snippet = content[:200]
                results.append(
                    f"📄 对话 [{conv_id}] {title or '(无标题)'}\n"
                    f"   匹配类型: 内容 | role: {role} | msg_idx: {idx}\n"
                    f"   片段: {snippet}"
                )
                if len(results) >= max_results:
                    break
        if len(results) >= max_results:
            break

    if not results:
        return "未找到匹配的历史对话。"
    return "\n\n---\n\n".join(results)


def execute_conversation_fetch(conv_id: str, msg_idx: str) -> str:
    """根据 conv_id 和 msg_idx 获取历史对话中某条（或多条）消息的完整内容。

    msg_idx 支持单个数字（如 "5"）或逗号分隔的多个数字（如 "2,3,6,8"），
    仅获取搜结果中匹配到的 msg_idx，禁止无差别批量拉取整个对话。
    """
    store = ConversationStore()
    conv = store.load_conversation(conv_id)
    if not conv:
        conv = _load_conversation_by_prefix(conv_id)
    if not conv:
        return f"❌ 未找到对话: {conv_id}"

    # 解析 msg_idx：支持 "5" 或 "2,3,6,8"
    indices = []
    for part in str(msg_idx).split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part)
            if 0 <= idx < len(conv.messages):
                indices.append(idx)

    if not indices:
        return f"❌ 无效的 msg_idx: {msg_idx}，对话共 {len(conv.messages)} 条消息"

    outputs = []
    for idx in indices:
        m = conv.messages[idx]
        content = m.content
        if isinstance(content, list):
            content = str(content)
        outputs.append(f"--- [{conv_id[:8]}] msg_idx={idx} role: {m.role} ---\n{content}")
    return "\n\n".join(outputs)


def _load_conversation_by_prefix(prefix: str) -> Optional['Conversation']:
    """按 conv_id 前缀匹配加载对话，用于处理 conv_search 返回的截断 ID。"""
    import glob as _glob
    pattern = os.path.join(CONVERSATIONS_DIR, f"{prefix}*.json")
    matches = _glob.glob(pattern)
    if len(matches) == 1:
        try:
            with open(matches[0]) as f:
                data = json.load(f)
            from clipboard_store import Conversation
            messages = [ChatMessage(**m) for m in data.get("messages", [])]
            return Conversation(
                id=data["id"],
                title=data.get("title", ""),
                system_prompt=data.get("system_prompt", ""),
                messages=messages,
                summary=data.get("summary", ""),
                model_config_snapshot=data.get("model_config_snapshot", {}),
                created_at=data.get("created_at", 0),
                updated_at=data.get("updated_at", 0),
            )
        except Exception:
            return None
    return None


def execute_memory_save(key: str, value: str, category: str = "general") -> str:
    """保存一条事实到长期语义记忆。"""
    store = _get_mem_store()
    store.put(key, value, category)
    store.save()
    return f"✅ 已保存记忆「{key}」({category})"


def execute_memory_recall(query: str, limit: int = 10) -> str:
    """按关键词查询语义记忆。"""
    store = _get_mem_store()
    items = store.search(query)
    if not items:
        return f"未找到与「{query}」相关的记忆。"
    lines = [f"找到 {len(items)} 条相关记忆："]
    for item in items[:limit]:
        lines.append(f"  [{item.category}] {item.key}: {item.value[:200]}")
    return "\n".join(lines)


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "conversation_search",
            "description": "搜索历史对话记录，返回匹配的 msg_idx。请记录每条结果中的 msg_idx，然后用 conversation_fetch 只获取这些 msg_idx 对应的消息。禁止范围遍历或批量获取整个对话。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回结果数",
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
            "name": "conversation_fetch",
            "description": "根据 conv_id 和 msg_idx 获取历史对话中消息的完整内容。msg_idx 仅填入 conversation_search 返回结果中匹配到的 msg_idx，禁止无差别获取整个对话范围内所有消息。支持一次取多条：'5'（单条）或 '2,3,6,8'（多条）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "conv_id": {
                        "type": "string",
                        "description": "对话 ID（conversation_search 返回结果中的 ID）"
                    },
                    "msg_idx": {
                        "type": "string",
                        "description": "消息下标，来自 conversation_search 返回的 msg_idx。传入单条如 '5'，或多条如 '2,3,6,8'。仅获取匹配到的 msg_idx，禁止范围遍历。"
                    }
                },
                "required": ["conv_id", "msg_idx"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "保存一条重要信息到长期记忆，例如用户的偏好、决策约定、项目规则等。这些信息会在未来所有对话中可用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "记忆键名，简短描述性名称，如「user_name」或「deployment_workflow」"
                    },
                    "value": {
                        "type": "string",
                        "description": "记忆内容"
                    },
                    "category": {
                        "type": "string",
                        "description": "分类：preference（偏好）、decision（决策）、fact（事实）、general（通用）",
                        "enum": ["preference", "decision", "fact", "general"],
                        "default": "general"
                    }
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_recall",
            "description": "查询长期记忆中保存的信息。当用户问及个人信息（名字、偏好、习惯等）或之前约定过的规则时，优先调用此工具查询。支持中英文关键词，例如查名字可搜 'name' 或 '名字'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "查询关键词，如 'name'、'deploy'、'database'、'名字'、'偏好'、'工作流'"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回条数",
                        "default": 10
                    }
                },
                "required": ["query"]
            }
        }
    },
]
