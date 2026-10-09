"""EngineListener -> Qt Signal 桥接。

桥接对象在工作线程中被 Engine 回调；由于信号发射对象生活在主线程
（通过 moveToThread 或默认主线程亲和性），Qt 自动以 QueuedConnection
把信号投递到主线程事件循环，UI 控件只能在主线程更新。

为彻底避免跨线程共享可变对象，这里只传递快照（dataclass / 基本类型），
不传递 RunContext 等带资源句柄的对象。
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from ..engine import EngineListener
from ..engine.model import ItemResult, ManualJudgeRequest, Script, Step
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
    manualJudgeRequested = Signal(object)             # ManualJudgeRequest

    def __init__(self) -> None:
        super().__init__()
        self._judge_event: threading.Event | None = None
        self._judge_decision: bool | None = None

    # ------------------------------------------------------------ 回调（工作线程）
    def on_run_start(self, ctx, script: Script) -> None:
        self.runStarted.emit(ctx.sn, script.name, script.version)

    def on_manual_judge(self, request: ManualJudgeRequest) -> bool | None:
        """工作线程被引擎回调：发信号请主线程弹窗，并阻塞等待操作员结果。

        Qt 自动以 queued connection 把信号投递到主线程；主线程弹窗后调用
        resolve_manual_judge() 置位本事件，工作线程随即返回。
        """
        self._judge_decision = None
        self._judge_event = threading.Event()
        self.manualJudgeRequested.emit(request)
        self._judge_event.wait()
        return self._judge_decision

    def resolve_manual_judge(self, decision: bool | None) -> None:
        """主线程在弹窗结束后回填结果。"""
        self._judge_decision = decision
        if self._judge_event is not None:
            self._judge_event.set()

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
