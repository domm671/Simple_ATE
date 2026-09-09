"""通信层抽象接口（同步模型）。

v0.1 只实现 CAN 与 Mock；serial 仅占位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..errors import TimeoutAteError  # noqa: F401  （供实现类统一引用）


@dataclass(frozen=True)
class Frame:
    """统一帧对象。"""

    id: int
    data: bytes = b""
    ext: bool = False
    timestamp: float = 0.0

    def __str__(self) -> str:
        data_hex = " ".join(f"{b:02X}" for b in self.data)
        return f"id=0x{self.id:X} ext={int(self.ext)} data={data_hex}"


class Communication(Protocol):
    """所有通信资源实现的同步接口。"""

    name: str

    def open(self) -> None: ...
    def close(self) -> None: ...
    def send(self, frame: Frame) -> None: ...
    def recv(self, timeout: float) -> Frame:
        """在 timeout 秒内等待下一帧；超时抛 TimeoutAteError(E301)。"""
        ...


@dataclass
class ResourceConfig:
    """station.toml 中单个 [resources.xxx] 的解析结果。"""

    name: str
    type: str                          # can / serial / mock
    options: dict = field(default_factory=dict)
