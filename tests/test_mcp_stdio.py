"""StdioTransport stderr 排空回归测试。

StdioTransport 以 stderr=PIPE 启动子进程，若 stderr 不被持续读取：
asyncio 的 subprocess 机制会把管道读入未消费的 StreamReader 缓冲，缓冲超过
暂停阈值（本 Python 中为 2×_STREAM_LIMIT=20MB）后停止读取，OS 管道缓冲随之
写满，子进程 write() 阻塞，导致 stdout 上的 JSON-RPC 通信死锁。本测试让
真实子进程先写出远超该阈值的 stderr，再输出 stdout 应答，验证排空修复有效。
"""

import asyncio
import sys
import unittest

from mcp_integration.transports.stdio import StdioTransport

# 子进程脚本：先写 24MB stderr（远超暂停阈值 20MB），再输出 stdout 应答。
_STDERR_FLOOD_SERVER = (
    "import sys\n"
    "sys.stdin.readline()\n"
    "chunk = 'x' * 4096 + '\\n'\n"
    "for _ in range(6144):\n"
    "    sys.stderr.write(chunk)\n"
    "sys.stderr.flush()\n"
    "sys.stdout.write('hello-from-server\\n')\n"
    "sys.stdout.flush()\n"
)

# 简单回显子进程：读一行 stdout 应答，验证正常路径不受影响。
_ECHO_SERVER = (
    "import sys\n"
    "line = sys.stdin.readline()\n"
    "sys.stdout.write('echo:' + line.strip() + '\\n')\n"
    "sys.stdout.flush()\n"
)


class TestStdioTransportStderrDrain(unittest.IsolatedAsyncioTestCase):
    async def test_read_line_succeeds_when_server_writes_large_stderr(self):
        t = StdioTransport(sys.executable, ["-c", _STDERR_FLOOD_SERVER])
        await t.connect()
        try:
            await t.send_line("go\n")
            # 若 stderr 未被排空，子进程会阻塞在 stderr 写入上，
            # stdout 应答永不到达，此处将超时失败。
            line = await asyncio.wait_for(t.read_line(), timeout=10)
            # read_line 保留尾部换行符（json_rpc 会话层会 strip），此处按原语义断言
            self.assertEqual(line.strip(), "hello-from-server")
        finally:
            await t.disconnect()
        self.assertFalse(t.is_connected)

    async def test_disconnect_cancels_drain_and_is_idempotent(self):
        t = StdioTransport(sys.executable, ["-c", _STDERR_FLOOD_SERVER])
        await t.connect()
        drain_task = t._stderr_task
        proc = t._process
        self.assertIsNotNone(drain_task)
        self.assertIsNotNone(proc)

        await t.disconnect()
        # 排空任务已被取消，强引用被清空
        self.assertIsNone(t._stderr_task)
        self.assertIsNone(t._process)
        self.assertTrue(drain_task.cancelled() or drain_task.done())
        # 子进程已被终止并回收（非僵尸）
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(t.is_connected)

        # 幂等：再次调用不抛异常
        await t.disconnect()

    async def test_disconnect_without_connect_is_noop(self):
        t = StdioTransport(sys.executable, ["-c", _ECHO_SERVER])
        await t.disconnect()
        self.assertIsNone(t._process)
        self.assertIsNone(t._stderr_task)
        self.assertFalse(t.is_connected)

    async def test_echo_roundtrip_still_works(self):
        t = StdioTransport(sys.executable, ["-c", _ECHO_SERVER])
        await t.connect()
        try:
            await t.send_line("ping\n")
            line = await asyncio.wait_for(t.read_line(), timeout=10)
            self.assertEqual(line.strip(), "echo:ping")
        finally:
            await t.disconnect()


if __name__ == "__main__":
    unittest.main()
