"""JSON-RPC 2.0 会话层 — 协议无关的请求/响应/通知管理。

JsonRpcSession 基于 BaseTransport 实现 JSON-RPC 2.0 协议的消息交换，
不依赖任何 MCP 概念，可被多协议复用（MCP / A2A / 自定义）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from mcp_integration.transport import BaseTransport

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  异常
# ═══════════════════════════════════════════════════════════════════


class JsonRpcError(Exception):
    """JSON-RPC 2.0 协议层错误。"""

    def __init__(self, message: str, code: int = 0, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class JsonRpcTimeoutError(JsonRpcError):
    """请求超时。"""

    def __init__(self, method: str, timeout: float) -> None:
        super().__init__(f"JSON-RPC 请求 '{method}' 超时 ({timeout}s)", code=-32000)
        self.method = method
        self.timeout = timeout


class JsonRpcDisconnectedError(JsonRpcError):
    """连接已断开。"""

    def __init__(self) -> None:
        super().__init__("JSON-RPC 连接已断开", code=-32001)


# ═══════════════════════════════════════════════════════════════════
#  消息数据模型
# ═══════════════════════════════════════════════════════════════════


@dataclass
class JsonRpcRequest:
    """JSON-RPC 2.0 请求。"""
    id: int
    method: str
    params: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self.id,
            "method": self.method,
        }
        if self.params is not None:
            d["params"] = self.params
        return d


@dataclass
class JsonRpcNotification:
    """JSON-RPC 2.0 通知（无 id，无需响应）。"""
    method: str
    params: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": self.method,
        }
        if self.params is not None:
            d["params"] = self.params
        return d


@dataclass
class JsonRpcResponse:
    """JSON-RPC 2.0 响应（成功或错误）。"""
    id: int
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None

    @property
    def is_success(self) -> bool:
        return self.error is None

    def raise_if_error(self) -> None:
        if self.error is not None:
            raise JsonRpcError(
                self.error.get("message", "未知错误"),
                code=self.error.get("code", 0),
                data=self.error.get("data"),
            )


# ═══════════════════════════════════════════════════════════════════
#  会话
# ═══════════════════════════════════════════════════════════════════


class JsonRpcSession:
    """JSON-RPC 2.0 会话。

    管理请求 ID 生成、挂起请求、超时控制、消息分发与服务端通知订阅。
    基于 BaseTransport 实现，与具体传输方式解耦。

    Parameters
    ----------
    transport : BaseTransport
        底层传输实现。
    request_timeout : float
        请求默认超时秒数（默认 120）。
    """

    def __init__(
        self,
        transport: BaseTransport,
        request_timeout: float = 120,
    ) -> None:
        self._transport = transport
        self._request_timeout = request_timeout
        self._request_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._notification_handlers: Dict[str, List[Callable[[Dict[str, Any]], Any]]] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._closed = False

    # ── 生命周期 ────────────────────────────────────────────────

    async def connect(self) -> None:
        """启动底层传输并开始读取后台任务。"""
        await self._transport.connect()
        self._reader_task = asyncio.create_task(self._reader())
        self._closed = False

    async def close(self) -> None:
        """关闭会话，取消所有挂起的请求。"""
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        await self._transport.disconnect()
        # 通知所有挂起请求连接已断开
        self._fail_all_pending(JsonRpcDisconnectedError())

    @property
    def is_connected(self) -> bool:
        return not self._closed and self._transport.is_connected

    # ── 通知监听 ────────────────────────────────────────────────

    def register_notification_handler(
        self,
        method: str,
        handler: Callable[[Dict[str, Any]], Any],
    ) -> None:
        """注册服务端通知处理器。

        Parameters
        ----------
        method : str
            通知方法名称（如 "notifications/tools/list_changed"）或 "*" 通配符。
        handler : callable
            接收通知 params (dict) 的回调函数，可为同步函数或异步协程函数。
        """
        handlers = self._notification_handlers.setdefault(method, [])
        if handler not in handlers:
            handlers.append(handler)

    def unregister_notification_handler(
        self,
        method: str,
        handler: Callable[[Dict[str, Any]], Any],
    ) -> None:
        """注销服务端通知处理器。"""
        handlers = self._notification_handlers.get(method)
        if handlers and handler in handlers:
            handlers.remove(handler)
            if not handlers:
                self._notification_handlers.pop(method, None)

    # ── 消息收发 ────────────────────────────────────────────────

    async def request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """发送 JSON-RPC 请求并等待响应。

        Parameters
        ----------
        method : str
            方法名。
        params : dict, optional
            参数。
        timeout : float, optional
            超时秒数，默认使用构造时的 request_timeout。

        Returns
        -------
        dict
            响应的 result 字段。

        Raises
        ------
        JsonRpcError
            协议层错误。
        JsonRpcTimeoutError
            请求超时。
        JsonRpcDisconnectedError
            连接已断开。
        asyncio.CancelledError
            协程被外部取消；对应的挂起请求已在重新抛出前从 _pending 清理。
        """
        if self._closed:
            raise JsonRpcDisconnectedError()

        self._request_id += 1
        req_id = self._request_id

        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut

        msg = JsonRpcRequest(req_id, method, params)

        # 在 try 前初始化：_send_line 自身也可能抛出 asyncio.TimeoutError，
        # 若在 try 内才赋值，except 处理程序将引用未绑定变量（UnboundLocalError）。
        timeout_val = timeout if timeout is not None else self._request_timeout

        try:
            await self._send_line(msg.to_dict())
            response = await asyncio.wait_for(fut, timeout=timeout_val)
            return response
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            asyncio.create_task(self._send_cancel_notification(req_id, "timeout"))
            raise JsonRpcTimeoutError(method, timeout_val)
        except asyncio.CancelledError:
            # 取消传播：外部取消（用户取消 / 会话关闭）到达此协程。
            # 主动取消内部 future（若在 wait_for 之前被取消，fut 仍挂起未取消；
            # 对已完成的 future 为 no-op），从挂起表移除对应请求，
            # 发送 notifications/cancelled 后重新抛出。
            fut.cancel()
            self._pending.pop(req_id, None)
            asyncio.create_task(self._send_cancel_notification(req_id, "cancelled"))
            raise

    async def notify(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> None:
        """发送 JSON-RPC 通知（无需响应）。"""
        if self._closed:
            raise JsonRpcDisconnectedError()
        msg = JsonRpcNotification(method, params)
        await self._send_line(msg.to_dict())

    # ── 内部 ────────────────────────────────────────────────────

    async def _send_line(self, obj: Dict[str, Any]) -> None:
        """序列化并发送一行 JSON。"""
        data = json.dumps(obj, ensure_ascii=False) + "\n"
        await self._transport.send_line(data)

    async def _send_cancel_notification(self, req_id: int, reason: str) -> None:
        """异步发送 MCP 标准取消通知 (notifications/cancelled)。"""
        try:
            if self.is_connected:
                msg = JsonRpcNotification("notifications/cancelled", {
                    "requestId": req_id,
                    "reason": reason,
                })
                await self._send_line(msg.to_dict())
        except Exception as e:
            logger.debug("发送 notifications/cancelled 异常 (req_id=%s): %s", req_id, e)

    async def _reader(self) -> None:
        """后台任务：持续读取并分发响应。"""
        try:
            while True:
                line = await self._transport.read_line()
                if line is None:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("JSON-RPC 非JSON输出（已忽略）: %s", line[:100])
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("JSON-RPC reader 异常: %s", e)
        finally:
            self._fail_all_pending(JsonRpcDisconnectedError())

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        """分发收到的 JSON-RPC 消息到对应的挂起请求或通知监听器。"""
        # 1. 服务端下发的 Notification（无 id 且有 method）
        if "id" not in msg and "method" in msg:
            self._dispatch_notification(msg["method"], msg.get("params"))
            return

        # 2. 处理错误响应
        if "error" in msg:
            err = msg["error"]
            error_obj = JsonRpcError(
                err.get("message", "未知错误"),
                code=err.get("code", 0),
                data=err.get("data"),
            )
            self._resolve_pending(msg.get("id"), exc=error_obj)
            return

        # 3. 处理成功响应
        self._resolve_pending(msg.get("id"), result=msg.get("result", {}))

    def _dispatch_notification(
        self,
        method: str,
        params: Optional[Dict[str, Any]],
    ) -> None:
        """分发收到的 JSON-RPC 通知给已注册的监听器。"""
        handlers = list(self._notification_handlers.get(method, []))
        if "*" in self._notification_handlers:
            handlers.extend(self._notification_handlers["*"])

        if not handlers:
            logger.debug("收到未监听的 JSON-RPC 通知: %s", method)
            return

        params_dict = params or {}
        for handler in handlers:
            try:
                res = handler(params_dict)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as e:
                logger.error("JSON-RPC 通知处理异常 (%s): %s", method, e)

    def _resolve_pending(
        self,
        msg_id: Any,
        result: Any = None,
        exc: Optional[Exception] = None,
    ) -> None:
        """完成一个挂起的请求。"""
        if msg_id is None:
            return
        # msg_id 可能是 int 或 str，统一转 int
        try:
            rid = int(msg_id)
        except (ValueError, TypeError):
            return
        fut = self._pending.pop(rid, None)
        if fut is None or fut.done():
            if fut is None:
                logger.warning("JSON-RPC 收到未知 msg_id=%s（可能已超时）", msg_id)
            return
        if exc:
            fut.set_exception(exc)
        else:
            fut.set_result(result)

    def _fail_all_pending(self, exc: Exception) -> None:
        """将所有挂起请求标记为失败。"""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
