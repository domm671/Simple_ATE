"""串口通信占位（v0.1 不实现内建报文原语，请使用扩展 action）。"""

from __future__ import annotations

from ..errors import CommunicationError
from .base import Frame


class SerialCommunication:
    def __init__(self, name: str, **options):
        self.name = name
        self.options = options

    def open(self) -> None:
        raise CommunicationError(
            "serial 资源在 v0.1 尚未提供内建帧原语；请在扩展 action 中自行使用 pyserial。"
        )

    def close(self) -> None:
        pass

    def send(self, frame: Frame) -> None:
        raise CommunicationError("serial 未实现")

    def recv(self, timeout: float) -> Frame:
        raise CommunicationError("serial 未实现")
