"""UI 集成测试（offscreen 平台）：真实 QThread + Engine + Mock 跑通主窗口。

需在导入 QApplication 前设置 QT_QPA_PLATFORM=offscreen。
"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from simple_ate.config_loader import load_config  # noqa: E402
from simple_ate.ui.main_window import IDLE, RUNNING, MainWindow  # noqa: E402

app = QApplication.instance() or QApplication([])

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "station.toml"


def pump(ms=100):
    """让事件循环处理排队信号（worker->主线程为 QueuedConnection）。"""
    import time
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


class TestMainWindow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = load_config(CONFIG)
        self.log_dir = self.tmp / "logs"
        self.result_dir = self.tmp / "results"
        self.win = MainWindow(self.cfg, self.log_dir, self.result_dir,
                              ROOT / "scripts")

    def tearDown(self):
        self.win.close()

    # ------------------------------------------------------------ 静态行为
    def test_scripts_loaded(self):
        self.assertGreaterEqual(self.win.script_combo.count(), 1)
        self.assertTrue(any(self.win.script_combo.itemText(i) == "bms_ft.xml"
                            for i in range(self.win.script_combo.count())))

    def test_sn_not_validated_by_default(self):
        # 默认 sn.validation_enabled=False：不做 pattern 校验，任意非空 SN 均有效
        self.assertFalse(self.cfg.sn.validation_enabled)
        self.assertTrue(self.win._sn_valid("bad-sn!!"))
        self.assertTrue(self.win._sn_valid("BMS20260909001"))
        # 仅空 SN 仍然不允许
        self.assertFalse(self.win._sn_valid(""))

    def test_sn_validation_blocks_start(self):
        # 使用方按需启用 pattern 校验
        self.cfg.sn.validation_enabled = True
        self.cfg.sn.pattern = r"^BMS[0-9]{11}$"
        self.win.sn_edit.setText("bad-sn!!")
        self.win._on_start()
        self.assertEqual(self.win.state, IDLE)
        self.assertIn("不符合", self.win.sn_hint.text())
        self.assertIsNone(self.win.worker)

    def test_valid_sn_pattern(self):
        # 启用校验后按 pattern = ^BMS[0-9]{11}$ 判定
        self.cfg.sn.validation_enabled = True
        self.cfg.sn.pattern = r"^BMS[0-9]{11}$"
        self.assertTrue(self.win._sn_valid("BMS20260909001"))
        self.assertFalse(self.win._sn_valid("X"))

    def test_start_without_script(self):
        self.win.script_combo.clear()
        self.win.sn_edit.setText("BMS20260909001")
        # 无脚本不应崩溃，也不应进入运行态，状态栏给出提示
        self.win._on_start()
        self.assertEqual(self.win.state, IDLE)
        self.assertIn("脚本", self.win.status_label.text())

    # ------------------------------------------------------------ 完整跑一遍（PASS）
    def test_full_run_pass(self):
        self.win.script_combo.setCurrentIndex(
            next(i for i in range(self.win.script_combo.count())
                 if self.win.script_combo.itemText(i) == "bms_ft.xml"))
        self.win.sn_edit.setText("BMS20260909001")
        self.win._on_start()
        self.assertEqual(self.win.state, RUNNING)
        self.assertFalse(self.win.btn_start.isEnabled())
        self.assertTrue(self.win.btn_stop.isEnabled())

        # 等待 worker 线程完成（mock 下很快），最多 10s
        self._wait_idle(10.0)

        self.assertEqual(self.win.state, IDLE)
        self.assertIn("PASS", self.win.result_label.text())
        self.assertEqual(self.win.table.rowCount(), 4)
        # 每行结果文字以 PASS 结尾
        for r in range(4):
            self.assertTrue(self.win.table.item(r, 5).text().startswith("PASS"))
        # 结果文件已保存
        files = list((self.result_dir / "uploaded").glob("BMS20260909001_*.json"))
        self.assertEqual(len(files), 1)
        # 结束后 SN 框清空并聚焦，便于下一次扫码
        self.assertEqual(self.win.sn_edit.text(), "")

    # ------------------------------------------------------------ Stop -> ABORT
    def test_stop_aborts_run(self):
        # 用一个不存在应答的临时脚本触发长时间等待
        script = self.tmp / "hang.xml"
        script.write_text(
            '<test name="HANG" version="1.0"><connect resource="can_main"/>'
            '<step name="WAIT_LONG" timeout="10" retry="0">'
            '<send id="0x18FF50E5" ext="true" data="FF FF"/>'
            '<wait id="0x18FF50E6" ext="true"/></step></test>',
            encoding="utf-8")
        self.win.script_combo.insertItem(0, "hang.xml", str(script))
        self.win.script_combo.setCurrentIndex(0)
        self.win.sn_edit.setText("BMS20260909002")
        self.win._on_start()
        self.assertEqual(self.win.state, RUNNING)
        pump(500)                       # 让其进入等待
        self.win._on_stop()
        self._wait_idle(5.0)
        self.assertEqual(self.win.state, IDLE)
        self.assertIn("ABORT", self.win.result_label.text())

    # ------------------------------------------------------------ 解析错误
    def test_bad_script_shows_start_failure(self):
        script = self.tmp / "bad.xml"
        script.write_text('<test name="B" version="1.0"><step name="S"><bogus/></step></test>',
                          encoding="utf-8")
        self.win.script_combo.insertItem(0, "bad.xml", str(script))
        self.win.script_combo.setCurrentIndex(0)
        self.win.sn_edit.setText("BMS20260909003")
        self.win._on_start()
        self._wait_idle(5.0)
        self.assertEqual(self.win.state, IDLE)
        self.assertIn("启动失败", self.win.result_label.text())

    def test_auto_start_disabled_does_not_run(self):
        self.cfg.sn.auto_start = False
        self.win.script_combo.setCurrentIndex(
            next(i for i in range(self.win.script_combo.count())
                 if self.win.script_combo.itemText(i) == "bms_ft.xml"))
        self.win.sn_edit.setText("BMS20260909001")
        self.win._on_sn_enter()
        self.assertEqual(self.win.state, IDLE)
        self.assertIsNone(self.win.worker)
        # auto_start=true（默认）时回车会真正启动
        self.cfg.sn.auto_start = True
        self.win._on_sn_enter()
        self.assertEqual(self.win.state, RUNNING)
        self._wait_idle(10.0)
        self.assertIn("PASS", self.win.result_label.text())

    def _wait_idle(self, seconds: float):
        import time
        end = time.monotonic() + seconds
        while time.monotonic() < end and self.win.state != IDLE:
            app.processEvents()
            time.sleep(0.02)
        app.processEvents()
        self.assertEqual(self.win.state, IDLE, "Run 未在限定时间内结束")


if __name__ == "__main__":
    unittest.main()
