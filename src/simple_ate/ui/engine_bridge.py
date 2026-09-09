"""EngineListener -> Qt Signal 桥接。

桥接对象在工作线程中被 Engine 回调；由于信号发射对象生活在主线程
（通过 moveToThread 或默认主线程亲和性），Qt 自动以 QueuedConnection
把信号投递到主线程事件循环，UI 控件只能在主线程更新。

为彻底避免跨线程共享可变对象，这里只传递快照（dataclass / 基本类型），
不传递 RunContext 等带资源句柄的对象。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..engine import EngineListener
from ..model import ItemResult, Script, Step
from ..communication.base import Frame
from ..storage.base import RunResult


class EngineBridge(QObject, EngineListener):
    # ---- Run 级 ----
    runStarted = Signal(str, str, str)                 # sn, script_name, script_version
    runFinished = Signal(object)                      # RunResult（快照，只读）
    # ---- Step 级 ----
    stepStarted = Signal(str, int)                    # step_name, attempt（1 基）
    stepFinished = Signal(object)                     # ItemResult
    # ---- 日志 / trace ----
    logMessage = Signal(str, str)                     # level, message
    traceFrame = Signal(str, str, str)                # direction(TX/RX), resource, 帧摘要

    # ------------------------------------------------------------ 回调（工作线程）
    def on_run_start(self, ctx, script: Script) -> None:
        self.runStarted.emit(ctx.sn, script.name, script.version)

    def on_run_finish(self, result: RunResult) -> None:
        self.runFinished.emit(result)

    def on_step_start(self, step: Step, attempt: int) -> None:
        self.stepStarted.emit(step.name, attempt)

    def on_step_finish(self, step: Step, item: ItemResult) -> None:
        self.stepFinished.emit(item)

    def on_log(self, level: str, message: str) -> None:
        self.logMessage.emit(level, message)

    def on_trace(self, direction: str, resource: str, frame: Frame) -> None:
        data = " ".join(f"{b:02X}" for b in frame.data)
        self.traceFrame.emit(direction, resource,
                             f"0x{frame.id:X} ext={int(frame.ext)} {data}")
