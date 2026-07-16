"""MCP Tool ↔ OpenAI-compatible tool schema 转换适配器。

MCP Server 返回的 Tool 对象需要转换为 LLM API
所需的 OpenAI function-calling schema 格式。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from mcp import Tool
from mcp.types import CallToolResult


def mcp_tool_to_openai_schema(server_name: str, tool: Tool) -> Dict[str, Any]:
    """将 MCP Tool 转换为 OpenAI-compatible function schema。

    工具名称添加 ``{server_name}:`` 前缀以避免同名冲突。
    自动移除 JSON Schema 元字段（如 ``$schema``），这些字段
    不被 OpenAI-compatible API 接受会导致 400 错误。

    Parameters
    ----------
    server_name : str
        MCP Server 的唯一名称，用作命名空间前缀。
    tool : Tool
        MCP SDK 的 Tool 对象。

    Returns
    -------
    dict
        OpenAI function-calling schema，可混入 TOOL_DEFINITIONS 列表。
    """
    params = tool.inputSchema or {"type": "object", "properties": {}}
    # 移除 OpenAI 不支持的 JSON Schema 元字段
    if isinstance(params, dict):
        params = {
            k: v for k, v in params.items()
            if k not in ("$schema", "$id", "$ref", "definitions", "title")
        }
    return {
        "type": "function",
        "function": {
            "name": f"{server_name}__{tool.name}",
            "description": tool.description or "",
            "parameters": params,
        },
    }


def parse_mcp_tool_name(full_name: str) -> Tuple[str, str]:
    """解析带命名空间的工具名称为 (server_name, tool_name)。

    若名称不含 ``__``，则视为内置工具，server_name 返回 ``"builtin"``。

    Parameters
    ----------
    full_name : str
        如 ``"filesystem__read_file"`` 或 ``"todo_create"``。

    Returns
    -------
    tuple
        (server_name, tool_name)
    """
    if "__" in full_name:
        parts = full_name.split("__", 1)
        return parts[0], parts[1]
    return "builtin", full_name


def tool_result_to_text(result: CallToolResult) -> str:
    """将 MCP CallToolResult 转换为纯文本字符串。

    MCP 工具结果可能包含多种内容类型（text、image 等），
    此函数提取所有文本内容并拼接。

    Parameters
    ----------
    result : CallToolResult
        MCP SDK 的工具调用结果。

    Returns
    -------
    str
        可用于 LLM 上下文的文本表示。
    """
    texts: List[str] = []
    for item in result.content:
        if hasattr(item, "text") and item.text:
            texts.append(item.text)
        elif hasattr(item, "type") and item.type == "text":
            texts.append(str(item))
        else:
            texts.append(str(item))
    text = "\n".join(texts)
    if result.isError:
        text = f"❌ {text}"
    return text


def merge_mcp_tools_into_definitions(
    server_name: str,
    mcp_tools: List[Tool],
    existing_defs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """将 MCP Server 的工具合并到现有工具定义列表中。

    同名工具以后添加的 MCP 工具为准（后覆盖前）。

    Parameters
    ----------
    server_name : str
        MCP Server 名称。
    mcp_tools : list of Tool
        MCP SDK 返回的工具列表。
    existing_defs : list of dict
        当前的 TOOL_DEFINITIONS 列表。

    Returns
    -------
    list of dict
        合并后的工具定义列表。
    """
    result = list(existing_defs)
    # 移除该 Server 之前注册的工具（如有更新）
    result = [
        d
        for d in result
        if not d.get("function", {}).get("name", "").startswith(f"{server_name}:")
    ]
    # 添加新工具
    for tool in mcp_tools:
        result.append(mcp_tool_to_openai_schema(server_name, tool))
    return result
