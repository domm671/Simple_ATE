"""串口通信（基于 pyserial，可选依赖 `[serial]`）。

用于普通 RS232/RS485 串口以及 USB CDC 虚拟串口。内建帧原语把串口数据当作
不定长字节帧处理：以“字节到达后的静默间隔”（`frame_gap`）作为帧边界，
`Frame.id` 固定为 0，`<wait id="0" min_len="..."/>` 即可等待整帧。

更结构化的 Modbus RTU/ASCII/TCP 请使用 `modbus.py`。

本模块延迟导入 pyserial；未安装时给出明确中文提示。

为便于无硬件回归，`SerialCommunication` 支持注入 `serial_factory`，
或通过 `mock_script` 指向 JSON 应答脚本，直接使用 pyserial 兼容的
`MockSerialPort`（见 `mock_serial.py`）。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from ..errors import CommunicationError, TimeoutAteError
from .base import Frame

_PARITY_MAP = {"none": "N", "even": "E", "odd": "O", "mark": "M", "space": "S"}


def default_serial_factory(**kwargs: Any):
    """默认串口工厂：延迟导入 pyserial 并创建 `serial.Serial`。

    抽成工厂以便测试注入 `MockSerialPort`，无需修改生产代码。
    """
    try:
        import serial  # type: ignore
    except ImportError as exc:
        raise CommunicationError(
            "未安装 pyserial，无法使用串口资源；请执行 pip install -e .[serial]"
        ) from exc
    return serial.Serial(**kwargs)


def build_serial_kwargs(port: str, baudrate: int, bytesize: int, parity: str,
                        stopbits: float, flowcontrol: str,
                        read_timeout: float) -> dict:
    return {
        "port": port,
        "baudrate": baudrate,
        "bytesize": bytesize,
        "parity": _PARITY_MAP.get(parity, "N"),
        "stopbits": stopbits,
        "timeout": read_timeout,
        "write_timeout": read_timeout,
        "xonxoff": (flowcontrol == "xonxoff"),
        "rtscts": (flowcontrol == "rtscts"),
        "dsrdtr": (flowcontrol == "dsrdtr"),
    }


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
    if vid is None and pid is None and not serial_number:
        return fallback
    matches = []
    for info in list_ports.comports():
        if vid is not None and info.vid != int(vid):
            continue
        if pid is not None and info.pid != int(pid):
            continue
        if serial_number and info.serial_number != serial_number:
            continue
        matches.append(info.device)
    if len(matches) > 1:
        raise CommunicationError(f"USB 筛选条件匹配多个设备: {', '.join(matches)}")
    return matches[0] if matches else fallback


class SerialCommunication:
    """原始串口字节流通信。"""

    def __init__(self, name: str, port: str = "", baudrate: int = 9600,
                 bytesize: int = 8, parity: str = "none", stopbits: float = 1.0,
                 flowcontrol: str = "none", read_timeout: float = 0.02,
                 frame_gap: float = 0.02, max_frame: int = 4096,
                 vid: int | None = None, pid: int | None = None,
                 serial_number: str | None = None, resolve_usb: bool = False,
                 mock_script: str = "",
                 serial_factory: Callable[..., Any] | None = None):
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
        self.mock_script = mock_script
        self.serial_factory = serial_factory
        self._ser = None
        self._rx = bytearray()
        self._last_rx = 0.0

    # ------------------------------------------------------------ 生命周期
    def open(self) -> None:
        # 1) 显式 Mock：无硬件，按 JSON 规则模拟设备
        if self.mock_script:
            from .mock_serial import MockSerialPort
            port = MockSerialPort.from_file(self.mock_script)
            port.open()
            self._ser = port
            return

        # 2) 真实 pyserial（或测试注入的工厂）
        port = self.port
        if self.resolve_usb and not port:
            port = resolve_usb_port(self.vid, self.pid, self.serial_number)
        if not port:
            raise CommunicationError(
                f"[{self.name}] serial/usb 资源未指定 port（或未找到匹配的 USB 设备）")
        factory = self.serial_factory or default_serial_factory
        kwargs = build_serial_kwargs(port, self.baudrate, self.bytesize,
                                     self.parity, self.stopbits, self.flowcontrol,
                                     self.read_timeout)
        try:
            self._ser = factory(**kwargs)
        except CommunicationError:
            raise
        except Exception as exc:  # noqa: BLE001 - pyserial 抛多种异常
            raise CommunicationError(
                f"[{self.name}] 打开串口 {port} 失败: {exc}") from exc

    def close(self) -> None:
        self._rx.clear()
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    # ------------------------------------------------------------ 收发
    def send(self, frame: Frame) -> None:
        self._ensure_open()
        try:
            if self._ser.write(frame.data) != len(frame.data):
                raise OSError("串口写入不完整")
            self._ser.flush()
        except Exception as exc:  # noqa: BLE001
            raise CommunicationError(f"[{self.name}] 串口发送失败: {exc}") from exc

    def recv(self, timeout: float) -> Frame:
        self._ensure_open()
        ser = self._ser
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            while len(self._rx) < self.max_frame:
                now = time.monotonic()
                gap_end = self._last_rx + self.frame_gap
                if self._rx and timeout > 0 and now >= gap_end and not ser.in_waiting:
                    break
                end = min(deadline, gap_end) if self._rx and timeout > 0 else deadline
                ser.timeout = max(0.0, end - now)
                count = min(max(1, ser.in_waiting), self.max_frame - len(self._rx))
                chunk = ser.read(count)
                if not chunk:
                    break
                self._rx += chunk
                self._last_rx = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            raise CommunicationError(f"[{self.name}] 串口读取失败: {exc}") from exc
        if (not self._rx or (timeout > 0 and len(self._rx) < self.max_frame
                            and time.monotonic() - self._last_rx < self.frame_gap)):
            raise TimeoutAteError(f"[{self.name}] 等待串口数据超时（{timeout}s）")
        frame = Frame(id=0, data=bytes(self._rx), ext=False, timestamp=time.time())
        self._rx.clear()
        return frame

    def discard_input(self) -> bool:
        self._ensure_open()
        try:
            self._ser.reset_input_buffer()
        except OSError as exc:
            raise CommunicationError(f"[{self.name}] 清空串口缓冲失败: {exc}") from exc
        self._rx.clear()
        return True

    # ------------------------------------------------------------ 内部
    def _ensure_open(self) -> None:
        if self._ser is None:
            raise CommunicationError(f"[{self.name}] 串口未打开")
