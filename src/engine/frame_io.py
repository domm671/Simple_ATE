"""内建通信原语 send/wait/field 的拼帧、匹配、字段解析与文件传输。

文件传输（`<send mode="file">` / `<wait mode="file">`）用于把本地文件按
固定分块逐帧发出，或在接收侧把多帧重新拼装为文件。设计要点：

- 每帧数据布局：`header` + 序号（`seq_len` 字节小端）+ 分块数据；
- 发送侧顺序读取文件，循环 send，帧间隔（interval）可被停止标志中断；
- 接收侧以 `size` / `chunks` / `idle_gap` 之一作为终止条件，
  用 `max_size` 做内存安全上限，最后原子落盘（临时文件 + os.replace）；
- 校验（crc32 / crc16_modbus / sum8）在接收侧可选，校验失败抛 E308 可重试。
"""

from __future__ import annotations

import os
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..communication.base import Communication, Frame
from ..errors import (
    FieldExtractError,
    FileTransferError,
    SendDataError,
    TimeoutAteError,
)
from .judge import resolve as resolve_value
from .model import Field as FieldSpec
from .model import Send as SendSpec
from .model import VarRef
from .model import Wait as WaitSpec


@dataclass
class FileTransferStats:
    """文件接收结果摘要。"""

    bytes_received: int
    chunks: int
    path: str
    checksum_kind: str = "none"


def compute_checksum(data: bytes, kind: str) -> bytes:
    """按指定算法计算校验字节（供文件传输与 Modbus 共用）。"""
    if kind == "crc32":
        return struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)
    if kind == "crc16_modbus":
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        return bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    if kind == "sum8":
        return bytes([sum(data) & 0xFF])
    raise FileTransferError(f"未知校验算法: {kind}")


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


# ---------------------------------------------------------------- 文件传输
def _resolve_transfer_path(file: str | None, base_dir: str | Path) -> Path:
    """解析传输文件路径：相对路径以脚本所在目录为基准。"""
    if not file:
        raise FileTransferError("文件传输未指定 file")
    path = Path(file)
    if not path.is_absolute():
        path = Path(base_dir) / path
    return path


def _sleep_interruptible(seconds: float, should_cancel=None) -> None:
    """文件分块之间的延时，每 50ms 响应停止标志。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if should_cancel is not None and should_cancel():
            return
        time.sleep(min(0.05, max(0.0, end - time.monotonic())))


def _atomic_write(path: Path, data: bytes) -> None:
    """原子写：临时文件 + fsync + os.replace，避免半写文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _id_matches(frame: Frame, spec: WaitSpec) -> bool:
    if spec.id_mask is None:
        return frame.id == spec.id
    return (frame.id & spec.id_mask) == (spec.id & spec.id_mask)


def _normalize_checksum_value(value: Any) -> bytes | None:
    """把 checksum_value（十六进制字符串 / 整数）归一化为字节串。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise FileTransferError(f"非法 checksum_value: {value!r}")
    if isinstance(value, int):
        length = max(1, (value.bit_length() + 7) // 8)
        return value.to_bytes(length, "big")
    text = str(value).strip().replace("0x", "").replace("0X", "").replace(" ", "")
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise FileTransferError(f"非法 checksum_value: {value!r}") from exc


def iter_send_file(spec: SendSpec, comm: Communication, base_dir: str | Path,
                   should_cancel=None):
    """把本地文件分块逐个发出：每发一帧 yield 一帧，供上层记 trace/日志。"""
    path = _resolve_transfer_path(spec.file, base_dir)
    if not path.exists():
        raise FileTransferError(f"待发送文件不存在: {path}")
    chunk_size = max(1, spec.chunk_size)
    header = bytes(spec.header)
    seq = 0
    with open(path, "rb") as f:
        while True:
            if should_cancel is not None and should_cancel():
                return
            chunk = f.read(chunk_size)
            if not chunk:
                break
            data = bytearray(header)
            if spec.seq_len:
                data += seq.to_bytes(spec.seq_len, "little")
            data += chunk
            frame = Frame(id=spec.id, data=bytes(data), ext=spec.ext)
            comm.send(frame)
            yield frame
            seq += 1
            if spec.interval > 0:
                _sleep_interruptible(spec.interval, should_cancel)


def do_wait_file(spec: WaitSpec, comm: Communication, variables: dict,
                 base_dir: str | Path, default_timeout: float,
                 should_cancel=None, on_frame=None) -> FileTransferStats:
    """接收多帧并拼装为文件。

    终止条件：收到 `size` 字节 / `chunks` 帧 / 静默 `idle_gap` 秒。
    任何条件到达或整体超时（且已有数据）后结束；无任何数据则抛 E301。
    """
    timeout = spec.timeout if spec.timeout is not None else default_timeout
    header = bytes(spec.header)
    expected: bytes | None = None
    if spec.checksum != "none":
        expected = _normalize_checksum_value(
            resolve_value(spec.checksum_value, variables))

    ordered: dict[int, bytes] = {}
    appended: list[bytes] = []
    total = 0
    chunks = 0
    start = time.monotonic()
    last = start

    while True:
        if should_cancel is not None and should_cancel():
            raise TimeoutAteError("文件接收被外部停止请求中断")
        if spec.size is not None and total >= spec.size:
            break
        if spec.chunks is not None and chunks >= spec.chunks:
            break
        now = time.monotonic()
        if spec.idle_gap is not None and chunks > 0 and now - last >= spec.idle_gap:
            break
        elapsed = now - start
        if elapsed >= timeout:
            if chunks == 0:
                raise TimeoutAteError(f"接收文件超时（{timeout}s），未收到任何数据帧")
            break
        try:
            frame = comm.recv(min(0.05, max(0.0, timeout - elapsed)))
        except TimeoutAteError:
            continue
        if on_frame is not None:
            on_frame(frame)
        if frame.ext != spec.ext or not _id_matches(frame, spec):
            continue

        data = frame.data
        if header:
            if not data.startswith(header):
                continue
            data = data[len(header):]
        seq: int | None = None
        if spec.seq_len:
            if len(data) < spec.seq_len:
                continue
            seq = int.from_bytes(data[:spec.seq_len], "little")
            data = data[spec.seq_len:]
        if spec.chunk_size is not None:
            data = data[:spec.chunk_size]
        if not data:
            if spec.size is not None and total >= spec.size:
                break
            continue
        if spec.seq_len:
            if seq in ordered:
                continue                        # 重复帧丢弃
            ordered[seq] = data
        else:
            appended.append(data)
        total += len(data)
        chunks += 1
        last = time.monotonic()
        if total > spec.max_size:
            raise FileTransferError(
                f"接收数据超过 max_size({spec.max_size})，已中止（可能存在协议错位）")

    if spec.seq_len:
        buf = b"".join(ordered[k] for k in sorted(ordered))
    else:
        buf = b"".join(appended)
    if spec.size is not None and len(buf) > spec.size:
        buf = buf[:spec.size]
    if spec.size is not None and len(buf) != spec.size:
        raise FileTransferError(
            f"接收文件长度 {len(buf)} 字节与期望 size={spec.size} 不符")
    if spec.checksum != "none":
        actual = compute_checksum(buf, spec.checksum)
        if expected is not None and actual != expected:
            raise FileTransferError(
                f"文件校验失败（{spec.checksum}）：期望 {expected.hex().upper()}，"
                f"实际 {actual.hex().upper()}")

    path = _resolve_transfer_path(spec.file, base_dir)
    _atomic_write(path, buf)
    return FileTransferStats(bytes_received=len(buf), chunks=chunks, path=str(path),
                             checksum_kind=spec.checksum)
