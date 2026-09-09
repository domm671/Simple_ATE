"""v0.1 单窗口主界面（五个区域）。

布局：
  ┌─ 区域1：脚本选择 ────────────────────────────────┐
  ├─ 区域2：SN 输入（扫码枪） ───────────────────────┤
  ├─ 区域3：Start / Stop / Reset + 工位/状态 ────────┤
  ├─ 区域4：测试进度（逐项表格） ────────────────────┤
  ├─ 区域5：结果大字 + 日志区 ───────────────────────┘

状态机：IDLE -> RUNNING -> IDLE（结束/中止/失败后回到 IDLE）。
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config_loader import StationConfig
from ..model import ItemResult
from ..storage.base import RunResult
from .engine_bridge import EngineBridge
from .worker import RunWorker

# 结果 -> 颜色
RESULT_COLORS = {
    "PASS": QColor(0x2E, 0x8B, 0x3C),      # 绿
    "FAIL": QColor(0xD0, 0x24, 0x24),      # 红
    "ERROR": QColor(0xD8, 0x9A, 0x00),     # 黄/琥珀
    "ABORT": QColor(0x80, 0x80, 0x80),     # 灰
}
RESULT_CN = {"PASS": "合格 PASS", "FAIL": "不合格 FAIL",
             "ERROR": "设备异常 ERROR", "ABORT": "已中止 ABORT"}

RUNNING = "RUNNING"
IDLE = "IDLE"


class MainWindow(QMainWindow):
    def __init__(self, config: StationConfig, log_dir: Path, result_dir: Path,
                 scripts_dir: Path):
        super().__init__()
        self.config = config
        self.log_dir = log_dir
        self.result_dir = result_dir
        self.scripts_dir = scripts_dir
        self.state = IDLE
        self.worker: RunWorker | None = None
        self.bridge: EngineBridge | None = None

        self.setWindowTitle(f"simple_ATE 终测工位 — {config.station_id}")
        self.resize(980, 760)

        self._build_ui()
        self._refresh_scripts()
        self._set_state(IDLE)

    # ============================================================ UI 构建
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)

        root.addWidget(self._build_script_box())      # 区域1
        root.addWidget(self._build_sn_box())          # 区域2
        root.addWidget(self._build_control_box())     # 区域3

        # 区域4：进度表
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["#", "测试项", "测量值", "下限", "上限", "结果"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        prog_box = QGroupBox("测试进度")
        pv = QVBoxLayout(prog_box)
        pv.addWidget(self.table)
        root.addWidget(prog_box, stretch=2)

        # 区域5：结果大字 + 日志
        bottom = QHBoxLayout()
        result_box = QGroupBox("结果")
        rv = QVBoxLayout(result_box)
        self.result_label = QLabel("就绪")
        f = QFont()
        f.setPointSize(34)
        f.setBold(True)
        self.result_label.setFont(f)
        self.result_label.setAlignment(Qt.AlignCenter)
        self.result_label.setMinimumWidth(280)
        rv.addWidget(self.result_label)
        self.detail_label = QLabel("")
        self.detail_label.setAlignment(Qt.AlignCenter)
        self.detail_label.setWordWrap(True)
        rv.addWidget(self.detail_label)
        bottom.addWidget(result_box)

        log_box = QGroupBox("运行日志")
        lv = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        lv.addWidget(self.log_view)
        bottom.addWidget(log_box, stretch=2)
        root.addLayout(bottom, stretch=3)

        self.setCentralWidget(central)

    def _build_script_box(self) -> QWidget:
        box = QGroupBox("测试脚本（产品型号）")
        lay = QHBoxLayout(box)
        self.script_combo = QComboBox()
        self.script_combo.setMinimumWidth(420)
        lay.addWidget(self.script_combo, stretch=1)
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self._refresh_scripts)
        lay.addWidget(btn_refresh)
        btn_browse = QPushButton("打开其它…")
        btn_browse.clicked.connect(self._browse_script)
        lay.addWidget(btn_browse)
        return box

    def _build_sn_box(self) -> QWidget:
        box = QGroupBox("产品序列号 SN（扫码枪输入后回车）")
        lay = QHBoxLayout(box)
        self.sn_edit = QLineEdit()
        self.sn_edit.setPlaceholderText("扫描或输入 SN…")
        self.sn_edit.returnPressed.connect(self._on_sn_enter)
        lay.addWidget(self.sn_edit, stretch=1)
        self.sn_hint = QLabel("")
        self.sn_hint.setStyleSheet("color: #D02424;")
        lay.addWidget(self.sn_hint)
        return box

    def _build_control_box(self) -> QWidget:
        box = QWidget()
        lay = QGridLayout(box)
        self.btn_start = QPushButton("开始测试 (Start)")
        self.btn_start.setMinimumHeight(52)
        self.btn_start.setStyleSheet("font-size:16px; font-weight:bold;")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop = QPushButton("停止 (Stop)")
        self.btn_stop.setMinimumHeight(52)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_reset = QPushButton("复位 (Reset)")
        self.btn_reset.setMinimumHeight(52)
        self.btn_reset.clicked.connect(self._on_reset)
        lay.addWidget(self.btn_start, 0, 0)
        lay.addWidget(self.btn_stop, 0, 1)
        lay.addWidget(self.btn_reset, 0, 2)

        self.status_label = QLabel(f"工位 {self.config.station_id} ｜ 软件 {self.config.software_version}")
        lay.addWidget(self.status_label, 0, 3)
        self.pending_label = QLabel("")
        self.pending_label.setStyleSheet("color:#B07000; font-weight:bold;")
        lay.addWidget(self.pending_label, 0, 4)
        lay.setColumnStretch(3, 1)
        self._refresh_pending_count()
        return box

    # ============================================================ 脚本选择
    def _refresh_scripts(self) -> None:
        self.script_combo.clear()
        if self.scripts_dir.exists():
            for p in sorted(self.scripts_dir.glob("*.xml")):
                self.script_combo.addItem(p.name, str(p))

    def _browse_script(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择测试脚本", str(self.scripts_dir), "XML 脚本 (*.xml)")
        if path:
            self.script_combo.insertItem(0, Path(path).name, path)
            self.script_combo.setCurrentIndex(0)

    def _current_script(self) -> str | None:
        return self.script_combo.currentData()

    # ============================================================ 状态控制
    def _set_state(self, state: str) -> None:
        self.state = state
        running = state == RUNNING
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.sn_edit.setEnabled(not running)
        self.script_combo.setEnabled(not running)

    def _sn_valid(self, sn: str) -> bool:
        pattern = self.config.sn.pattern
        try:
            return bool(sn) and re.fullmatch(pattern, sn) is not None
        except re.error:
            return bool(sn)

    def _on_sn_enter(self) -> None:
        """扫码枪回车：auto_start=true 时直接启动，否则仅校验并聚焦开始按钮。"""
        if self.state == RUNNING:
            return
        if self.config.sn.auto_start:
            self._on_start()
        else:
            sn = self.sn_edit.text().strip()
            if self._sn_valid(sn):
                self.sn_hint.setText("")
                self.btn_start.setFocus()
            else:
                self.sn_hint.setText(f"SN 不符合规则：{self.config.sn.pattern}")

    def _on_start(self) -> None:
        if self.state == RUNNING:
            return
        script = self._current_script()
        if not script:
            self._notify("请先选择测试脚本。")
            return
        sn = self.sn_edit.text().strip()
        if not self._sn_valid(sn):
            self.sn_hint.setText(f"SN 不符合规则：{self.config.sn.pattern}")
            self.sn_edit.setFocus()
            return
        self.sn_hint.setText("")
        self._prepare_run()

        self.bridge = EngineBridge()
        self._connect_bridge_signals()
        self.worker = RunWorker(
            self.config, script, sn, self.bridge,
            self.log_dir, self.result_dir)
        self.worker.finished.connect(self._on_run_finished)
        self.worker.failed.connect(self._on_run_failed)
        self._set_state(RUNNING)
        self._append_log("info", f"启动测试：SN={sn} 脚本={Path(script).name}")
        self.worker.start()

    def _on_stop(self) -> None:
        if self.worker:
            self.worker.request_stop()
            self.btn_stop.setEnabled(False)
            self._append_log("warning", "已请求停止，等待当前操作安全结束…")

    def _on_reset(self) -> None:
        if self.state == RUNNING:
            return
        self.table.setRowCount(0)
        self.result_label.setText("就绪")
        self.result_label.setStyleSheet("color: black;")
        self.detail_label.setText("")
        self.log_view.clear()
        self.sn_edit.clear()
        self.sn_hint.setText("")
        self.sn_edit.setFocus()

    def _prepare_run(self) -> None:
        self.table.setRowCount(0)
        self.result_label.setText("测试中…")
        self.result_label.setStyleSheet("color: #0050A0;")
        self.detail_label.setText("")

    # ============================================================ 桥接信号
    def _connect_bridge_signals(self) -> None:
        assert self.bridge is not None
        self.bridge.runStarted.connect(self._on_run_started)
        self.bridge.stepStarted.connect(self._on_step_started)
        self.bridge.stepFinished.connect(self._on_step_finished)
        self.bridge.logMessage.connect(self._append_log)
        self.bridge.traceFrame.connect(self._on_trace)

    def _on_run_started(self, sn: str, name: str, version: str) -> None:
        self.status_label.setText(f"运行中：{name} v{version} ｜ SN {sn}")

    def _on_step_started(self, step_name: str, attempt: int) -> None:
        # 首次尝试新增/定位行；重试时更新状态文字
        if attempt <= 1:
            row = self.table.rowCount()
            self.table.insertRow(row)
            vals = [str(row + 1), step_name, "…", "", "", "运行中"]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if col == 5:
                    item.setForeground(QColor(0x00, 0x50, 0xA0))
                self.table.setItem(row, col, item)
        else:
            for r in range(self.table.rowCount()):
                if self.table.item(r, 1).text() == step_name:
                    self.table.item(r, 5).setText(f"重试 {attempt - 1}…")
                    break

    def _on_step_finished(self, item: ItemResult) -> None:
        row = self._row_for_step(item.step_name)
        if row is None:
            return
        value = self._fmt(item.value, item.unit)
        self.table.item(row, 2).setText(value)
        self.table.item(row, 3).setText("" if item.low_limit is None else str(item.low_limit))
        self.table.item(row, 4).setText("" if item.high_limit is None else str(item.high_limit))
        res_item = self.table.item(row, 5)
        text = item.result
        if item.error_code:
            text += f"（{item.error_code}）"
        res_item.setText(text)
        res_item.setForeground(RESULT_COLORS.get(item.result, QColor("black")))

    def _on_trace(self, direction: str, resource: str, summary: str) -> None:
        # trace 不进 UI 主日志区（规范：trace 只写文件）；这里仅留调试钩子，默认不显示
        pass

    # ============================================================ 结束
    def _on_run_finished(self, result: RunResult) -> None:
        color = RESULT_COLORS.get(result.result, QColor("black"))
        self.result_label.setText(RESULT_CN.get(result.result, result.result))
        self.result_label.setStyleSheet(f"color: rgb({color.red()},{color.green()},{color.blue()});")
        fails = [i for i in result.items if i.result != "PASS"]
        if fails:
            names = "、".join(
                f"{i.step_name}" + (f"[{i.error_code}]" if i.error_code else "")
                for i in fails[:6])
            extra = " …" if len(fails) > 6 else ""
            self.detail_label.setText(f"异常/不合格项：{names}{extra}")
        else:
            self.detail_label.setText(f"全部 {len(result.items)} 项通过")
        self.status_label.setText(
            f"工位 {self.config.station_id} ｜ 结论 {result.result} ｜ SN {result.meta.sn}")
        self._set_state(IDLE)
        self._refresh_pending_count()
        # 为下一次扫码做准备
        self.sn_edit.clear()
        self.sn_edit.setFocus()

    def _on_run_failed(self, message: str) -> None:
        self.result_label.setText("启动失败")
        self.result_label.setStyleSheet("color: #D02424;")
        self.detail_label.setText("脚本或配置错误，详见日志")
        self._append_log("error", message)
        self._set_state(IDLE)
        self._notify(message)
        self.sn_edit.setFocus()

    def _refresh_pending_count(self) -> None:
        """统计 pending 目录待上传 MES 的结果数。MES 禁用时不显示。"""
        if not self.config.mes.enabled:
            self.pending_label.setText("")
            return
        pending_dir = self.result_dir / "pending"
        try:
            n = len(list(pending_dir.glob("*.json")))
        except OSError:
            n = 0
        self.pending_label.setText(f"待传 MES：{n}" if n else "MES 已同步")

    def _notify(self, text: str) -> None:
        """非阻塞提示：写状态栏 + detail，避免模态框卡住自动化/扫码流程。"""
        self.status_label.setText(text)

    # ============================================================ 工具
    def _row_for_step(self, step_name: str) -> int | None:
        for r in range(self.table.rowCount()):
            if self.table.item(r, 1) and self.table.item(r, 1).text() == step_name:
                return r
        return None

    @staticmethod
    def _fmt(value, unit) -> str:
        if value is None:
            return ""
        if isinstance(value, float):
            text = f"{value:g}"
        else:
            text = str(value)
        return f"{text}{unit or ''}"

    def _append_log(self, level: str, message: str) -> None:
        color = {"error": "#D02424", "warning": "#B07000",
                 "info": "#222222", "debug": "#777777"}.get(level.lower(), "#222222")
        self.log_view.appendHtml(
            f'<span style="color:{color}">[{level.upper()}] {_escape(message)}</span>')

    def closeEvent(self, event) -> None:
        if self.state == RUNNING and self.worker:
            reply = QMessageBox.question(
                self, "确认退出", "测试正在进行，确定停止并退出吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.request_stop()
            self.worker.wait(3000)
        event.accept()


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))
