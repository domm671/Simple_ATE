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
    protocol: str | None = None
    # 连接参数（串口/Modbus/USB/CAN）：已由解析器归一化为 Python 类型
    options: tuple[tuple[str, Any], ...] = ()
    line: int = 0

    def option(self, name: str, default: Any = None) -> Any:
        for key, value in self.options:
            if key == name:
                return value
        return default

    def options_dict(self) -> dict[str, Any]:
        return dict(self.options)


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
    data: tuple[Any, ...] = ()            # int 或 VarRef
    ext: bool = False
    id_mask: int | None = None
    mode: str = "single"                  # single / file
    file: str | None = None               # mode=file 时的文件路径
    chunk_size: int = 8                   # mode=file 时每帧数据字节数
    seq_len: int = 0                      # mode=file 时每帧前置序号字节数(0/1/2/4)
    header: tuple[int, ...] = ()          # mode=file 时每帧固定前缀字节
    interval: float = 0.0                 # mode=file 时帧间隔(秒)
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
    mode: str = "single"                  # single / file
    file: str | None = None               # mode=file 时保存路径
    chunk_size: int | None = None         # mode=file 时每帧有效数据字节数
    seq_len: int = 0                      # mode=file 时每帧前置序号字节数(0/1/2/4)
    header: tuple[int, ...] = ()          # mode=file 时期望的固定前缀字节
    size: int | None = None               # mode=file 终止条件：期望总字节数
    chunks: int | None = None             # mode=file 终止条件：期望帧数
    idle_gap: float | None = None         # mode=file 终止条件：静默秒数
    max_size: int = 64 * 1024 * 1024      # mode=file 安全上限
    checksum: str = "none"                # mode=file: none/crc32/crc16_modbus/sum8
    checksum_value: Any = None            # 期望校验值（十六进制字符串或 VarRef）
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
    value: Any = None                     # float / int / str / VarRef
    min: float | None = None
    max: float | None = None
    eq: Any = None                        # int / float / bool / str / VarRef
    unit: str | None = None
    mode: str = "auto"                    # auto（按返回帧判定）/ manual（人工判定）
    prompt: str | None = None             # 人工判定时界面提示内容
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
@dataclass(frozen=True)
class ManualJudgeRequest:
    """人工判定请求：引擎交给监听方（UI 弹窗 / CLI 询问）。"""

    seq: int
    step_name: str
    prompt: str
    value: Any = None
    unit: str | None = None


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
    judge_mode: str = "auto"              # auto / manual（新增字段只追加不更名）

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
            "judge_mode": self.judge_mode,
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
            judge_mode=d.get("judge_mode", "auto"),
        )
