"""MCP 第一阶段（JSON-RPC 通知、取消传播、游标分页、动态工具热更）单元测试。"""

import asyncio
import json
import unittest
from typing import Any, Callable, Dict, List, Optional

from mcp_integration.json_rpc import JsonRpcSession, JsonRpcTimeoutError
from mcp_integration.mcp_session import MCPSession, SUPPORTED_MCP_VERSIONS
from mcp_integration.client_manager import MCPClientManager
from mcp_integration.transport import BaseTransport


class MockTransport(BaseTransport):
    """用于测试的 Mock 传输层。"""

    def __init__(self) -> None:
        self._connected = False
        self._sent: List[str] = []
        self._response_queue: asyncio.Queue[str] = asyncio.Queue()
        self.on_send: Optional[Callable[[Dict[str, Any]], None]] = None

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def send_line(self, data: str) -> None:
        self._sent.append(data)
        if self.on_send:
            try:
                msg = json.loads(data)
                self.on_send(msg)
            except json.JSONDecodeError:
                pass

    async def read_line(self) -> Optional[str]:
        try:
            return await self._response_queue.get()
        except asyncio.CancelledError:
            return None

    def push_incoming(self, msg: Dict[str, Any]) -> None:
        """模拟服务端下发消息（响应或通知）。"""
        self._response_queue.put_nowait(json.dumps(msg, ensure_ascii=False) + "\n")

    def get_sent_messages(self) -> List[Dict[str, Any]]:
        msgs = []
        for s in self._sent:
            try:
                msgs.append(json.loads(s))
            except json.JSONDecodeError:
                pass
        return msgs


class TestJsonRpcNotificationAndCancel(unittest.IsolatedAsyncioTestCase):
    async def test_notification_registration_and_dispatch(self):
        t = MockTransport()
        s = JsonRpcSession(t, request_timeout=2)
        await s.connect()

        received_params = []
        wildcard_params = []

        def handle_custom(params):
            received_params.append(params)

        def handle_wildcard(params):
            wildcard_params.append(params)

        s.register_notification_handler("custom/alert", handle_custom)
        s.register_notification_handler("*", handle_wildcard)

        # 模拟服务端下发通知
        t.push_incoming({
            "jsonrpc": "2.0",
            "method": "custom/alert",
            "params": {"level": "info", "msg": "hello"},
        })
        await asyncio.sleep(0.05)

        self.assertEqual(len(received_params), 1)
        self.assertEqual(received_params[0], {"level": "info", "msg": "hello"})
        self.assertEqual(len(wildcard_params), 1)

        # 注销特定监听器
        s.unregister_notification_handler("custom/alert", handle_custom)
        t.push_incoming({
            "jsonrpc": "2.0",
            "method": "custom/alert",
            "params": {"level": "warn"},
        })
        await asyncio.sleep(0.05)

        # handle_custom 没有再收到，但 wildcard 依然收到
        self.assertEqual(len(received_params), 1)
        self.assertEqual(len(wildcard_params), 2)

        await s.close()

    async def test_cancelled_notification_sent_on_timeout(self):
        t = MockTransport()
        s = JsonRpcSession(t, request_timeout=0.05)
        await s.connect()

        with self.assertRaises(JsonRpcTimeoutError):
            await s.request("tools/call", {"name": "slow_tool"})

        # 等待后台 cancel 通知发送
        await asyncio.sleep(0.05)
        sent = t.get_sent_messages()
        # 应包含初始请求和取消通知
        self.assertTrue(any(
            m.get("method") == "notifications/cancelled"
            and m.get("params", {}).get("reason") == "timeout"
            and m.get("params", {}).get("requestId") == 1
            for m in sent
        ))
        await s.close()

    async def test_cancelled_notification_sent_on_cancelled(self):
        t = MockTransport()
        s = JsonRpcSession(t, request_timeout=5)
        await s.connect()

        task = asyncio.create_task(s.request("tools/call", {"name": "long_task"}))
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        await asyncio.sleep(0.05)
        sent = t.get_sent_messages()
        self.assertTrue(any(
            m.get("method") == "notifications/cancelled"
            and m.get("params", {}).get("reason") == "cancelled"
            and m.get("params", {}).get("requestId") == 1
            for m in sent
        ))
        await s.close()


class TestMCPSessionPaginationAndDynamicTools(unittest.IsolatedAsyncioTestCase):
    async def test_supported_versions_includes_2026_and_2024(self):
        self.assertIn("2026-07-28", SUPPORTED_MCP_VERSIONS)
        self.assertIn("2024-11-05", SUPPORTED_MCP_VERSIONS)

    async def test_list_tools_cursor_pagination(self):
        t = MockTransport()
        jrpc = JsonRpcSession(t, request_timeout=2)
        session = MCPSession(jrpc)
        await session.connect()

        def handle_send(msg):
            req_id = msg.get("id")
            if msg.get("method") == "tools/list":
                cursor = (msg.get("params") or {}).get("cursor")
                if not cursor:
                    t.push_incoming({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "tools": [{"name": "tool_1"}],
                            "nextCursor": "page_2",
                        },
                    })
                elif cursor == "page_2":
                    t.push_incoming({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "tools": [{"name": "tool_2"}],
                        },
                    })

        t.on_send = handle_send

        tools = await session.list_tools()
        self.assertEqual(len(tools), 2)
        self.assertEqual([tool["name"] for tool in tools], ["tool_1", "tool_2"])
        self.assertEqual(session.get_tools_cache(), tools)

        await session.close()

    async def test_dynamic_tools_list_changed_notification(self):
        t = MockTransport()
        jrpc = JsonRpcSession(t, request_timeout=2)
        session = MCPSession(jrpc)
        await session.connect()

        session._tools_cache = [{"name": "old_tool"}]

        changed_called = []
        session.add_tools_changed_callback(lambda: changed_called.append(True))

        # 服务端发送 tools/list_changed 通知
        t.push_incoming({
            "jsonrpc": "2.0",
            "method": "notifications/tools/list_changed",
            "params": {},
        })
        await asyncio.sleep(0.05)

        self.assertIsNone(session.get_tools_cache())
        self.assertEqual(len(changed_called), 1)

        await session.close()

    async def test_list_prompts_and_resources_pagination(self):
        t = MockTransport()
        jrpc = JsonRpcSession(t, request_timeout=2)
        session = MCPSession(jrpc)
        await session.connect()

        def handle_send(msg):
            req_id = msg.get("id")
            method = msg.get("method")
            if method == "prompts/list":
                t.push_incoming({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"prompts": [{"name": "p1"}]},
                })
            elif method == "resources/list":
                t.push_incoming({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"resources": [{"uri": "file:///a.txt"}]},
                })

        t.on_send = handle_send

        prompts = await session.list_prompts()
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["name"], "p1")

        resources = await session.list_resources()
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0]["uri"], "file:///a.txt")

        await session.close()


class TestMCPClientManagerToolsChanged(unittest.IsolatedAsyncioTestCase):
    async def test_client_manager_tools_changed_broadcast(self):
        mgr = MCPClientManager()
        broadcasts = []
        mgr.add_tools_changed_callback(lambda sname: broadcasts.append(sname))

        mgr._notify_tools_changed("test_server")
        self.assertEqual(broadcasts, ["test_server"])


if __name__ == "__main__":
    unittest.main()
