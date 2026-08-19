"""MCP 协议会话层 — MCP 语义封装。

基于 JsonRpcSession 实现 MCP 协议的生命周期管理：
- 初始化握手（initialize + initialized 通知）
- 协议版本协商
- Capability 交换
- 工具发现与调用
- 资源读取（预留）
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from mcp_integration.json_rpc import JsonRpcSession

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════════════════════════

# 按从新到旧排列，协商时取双方支持的最新版本
SUPPORTED_MCP_VERSIONS = ["2026-07-28", "2025-11-25", "2025-03-26", "2024-11-05"]

DEFAULT_CLIENT_INFO = {
    "name": "opencode-switcher",
    "version": "1.0",
}

# 工具调用默认超时（有界默认，秒）：覆盖底层 JsonRpcSession.request_timeout，
# 防止服务器挂起时工具调用无限等待。
MCP_TOOL_CALL_TIMEOUT_SEC = 60.0

# 游标分页最大页数上限（防御恶意或故障服务端的无限循环）
_MAX_PAGINATION_PAGES = 100


# ═══════════════════════════════════════════════════════════════════
#  协议协商
# ═══════════════════════════════════════════════════════════════════


def negotiate_version(
    client_versions: List[str],
    server_version: str,
) -> str:
    """在客户端支持的版本列表中选取与服务器兼容的最新版本。

    策略：遍历客户端版本列表（从新到旧），返回第一个与 server_version 匹配的。
    若无匹配，降级至客户端最新版本（服务端可能后向兼容）。

    Parameters
    ----------
    client_versions : list of str
        客户端支持的版本列表（从新到旧排列）。
    server_version : str
        服务器返回的 protocolVersion。

    Returns
    -------
    str
        协商后的协议版本。
    """
    for v in client_versions:
        if v == server_version:
            return v
    # 无精确匹配时使用客户端最新版本
    return client_versions[0]


# ═══════════════════════════════════════════════════════════════════
#  会话
# ═══════════════════════════════════════════════════════════════════


@dataclass
class MCPServerInfo:
    """MCP Server 信息。"""
    name: str = "unknown"
    version: str = "?"
    capabilities: Dict[str, Any] = field(default_factory=dict)


class MCPSession:
    """MCP 协议会话。

    封装 MCP 协议的初始化、工具发现和工具调用语义。
    内部使用 JsonRpcSession 处理底层消息交换。

    Parameters
    ----------
    json_rpc : JsonRpcSession
        已连接或待连接的 JSON-RPC 会话。
    client_info : dict, optional
        客户端信息（name, version）。
    """

    def __init__(
        self,
        json_rpc: JsonRpcSession,
        client_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._jrpc = json_rpc
        self._client_info = client_info or dict(DEFAULT_CLIENT_INFO)
        self.server_info: MCPServerInfo = MCPServerInfo()
        self._negotiated_version: Optional[str] = None
        self._tools_cache: Optional[List[dict]] = None
        self._on_tools_changed_callbacks: List[Callable[[], Any]] = []
        self._background_tasks: set[asyncio.Task] = set()

        # 订阅服务端工具变更通知
        if hasattr(self._jrpc, "register_notification_handler"):
            self._jrpc.register_notification_handler(
                "notifications/tools/list_changed",
                self._on_tools_list_changed_notification,
            )

    # ── 生命周期 ────────────────────────────────────────────────

    async def connect(self) -> None:
        """建立底层传输连接。"""
        await self._jrpc.connect()

    async def initialize(self) -> str:
        """执行 MCP 初始化握手。

        步骤：
        1. 发送 initialize 请求
        2. 协商协议版本
        3. 发送 initialized 通知

        Returns
        -------
        str
            Server 描述信息，如 "filesystem v0.1.0"。
        """
        # 协议版本：优先与底层传输层配置的版本保持一致（如 HttpTransport 的 MCP-Protocol-Version 头），
        # 避免 HTTP 请求头与 JSON body 中的协议版本冲突导致服务端拒绝会话。
        transport_proto = getattr(getattr(self._jrpc, "_transport", None), "protocol_version", None)
        init_version = transport_proto if transport_proto in SUPPORTED_MCP_VERSIONS else "2025-11-25"

        result = await self._jrpc.request("initialize", {
            "protocolVersion": init_version,
            "capabilities": {},
            "clientInfo": self._client_info,
        })

        server_ver = result.get("protocolVersion", "")
        self._negotiated_version = negotiate_version(
            SUPPORTED_MCP_VERSIONS, server_ver,
        )

        svr_info = result.get("serverInfo", {})
        caps = result.get("capabilities", {})
        self.server_info = MCPServerInfo(
            name=svr_info.get("name", "unknown"),
            version=svr_info.get("version", "?"),
            capabilities=caps,
        )

        # initialized 通知（fire-and-forget）
        await self._jrpc.notify("notifications/initialized")

        logger.info(
            "MCP 已初始化: %s v%s (协议: %s, 协商: %s)",
            self.server_info.name,
            self.server_info.version,
            server_ver,
            self._negotiated_version,
        )
        return f"{self.server_info.name} v{self.server_info.version}"

    async def close(self) -> None:
        """关闭会话。"""
        if hasattr(self._jrpc, "unregister_notification_handler"):
            self._jrpc.unregister_notification_handler(
                "notifications/tools/list_changed",
                self._on_tools_list_changed_notification,
            )
        # 取消所有未完成的后台任务
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        self._background_tasks.clear()

        await self._jrpc.close()

    @property
    def is_connected(self) -> bool:
        return self._jrpc.is_connected

    # ── 工具变更通知订阅 ────────────────────────────────────────

    def add_tools_changed_callback(self, cb: Callable[[], Any]) -> None:
        """注册工具列表变更回调。"""
        if cb not in self._on_tools_changed_callbacks:
            self._on_tools_changed_callbacks.append(cb)

    def remove_tools_changed_callback(self, cb: Callable[[], Any]) -> None:
        """注销工具列表变更回调。"""
        if cb in self._on_tools_changed_callbacks:
            self._on_tools_changed_callbacks.remove(cb)

    def _on_tools_list_changed_notification(self, params: Dict[str, Any]) -> None:
        """处理服务端 notifications/tools/list_changed 通知。"""
        logger.info("收到 MCP Server (%s) 工具变更通知，清空工具缓存", self.server_info.name)
        self._tools_cache = None
        for cb in list(self._on_tools_changed_callbacks):
            try:
                res = cb()
                if asyncio.iscoroutine(res):
                    task = asyncio.create_task(res)
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
            except Exception as e:
                logger.warning("执行 tools_changed 回调异常: %s", e)

    # ── 工具发现 ────────────────────────────────────────────────

    async def list_tools(self) -> List[dict]:
        """获取工具列表（支持 cursor 分页遍历与死循环熔断保护）。

        Returns
        -------
        list of dict
            每个工具包含 name, description, inputSchema 等字段。
        """
        all_tools: List[dict] = []
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        page_count = 0

        while page_count < _MAX_PAGINATION_PAGES:
            page_count += 1
            params: Dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            result = await self._jrpc.request("tools/list", params or None)
            tools = result.get("tools", [])
            all_tools.extend(tools)
            cursor = result.get("nextCursor")
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)

        self._tools_cache = all_tools
        return all_tools

    # ── 工具调用 ────────────────────────────────────────────────

    async def call_tool(
        self,
        name: str,
        arguments: dict,
        timeout: Optional[float] = None,
    ) -> str:
        """调用工具并返回文本结果。

        Parameters
        ----------
        name : str
            工具名称。
        arguments : dict
            工具参数。
        timeout : float, optional
            超时秒数，默认使用 MCP_TOOL_CALL_TIMEOUT_SEC（60s）。
            传入 None 表示使用有界默认值，传入显式值则覆盖。

        Returns
        -------
        str
            工具结果的文本表示。
        """
        timeout_val = timeout if timeout is not None else MCP_TOOL_CALL_TIMEOUT_SEC
        result = await self._jrpc.request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=timeout_val,
        )
        content = result.get("content", [])
        is_error = result.get("isError", False)

        texts = []
        for item in content:
            if isinstance(item, dict):
                texts.append(item.get("text", json.dumps(item, ensure_ascii=False)))
            else:
                texts.append(str(item))
        text = "\n".join(texts)
        if is_error:
            text = f"❌ {text}"
        return text

    # ── 健康检测 ────────────────────────────────────────────────

    async def ping(self) -> bool:
        """发送 ping 健康检测（notifications/ping）。

        Returns
        -------
        bool
            True 表示 Server 正常响应，False 表示连接异常。
        """
        try:
            # 使用通知（无响应）或请求（有响应）方式
            await self._jrpc.request("ping", timeout=10)
            return True
        except Exception as e:
            logger.warning("MCP ping 失败: %s", e)
            return False

    async def refresh_tools_cache(self) -> List[dict]:
        """强制刷新工具缓存并返回最新列表。"""
        self._tools_cache = None
        return await self.list_tools()

    def get_tools_cache(self) -> Optional[List[dict]]:
        """获取缓存的工具列表（若存在）。"""
        return self._tools_cache

    # ── 资源与 Prompts ──────────────────────────────────────────

    async def list_resources(self) -> List[dict]:
        """获取资源列表（支持 cursor 分页遍历与死循环熔断保护）。

        Returns
        -------
        list of dict
            资源列表，若 Server 不支持则返回空列表。
        """
        all_resources: List[dict] = []
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        page_count = 0

        while page_count < _MAX_PAGINATION_PAGES:
            page_count += 1
            try:
                params: Dict[str, Any] = {}
                if cursor:
                    params["cursor"] = cursor
                result = await self._jrpc.request("resources/list", params or None)
                resources = result.get("resources", [])
                all_resources.extend(resources)
                cursor = result.get("nextCursor")
                if not cursor or cursor in seen_cursors:
                    break
                seen_cursors.add(cursor)
            except Exception as e:
                logger.debug("resources/list 不可用（Server 可能不支持）: %s", e)
                break
        return all_resources

    async def read_resource(self, uri: str) -> Optional[str]:
        """读取资源内容。

        Parameters
        ----------
        uri : str
            资源 URI。

        Returns
        -------
        str or None
            资源内容的文本表示。
        """
        try:
            result = await self._jrpc.request("resources/read", {"uri": uri})
            contents = result.get("contents", [])
            texts = []
            for item in contents:
                if isinstance(item, dict):
                    texts.append(item.get("text", ""))
                else:
                    texts.append(str(item))
            return "\n".join(texts)
        except Exception as e:
            logger.debug("resources/read 不可用: %s", e)
            return None

    async def list_prompts(self) -> List[dict]:
        """获取 Prompt 模板列表（支持 cursor 分页遍历与死循环熔断保护）。

        Returns
        -------
        list of dict
            Prompt 模板列表，若 Server 不支持则返回空列表。
        """
        all_prompts: List[dict] = []
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        page_count = 0

        while page_count < _MAX_PAGINATION_PAGES:
            page_count += 1
            try:
                params: Dict[str, Any] = {}
                if cursor:
                    params["cursor"] = cursor
                result = await self._jrpc.request("prompts/list", params or None)
                prompts = result.get("prompts", [])
                all_prompts.extend(prompts)
                cursor = result.get("nextCursor")
                if not cursor or cursor in seen_cursors:
                    break
                seen_cursors.add(cursor)
            except Exception as e:
                logger.debug("prompts/list 不可用（Server 可能不支持）: %s", e)
                break
        return all_prompts
