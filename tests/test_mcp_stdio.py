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


_CWD_ENV_SERVER = (
    "import os, sys\n"
    "sys.stdin.readline()\n"
    "cwd = os.getcwd()\n"
    "custom_env = os.environ.get('MCP_TEST_VAR', '')\n"
    "sys.stdout.write(f'{cwd}|{custom_env}\\n')\n"
    "sys.stdout.flush()\n"
)


class TestStdioTransportCwdAndEnv(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_cwd_and_env_passed_to_subprocess(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            real_tmpdir = os.path.realpath(tmpdir)
            t = StdioTransport(
                sys.executable,
                ["-c", _CWD_ENV_SERVER],
                cwd=real_tmpdir,
                env={"MCP_TEST_VAR": "secret_token_123"},
            )
            await t.connect()
            try:
                await t.send_line("go\n")
                line = await asyncio.wait_for(t.read_line(), timeout=10)
                out_cwd, out_env = line.strip().split("|", 1)
                self.assertEqual(out_cwd, real_tmpdir)
                self.assertEqual(out_env, "secret_token_123")
            finally:
                await t.disconnect()


class TestMCPServerConfigModel(unittest.TestCase):
    def test_server_config_env_serialization(self):
        from mcp_integration.server_config import MCPServerConfig

        cfg = MCPServerConfig(
            name="test_srv",
            transport="stdio",
            command="python3",
            args=["-m", "server"],
            cwd="/tmp",
            env={"API_KEY": "abc", "PORT": "8080"},
        )
        d = cfg.to_dict()
        self.assertEqual(d["env"], {"API_KEY": "abc", "PORT": "8080"})
        self.assertEqual(d["cwd"], "/tmp")

        restored = MCPServerConfig.from_dict(d)
        self.assertEqual(restored.env, {"API_KEY": "abc", "PORT": "8080"})
        self.assertEqual(restored.cwd, "/tmp")
        self.assertIsNone(restored.validate())

    def test_server_config_validation(self):
        from mcp_integration.server_config import MCPServerConfig

        # 无效 env（非 dict）
        cfg_bad_env = MCPServerConfig(name="bad", transport="stdio", command="echo", env="not_a_dict")
        self.assertIn("env 必须为键值对字典", cfg_bad_env.validate())

        # 无效 cwd（非 str）
        cfg_bad_cwd = MCPServerConfig(name="bad", transport="stdio", command="echo", cwd=123)
        self.assertIn("cwd 必须为路径字符串", cfg_bad_cwd.validate())


if __name__ == "__main__":
    unittest.main()
