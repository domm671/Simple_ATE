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

import json
import queue
import time
from pathlib import Path

from ..errors import CommunicationError, TimeoutAteError
from .base import Frame


def _parse_id(value) -> int:
    if isinstance(value, int):
        return value
    v = str(value).strip().lower()
    return int(v, 16) if v.startswith("0x") else int(v)


def _parse_data(value) -> bytes:
    if value is None:
        return b""
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
        self._rx: queue.Queue[Frame] = queue.Queue()
        self._opened = False

    # ------------------------------------------------------------ 生命周期
    def open(self) -> None:
        if self.script_path:
            path = Path(self.script_path)
            if not path.exists():
                raise CommunicationError(f"Mock 应答脚本不存在: {path}")
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            self.default_timeout = float(doc.get("default_timeout", self.default_timeout))
            self._rules = list(doc.get("rules", []))
        self._opened = True

    def close(self) -> None:
        self._opened = False
        self._rx = queue.Queue()

    # ------------------------------------------------------------ 测试辅助
    def inject(self, frame: Frame) -> None:
        """直接注入一帧（模拟设备主动上报）。"""
        self._rx.put(frame)

    # ------------------------------------------------------------ 收发
    def send(self, frame: Frame) -> None:
        if not self._opened:
            raise CommunicationError(f"资源 {self.name} 未打开")
        rule = self._match(frame)
        if rule is None:
            return                                   # 无规则 -> 设备无应答 -> wait 超时
        delay = float(rule.get("delay", 0.0))
        if delay:
            time.sleep(delay)
        resp = self._build_response(rule)
        if resp is not None:
            self._rx.put(resp)

    def recv(self, timeout: float) -> Frame:
        try:
            return self._rx.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutAteError(f"[{self.name}] 等待应答超时（{timeout}s）") from exc

    # ------------------------------------------------------------ 内部
    def _match(self, frame: Frame) -> dict | None:
        for rule in self._rules:
            if _parse_id(rule["request_id"]) != frame.id:
                continue
            if "request_data" in rule and rule["request_data"] is not None:
                if _parse_data(rule["request_data"]) != frame.data:
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
            data=_parse_data(spec.get("bytes", "")),
            ext=bool(spec.get("ext", rule.get("ext", False))),
            timestamp=time.time(),
        )
