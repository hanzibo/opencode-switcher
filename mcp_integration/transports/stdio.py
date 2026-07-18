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

    async def connect(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            self._command, *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )

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
