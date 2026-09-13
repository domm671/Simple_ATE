"""在 QThread 中运行 Engine，UI 主线程不被阻塞。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from ..config_loader import StationConfig
from ..engine import Engine
from ..parser import ScriptParser
from ..storage.file_store import FileResultStore


class _Runner(QObject):
    """实际工作对象，moveToThread 到工作线程。"""

    finished = Signal(object)                 # RunResult
    failed = Signal(str)                      # 启动/解析阶段错误信息

    def __init__(self, config: StationConfig, script_path: str, sn: str,
                 bridge, log_dir: Path, result_dir: Path):
        super().__init__()
        self.config = config
        self.script_path = script_path
        self.sn = sn
        self.bridge = bridge
        self.log_dir = log_dir
        self.result_dir = result_dir
        self.engine: Engine | None = None

    def request_stop(self) -> None:
        if self.engine is not None:
            self.engine.request_stop()

    # ------------------------------------------------------------ 槽（工作线程执行）
    def run(self) -> None:
        try:
            parser = ScriptParser(
                station_resources=self.config.resource_names(),
                allowed_extensions=set(self.config.extensions_allowed))
            script = parser.parse_file(self.script_path)

            store = FileResultStore(self.result_dir, mes_enabled=self.config.mes.enabled)
            self.engine = Engine(
                config=self.config, store=store, listener=self.bridge,
                extensions_dir=self.config.base_dir / self.config.extensions_dir)

            from datetime import datetime
            trace_dir = self.log_dir / "trace"
            trace_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            trace_path = trace_dir / f"trace_{self.sn}_{stamp}.log"

            result = self.engine.run(script, sn=self.sn, trace_path=trace_path)
            self.finished.emit(result)
        except Exception as exc:  # noqa: BLE001 - 任何启动/解析错误回传 UI
            self.failed.emit(str(exc))


class RunWorker(QObject):
    """UI 持有的句柄：管理线程生命周期，暴露停止接口。"""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, config: StationConfig, script_path: str, sn: str,
                 bridge, log_dir: Path, result_dir: Path):
        super().__init__()
        self.thread = QThread()
        self.runner = _Runner(config, script_path, sn, bridge, log_dir, result_dir)
        self.runner.moveToThread(self.thread)

        self.thread.started.connect(self.runner.run)
        self.runner.finished.connect(self._on_finished)
        self.runner.failed.connect(self._on_failed)
        # 结束后自动退出线程并释放
        self.runner.finished.connect(self.thread.quit)
        self.runner.failed.connect(self.thread.quit)
        self.thread.finished.connect(self.runner.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)

    def start(self) -> None:
        self.thread.start()

    def request_stop(self) -> None:
        self.runner.request_stop()

    def wait(self, ms: int = 5000) -> None:
        self.thread.wait(ms)

    def _on_finished(self, result) -> None:
        self.finished.emit(result)

    def _on_failed(self, message: str) -> None:
        self.failed.emit(message)
