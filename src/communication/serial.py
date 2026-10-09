"""串口通信（基于 pyserial，可选依赖 `[serial]`）。

用于普通 RS232/RS485 串口以及 USB CDC 虚拟串口。内建帧原语把串口数据当作
不定长字节帧处理：以“字节到达后的静默间隔”（`frame_gap`）作为帧边界，
`Frame.id` 固定为 0，`<wait id="0" min_len="..."/>` 即可等待整帧。

更结构化的 Modbus RTU/ASCII/TCP 请使用 `modbus.py`。

本模块延迟导入 pyserial；未安装时给出明确中文提示。
"""

from __future__ import annotations

import time

from ..errors import CommunicationError, TimeoutAteError
from .base import Frame

_PARITY_MAP = {"none": "N", "even": "E", "odd": "O", "mark": "M", "space": "S"}


def resolve_usb_port(vid: int | None = None, pid: int | None = None,
                     serial_number: str | None = None,
                     fallback: str = "") -> str:
    """按 VID/PID/序列号查找 USB 串口设备名；找不到返回 fallback。"""
    try:
        from serial.tools import list_ports  # type: ignore
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise CommunicationError(
            "未安装 pyserial，无法按 USB VID/PID 自动查找串口；"
            "请安装 pyserial 或直接给出 port") from exc
    for info in list_ports.comports():
        if vid is not None and info.vid != int(vid):
            continue
        if pid is not None and info.pid != int(pid):
            continue
        if serial_number and info.serial_number != serial_number:
            continue
        return info.device
    return fallback


class SerialCommunication:
    """原始串口字节流通信。"""

    def __init__(self, name: str, port: str = "", baudrate: int = 9600,
                 bytesize: int = 8, parity: str = "none", stopbits: float = 1.0,
                 flowcontrol: str = "none", read_timeout: float = 0.02,
                 frame_gap: float = 0.02, max_frame: int = 4096,
                 vid: int | None = None, pid: int | None = None,
                 serial_number: str | None = None, resolve_usb: bool = False,
                 **options):
        self.name = name
        self.port = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.flowcontrol = flowcontrol
        self.read_timeout = read_timeout
        self.frame_gap = frame_gap
        self.max_frame = max_frame
        self.vid = vid
        self.pid = pid
        self.serial_number = serial_number
        self.resolve_usb = resolve_usb
        self._ser = None

    # ------------------------------------------------------------ 生命周期
    def open(self) -> None:
        try:
            import serial  # type: ignore
        except ImportError as exc:
            raise CommunicationError(
                "未安装 pyserial，无法使用串口资源；请执行 pip install -e .[serial]"
            ) from exc
        port = self.port
        if self.resolve_usb and not port:
            port = resolve_usb_port(self.vid, self.pid, self.serial_number)
        if not port:
            raise CommunicationError(
                f"[{self.name}] serial/usb 资源未指定 port（或未找到匹配的 USB 设备）")
        try:
            self._ser = serial.Serial(
                port=port, baudrate=self.baudrate, bytesize=self.bytesize,
                parity=_PARITY_MAP.get(self.parity, "N"), stopbits=self.stopbits,
                timeout=self.read_timeout, write_timeout=self.read_timeout,
                xonxoff=(self.flowcontrol == "xonxoff"),
                rtscts=(self.flowcontrol == "rtscts"),
                dsrdtr=(self.flowcontrol == "dsrdtr"),
            )
        except Exception as exc:  # noqa: BLE001 - pyserial 抛多种异常
            raise CommunicationError(
                f"[{self.name}] 打开串口 {port} 失败: {exc}") from exc

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    # ------------------------------------------------------------ 收发
    def send(self, frame: Frame) -> None:
        self._ensure_open()
        try:
            self._ser.write(frame.data)
            self._ser.flush()
        except Exception as exc:  # noqa: BLE001
            raise CommunicationError(f"[{self.name}] 串口发送失败: {exc}") from exc

    def recv(self, timeout: float) -> Frame:
        self._ensure_open()
        ser = self._ser
        ser.timeout = max(0.0, timeout)
        try:
            first = ser.read(1)
        except Exception as exc:  # noqa: BLE001
            raise CommunicationError(f"[{self.name}] 串口读取失败: {exc}") from exc
        if not first:
            raise TimeoutAteError(f"[{self.name}] 等待串口数据超时（{timeout}s）")
        buf = bytearray(first)
        # 读到静默间隔即认为一帧结束；限制单帧最大长度避免异常时无限读取
        ser.timeout = self.frame_gap
        while len(buf) < self.max_frame:
            try:
                waiting = ser.in_waiting
                chunk = ser.read(waiting if waiting > 0 else 1)
            except Exception as exc:  # noqa: BLE001
                raise CommunicationError(f"[{self.name}] 串口读取失败: {exc}") from exc
            if not chunk:
                break
            buf += chunk
        return Frame(id=0, data=bytes(buf), ext=False, timestamp=time.time())

    # ------------------------------------------------------------ 内部
    def _ensure_open(self) -> None:
        if self._ser is None:
            raise CommunicationError(f"[{self.name}] 串口未打开")
