"""串口 Mock 设备（pyserial 兼容的伪串口，无硬件模拟）。

用途：在没有真实串口/USB 设备时，用 JSON 应答脚本驱动串口与 Modbus 代码路径，
实现“真机 / Mock 等价”的回归测试。

应答脚本格式（`config/mock/*.json`）与 CAN Mock 相同，只是串口按**字节**匹配，
忽略 `request_id`/`ext`：:

    {
      "default_timeout": 0.2,
      "rules": [
        {
          "request_data": "01 03 00 00 00 02",   // 命中后把以下字节压入接收队列
          "response_data": "01 03 04 00 0B 00 0C",
          "delay": 0.0
        },
        {
          "request_data": "AA 01 01",
          "response_computed": { "bytes": "AA 01 01" }
        },
        { "response_data": "AA 55" }             // 无 request_data：匹配任意请求
      ]
    }

`MockSerialPort` 实现 pyserial 常用子集（`timeout`/`write`/`read`/`in_waiting`/
`flush`/`close`），可直接注入 `SerialCommunication` / `ModbusCommunication`。
"""

from __future__ import annotations

import json
import queue
import time
from pathlib import Path
from typing import Any

from ..errors import CommunicationError


def parse_data(value: Any) -> bytes:
    """把 Mock 规则里的数据字段解析为字节：列表、整数或空格分隔十六进制串。"""
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, (list, tuple)):
        return bytes(int(b) for b in value)
    return bytes(int(tok, 16) for tok in str(value).split())


class MockSerialPort:
    """行为近似 pyserial.Serial 的内存伪串口。"""

    def __init__(self, rules: list[dict] | None = None,
                 default_timeout: float = 0.0):
        self._rules = list(rules or [])
        self.default_timeout = float(default_timeout)
        self.timeout = float(default_timeout)
        self._rx: queue.Queue[bytes] = queue.Queue()
        self._buf = bytearray()
        self.is_open = False
        self.written: list[bytes] = []          # 记录写出的字节，供断言

    # ------------------------------------------------------------ 构造
    @classmethod
    def from_file(cls, path: str | Path) -> "MockSerialPort":
        p = Path(path)
        if not p.exists():
            raise CommunicationError(f"串口 Mock 应答脚本不存在: {p}")
        with open(p, "r", encoding="utf-8") as f:
            doc = json.load(f)
        return cls(rules=list(doc.get("rules", [])),
                   default_timeout=float(doc.get("default_timeout", 0.0)))

    # ------------------------------------------------------------ 生命周期
    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False
        self._buf.clear()
        self._rx = queue.Queue()

    def reset_input_buffer(self) -> None:
        self._buf.clear()
        while True:
            try:
                self._rx.get_nowait()
            except queue.Empty:
                break

    def flush(self) -> None:
        pass

    # ------------------------------------------------------------ 队列注入
    def inject(self, data: bytes) -> None:
        """模拟设备主动上报：直接把字节放入接收队列。"""
        self._rx.put(bytes(data))

    # ------------------------------------------------------------ 收发
    def write(self, data: bytes) -> int:
        data = bytes(data)
        self.written.append(data)
        rule = self._match(data)
        if rule is None:
            return len(data)                     # 无规则 -> 设备不应答 -> 上层超时
        delay = float(rule.get("delay", 0.0))
        if delay > 0:
            time.sleep(delay)
        resp = self._build_response(rule)
        if resp:
            self._rx.put(resp)
        return len(data)

    @property
    def in_waiting(self) -> int:
        return len(self._buf)

    def read(self, size: int = 1) -> bytes:
        """近似 pyserial.read(size)：最多等 self.timeout 秒，返回已到达的字节。"""
        if size <= 0:
            return b""
        deadline = time.monotonic() + max(0.0, self.timeout)
        out = bytearray()
        while len(out) < size:
            if self._buf:
                take = min(size - len(out), len(self._buf))
                out += self._buf[:take]
                del self._buf[:take]
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    self._buf += self._rx.get_nowait()
                except queue.Empty:
                    break
                continue
            try:
                self._buf += self._rx.get(timeout=remaining)
            except queue.Empty:
                break
        return bytes(out)

    # ------------------------------------------------------------ 内部
    def _match(self, data: bytes) -> dict | None:
        for rule in self._rules:
            if rule.get("request_data") is not None:
                if parse_data(rule["request_data"]) != data:
                    continue
            return rule
        return None

    def _build_response(self, rule: dict) -> bytes:
        if "response_computed" in rule:
            return parse_data(rule["response_computed"].get("bytes", ""))
        if "response_data" in rule:
            return parse_data(rule["response_data"])
        return b""
