"""MCP 协议会话层 — MCP 语义封装。

基于 JsonRpcSession 实现 MCP 协议的生命周期管理：
- 初始化握手（initialize + initialized 通知）
- 协议版本协商
- Capability 交换
- 工具发现与调用
- 资源读取（预留）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from mcp_integration.json_rpc import JsonRpcSession

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════════════════════════

# 按从新到旧排列，协商时取双方支持的最新版本
SUPPORTED_MCP_VERSIONS = ["2025-11-25", "2025-03-26"]

DEFAULT_CLIENT_INFO = {
    "name": "opencode-switcher",
    "version": "1.0",
}


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
        result = await self._jrpc.request("initialize", {
            "protocolVersion": SUPPORTED_MCP_VERSIONS[0],
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
        await self._jrpc.close()

    @property
    def is_connected(self) -> bool:
        return self._jrpc.is_connected

    # ── 工具发现 ────────────────────────────────────────────────

    async def list_tools(self) -> List[dict]:
        """获取工具列表。

        Returns
        -------
        list of dict
            每个工具包含 name, description, inputSchema 等字段。
        """
        result = await self._jrpc.request("tools/list")
        tools = result.get("tools", [])
        self._tools_cache = tools
        return tools

    # ── 工具调用 ────────────────────────────────────────────────

    async def call_tool(self, name: str, arguments: dict) -> str:
        """调用工具并返回文本结果。

        Parameters
        ----------
        name : str
            工具名称。
        arguments : dict
            工具参数。

        Returns
        -------
        str
            工具结果的文本表示。
        """
        result = await self._jrpc.request("tools/call", {
            "name": name,
            "arguments": arguments,
        })
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

    # ── 预留：资源 ──────────────────────────────────────────────

    async def list_resources(self) -> List[dict]:
        """获取资源列表（预留）。

        Returns
        -------
        list of dict
            资源列表，若 Server 不支持则返回空列表。
        """
        try:
            result = await self._jrpc.request("resources/list")
            return result.get("resources", [])
        except Exception as e:
            logger.debug("resources/list 不可用（Server 可能不支持）: %s", e)
            return []

    async def read_resource(self, uri: str) -> Optional[str]:
        """读取资源内容（预留）。

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
