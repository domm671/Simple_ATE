"""结果存储抽象接口（数据库等实现预留，见设计说明 9.3）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

if TYPE_CHECKING:
    from ..engine.model import ItemResult


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
    run_id: str | None = field(default_factory=lambda: str(uuid4()))
    planned_steps: int | None = None
    station_mode: str | None = None
    station_config_sha256: str | None = None
    resource_backends: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sn": self.sn,
            "script_name": self.script_name,
            "script_version": self.script_version,
            "script_sha256": self.script_sha256,
            "software_version": self.software_version,
            "station_id": self.station_id,
            "start_time": self.start_time,
            "run_id": self.run_id,
            "planned_steps": self.planned_steps,
            "station_mode": self.station_mode,
            "station_config_sha256": self.station_config_sha256,
            "resource_backends": self.resource_backends,
        }


@dataclass
class RunHandle:
    """一次 Run 的存储句柄。token 由具体存储实现管理。"""

    token: Any
    path: str = ""
    termination_reason: str | None = None


@dataclass
class RunResult:
    meta: RunMeta
    end_time: str
    result: str                        # PASS / FAIL / ERROR / ABORT
    items: list[ItemResult]
    termination_reason: str | None = None

    @property
    def executed_steps(self) -> int:
        return len(self.items)

    def to_dict(self) -> dict:
        return {
            **self.meta.to_dict(),
            "end_time": self.end_time,
            "result": self.result,
            "executed_steps": self.executed_steps,
            "termination_reason": self.termination_reason,
            "items": [i.to_dict() for i in self.items],
        }


class ResultStore(Protocol):
    def create_run(self, meta: RunMeta) -> RunHandle: ...
    def append_item(self, handle: RunHandle, item: ItemResult) -> None: ...
    def finish_run(self, handle: RunHandle, result: str, end_time: str) -> RunResult: ...
