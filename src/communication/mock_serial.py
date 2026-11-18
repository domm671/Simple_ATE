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

import queue
import time
from pathlib import Path

from .mock import _ScheduledResponses, load_rules, parse_data


class MockSerialPort:
    """行为近似 pyserial.Serial 的内存伪串口。"""

    def __init__(self, rules: list[dict] | None = None,
                 default_timeout: float = 0.0):
        self._rules = list(rules or [])
        self.timeout = float(default_timeout)
        self._rx = _ScheduledResponses[bytes]()
        self._buf = bytearray()
        self.is_open = False
        self.written: list[bytes] = []          # 记录写出的字节，供断言

    # ------------------------------------------------------------ 构造
    @classmethod
    def from_file(cls, path: str | Path) -> "MockSerialPort":
        rules, timeout = load_rules(path, 0.0)
        return cls(rules=rules, default_timeout=timeout)

    # ------------------------------------------------------------ 生命周期
    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False
        self._buf.clear()
        self._rx = _ScheduledResponses[bytes]()

    def reset_input_buffer(self) -> None:
        self._buf.clear()
        while True:
            try:
                self._rx.get(0.0)
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
        resp = self._build_response(rule)
        if resp:
            self._rx.put(resp, float(rule.get("delay", 0.0)))
        return len(data)

    @property
    def in_waiting(self) -> int:
        while True:
            try:
                self._buf += self._rx.get(0.0)
            except queue.Empty:
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
            try:
                self._buf += self._rx.get(deadline - time.monotonic())
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
