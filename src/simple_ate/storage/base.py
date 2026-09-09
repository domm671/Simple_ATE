"""结果存储抽象接口（数据库等实现预留，见设计说明 9.3）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..model import ItemResult


@dataclass
class RunMeta:
    sn: str
    script_name: str
    script_version: str
    script_sha256: str
    software_version: str
    station_id: str
    start_time: str
    script_path: str = ""


@dataclass
class RunHandle:
    """一次 Run 的存储句柄（文件存储中即文件路径）。"""

    token: Any
    path: str = ""


@dataclass
class RunResult:
    meta: RunMeta
    end_time: str
    result: str                        # PASS / FAIL / ERROR / ABORT
    items: list[ItemResult]

    def to_dict(self) -> dict:
        return {
            "sn": self.meta.sn,
            "script_name": self.meta.script_name,
            "script_version": self.meta.script_version,
            "script_sha256": self.meta.script_sha256,
            "software_version": self.meta.software_version,
            "station_id": self.meta.station_id,
            "start_time": self.meta.start_time,
            "end_time": self.end_time,
            "result": self.result,
            "items": [i.to_dict() for i in self.items],
        }


class ResultStore(Protocol):
    def create_run(self, meta: RunMeta) -> RunHandle: ...
    def append_item(self, handle: RunHandle, item: ItemResult) -> None: ...
    def finish_run(self, handle: RunHandle, result: str, end_time: str) -> RunResult: ...
