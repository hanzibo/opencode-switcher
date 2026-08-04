"""MCP 集成包的单元测试。

测试分层架构的各层可独立工作：
  StdioTransport → JsonRpcSession → MCPSession → MCPClientManager
"""

import asyncio
import json
import unittest
from typing import Optional

from mcp_integration.transport import BaseTransport


# ═══════════════════════════════════════════════════════════════════
#  辅助：模拟 Transport
# ═══════════════════════════════════════════════════════════════════

class MockTransport(BaseTransport):
    """模拟传输层：预置响应映射表，send_line 自动触发对应响应。"""

    def __init__(self) -> None:
        self._connected = False
        self._sent: list[str] = []
        self._responses: dict[int, str] = {}  # msg_id -> JSON line
        self._response_queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()

    async def connect(self) -> None:
        self._connected = True

    async def send_line(self, data: str) -> None:
        self._sent.append(data)
        # 自动触发响应
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            return
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in self._responses:
            self._response_queue.put_nowait((msg_id, self._responses.pop(msg_id)))

    async def read_line(self) -> Optional[str]:
        try:
            _, line = await asyncio.wait_for(self._response_queue.get(), timeout=3)
            return line
        except asyncio.TimeoutError:
            return None

    async def disconnect(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── 测试辅助 ──

    def enqueue_response(self, msg_id: int, result: dict) -> None:
        self._responses[msg_id] = json.dumps(
            {"jsonrpc": "2.0", "id": msg_id, "result": result}
        )

    def enqueue_error(self, msg_id: int, code: int, message: str) -> None:
        self._responses[msg_id] = json.dumps(
            {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
        )

    def last_sent(self) -> Optional[dict]:
        if not self._sent:
            return None
        return json.loads(self._sent[-1])


# ═══════════════════════════════════════════════════════════════════
#  测试 JsonRpcSession
# ═══════════════════════════════════════════════════════════════════

class TestJsonRpcSession(unittest.IsolatedAsyncioTestCase):
    async def test_request_success(self):
        t = MockTransport()
        t.enqueue_response(1, {"ok": True})
        s = self._make_session(t)
        await s.connect()

        result = await s.request("test.method", {"foo": "bar"})
        self.assertEqual(result, {"ok": True})

        sent = t.last_sent()
        self.assertEqual(sent["jsonrpc"], "2.0")
        self.assertEqual(sent["method"], "test.method")
        self.assertEqual(sent["params"], {"foo": "bar"})
        self.assertEqual(sent["id"], 1)
        await s.close()

    async def test_request_error(self):
        t = MockTransport()
        t.enqueue_error(1, -32601, "Method not found")
        s = self._make_session(t)
        await s.connect()

        from mcp_integration.json_rpc import JsonRpcError
        with self.assertRaises(JsonRpcError) as ctx:
            await s.request("bad.method")
        self.assertEqual(ctx.exception.code, -32601)
        self.assertIn("Method not found", str(ctx.exception))
        await s.close()

    async def test_notify(self):
        t = MockTransport()
        s = self._make_session(t)
        await s.connect()
        await s.notify("notifications/initialized")
        sent = t.last_sent()
        self.assertEqual(sent["method"], "notifications/initialized")
        self.assertNotIn("id", sent)
        await s.close()

    async def test_disconnected_error(self):
        t = MockTransport()
        s = self._make_session(t)
        await s.connect()
        await s.close()

        from mcp_integration.json_rpc import JsonRpcDisconnectedError
        with self.assertRaises(JsonRpcDisconnectedError):
            await s.request("test.method")

    async def test_multiple_requests(self):
        t = MockTransport()
        t.enqueue_response(1, {"first": True})
        t.enqueue_response(2, {"second": True})
        s = self._make_session(t)
        await s.connect()

        self.assertEqual(await s.request("m1"), {"first": True})
        self.assertEqual(await s.request("m2"), {"second": True})
        await s.close()

    def _make_session(self, transport):
        from mcp_integration.json_rpc import JsonRpcSession
        return JsonRpcSession(transport, request_timeout=5)


# ═══════════════════════════════════════════════════════════════════
#  测试 MCPSession
# ═══════════════════════════════════════════════════════════════════

class TestMCPSession(unittest.IsolatedAsyncioTestCase):
    def _make_session(self, transport):
        from mcp_integration.json_rpc import JsonRpcSession
        return JsonRpcSession(transport, request_timeout=5)

    async def test_initialize(self):
        t = MockTransport()
        t.enqueue_response(1, {
            "protocolVersion": "2025-11-25",
            "serverInfo": {"name": "test-server", "version": "1.0.0"},
            "capabilities": {"tools": {}},
        })
        s = self._make_session(t)
        await s.connect()

        result = await s.request("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        })
        self.assertEqual(result["serverInfo"]["name"], "test-server")
        await s.close()

    async def test_list_and_call_tool(self):
        from mcp_integration.json_rpc import JsonRpcSession
        from mcp_integration.mcp_session import MCPSession

        t = MockTransport()
        # 预置全流程响应
        t.enqueue_response(1, {
            "protocolVersion": "2025-11-25",
            "serverInfo": {"name": "ts", "version": "1"},
            "capabilities": {"tools": {}},
        })
        t.enqueue_response(2, {"tools": [{"name": "echo", "description": "Echo", "inputSchema": {}}]})
        t.enqueue_response(3, {"content": [{"type": "text", "text": "hello"}]})

        jrpc = JsonRpcSession(t, request_timeout=5)
        session = MCPSession(jrpc)
        await session.connect()
        await session.initialize()

        tools = await session.list_tools()
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "echo")

        result = await session.call_tool("echo", {"x": 1})
        self.assertEqual(result, "hello")
        await jrpc.close()

    async def test_error_propagation(self):
        t = MockTransport()
        t.enqueue_error(1, -32603, "Internal error")
        from mcp_integration.json_rpc import JsonRpcSession, JsonRpcError
        jrpc = JsonRpcSession(t, request_timeout=5)
        await jrpc.connect()

        with self.assertRaises(JsonRpcError) as ctx:
            await jrpc.request("tools/call", {"name": "bad"})
        self.assertEqual(ctx.exception.code, -32603)
        await jrpc.close()


# ═══════════════════════════════════════════════════════════════════
#  测试 negotiate_version
# ═══════════════════════════════════════════════════════════════════

class TestProtocolNegotiation(unittest.TestCase):
    def test_exact_match(self):
        from mcp_integration.mcp_session import negotiate_version
        v = negotiate_version(["2025-11-25", "2025-03-26"], "2025-03-26")
        self.assertEqual(v, "2025-03-26")

    def test_fallback_to_latest(self):
        from mcp_integration.mcp_session import negotiate_version
        v = negotiate_version(["2025-11-25", "2025-03-26"], "2024-11-05")
        self.assertEqual(v, "2025-11-25")

    def test_single_version(self):
        from mcp_integration.mcp_session import negotiate_version
        v = negotiate_version(["2025-11-25"], "2025-11-25")
        self.assertEqual(v, "2025-11-25")


# ═══════════════════════════════════════════════════════════════════
#  测试 MCPClientManager（接口完整性）
# ═══════════════════════════════════════════════════════════════════

class TestMCPClientManager(unittest.TestCase):
    def test_no_servers_by_default(self):
        from mcp_integration import MCPClientManager
        mgr = MCPClientManager()
        self.assertEqual(mgr.get_server_count(), 0)
        self.assertEqual(mgr.get_connected_servers(), [])

    def test_list_tools_not_connected(self):
        from mcp_integration import MCPClientManager
        mgr = MCPClientManager()
        async def run():
            tools = await mgr.list_tools("nonexistent")
            self.assertEqual(tools, [])
        asyncio.run(run())

    def test_call_tool_not_connected(self):
        from mcp_integration import MCPClientManager
        mgr = MCPClientManager()
        async def run():
            result = await mgr.call_tool("nonexistent", "test", {})
            self.assertIn("未连接", result)
        asyncio.run(run())

    def test_bridge_accessible(self):
        from mcp_integration import MCPClientManager, GtkAsyncioBridge
        mgr = MCPClientManager()
        self.assertIsNotNone(mgr.bridge)
        self.assertIsInstance(mgr.bridge, GtkAsyncioBridge)


# ═══════════════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main()
