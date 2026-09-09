"""真实 CAN 通信（基于 python-can）。

M3 里程碑实现。M1 不安装 python-can；在工位配置 type="can" 且未安装库时
给出明确错误提示。
"""

from __future__ import annotations

from ..errors import CommunicationError, TimeoutAteError  # noqa: F401
from .base import Frame


class CanCommunication:
    def __init__(self, name: str, interface: str = "", channel: str = "",
                 bitrate: int = 500000):
        self.name = name
        self.interface = interface
        self.channel = channel
        self.bitrate = bitrate
        self._bus = None

    def open(self) -> None:
        try:
            import can  # type: ignore
        except ImportError as exc:
            raise CommunicationError(
                "未安装 python-can，无法使用 type=\"can\" 的资源；"
                "M1 请在工位配置中使用 type=\"mock\"。"
            ) from exc
        self._bus = can.Bus(interface=self.interface, channel=self.channel,
                           bitrate=self.bitrate)

    def close(self) -> None:
        if self._bus is not None:
            self._bus.shutdown()
            self._bus = None

    def send(self, frame: Frame) -> None:
        import can  # type: ignore
        msg = can.Message(arbitration_id=frame.id, data=frame.data,
                          is_extended_id=frame.ext)
        self._bus.send(msg)

    def recv(self, timeout: float) -> Frame:
        msg = self._bus.recv(timeout=timeout)
        if msg is None:
            raise TimeoutAteError(f"[{self.name}] 等待应答超时（{timeout}s）")
        return Frame(id=msg.arbitration_id, data=bytes(msg.data),
                     ext=bool(msg.is_extended_id), timestamp=msg.timestamp)
