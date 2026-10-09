"""Modbus 通信（RTU / ASCII 串口，或 TCP）。

脚本层映射约定（与内建 `<send>/<wait>` 原语配合）：

- `<send id="<unit>" data="<function code + 数据>"/>`：`id` 即从站地址，
  `data` 为 Modbus PDU（功能码 + 数据域），本层负责加 CRC/LRC/MBAP 后发送；
- `<wait id="<unit>">`：收到完整应答并校验后，把 **PDU**（功能码 + 数据域）
  作为统一 `Frame.data` 返回，`Frame.id` 为从站地址，字段提取即可沿用 `<field>`。

pyserial 仅在 RTU/ASCII 模式下按需导入；TCP 使用标准库 socket。
"""

from __future__ import annotations

import socket
import time

from ..errors import CommunicationError, TimeoutAteError
from .base import Frame

_PARITY_MAP = {"none": "N", "even": "E", "odd": "O", "mark": "M", "space": "S"}


# ---------------------------------------------------------------- 纯函数
def crc16_modbus(data: bytes) -> bytes:
    """Modbus RTU 的 CRC16（多项式 0xA001），小端返回。"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def lrc(data: bytes) -> int:
    """Modbus ASCII 的 LRC（二进制补码）。"""
    return (-sum(data)) & 0xFF


def build_rtu_adu(unit: int, pdu: bytes) -> bytes:
    body = bytes([unit]) + pdu
    return body + crc16_modbus(body)


def parse_rtu_adu(adu: bytes) -> tuple[int, bytes]:
    if len(adu) < 5:
        raise CommunicationError(f"Modbus RTU 应答长度不足: {adu.hex().upper()}")
    body, crc = adu[:-2], adu[-2:]
    if crc != crc16_modbus(body):
        raise CommunicationError(
            f"Modbus RTU CRC 校验失败: {adu.hex().upper()}")
    return body[0], body[1:]


def build_ascii_adu(unit: int, pdu: bytes) -> bytes:
    body = bytes([unit]) + pdu
    body += bytes([lrc(body)])
    return b":" + body.hex().upper().encode("ascii") + b"\r\n"


def parse_ascii_adu(adu: bytes) -> tuple[int, bytes]:
    text = adu.strip()
    if text.startswith(b":"):
        text = text[1:]
    try:
        raw = bytes.fromhex(text.decode("ascii"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CommunicationError(
            f"Modbus ASCII 应答格式非法: {adu!r}") from exc
    if len(raw) < 3:
        raise CommunicationError(f"Modbus ASCII 应答长度不足: {adu!r}")
    body, got_lrc = raw[:-1], raw[-1]
    if got_lrc != lrc(body):
        raise CommunicationError(f"Modbus ASCII LRC 校验失败: {adu!r}")
    return body[0], body[1:]


def build_tcp_adu(txid: int, unit: int, pdu: bytes) -> bytes:
    payload = bytes([unit]) + pdu
    header = (txid & 0xFFFF).to_bytes(2, "big") + b"\x00\x00" \
        + len(payload).to_bytes(2, "big")
    return header + payload


def parse_tcp_adu(adu: bytes) -> tuple[int, int, bytes]:
    if len(adu) < 8:
        raise CommunicationError(f"Modbus TCP 应答长度不足: {adu.hex().upper()}")
    txid = int.from_bytes(adu[0:2], "big")
    length = int.from_bytes(adu[4:6], "big")
    if length < 2 or len(adu) < 6 + length:
        raise CommunicationError(f"Modbus TCP 长度字段非法: {adu.hex().upper()}")
    unit = adu[6]
    pdu = adu[7:6 + length]
    return txid, unit, pdu


class ModbusCommunication:
    """Modbus 主站。串口（RTU/ASCII）或 TCP。"""

    def __init__(self, name: str, modbus_mode: str = "rtu", port: str = "",
                 baudrate: int = 9600, bytesize: int = 8, parity: str = "none",
                 stopbits: float = 1.0, flowcontrol: str = "none",
                 read_timeout: float = 0.1, frame_gap: float = 0.02,
                 max_frame: int = 256, host: str = "", tcp_port: int = 502,
                 unit: int = 1, **options):
        self.name = name
        self.mode = (modbus_mode or "rtu").lower()
        self.port = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.flowcontrol = flowcontrol
        self.read_timeout = read_timeout
        self.frame_gap = frame_gap
        self.max_frame = max_frame
        self.host = host
        self.tcp_port = tcp_port
        self.unit = unit
        self._ser = None
        self._sock: socket.socket | None = None
        self._txid = 0

    # ------------------------------------------------------------ 生命周期
    def open(self) -> None:
        if self.mode == "tcp":
            if not self.host:
                raise CommunicationError(f"[{self.name}] Modbus TCP 需要 host")
            try:
                self._sock = socket.create_connection(
                    (self.host, self.tcp_port), timeout=self.read_timeout)
            except OSError as exc:
                raise CommunicationError(
                    f"[{self.name}] 连接 Modbus TCP {self.host}:{self.tcp_port} "
                    f"失败: {exc}") from exc
            return
        try:
            import serial  # type: ignore
        except ImportError as exc:
            raise CommunicationError(
                "未安装 pyserial，无法使用串口 Modbus；请执行 pip install -e .[serial]"
            ) from exc
        if not self.port:
            raise CommunicationError(f"[{self.name}] Modbus 串口未指定 port")
        try:
            self._ser = serial.Serial(
                port=self.port, baudrate=self.baudrate, bytesize=self.bytesize,
                parity=_PARITY_MAP.get(self.parity, "N"), stopbits=self.stopbits,
                timeout=self.read_timeout, write_timeout=self.read_timeout,
                xonxoff=(self.flowcontrol == "xonxoff"),
                rtscts=(self.flowcontrol == "rtscts"),
                dsrdtr=(self.flowcontrol == "dsrdtr"),
            )
        except Exception as exc:  # noqa: BLE001
            raise CommunicationError(
                f"[{self.name}] 打开 Modbus 串口 {self.port} 失败: {exc}") from exc

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    # ------------------------------------------------------------ 收发
    def send(self, frame: Frame) -> None:
        unit = frame.id if frame.id else self.unit
        pdu = bytes(frame.data)
        try:
            if self.mode == "tcp":
                self._txid = (self._txid + 1) & 0xFFFF
                self._sock_send(build_tcp_adu(self._txid, unit, pdu))
            elif self.mode == "ascii":
                self._ser.write(build_ascii_adu(unit, pdu))
                self._ser.flush()
            else:
                self._ser.write(build_rtu_adu(unit, pdu))
                self._ser.flush()
        except (CommunicationError, OSError) as exc:
            if isinstance(exc, CommunicationError):
                raise
            raise CommunicationError(f"[{self.name}] Modbus 发送失败: {exc}") from exc

    def recv(self, timeout: float) -> Frame:
        try:
            if self.mode == "tcp":
                unit, pdu = self._recv_tcp(timeout)
            elif self.mode == "ascii":
                unit, pdu = self._recv_ascii(timeout)
            else:
                unit, pdu = self._recv_rtu(timeout)
        except OSError as exc:
            raise CommunicationError(f"[{self.name}] Modbus 接收失败: {exc}") from exc
        return Frame(id=unit, data=pdu, ext=False, timestamp=time.time())

    # ------------------------------------------------------------ 内部：串口
    def _read_exact(self, n: int, timeout: float) -> bytes:
        deadline = time.monotonic() + max(0.0, timeout)
        buf = bytearray()
        while len(buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutAteError(
                    f"[{self.name}] Modbus 读取超时，已收 {len(buf)}/{n} 字节")
            self._ser.timeout = remaining
            chunk = self._ser.read(n - len(buf))
            if not chunk:
                raise TimeoutAteError(
                    f"[{self.name}] Modbus 读取超时，已收 {len(buf)}/{n} 字节")
            buf += chunk
        return bytes(buf)

    def _recv_rtu(self, timeout: float) -> tuple[int, bytes]:
        head = self._read_exact(3, timeout)
        func = head[1]
        if func & 0x80:                       # 异常应答：unit+func+code+crc
            rest = self._read_exact(2, timeout)
        elif func in (1, 2, 3, 4):            # 读类：unit+func+bytecount+data+crc
            rest = self._read_exact(head[2] + 2, timeout)
        else:                                  # 写类等：固定 8 字节
            rest = self._read_exact(5, timeout)
        return parse_rtu_adu(head + rest)

    def _recv_ascii(self, timeout: float) -> tuple[int, bytes]:
        deadline = time.monotonic() + max(0.0, timeout)
        buf = bytearray()
        while b"\r\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutAteError(f"[{self.name}] Modbus ASCII 读取超时")
            self._ser.timeout = remaining
            chunk = self._ser.read(1)
            if not chunk:
                raise TimeoutAteError(f"[{self.name}] Modbus ASCII 读取超时")
            buf += chunk
            if len(buf) > self.max_frame * 2 + 8:
                raise CommunicationError(f"[{self.name}] Modbus ASCII 帧过长")
        return parse_ascii_adu(bytes(buf))

    # ------------------------------------------------------------ 内部：TCP
    def _sock_send(self, data: bytes) -> None:
        if self._sock is None:
            raise CommunicationError(f"[{self.name}] Modbus TCP 未连接")
        self._sock.sendall(data)

    def _sock_read_exact(self, n: int, timeout: float) -> bytes:
        if self._sock is None:
            raise CommunicationError(f"[{self.name}] Modbus TCP 未连接")
        deadline = time.monotonic() + max(0.0, timeout)
        buf = bytearray()
        while len(buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutAteError(
                    f"[{self.name}] Modbus TCP 读取超时，已收 {len(buf)}/{n} 字节")
            self._sock.settimeout(remaining)
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise CommunicationError(f"[{self.name}] Modbus TCP 连接关闭")
            buf += chunk
        return bytes(buf)

    def _recv_tcp(self, timeout: float) -> tuple[int, bytes]:
        head = self._sock_read_exact(7, timeout)
        length = int.from_bytes(head[4:6], "big")
        body = self._sock_read_exact(max(0, length - 1), timeout)
        _, unit, pdu = parse_tcp_adu(head + body)
        return unit, pdu
