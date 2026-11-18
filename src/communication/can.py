"""真实 CAN 通信（基于 python-can）。

M3 里程碑实现。M1 不安装 python-can；在工位配置 type="can" 且未安装库时
给出明确错误提示。
"""

from __future__ import annotations

import time

from ..errors import CommunicationError, TimeoutAteError
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
        try:
            self._bus = can.Bus(interface=self.interface, channel=self.channel,
                                bitrate=self.bitrate)
        except (can.CanError, ValueError, OSError) as exc:
            raise CommunicationError(f"[{self.name}] 打开 CAN 失败: {exc}") from exc

    def close(self) -> None:
        if self._bus is not None:
            try:
                self._bus.shutdown()
            finally:
                self._bus = None

    def send(self, frame: Frame) -> None:
        import can  # type: ignore
        if self._bus is None:
            raise CommunicationError(f"[{self.name}] CAN 未打开")
        try:
            msg = can.Message(arbitration_id=frame.id, data=frame.data,
                              is_extended_id=frame.ext, check=True)
            self._bus.send(msg)
        except (can.CanError, ValueError, OSError) as exc:
            raise CommunicationError(f"[{self.name}] CAN 发送失败: {exc}") from exc

    def recv(self, timeout: float) -> Frame:
        if self._bus is None:
            raise CommunicationError(f"[{self.name}] CAN 未打开")
        import can  # type: ignore
        deadline = time.monotonic() + timeout
        try:
            msg = self._bus.recv(timeout=max(0.0, timeout))
            while msg is not None and (msg.is_error_frame or msg.is_remote_frame):
                if time.monotonic() >= deadline:
                    msg = None
                    break
                msg = self._bus.recv(timeout=max(0.0, deadline - time.monotonic()))
        except (can.CanError, OSError) as exc:
            raise CommunicationError(f"[{self.name}] CAN 接收失败: {exc}") from exc
        if msg is None:
            raise TimeoutAteError(f"[{self.name}] 等待应答超时（{timeout}s）")
        return Frame(id=msg.arbitration_id, data=bytes(msg.data),
                     ext=bool(msg.is_extended_id), timestamp=msg.timestamp)
