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
from typing import Any, Callable

from ..errors import CommunicationError, TimeoutAteError
from .base import Frame
from .serial import build_serial_kwargs, default_serial_factory

MAX_PDU = 253

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
    if len(adu) < 4:
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
    if not adu.startswith(b":") or not adu.endswith(b"\r\n"):
        raise CommunicationError(f"Modbus ASCII 应答缺少起止标记: {adu!r}")
    text = adu[1:-2]
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
    if adu[2:4] != b"\x00\x00":
        raise CommunicationError("Modbus TCP 协议号必须为 0")
    if not 2 <= length <= 1 + MAX_PDU or len(adu) != 6 + length:
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
                 unit: int = 1, mock_script: str = "",
                 serial_factory: Callable[..., Any] | None = None,
                 socket_factory: Callable[..., Any] | None = None,
                 connect_timeout: float | None = None):
        self.name = name
        self.mode = (modbus_mode or "rtu").lower()
        self.port = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.flowcontrol = flowcontrol
        self.read_timeout = read_timeout
        self.connect_timeout = read_timeout if connect_timeout is None else connect_timeout
        self.frame_gap = frame_gap
        self.max_frame = max_frame
        self.host = host
        self.tcp_port = tcp_port
        self.unit = unit
        self.mock_script = mock_script
        self.serial_factory = serial_factory
        self.socket_factory = socket_factory
        self._ser = None
        self._sock: socket.socket | None = None
        self._txid = 0
        self._tcp_unit: int | None = None
        self._rx = bytearray()

    # ------------------------------------------------------------ 生命周期
    def open(self) -> None:
        if self.mode not in ("rtu", "ascii", "tcp"):
            raise CommunicationError(f"[{self.name}] 未知 Modbus 模式: {self.mode}")
        if self.mode == "tcp":
            if not self.host:
                raise CommunicationError(f"[{self.name}] Modbus TCP 需要 host")
            factory = self.socket_factory or socket.create_connection
            try:
                self._sock = factory(
                    (self.host, self.tcp_port), timeout=self.connect_timeout)
            except OSError as exc:
                raise CommunicationError(
                    f"[{self.name}] 连接 Modbus TCP {self.host}:{self.tcp_port} "
                    f"失败: {exc}") from exc
            return
        # 串口 RTU/ASCII：支持 JSON Mock 或注入工厂
        if self.mock_script:
            from .mock_serial import MockSerialPort
            port = MockSerialPort.from_file(self.mock_script)
            port.open()
            self._ser = port
            return
        if not self.port:
            raise CommunicationError(f"[{self.name}] Modbus 串口未指定 port")
        factory = self.serial_factory or default_serial_factory
        kwargs = build_serial_kwargs(self.port, self.baudrate, self.bytesize,
                                     self.parity, self.stopbits, self.flowcontrol,
                                     self.read_timeout)
        try:
            self._ser = factory(**kwargs)
        except CommunicationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CommunicationError(
                f"[{self.name}] 打开 Modbus 串口 {self.port} 失败: {exc}") from exc

    def close(self) -> None:
        self._rx.clear()
        self._tcp_unit = None
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
        if not 0 <= unit <= (255 if self.mode == "tcp" else 247):
            raise CommunicationError(f"[{self.name}] Modbus 单元地址无效: {unit}")
        if not 1 <= len(pdu) <= MAX_PDU:
            raise CommunicationError(f"[{self.name}] Modbus PDU 长度须为 1–{MAX_PDU} 字节")
        if self.mode != "tcp" and self._ser is None:
            raise CommunicationError(f"[{self.name}] Modbus 串口未打开")
        try:
            if self.mode == "tcp":
                self._txid = (self._txid + 1) & 0xFFFF
                self._sock_send(build_tcp_adu(self._txid, unit, pdu))
                self._tcp_unit = unit
            else:
                adu = (build_ascii_adu(unit, pdu) if self.mode == "ascii"
                       else build_rtu_adu(unit, pdu))
                if self._ser.write(adu) != len(adu):
                    raise CommunicationError(f"[{self.name}] Modbus 串口写入不完整")
                self._ser.flush()
        except OSError as exc:
            raise CommunicationError(f"[{self.name}] Modbus 发送失败: {exc}") from exc

    def recv(self, timeout: float) -> Frame:
        if self.mode != "tcp" and self._ser is None:
            raise CommunicationError(f"[{self.name}] Modbus 串口未打开")
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            if self.mode == "tcp":
                unit, pdu = self._recv_tcp(deadline, nonblocking=timeout <= 0)
            elif self.mode == "ascii":
                unit, pdu = self._recv_ascii(deadline)
            else:
                unit, pdu = self._recv_rtu(deadline)
        except OSError as exc:
            raise CommunicationError(f"[{self.name}] Modbus 接收失败: {exc}") from exc
        return Frame(id=unit, data=pdu, ext=False, timestamp=time.time())

    def discard_input(self) -> bool:
        if self.mode == "tcp":
            return False
        if self._ser is None:
            raise CommunicationError(f"[{self.name}] Modbus 串口未打开")
        try:
            self._ser.reset_input_buffer()
        except OSError as exc:
            raise CommunicationError(f"[{self.name}] 清空 Modbus 串口缓冲失败: {exc}") from exc
        self._rx.clear()
        return True

    # ------------------------------------------------------------ 内部：串口
    def _read_serial(self, n: int, deadline: float) -> None:
        while len(self._rx) < n:
            self._ser.timeout = max(0.0, deadline - time.monotonic())
            chunk = self._ser.read(n - len(self._rx))
            if not chunk:
                raise TimeoutAteError(f"[{self.name}] Modbus 串口读取超时")
            self._rx += chunk

    def _take(self, size: int) -> bytes:
        data = bytes(self._rx[:size])
        del self._rx[:size]
        return data

    def _recv_rtu(self, deadline: float) -> tuple[int, bytes]:
        self._read_serial(3, deadline)
        func = self._rx[1]
        if func & 0x80:                       # 异常应答：unit+func+code+crc
            size = 5
        elif func in (1, 2, 3, 4, 12, 17, 20, 21, 23):
            size = self._rx[2] + 5
        elif func in (5, 6, 11, 15, 16):
            size = 8
        elif func == 7:
            size = 5
        elif func == 22:
            size = 10
        elif func == 24:
            self._read_serial(4, deadline)
            size = int.from_bytes(self._rx[2:4], "big") + 6
        else:
            # 未知功能码按 RTU 静默间隔分帧。
            while len(self._rx) <= min(self.max_frame, MAX_PDU + 3):
                gap_end = time.monotonic() + self.frame_gap
                try:
                    self._read_serial(len(self._rx) + 1, min(deadline, gap_end))
                except TimeoutAteError:
                    if time.monotonic() < gap_end:
                        raise
                    break
            size = len(self._rx)
        if size > min(self.max_frame, MAX_PDU + 3):
            self._rx.clear()
            raise CommunicationError(f"[{self.name}] Modbus RTU 帧过长")
        self._read_serial(size, deadline)
        return parse_rtu_adu(self._take(size))

    def _recv_ascii(self, deadline: float) -> tuple[int, bytes]:
        while not self._rx.endswith(b"\r\n"):
            self._read_serial(len(self._rx) + 1, deadline)
            if self._rx[-1] == ord(":"):
                self._rx[:] = b":"
            if len(self._rx) > min(self.max_frame, MAX_PDU + 2) * 2 + 3:
                self._rx.clear()
                raise CommunicationError(f"[{self.name}] Modbus ASCII 帧过长")
        return parse_ascii_adu(self._take(len(self._rx)))

    # ------------------------------------------------------------ 内部：TCP
    def _sock_send(self, data: bytes) -> None:
        if self._sock is None:
            raise CommunicationError(f"[{self.name}] Modbus TCP 未连接")
        self._sock.settimeout(self.read_timeout)
        self._sock.sendall(data)

    def _read_tcp(self, n: int, deadline: float) -> None:
        if self._sock is None:
            raise CommunicationError(f"[{self.name}] Modbus TCP 未连接")
        while len(self._rx) < n:
            self._sock.settimeout(max(0.0, deadline - time.monotonic()))
            try:
                chunk = self._sock.recv(n - len(self._rx))
            except (TimeoutError, BlockingIOError) as exc:
                raise TimeoutAteError(f"[{self.name}] Modbus TCP 读取超时") from exc
            if not chunk:
                raise CommunicationError(f"[{self.name}] Modbus TCP 连接关闭")
            self._rx += chunk

    def _recv_tcp(self, deadline: float, nonblocking: bool) -> tuple[int, bytes]:
        while True:
            self._read_tcp(7, deadline)
            length = int.from_bytes(self._rx[4:6], "big")
            if not 2 <= length <= 1 + MAX_PDU:
                self.close()
                raise CommunicationError(f"[{self.name}] Modbus TCP 长度字段无效: {length}")
            self._read_tcp(6 + length, deadline)
            txid, unit, pdu = parse_tcp_adu(self._take(6 + length))
            if (nonblocking or self._tcp_unit is None
                    or (txid == self._txid and unit == self._tcp_unit)):
                return unit, pdu
            if time.monotonic() >= deadline:
                raise TimeoutAteError(f"[{self.name}] Modbus TCP 未收到当前事务的应答")
