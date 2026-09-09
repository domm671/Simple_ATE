"""内建通信原语 send/wait/field 的拼帧、匹配与字段解析。"""

from __future__ import annotations

import time

from .communication.base import Communication, Frame
from .errors import FieldExtractError, SendDataError, TimeoutAteError
from .model import Field as FieldSpec
from .model import Send as SendSpec
from .model import VarRef
from .model import Wait as WaitSpec


def build_data(tokens: tuple, variables: dict) -> bytes:
    """把 send data 的 token 列表（int / VarRef）解析为字节串。"""
    out = bytearray()
    for tok in tokens:
        if isinstance(tok, VarRef):
            value = variables.get(tok.name)
            if isinstance(value, bool) or not isinstance(value, int) or not (0 <= value <= 255):
                raise SendDataError(
                    f"data 变量 ${{{tok.name}}} 的值 {value!r} 不是 0..255 的整数")
            out.append(value)
        else:
            out.append(int(tok))
    return bytes(out)


def do_send(spec: SendSpec, comm: Communication, variables: dict) -> Frame:
    data = build_data(spec.data, variables)
    frame = Frame(id=spec.id, data=data, ext=spec.ext)
    comm.send(frame)
    return frame


def _round_engineering(value: float, gain: float, bias: float) -> float:
    """消除浮点尾数噪声（如 1203*0.01=12.030000000000001）。

    按 gain/bias 的十进制有效位数做舍入，上限 9 位，避免过度截断。
    """
    import decimal

    def decimals(x: float) -> int:
        d = decimal.Decimal(repr(x))
        return max(0, -d.as_tuple().exponent)

    ndigits = min(9, max(decimals(gain), decimals(bias)))
    return round(value, ndigits)


def extract_field(frame: Frame, spec: FieldSpec) -> int | float:
    start = spec.offset
    end = start + spec.length
    if start >= len(frame.data) or end > len(frame.data):
        raise FieldExtractError(
            f"字段 {spec.var} 提取越界：需要 data[{start}:{end}]，"
            f"实际帧长 {len(frame.data)} 字节（帧: {frame}）")
    raw_bytes = frame.data[start:end]
    raw_int = int.from_bytes(raw_bytes, byteorder=spec.endian, signed=False)
    if spec.raw:
        return raw_int
    return _round_engineering(raw_int * spec.gain + spec.bias, spec.gain, spec.bias)


def do_wait(spec: WaitSpec, comm: Communication, variables: dict,
            default_timeout: float,
            should_cancel=None) -> tuple[Frame, dict[str, int | float]]:
    """等待匹配帧并提取字段。

    返回 (命中帧, 新字段字典)。超时抛 TimeoutAteError(E301)。
    drain（清空残留帧）在发送前由执行器调用，不在此处，
    以免把同步实现（mock）中 send 即入队的应答误删。
    should_cancel: 可选的零参回调，返回 True 时提前中止等待（抛 TimeoutAteError）。
    """
    timeout = spec.timeout if spec.timeout is not None else default_timeout
    deadline_elapsed = 0.0
    start = time.monotonic()
    poll = 0.05
    while True:
        if should_cancel is not None and should_cancel():
            raise TimeoutAteError("等待被外部停止请求中断")
        remaining = timeout - deadline_elapsed
        if remaining <= 0:
            raise TimeoutAteError(
                f"等待应答 0x{spec.id:X}{'/' + hex(spec.id_mask) if spec.id_mask is not None else ''} 超时（{timeout}s）")
        try:
            frame = comm.recv(min(poll, remaining))
        except TimeoutAteError:
            deadline_elapsed = time.monotonic() - start
            continue
        if frame.ext != spec.ext:
            deadline_elapsed = time.monotonic() - start
            continue
        if spec.id_mask is None:
            matched = frame.id == spec.id
        else:
            matched = (frame.id & spec.id_mask) == (spec.id & spec.id_mask)
        if matched and len(frame.data) >= spec.min_len:
            extracted: dict[str, int | float] = {}
            for fld in spec.fields:
                extracted[fld.var] = extract_field(frame, fld)
            variables.update(extracted)
            return frame, extracted
        deadline_elapsed = time.monotonic() - start


def drain(comm: Communication) -> None:
    """排空接收缓冲中的残留帧（应在发送请求帧之前调用）。"""
    while True:
        try:
            comm.recv(0.0)
        except TimeoutAteError:
            return
        except Exception:                       # noqa: BLE001 - mock/不同实现差异
            return
