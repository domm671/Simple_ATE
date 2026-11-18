"""Mock 通信：按应答脚本（JSON）对请求帧返回预设应答。

应答脚本格式（config/mock/*.json）::

    {
      "default_timeout": 0.5,
      "rules": [
        {
          "request_id": "0x18FF50E5",
          "request_data": "01 00",        // 可选；缺省表示只按 ID 匹配
          "ext": true,
          "delay": 0.0,                   // 可选，应答延迟秒数
          "response_id": "0x18FF50E6",
          "response_data": "01 00"
        },
        {
          "request_id": "0x18FF50E5",
          "request_data": "02 01 00",
          "response_computed": {
            "id": "0x18FF50E6",
            // 小端多字节：电压 12.03V @0.01V/LSB -> 1203 = 0x04B3
            "bytes": "B3 04 00 00 00 00 00 00"
          }
        }
      ]
    }

规则：
- send() 按 (id, data) 找到规则，把应答帧放入队列；找不到规则不产生应答（模拟超时）。
- recv() 从队列取帧，模拟 timeout 与 delay。
"""

from __future__ import annotations

import heapq
import itertools
import json
import math
import queue
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Generic, TypeVar

from ..errors import CommunicationError, TimeoutAteError
from .base import Frame

_T = TypeVar("_T")


class _ScheduledResponses(Generic[_T]):
    """应答延迟发生在接收侧，发送后仍可按 timeout 等待或停止。"""

    def __init__(self) -> None:
        self._pending: list[tuple[float, int, _T]] = []
        self._order = itertools.count()
        self._ready = threading.Condition()

    def put(self, value: _T, delay: float = 0.0) -> None:
        if not math.isfinite(delay) or delay < 0:
            raise CommunicationError("Mock 应答 delay 必须是有限非负数")
        with self._ready:
            heapq.heappush(self._pending, (time.monotonic() + delay, next(self._order), value))
            self._ready.notify_all()

    def get(self, timeout: float) -> _T:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._ready:
            while True:
                now = time.monotonic()
                wake_at = deadline
                if self._pending:
                    due = self._pending[0][0]
                    if due <= min(now, deadline):
                        return heapq.heappop(self._pending)[2]
                    wake_at = min(wake_at, due)
                if wake_at <= now:
                    raise queue.Empty
                self._ready.wait(wake_at - now)


def load_rules(path: str | Path, default_timeout: float) -> tuple[list[dict], float]:
    try:
        with open(path, "r", encoding="utf-8") as file:
            doc = json.load(file)
        rules = doc.get("rules", [])
        timeout = float(doc.get("default_timeout", default_timeout))
        if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
            raise ValueError("rules 必须是对象数组")
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("default_timeout 必须是有限非负数")
        return rules, timeout
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise CommunicationError(f"读取 Mock 应答脚本 {path} 失败: {exc}") from exc


def _parse_id(value) -> int:
    if isinstance(value, int):
        return value
    v = str(value).strip().lower()
    return int(v, 16) if v.startswith("0x") else int(v)


def parse_data(value) -> bytes:
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, (list, tuple)):
        return bytes(int(b) for b in value)
    return bytes(int(tok, 16) for tok in str(value).split())


class MockCommunication:
    """供无硬件开发/单元测试使用。"""

    def __init__(self, name: str, script_path: str = "", default_timeout: float = 0.5):
        self.name = name
        self.script_path = script_path
        self.default_timeout = default_timeout
        self._rules: list[dict] = []
        self._rx = _ScheduledResponses[Frame]()
        self._opened = False

    # ------------------------------------------------------------ 生命周期
    def open(self) -> None:
        if self.script_path:
            self._rules, self.default_timeout = load_rules(self.script_path, self.default_timeout)
        self._opened = True

    def close(self) -> None:
        self._opened = False
        self._rx = _ScheduledResponses[Frame]()

    # ------------------------------------------------------------ 测试辅助
    def inject(self, frame: Frame) -> None:
        """直接注入一帧（模拟设备主动上报）。"""
        self._rx.put(frame)

    # ------------------------------------------------------------ 收发
    def send(self, frame: Frame) -> None:
        if not self._opened:
            raise CommunicationError(f"资源 {self.name} 未打开")
        try:
            rule = self._match(frame)
            if rule is None:
                return
            resp = self._build_response(rule)
            if resp is not None:
                self._rx.put(resp, float(rule.get("delay", 0.0)))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise CommunicationError(f"[{self.name}] Mock 应答规则无效: {exc}") from exc

    def recv(self, timeout: float | None = None) -> Frame:
        if not self._opened:
            raise CommunicationError(f"资源 {self.name} 未打开")
        timeout = self.default_timeout if timeout is None else timeout
        try:
            frame = self._rx.get(timeout=timeout)
            return frame if frame.timestamp else replace(frame, timestamp=time.time())
        except queue.Empty as exc:
            raise TimeoutAteError(f"[{self.name}] 等待应答超时（{timeout}s）") from exc

    # ------------------------------------------------------------ 内部
    def _match(self, frame: Frame) -> dict | None:
        for rule in self._rules:
            if _parse_id(rule["request_id"]) != frame.id:
                continue
            if "request_data" in rule and rule["request_data"] is not None:
                if parse_data(rule["request_data"]) != frame.data:
                    continue
            if "ext" in rule and bool(rule["ext"]) != frame.ext:
                continue
            return rule
        return None

    def _build_response(self, rule: dict) -> Frame | None:
        if "response_computed" in rule:
            spec = rule["response_computed"]
        else:
            if "response_id" not in rule:
                return None
            spec = {"id": rule["response_id"], "bytes": rule.get("response_data", "")}
        return Frame(
            id=_parse_id(spec["id"]),
            data=parse_data(spec.get("bytes", "")),
            ext=bool(spec.get("ext", rule.get("ext", False))),
        )
