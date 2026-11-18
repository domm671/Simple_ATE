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
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..communication.base import Communication, Frame
from ..communication.modbus import crc16_modbus
from ..errors import (
    CommunicationError,
    FieldExtractError,
    FileTransferError,
    JudgeTypeError,
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
        return crc16_modbus(data)
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

    按 gain/bias 的十进制小数位数做舍入，保留小于 1e-9 的标定量。
    """
    import decimal

    def decimals(x: float) -> int:
        d = decimal.Decimal(repr(x))
        return max(0, -d.as_tuple().exponent)

    ndigits = max(decimals(gain), decimals(bias))
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
            should_cancel=None, on_frame=None) -> tuple[Frame, dict[str, int | float]]:
    """等待匹配帧并提取字段。

    返回 (命中帧, 新字段字典)。超时抛 TimeoutAteError(E301)。
    drain（清空残留帧）在发送前由执行器调用，不在此处，
    以免把同步实现（mock）中 send 即入队的应答误删。
    should_cancel: 可选的零参回调，返回 True 时提前中止等待（抛 TimeoutAteError）。
    """
    timeout = spec.timeout if spec.timeout is not None else default_timeout
    deadline = time.monotonic() + timeout
    while True:
        if should_cancel is not None and should_cancel():
            raise TimeoutAteError("等待被外部停止请求中断")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutAteError(
                f"等待应答 0x{spec.id:X}{'/' + hex(spec.id_mask) if spec.id_mask is not None else ''} 超时（{timeout}s）")
        try:
            frame = comm.recv(min(0.05, remaining))
        except TimeoutAteError:
            continue
        if on_frame is not None:
            on_frame(frame)
        if frame.ext == spec.ext and _id_matches(frame, spec) and len(frame.data) >= spec.min_len:
            extracted: dict[str, int | float] = {}
            for fld in spec.fields:
                extracted[fld.var] = extract_field(frame, fld)
            variables.update(extracted)
            return frame, extracted


def drain(comm: Communication, should_cancel=None, timeout: float = 5.0) -> None:
    """排空接收缓冲中的残留帧（应在发送请求帧之前调用）。"""
    if should_cancel is not None and should_cancel():
        raise TimeoutAteError("清空接收缓冲被外部停止请求中断")
    discard = getattr(comm, "discard_input", None)
    if discard is not None and discard():
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if should_cancel is not None and should_cancel():
            raise TimeoutAteError("清空接收缓冲被外部停止请求中断")
        try:
            comm.recv(0.0)
        except TimeoutAteError:
            return
    raise CommunicationError(f"[{comm.name}] 接收缓冲在 {timeout}s 内未排空")


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
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _id_matches(frame: Frame, spec: WaitSpec) -> bool:
    if spec.id_mask is None:
        return frame.id == spec.id
    return (frame.id & spec.id_mask) == (spec.id & spec.id_mask)


def _normalize_checksum_value(value: Any, kind: str) -> bytes:
    """把 checksum_value（十六进制字符串 / 整数）归一化为字节串。"""
    length = {"crc32": 4, "crc16_modbus": 2, "sum8": 1}[kind]
    try:
        if isinstance(value, int) and not isinstance(value, bool):
            return value.to_bytes(length, "big")
        if isinstance(value, str):
            text = value.strip().replace("0x", "").replace("0X", "")
            result = bytes.fromhex(text)
            if len(result) == length:
                return result
    except (ValueError, OverflowError) as exc:
        raise FileTransferError(f"非法 checksum_value: {value!r}") from exc
    raise FileTransferError(f"非法 checksum_value: {value!r}，需为 {length} 字节")


def iter_send_file(spec: SendSpec, comm: Communication, base_dir: str | Path,
                   should_cancel=None):
    """把本地文件分块逐个发出：每发一帧 yield 一帧，供上层记 trace/日志。"""
    path = _resolve_transfer_path(spec.file, base_dir)
    chunk_size = max(1, spec.chunk_size)
    header = bytes(spec.header)
    seq = 0
    try:
        with open(path, "rb") as f:
            max_chunks = 1 << (8 * spec.seq_len) if spec.seq_len else None
            if max_chunks is not None and os.fstat(f.fileno()).st_size > max_chunks * chunk_size:
                raise FileTransferError(f"{path} 的分块数量超过序号容量 {max_chunks}")
            while should_cancel is None or not should_cancel():
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                if max_chunks is not None and seq >= max_chunks:
                    raise FileTransferError(f"{path} 的分块数量超过序号容量 {max_chunks}")
                sequence = seq.to_bytes(spec.seq_len, "little") if spec.seq_len else b""
                frame = Frame(id=spec.id, data=header + sequence + chunk, ext=spec.ext)
                comm.send(frame)
                yield frame
                seq += 1
                if spec.interval > 0:
                    _sleep_interruptible(spec.interval, should_cancel)
    except OSError as exc:
        raise FileTransferError(f"读取待发送文件 {path} 失败: {exc}") from exc


def do_wait_file(spec: WaitSpec, comm: Communication, variables: dict,
                 base_dir: str | Path, default_timeout: float,
                 should_cancel=None, on_frame=None) -> FileTransferStats:
    """接收多帧并拼装为文件。

    终止条件：收到 `size` 字节 / `chunks` 帧 / 静默 `idle_gap` 秒。
    整体超时前必须达到终止条件，否则抛 E301 并保留原文件。
    """
    timeout = spec.timeout if spec.timeout is not None else default_timeout
    header = bytes(spec.header)
    expected: bytes | None = None
    if spec.checksum != "none" and spec.checksum_value is not None:
        try:
            expected = _normalize_checksum_value(
                resolve_value(spec.checksum_value, variables), spec.checksum)
        except JudgeTypeError as exc:
            raise FileTransferError(exc.message) from exc

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
        remaining = start + timeout - now
        if remaining <= 0:
            raise TimeoutAteError(f"接收文件超时（{timeout}s），已收 {total} 字节 / {chunks} 帧")
        if spec.idle_gap is not None and chunks:
            remaining = min(remaining, last + spec.idle_gap - now)
        try:
            frame = comm.recv(min(0.05, remaining))
        except TimeoutAteError:
            continue
        if on_frame is not None:
            on_frame(frame)
        if frame.ext != spec.ext or not _id_matches(frame, spec) or len(frame.data) < spec.min_len:
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
            continue
        if seq is not None:
            if seq in ordered:
                if ordered[seq] != data:
                    raise FileTransferError(f"接收文件的序号 {seq} 出现不同数据")
                continue
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
        if ordered and max(ordered) != chunks - 1:
            raise FileTransferError("接收文件的分块序号不连续或未从 0 开始")
        buf = b"".join(ordered[k] for k in range(chunks))
    else:
        buf = b"".join(appended)
    if spec.size is not None and len(buf) > spec.size:
        buf = buf[:spec.size]
    if spec.size is not None and len(buf) != spec.size:
        raise FileTransferError(
            f"接收文件长度 {len(buf)} 字节与期望 size={spec.size} 不符")
    if expected is not None:
        actual = compute_checksum(buf, spec.checksum)
        if actual != expected:
            raise FileTransferError(
                f"文件校验失败（{spec.checksum}）：期望 {expected.hex().upper()}，"
                f"实际 {actual.hex().upper()}")

    path = _resolve_transfer_path(spec.file, base_dir)
    if should_cancel is not None and should_cancel():
        raise TimeoutAteError("文件接收被外部停止请求中断")
    try:
        _atomic_write(path, buf)
    except OSError as exc:
        raise FileTransferError(f"写入接收文件 {path} 失败: {exc}") from exc
    return FileTransferStats(bytes_received=len(buf), chunks=chunks, path=str(path),
                             checksum_kind=spec.checksum)
