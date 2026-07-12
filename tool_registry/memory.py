"""Agent memory tools — save, list, and recall semantic memories."""

from typing import Optional
from clipboard_store import MemStore

_MEM_STORE: Optional[MemStore] = None


def _get_mem_store() -> MemStore:
    global _MEM_STORE
    if _MEM_STORE is None:
        _MEM_STORE = MemStore()
    return _MEM_STORE


def execute_memory_save(key: str, value: str) -> str:
    """保存一条记忆到长期存储。"""
    store = _get_mem_store()
    store.put(key, value)
    store.save()
    return f"✅ 已保存记忆「{key}」"


def execute_memory_list(bm25_filter: str = "") -> str:
    """列出已保存记忆的键名。可选的 bm25_filter 参数用自然语言描述要查的内容，工具会先做语义过滤只返回相关键名，帮助减少无关干扰。"""
    store = _get_mem_store()
    if bm25_filter:
        items = store.search(bm25_filter, limit=20)
    else:
        items = store.list_recent(100)
    if not items:
        return "暂无已保存的记忆。"
    lines = ["已保存的记忆键值列表："]
    for item in items:
        lines.append(f"  - {item.key}")
    return "\n".join(lines)


def execute_memory_recall(keys: str) -> str:
    """根据键名查询记忆内容。keys 支持单个键名或多个逗号分隔。"""
    store = _get_mem_store()
    key_list = [k.strip() for k in keys.split(",") if k.strip()]
    results = []
    for key in key_list:
        item = store.get(key)
        if item:
            results.append(f"「{key}」: {item.value}")
        else:
            results.append(f"「{key}」: 未找到")
    return "\n".join(results)


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "保存一条记忆到长期存储，未来所有对话中均可查询。请使用语义化的英文键名，例如 user_name、deploy_workflow、coding_style。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "记忆键名，简短描述性名称，如 user_name、deploy_workflow"
                    },
                    "value": {
                        "type": "string",
                        "description": "记忆内容"
                    }
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_list",
            "description": "列出已保存记忆的键名。如果用户问的是具体问题（如'怎么pdf转txt'），传入 bm25_filter 做语义预过滤；如果用户问的是'你记得关于我的什么'，不传 bm25_filter 返回全部键名。",
            "parameters": {
                "type": "object",
                "properties": {
                    "bm25_filter": {
                        "type": "string",
                        "description": "可选。用户的原始问题或关键词，工具用 BM25 做语义匹配，只返回相关的键名。例如用户问'怎么转pdf'传'pdf转txt'，用户问'我叫什么'传'名字'。不传则返回全部。",
                        "default": ""
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_recall",
            "description": "根据键名查询具体记忆内容。仅传入 memory_list 返回结果中你认为与用户问题相关的键名，传入多个键名时用逗号分隔。不要传入 memory_list 未返回的键名。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "string",
                        "description": "键名，单个如 user_name，或多个逗号分隔如 user_name,assistant_name"
                    }
                },
                "required": ["keys"]
            }
        }
    },
]
