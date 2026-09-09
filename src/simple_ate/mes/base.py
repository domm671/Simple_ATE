"""MES 上传扩展点（M4 实现）。

v0.1 仅定义协议，不实现上传与 Outbox 退避重试。
定制方提供实现类（构造可接收工位配置），方法：

    def upload(self, result: RunResult) -> None: ...

成功返回；失败抛异常，由 Outbox 调度重试（M4）。
"""

from __future__ import annotations

from typing import Protocol

from ..storage.base import RunResult


class MesUploader(Protocol):
    def upload(self, result: RunResult) -> None: ...


class NullUploader:
    """默认空实现：不上传，结果直接归档 uploaded。"""

    def upload(self, result: RunResult) -> None:
        return None
