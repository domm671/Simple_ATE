"""脚本编辑器测试（offscreen 平台）：XML/图形两种方式、双向同步、校验、保存。"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFileDialog,
    QMessageBox,
)

from simple_ate.config_loader import load_config  # noqa: E402
from simple_ate.ui.script_editor import (  # noqa: E402
    ScriptEditorDialog,
    new_script_template,
    serialize,
)
import xml.etree.ElementTree as ET  # noqa: E402

app = QApplication.instance() or QApplication([])

# offscreen 下模态框会永久阻塞，统一自动放行
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "station.toml"
SCRIPT = ROOT / "scripts" / "bms_ft.xml"


def make_dlg(path=SCRIPT):
    cfg = load_config(CONFIG)
    return ScriptEditorDialog(cfg, path=path)


def new_dlg():
    return ScriptEditorDialog(load_config(CONFIG), path=None)


class TestXmlTab(unittest.TestCase):
    def test_existing_script_validates(self):
        dlg = make_dlg()
        self.assertEqual(dlg.validate_text(dlg._current_text()), "")
        dlg.deleteLater()

    def test_invalid_xml_stays_on_xml_tab(self):
        dlg = make_dlg()
        dlg.xml_edit.setPlainText("<test><step></test>")
        dlg.tabs.setCurrentIndex(1)          # 尝试切到图形页签
        self.assertEqual(dlg.tabs.currentIndex(), 0)
        self.assertIsNone(dlg.root)
        dlg.deleteLater()

    def test_wrong_root_tag_blocked(self):
        dlg = make_dlg()
        dlg.xml_edit.setPlainText('<?xml version="1.0"?><foo name="x" version="1.0"/>')
        dlg.tabs.setCurrentIndex(1)
        self.assertEqual(dlg.tabs.currentIndex(), 0)
        dlg.deleteLater()

    def test_check_reports_semantic_error(self):
        dlg = make_dlg()
        dlg.xml_edit.setPlainText(
            '<?xml version="1.0"?><test name="T" version="1.0">'
            '<connect resource="can_main"/>'
            '<step name="Empty"/></test>')
        msg = dlg.validate_text(dlg._current_text())
        self.assertIn("E108", msg)
        dlg.deleteLater()


class TestGraphTab(unittest.TestCase):
    def test_load_tree_structure(self):
        dlg = make_dlg()
        dlg.tabs.setCurrentIndex(1)
        self.assertEqual(dlg.tabs.currentIndex(), 1)
        top = dlg.tree.topLevelItem(0)
        tags = [top.child(i).data(0, Qt.UserRole).tag
                for i in range(top.childCount())]
        self.assertIn("step", tags)
        self.assertIn("connect", tags)
        dlg.deleteLater()

    def test_graph_to_xml_sync(self):
        dlg = make_dlg()
        dlg.tabs.setCurrentIndex(1)
        top = dlg.tree.topLevelItem(0)
        for i in range(top.childCount()):
            it = top.child(i)
            if it.data(0, Qt.UserRole).tag == "step":
                dlg.tree.setCurrentItem(it)
                break
        dlg._form_widgets["name"].setText("GRAPH_EDITED")
        dlg.tabs.setCurrentIndex(0)           # 图形 -> XML
        self.assertIn("GRAPH_EDITED", dlg.xml_edit.toPlainText())
        dlg.deleteLater()

    def test_xml_to_graph_sync(self):
        dlg = make_dlg()
        dlg.xml_edit.setPlainText(new_script_template("can_main"))
        dlg.tabs.setCurrentIndex(1)
        self.assertIsNotNone(dlg.root)
        self.assertEqual(dlg.root.get("name"), "NEW_SCRIPT")
        dlg.deleteLater()

    def test_add_step_and_instruction(self):
        dlg = make_dlg()
        dlg.tabs.setCurrentIndex(1)
        steps_before = len(dlg.root.findall("step"))

        # 选中根节点添加 step
        dlg.tree.setCurrentItem(dlg.tree.topLevelItem(0))
        dlg.add_combo.setCurrentIndex(dlg.add_combo.findData("step"))
        dlg._add_node()
        self.assertEqual(len(dlg.root.findall("step")), steps_before + 1)

        # 新建 step 自带一个 wait，整份脚本仍应通过校验
        self.assertEqual(dlg.validate_text(dlg._current_text()), "")

        # 在该新 step 下添加 send
        self.assertEqual(dlg.add_combo.currentData(), "send")
        dlg._add_node()
        selected = dlg.tree.currentItem().data(0, Qt.UserRole)
        self.assertEqual(selected.tag, "send")
        self.assertEqual(dlg.validate_text(dlg._current_text()), "")
        dlg.deleteLater()

    def test_delete_node(self):
        dlg = make_dlg()
        dlg.tabs.setCurrentIndex(1)
        before = len(dlg.root.findall("step"))
        top = dlg.tree.topLevelItem(0)
        for i in range(top.childCount()):
            it = top.child(i)
            if it.data(0, Qt.UserRole).tag == "step":
                dlg.tree.setCurrentItem(it)
                break
        dlg._delete_node()
        self.assertEqual(len(dlg.root.findall("step")), before - 1)
        dlg.deleteLater()

    def test_move_node(self):
        dlg = make_dlg()
        dlg.tabs.setCurrentIndex(1)
        top = dlg.tree.topLevelItem(0)
        steps = [c for c in dlg.root if c.tag == "step"]
        first_name = steps[0].get("name")
        # 找到第一个 step 的树节点并下移
        for i in range(top.childCount()):
            it = top.child(i)
            if it.data(0, Qt.UserRole) is steps[0]:
                dlg.tree.setCurrentItem(it)
                break
        dlg._move_node(1)
        self.assertEqual([c for c in dlg.root if c.tag == "step"][1].get("name"),
                         first_name)
        dlg._move_node(-1)
        self.assertEqual([c for c in dlg.root if c.tag == "step"][0].get("name"),
                         first_name)
        dlg.deleteLater()

    def test_limit_inserted_last(self):
        dlg = make_dlg()
        dlg.tabs.setCurrentIndex(1)
        top = dlg.tree.topLevelItem(0)
        # 选第一个 step
        for i in range(top.childCount()):
            it = top.child(i)
            if it.data(0, Qt.UserRole).tag == "step":
                step_el = it.data(0, Qt.UserRole)
                dlg.tree.setCurrentItem(it)
                break
        # 在已有指令（无 limit）的 step 中先加 send，再加 limit，limit 必须在最后
        dlg.add_combo.setCurrentIndex(dlg.add_combo.findData("send"))
        dlg._add_node()
        dlg.add_combo.setCurrentIndex(dlg.add_combo.findData("limit"))
        dlg._add_node()
        children = [c.tag for c in step_el]
        self.assertEqual(children[-1], "limit")
        dlg.deleteLater()

    def test_roundtrip_preserves_structure(self):
        original = SCRIPT.read_text(encoding="utf-8")
        dlg = make_dlg()
        dlg.tabs.setCurrentIndex(1)          # 解析进树
        dlg.tabs.setCurrentIndex(0)          # 再序列化回 XML
        out = dlg.xml_edit.toPlainText()
        # 结构等价（忽略注释/空白）：两边解析出的标签-属性序列一致
        def shape(text):
            r = ET.fromstring(text)
            return [(e.tag, tuple(sorted(e.attrib.items())))
                    for e in r.iter()]
        self.assertEqual(shape(original), shape(out))
        dlg.deleteLater()


class TestActionExtraParams(unittest.TestCase):
    def test_action_extra_params_roundtrip(self):
        cfg = load_config(CONFIG)
        xml = (
            '<?xml version="1.0"?><test name="T" version="1.0">'
            '<connect resource="can_main"/>'
            '<step name="S"><action handler="sample_ext:noop" var="r" '
            'addr="0x10" len="${x}"/></step></test>')
        dlg = ScriptEditorDialog(cfg, path=None)
        dlg.xml_edit.setPlainText(xml)
        self.assertTrue(dlg._sync_text_to_graph())
        # 找到 action 节点
        action_item = None
        def walk(it):
            nonlocal action_item
            if it.data(0, Qt.UserRole).tag == "action":
                action_item = it
            for i in range(it.childCount()):
                walk(it.child(i))
        walk(dlg.tree.topLevelItem(0))
        dlg.tree.setCurrentItem(action_item)
        self.assertIn("addr=0x10", dlg.action_extra.toPlainText())
        dlg.action_extra.setPlainText("addr=0x20\nmode=fast")
        dlg._apply_form_to_element()
        el = action_item.data(0, Qt.UserRole)
        self.assertEqual(el.get("addr"), "0x20")
        self.assertEqual(el.get("mode"), "fast")
        dlg.deleteLater()


class TestSave(unittest.TestCase):
    def test_new_script_save_creates_parseable_file(self):
        tmp = Path(tempfile.mkdtemp())
        target = tmp / "created.xml"
        QFileDialog.getSaveFileName = staticmethod(
            lambda *a, **k: (str(target), "测试脚本 (*.xml)"))
        dlg = new_dlg()
        self.assertIsNone(dlg.path)
        dlg._on_save()
        self.assertEqual(dlg.saved_path, target)
        self.assertTrue(target.exists())
        # 保存内容必须能被正式解析器加载
        from simple_ate.parser import ScriptParser
        cfg = load_config(CONFIG)
        ScriptParser(
            station_resources=cfg.resource_names(),
            allowed_extensions=set(cfg.extensions_allowed)
        ).parse_file(str(target))
        dlg.deleteLater()

    def test_save_blocked_when_invalid(self):
        tmp = Path(tempfile.mkdtemp())
        target = tmp / "bad.xml"
        QFileDialog.getSaveFileName = staticmethod(
            lambda *a, **k: (str(target), "测试脚本 (*.xml)"))
        dlg = new_dlg()
        dlg.xml_edit.setPlainText(
            '<?xml version="1.0"?><test name="T" version="1.0">'
            '<step name="Empty"/></test>')
        dlg._on_save()                     # E108 + 缺少 connect
        self.assertFalse(target.exists())
        self.assertIsNone(dlg.saved_path)
        self.assertIn("E108", dlg.error_view.toPlainText())
        dlg.deleteLater()

    def test_edit_existing_save_in_place(self):
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "edit.xml"
        f.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        dlg = make_dlg(f)
        dlg.tabs.setCurrentIndex(1)
        top = dlg.tree.topLevelItem(0)
        it = top.child(0)
        dlg.tree.setCurrentItem(it)
        # 改脚本根 name：切回根节点
        dlg.tree.setCurrentItem(top)
        dlg._form_widgets["version"].setText("9.9")
        dlg._on_save()
        self.assertEqual(dlg.saved_path, f)
        self.assertIn('version="9.9"', f.read_text(encoding="utf-8"))
        dlg.deleteLater()


if __name__ == "__main__":
    unittest.main()
