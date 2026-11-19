"""在 QThread 中运行 Engine，UI 主线程不被阻塞。"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot

from ..config_loader import StationConfig
from ..engine import Engine
from ..engine.parser import ScriptParser
from ..logging_conf.logger import run_logging, trace_path_for
from ..storage.base import RunResult
from ..storage.file_store import FileResultStore
from .engine_bridge import EngineBridge


class _BridgeLogHandler(logging.Handler):
    def __init__(self, bridge: EngineBridge):
        super().__init__()
        self.bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        self.bridge.on_log(record.levelname.lower(), self.format(record))


class _Runner(QObject):
    """实际工作对象，moveToThread 到工作线程。"""

    finished = Signal(object)                 # RunResult
    failed = Signal(str)                      # 启动/解析阶段错误信息

    def __init__(self, config: StationConfig, script_path: str, sn: str,
                 bridge: EngineBridge, log_dir: Path, result_dir: Path):
        super().__init__()
        self.config = config
        self.script_path = script_path
        self.sn = sn
        self.bridge = bridge
        self.log_dir = log_dir
        self.result_dir = result_dir
        self.stop_event = threading.Event()

    # ------------------------------------------------------------ 槽（工作线程执行）
    @Slot()
    def run(self) -> None:
        try:
            script = ScriptParser(station=self.config).parse_file(self.script_path)

            with run_logging(self.log_dir, console=False) as (logger, log_path):
                logger.addHandler(_BridgeLogHandler(self.bridge))
                store = FileResultStore(self.result_dir, mes_enabled=self.config.mes.enabled)
                engine = Engine(
                    config=self.config, store=store, listener=self.bridge,
                    extensions_dir=self.config.resolve_path(self.config.extensions_dir),
                    stop_event=self.stop_event, logger=logger)
                result = engine.run(script, sn=self.sn, trace_path=trace_path_for(log_path, self.sn))
            self.finished.emit(result)
        except Exception as exc:  # noqa: BLE001 - 任何启动/解析错误回传 UI
            self.failed.emit(str(exc))


class RunWorker(QObject):
    """UI 持有的句柄：管理线程生命周期，暴露停止接口。"""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, config: StationConfig, script_path: str, sn: str,
                 bridge: EngineBridge, log_dir: Path, result_dir: Path):
        super().__init__()
        self.thread = QThread(self)
        self.bridge = bridge
        self.runner = _Runner(config, script_path, sn, bridge, log_dir, result_dir)
        self.stop_event = self.runner.stop_event
        self._result: RunResult | None = None
        self._error = ""
        self.runner.moveToThread(self.thread)

        self.thread.started.connect(self.runner.run)
        self.runner.finished.connect(self._on_finished)
        self.runner.failed.connect(self._on_failed)
        # 结束后自动退出线程并释放
        self.runner.finished.connect(self.thread.quit, Qt.ConnectionType.DirectConnection)
        self.runner.failed.connect(self.thread.quit, Qt.ConnectionType.DirectConnection)
        self.thread.finished.connect(self.runner.deleteLater)
        self.thread.finished.connect(self._on_thread_finished)

    def start(self) -> None:
        self.thread.start()

    def request_stop(self) -> None:
        self.stop_event.set()
        self.bridge.cancel_manual_judge()

    @Slot(object)
    def _on_finished(self, result: RunResult) -> None:
        self._result = result

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self._error = message

    @Slot()
    def _on_thread_finished(self) -> None:
        if self._result is not None:
            self.finished.emit(self._result)
        else:
            self.failed.emit(self._error)
        self.deleteLater()
