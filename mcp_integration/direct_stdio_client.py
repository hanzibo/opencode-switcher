"""绕过任何 MCP SDK 传输层问题的直接 stdio 客户端。

MCP SDK v1.28 的 stdio_client 基于 anyio.open_process，
在 Python 3.14.4 上 anyio.open_process 会挂死。
此模块使用原生 asyncio.create_subprocess_exec 实现相同的
MCP stdio 传输协议。

用法与 mcp.stdio_client 兼容：
    async with direct_stdio_client(params) as (read_stream, write_stream):
        ...
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Tuple

from mcp import StdioServerParameters
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

logger = logging.getLogger(__name__)


class _ReadStream:
    """模拟 MCP SDK 的 read stream（MemoryObjectReceiveStream 的替代）。

    支持 async for 迭代和直接 receive() 调用。
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._closed and self._queue.empty():
            raise StopAsyncIteration
        result = await self._queue.get()
        if isinstance(result, StopAsyncIteration):
            raise StopAsyncIteration
        return result

    async def receive(self) -> SessionMessage:
        result = await self._queue.get()
        if isinstance(result, Exception):
            raise result
        return result

    def _put(self, item: SessionMessage | Exception) -> None:
        self._queue.put_nowait(item)

    def _close(self) -> None:
        self._closed = True
        self._queue.put_nowait(StopAsyncIteration())

    async def aclose(self) -> None:
        self._close()


class _WriteStream:
    """模拟 MCP SDK 的 write stream（MemoryObjectSendStream 的替代）。"""

    def __init__(self, stdin) -> None:
        self._stdin = stdin

    async def send(self, msg: SessionMessage) -> None:
        data = msg.message.model_dump_json(
            by_alias=True, exclude_none=True
        )
        self._stdin.write((data + "\n").encode("utf-8"))
        await self._stdin.drain()

    async def aclose(self) -> None:
        if self._stdin and not self._stdin.is_closing():
            self._stdin.close()


@asynccontextmanager
async def direct_stdio_client(
    params: StdioServerParameters,
) -> AsyncIterator[Tuple[_ReadStream, _WriteStream]]:
    """直接 stdio 传输：使用 asyncio.create_subprocess_exec 创建子进程。

    替代 mcp.stdio_client，解决 anyio 在 Python 3.14 上的兼容性问题。
    """
    process = await asyncio.create_subprocess_exec(
        params.command,
        *params.args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=params.cwd or None,
        env=params.env or None,
    )

    read_stream = _ReadStream()
    write_stream = _WriteStream(process.stdin)

    # 后台任务：读取 stdout，解析为 JSONRPCMessage
    async def _reader() -> None:
        try:
            assert process.stdout
            while True:
                chunk = await process.stdout.readline()
                if not chunk:
                    break
                line = chunk.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    message = JSONRPCMessage.model_validate_json(line)
                    read_stream._put(SessionMessage(message))
                except Exception as exc:
                    logger.exception("Failed to parse MCP message")
                    read_stream._put(exc)
        except Exception as exc:
            read_stream._put(exc)
        finally:
            read_stream._close()

    reader_task = asyncio.create_task(_reader())

    try:
        yield read_stream, write_stream
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
