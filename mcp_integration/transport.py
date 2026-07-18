"""传输层抽象 — 可插拔的底层通信接口。

MCP 支持 stdio 和 Streamable HTTP 两种标准传输方式。
此模块定义抽象基类，所有传输实现须继承此类。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class BaseTransport(ABC):
    """传输层抽象基类。

    每个 Transport 实例代表一条到 MCP Server 的底层连接。
    负责字节流的收发，不关心消息的语义。
    """

    @abstractmethod
    async def connect(self) -> None:
        """建立连接（启动子进程 / 建立 HTTP 连接等）。"""

    @abstractmethod
    async def send_line(self, data: str) -> None:
        """发送一行数据（含换行符）。

        Parameters
        ----------
        data : str
            要发送的 JSON 字符串（不含换行符，方法内部追加 \\n）。
        """

    @abstractmethod
    async def read_line(self) -> Optional[str]:
        """读取一行数据。

        Returns
        -------
        str or None
            读取到的行（已去除尾部换行符），连接关闭时返回 None。
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接并清理资源。"""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """连接是否仍然有效。"""
