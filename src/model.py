"""脚本内存模型与结果数据结构（对应《ATE脚本格式规范.md》附录 B）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

OnFail = Literal["abort", "continue"]
Endian = Literal["little", "big"]


# ---------------------------------------------------------------- 脚本模型
@dataclass(frozen=True)
class VarRef:
    """属性值中的 ${name} 变量引用。"""

    name: str
    line: int = 0
    column: int = 0

    def __str__(self) -> str:
        return "${" + self.name + "}"


@dataclass(frozen=True)
class ConnectStmt:
    resource: str
    timeout: float = 3.0
    line: int = 0


@dataclass(frozen=True)
class DisconnectStmt:
    resource: str
    line: int = 0


@dataclass(frozen=True)
class DelayStmt:
    ms: int
    line: int = 0


@dataclass(frozen=True)
class Send:
    id: int
    data: tuple[Any, ...]                 # int 或 VarRef
    ext: bool = False
    id_mask: int | None = None
    line: int = 0


@dataclass(frozen=True)
class Field:
    var: str
    offset: int
    length: int = 1
    endian: Endian = "little"
    gain: float = 1.0
    bias: float = 0.0
    raw: bool = False
    unit: str | None = None
    line: int = 0


@dataclass(frozen=True)
class Wait:
    id: int
    ext: bool = False
    id_mask: int | None = None
    timeout: float | None = None          # None 表示取 step.timeout
    drain: str = "before"                 # before / off
    min_len: int = 0
    fields: tuple[Field, ...] = ()
    line: int = 0


@dataclass(frozen=True)
class Action:
    """可选扩展：handler="module:func"。kwargs 的值为 str 或 VarRef。"""

    handler: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    var: str | None = None
    line: int = 0


# 一个 step 内的有序指令
Instruction = Send | Wait | DelayStmt | Action


@dataclass(frozen=True)
class Limit:
    value: Any                            # float / int / str / VarRef
    min: float | None = None
    max: float | None = None
    eq: Any = None                        # int / float / bool / str / VarRef
    unit: str | None = None
    line: int = 0


@dataclass(frozen=True)
class Step:
    name: str
    instructions: tuple[Instruction, ...]
    limit: Limit | None = None
    resource: str | None = None
    timeout: float = 5.0
    retry: int = 0
    retry_interval: float = 0.0
    on_fail: OnFail = "abort"
    line: int = 0


Statement = ConnectStmt | DisconnectStmt | DelayStmt | Step


@dataclass(frozen=True)
class Script:
    name: str
    version: str
    sha256: str
    statements: tuple[Statement, ...]
    path: str = ""


# ---------------------------------------------------------------- 结果模型
@dataclass
class ItemResult:
    seq: int
    step_name: str
    value: Any = None
    unit: str | None = None
    low_limit: float | None = None
    high_limit: float | None = None
    result: str = "PASS"                  # PASS / FAIL / ERROR
    duration_ms: int = 0
    retries: int = 0
    error_code: str | None = None
    message: str | None = None

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "step_name": self.step_name,
            "value": self.value,
            "unit": self.unit,
            "low_limit": self.low_limit,
            "high_limit": self.high_limit,
            "result": self.result,
            "duration_ms": self.duration_ms,
            "retries": self.retries,
            "error_code": self.error_code,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ItemResult":
        return cls(
            seq=d["seq"],
            step_name=d["step_name"],
            value=d.get("value"),
            unit=d.get("unit"),
            low_limit=d.get("low_limit"),
            high_limit=d.get("high_limit"),
            result=d.get("result", "PASS"),
            duration_ms=d.get("duration_ms", 0),
            retries=d.get("retries", 0),
            error_code=d.get("error_code"),
            message=d.get("message"),
        )
