"""Streamable HTTP 传输实现 — 通过 HTTP POST 与远程 MCP Server 通信。

实现 MCP 2025-11-25 Streamable HTTP 规范：
- 单一 POST 端点
- Accept: application/json, text/event-stream
- MCP-Protocol-Version header
- 可选 Mcp-Session-Id header（session-based）
- 支持 JSON 和 SSE 两种响应格式

2026-07-28 兼容预留：
- Mcp-Method / Mcp-Name headers（可选注入）
- 无 session 模式（session_id="" 时跳过）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional

from mcp_integration.transport import BaseTransport

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────────────────

_DEFAULT_TIMEOUT = 120
_SSE_TIMEOUT = 300  # SSE 流等待最终响应的超时
_MAX_RESPONSE_SIZE = 100 * 1024 * 1024  # 100MB 响应上限
_KEEPALIVE_INTERVAL = 30  # SSE keepalive 检测间隔（秒）

# SSE 事件行正则
_RE_SSE_DATA = re.compile(r"^data:\s?(.*)")
_RE_SSE_EVENT = re.compile(r"^event:\s?(.*)")


# ── 异常 ────────────────────────────────────────────────────────────


class HttpTransportError(Exception):
    """HTTP 传输层错误。"""


class HttpTransportAuthError(HttpTransportError):
    """认证失败（401/403）。"""


class HttpTransportStatusError(HttpTransportError):
    """非预期 HTTP 状态码。"""

    def __init__(self, status: int, body: str = "") -> None:
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


# ── SSE 解析 ─────────────────────────────────────────────────────────


@dataclass
class SseEvent:
    """单个 SSE 事件。"""
    event: str = "message"
    data: str = ""
    id: Optional[str] = None


def _parse_sse_chunk(chunk: str) -> List[SseEvent]:
    """解析 SSE 文本块，返回事件列表。

    支持标准 SSE 格式：
        event: message
        data: {...}

        data: {...}

    Also handles the `data: {...}\n\n` compact form.
    """
    events: List[SseEvent] = []
    current = SseEvent()
    for line in chunk.split("\n"):
        if line.startswith(":"):
            # 注释行，忽略
            continue
        if line == "":
            # 空行 = 事件分隔符
            if current.data or current.event != "message":
                events.append(current)
            current = SseEvent()
            continue
        m = _RE_SSE_EVENT.match(line)
        if m:
            current.event = m.group(1).strip()
            continue
        m = _RE_SSE_DATA.match(line)
        if m:
            data_val = m.group(1)
            if current.data:
                current.data += "\n" + data_val
            else:
                current.data = data_val
            continue
        # 未知行，忽略
    # 未终止的最后事件
    if current.data or current.event != "message":
        events.append(current)
    return events


# ── HTTP Transport ────────────────────────────────────────────────────


class HttpTransport(BaseTransport):
    """基于 Streamable HTTP 的 MCP 传输实现。

    通过 aiohttp 发送 JSON-RPC 消息到远程 MCP Server 并接收响应。

    Parameters
    ----------
    url : str
        MCP Server 的 HTTP 端点 URL（如 http://localhost:8123/mcp）。
    api_key : str, optional
        Bearer token 认证。
    protocol_version : str
        MCP 协议版本，默认 "2025-11-25"。
    session_id : str, optional
        已有的 session ID（2025-11-25 版本）。
    request_timeout : float
        请求超时秒数，默认 120。
    extra_headers : dict, optional
        额外 HTTP 头。
    force_2025_headers : bool
        始终发送 Mcp-Session-Id 等 2025-11-25 旧版 headers，即使未设置 session_id。
        默认 False（仅当 session_id 有值时发送）。
    enable_2026_headers : bool
        是否发送 2026-07-28 新增的 Mcp-Method / Mcp-Name headers，默认 False。
    """

    def __init__(
        self,
        url: str,
        api_key: str = "",
        protocol_version: str = "2025-11-25",
        session_id: str = "",
        request_timeout: float = _DEFAULT_TIMEOUT,
        extra_headers: Optional[Dict[str, str]] = None,
        force_2025_headers: bool = False,
        enable_2026_headers: bool = False,
    ) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._protocol_version = protocol_version
        self._session_id = session_id
        self._request_timeout = request_timeout
        self._extra_headers = extra_headers or {}
        self._force_2025_headers = force_2025_headers
        self._enable_2026_headers = enable_2026_headers

        # aiohttp
        self._session: Optional["aiohttp.ClientSession"] = None
        self._connector: Optional["aiohttp.TCPConnector"] = None

        # 响应缓冲：_reader() 从 queue 中读，send_line() 将响应压入
        self._response_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self._closed = False

    # ── 生命周期 ─────────────────────────────────────────────────

    async def connect(self) -> None:
        """建立 HTTP 连接池。"""
        try:
            import aiohttp
        except ImportError:
            raise HttpTransportError(
                "需要 aiohttp 库：pip install aiohttp"
            )

        self._connector = aiohttp.TCPConnector(
            limit=10,  # 连接池上限
            force_close=False,
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(
            total=self._request_timeout,
            sock_connect=30,  # 连接超时
            sock_read=self._request_timeout,
        )

        headers: Dict[str, str] = {
            "User-Agent": "OpenCodeSwitcher-MCP/1.0",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self._protocol_version,
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # 注意：Mcp-Session-Id 不由连接级别 headers 发送，
        # 而是在 send_line 中动态添加（因为 session ID 在 initialize 之后才获得）
        if self._force_2025_headers and self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        headers.update(self._extra_headers)

        self._session = aiohttp.ClientSession(
            connector=self._connector,
            headers=headers,
            timeout=timeout,
        )
        self._closed = False
        logger.info("HttpTransport 已连接: %s (proto=%s)", self._url, self._protocol_version)

    async def disconnect(self) -> None:
        """关闭连接池。"""
        self._closed = True
        if self._session:
            await self._session.close()
            self._session = None
        if self._connector:
            await self._connector.close()
            self._connector = None
        # 通知 reader 退出
        await self._response_queue.put(None)
        logger.info("HttpTransport 已断开: %s", self._url)

    @property
    def is_connected(self) -> bool:
        return (self._session is not None
                and not self._session.closed
                and not self._closed)

    # ── 核心收发 ─────────────────────────────────────────────────

    async def send_line(self, data: str) -> None:
        """发送 JSON-RPC 消息并等待响应。

        data 为 JSON 字符串（可能含尾随换行符，会被剥离）。
        """
        if not self._session or self._session.closed:
            raise HttpTransportError("连接未建立")

        # 剥离可能的尾随换行符
        payload = data.strip()
        if not payload:
            return

        # 判断是 request（有 id）还是 notification（无 id）
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            raise HttpTransportError(f"无效 JSON: {payload[:100]}")

        is_notification = "id" not in obj

        # ── 动态添加 Mcp-Session-Id（若已有 session ID） ──
        request_headers: Dict[str, str] = {}
        if self._session_id:
            request_headers["Mcp-Session-Id"] = self._session_id

        # ── 构建 2026-07-28 headers（可选） ──
        if self._enable_2026_headers:
            method = obj.get("method", "")
            request_headers["Mcp-Method"] = method
            request_headers["Mcp-Name"] = method.rpartition("/")[-1] or method

        try:
            async with self._session.post(
                self._url,
                data=payload.encode("utf-8"),
                headers=request_headers or None,
            ) as resp:
                status = resp.status

                # ── 认证错误 ──
                if status in (401, 403):
                    body = await resp.read()
                    raise HttpTransportAuthError(
                        f"HTTP {status}: {body.decode('utf-8', errors='replace')[:200]}"
                    )

                # ── 非预期错误 ──
                if status >= 400:
                    body = await resp.read()
                    raise HttpTransportStatusError(
                        status, body.decode("utf-8", errors="replace")
                    )

                # ── Notification（202 Accepted，无响应体） ──
                if status == 202 and is_notification:
                    # 无响应需要读取，直接返回
                    return

                # ── 读取响应 ──
                content_type = resp.headers.get("Content-Type", "").lower()

                # 从响应头提取 session ID（2025-11-25 协议）
                resp_session_id = resp.headers.get("Mcp-Session-Id", "")
                if resp_session_id and not self._session_id:
                    self._session_id = resp_session_id
                    logger.debug("HttpTransport 收到 session_id: %s", resp_session_id)

                if "text/event-stream" in content_type:
                    await self._handle_sse_response(resp)
                else:
                    await self._handle_json_response(resp)

        except (HttpTransportAuthError, HttpTransportStatusError):
            raise
        except asyncio.TimeoutError:
            raise HttpTransportError(f"请求超时 ({self._request_timeout}s)")
        except Exception as e:
            raise HttpTransportError(f"HTTP 请求失败: {e}")

    async def read_line(self) -> Optional[str]:
        """从响应缓冲读取一行。

        Returns
        -------
        str or None
            一行 JSON 字符串（无尾随换行符），连接关闭返回 None。
        """
        item = await self._response_queue.get()
        if item is None:
            return None
        # 从缓冲中取出时已经包含换行信息，但这里按行返回
        return item.rstrip("\n")

    # ── 响应处理 ─────────────────────────────────────────────────

    async def _handle_json_response(self, resp) -> None:
        """处理 Content-Type: application/json 响应。"""
        body = await resp.read()
        text = body.decode("utf-8", errors="replace")

        if not text.strip():
            logger.warning("HttpTransport: 收到空 JSON 响应")
            return

        # 验证是有效 JSON
        try:
            json.loads(text)
        except json.JSONDecodeError:
            logger.warning("HttpTransport: 非 JSON 响应: %s", text[:100])
            # 仍然放入队列，让调用方做最终判断
            pass

        await self._response_queue.put(text + "\n")

    async def _handle_sse_response(self, resp) -> None:
        """处理 Content-Type: text/event-stream 响应。

        读取 SSE 流，从中提取 JSON-RPC 消息（event: message + data: {...}），
        逐个放入响应队列。
        """
        buf = ""
        keepalive_count = 0
        try:
            async for chunk_bytes in resp.content:
                chunk = chunk_bytes.decode("utf-8", errors="replace")
                if not chunk:
                    continue
                buf += chunk

                # 检测 keepalive 注释行
                if ":\n" in chunk or ":\r\n" in chunk:
                    keepalive_count += 1

                # 尝试解析完整的 SSE 事件
                events = _parse_sse_chunk(buf)
                if events:
                    # 找到事件，消耗已解析的部分
                    last_event_end = self._find_last_event_end(buf, events)
                    if last_event_end > 0:
                        buf = buf[last_event_end:]

                    for event in events:
                        if not event.data.strip():
                            continue
                        # 验证 data 是有效 JSON
                        try:
                            json.loads(event.data)
                        except json.JSONDecodeError:
                            logger.debug(
                                "SSE data 非 JSON（已忽略）: %s",
                                event.data[:80],
                            )
                            continue
                        await self._response_queue.put(event.data + "\n")

        except asyncio.CancelledError:
            logger.debug("HttpTransport SSE reader 被取消")
        except Exception as e:
            logger.error("HttpTransport SSE 读取异常: %s", e)

    @staticmethod
    def _find_last_event_end(text: str, events: List[SseEvent]) -> int:
        """找到已解析事件的结束位置。"""
        # 简单方法：找到最后一个解析出的事件的 data 所在位置之后的第一个 \n\n
        last_data = events[-1].data if events else ""
        if not last_data:
            idx = text.rfind("\n\n")
            return idx + 2 if idx >= 0 else len(text)
        idx = text.rfind(last_data)
        if idx >= 0:
            after = idx + len(last_data)
            # 跳过尾部 \n\n
            while after < len(text) and text[after] in ("\n", "\r"):
                after += 1
            return after
        return len(text)

    # ── 辅助 ─────────────────────────────────────────────────────

    def update_session_id(self, session_id: str) -> None:
        """更新 session ID（从 initialize 响应中获取后调用）。"""
        self._session_id = session_id
        logger.debug("HttpTransport session_id 已更新: %s", session_id)

    @property
    def url(self) -> str:
        return self._url

    @property
    def protocol_version(self) -> str:
        return self._protocol_version

    def set_protocol_version(self, version: str) -> None:
        """更新协议版本（对应 2026-07-28 升级时调用）。"""
        self._protocol_version = version
        # 2026-07-28 及以上版本：无 session
        if self._protocol_version >= "2026-":
            self._session_id = ""
