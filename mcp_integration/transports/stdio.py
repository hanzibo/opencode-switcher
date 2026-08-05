"""stdio 传输实现 — 通过子进程 stdin/stdout 通信。"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from mcp_integration.transport import BaseTransport

logger = logging.getLogger(__name__)

_STREAM_LIMIT = 10 * 1024 * 1024  # 流缓冲区 10MB


class StdioTransport(BaseTransport):
    """基于 asyncio 子进程的 stdio 传输。

    启动 MCP Server 子进程，通过 stdin/stdout 通信。
    """

    def __init__(self, command: str, args: List[str]) -> None:
        self._command = command
        self._args = args
        self._process: Optional[asyncio.subprocess.Process] = None
        # 强引用 stderr 排空任务，防止被 GC 回收导致管道不再被读取
        self._stderr_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            self._command, *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )
        # stderr 可能为 None（如子进程已退出或 stderr 未定向到管道），需容错
        if self._process.stderr is not None:
            self._stderr_task = asyncio.create_task(
                self._drain_stderr(self._process.stderr)
            )

    async def _drain_stderr(self, stderr) -> None:
        """持续排空子进程 stderr，防止管道缓冲写满阻塞子进程。

        若 stderr 不被读取，OS 管道缓冲（通常 64KB）填满后子进程的
        write() 会阻塞，进而导致 stdout 上的 JSON-RPC 通信死锁。
        此处只排空并记录日志，不参与协议解析。
        """
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.debug("MCP server stderr: %s", text)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # 排空失败不应影响主通信，仅记录警告
            logger.warning("MCP server stderr 排空异常: %s", e)

    async def send_line(self, data: str) -> None:
        if not self._process or not self._process.stdin:
            raise RuntimeError("StdioTransport: 未连接")
        self._process.stdin.write(data.encode("utf-8"))
        await self._process.stdin.drain()

    async def read_line(self) -> Optional[str]:
        if not self._process or not self._process.stdout:
            return None
        line = await self._process.stdout.readline()
        if not line:
            return None
        return line.decode("utf-8", errors="replace")

    async def disconnect(self) -> None:
        # 先取消 stderr 排空任务（幂等：多次调用无副作用）
        task = self._stderr_task
        self._stderr_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                if self._process and self._process.returncode is None:
                    self._process.kill()
                    await self._process.wait()
        self._process = None

    @property
    def is_connected(self) -> bool:
        return (self._process is not None
                and self._process.returncode is None)
