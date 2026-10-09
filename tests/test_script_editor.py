"""脚本编辑器测试（offscreen 平台）：XML/图形两种方式、双向同步、校验、保存。"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt  # noqa: E402
    from PySide6.QtWidgets import (  # noqa: E402
        QApplication,
        QFileDialog,
        QMessageBox,
    )
except ImportError:  # 未安装可选依赖 PySide6 时，测试优雅跳过而非收集报错
    PYSIDE6_AVAILABLE = False
    Qt = QApplication = QFileDialog = QMessageBox = None
else:
    PYSIDE6_AVAILABLE = True

import xml.etree.ElementTree as ET  # noqa: E402

if PYSIDE6_AVAILABLE:
    from simple_ate.config_loader import load_config  # noqa: E402
    from simple_ate.ui.script_editor import (  # noqa: E402
        ScriptEditorDialog,
        new_script_template,
        serialize,
    )

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


@unittest.skipUnless(PYSIDE6_AVAILABLE, "未安装 PySide6，跳过脚本编辑器测试")
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


@unittest.skipUnless(PYSIDE6_AVAILABLE, "未安装 PySide6，跳过脚本编辑器测试")
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


@unittest.skipUnless(PYSIDE6_AVAILABLE, "未安装 PySide6，跳过脚本编辑器测试")
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


@unittest.skipUnless(PYSIDE6_AVAILABLE, "未安装 PySide6，跳过脚本编辑器测试")
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
        from simple_ate.engine.parser import ScriptParser
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


@unittest.skipUnless(PYSIDE6_AVAILABLE, "未安装 PySide6，跳过脚本编辑器测试")
class TestNewAttributes(unittest.TestCase):
    def test_attr_spec_covers_new_features(self):
        from simple_ate.ui.script_editor import ATTR_SPEC, _ENUM_OPTIONS
        self.assertIn("protocol", {a[0] for a in ATTR_SPEC["connect"]})
        self.assertIn("port", {a[0] for a in ATTR_SPEC["connect"]})
        self.assertIn("unit", {a[0] for a in ATTR_SPEC["connect"]})
        self.assertIn("mode", {a[0] for a in ATTR_SPEC["send"]})
        self.assertIn("file", {a[0] for a in ATTR_SPEC["send"]})
        self.assertIn("chunk_size", {a[0] for a in ATTR_SPEC["send"]})
        self.assertIn("size", {a[0] for a in ATTR_SPEC["wait"]})
        self.assertIn("checksum", {a[0] for a in ATTR_SPEC["wait"]})
        self.assertIn("manual", [v for v, _ in _ENUM_OPTIONS["limitmode"]])

    def test_limit_mode_dynamic_fields(self):
        dlg = make_dlg()
        dlg.tabs.setCurrentIndex(1)
        limit_item = None

        def walk(it):
            nonlocal limit_item
            el = it.data(0, Qt.UserRole)
            if el is not None and el.tag == "limit":
                limit_item = it
            for i in range(it.childCount()):
                walk(it.child(i))

        walk(dlg.tree.topLevelItem(0))
        self.assertIsNotNone(limit_item)
        dlg.tree.setCurrentItem(limit_item)
        limit_el = limit_item.data(0, Qt.UserRole)

        # 默认 auto：min/max/eq 可见，prompt 隐藏
        self.assertFalse(dlg._form_widgets["min"].isHidden())
        self.assertFalse(dlg._form_widgets["eq"].isHidden())
        self.assertTrue(dlg._form_widgets["prompt"].isHidden())

        # 切换到 manual：隐藏 min/max/eq，显示 prompt，并从元素中移除互斥属性
        mode = dlg._form_widgets["mode"]
        mode.setCurrentIndex(mode.findData("manual"))
        self.assertTrue(dlg._form_widgets["min"].isHidden())
        self.assertTrue(dlg._form_widgets["max"].isHidden())
        self.assertTrue(dlg._form_widgets["eq"].isHidden())
        self.assertFalse(dlg._form_widgets["prompt"].isHidden())
        self.assertNotIn("min", limit_el.attrib)
        self.assertNotIn("max", limit_el.attrib)
        self.assertEqual(limit_el.get("mode"), "manual")
        self.assertEqual(limit_el.get("value"), "${relay_st}")
        # 序列化后所有 manual limit 均不含 min/max
        out = dlg._current_text()
        manual_limits = [e for e in ET.fromstring(out).iter("limit")
                         if e.get("mode") == "manual"]
        self.assertTrue(manual_limits)
        for ml in manual_limits:
            self.assertNotIn("min", ml.attrib)
            self.assertNotIn("max", ml.attrib)
        dlg.deleteLater()

    def test_new_attrs_roundtrip_and_validate(self):
        xml = (
            '<?xml version="1.0"?>'
            '<test name="T" version="1.0">'
            '<connect resource="rs485" protocol="serial" port="COM7" '
            'baudrate="115200"/>'
            '<step name="S" resource="rs485">'
            '<send id="0" mode="file" file="fw.bin" chunk_size="256" '
            'seq_len="2"/>'
            '<wait id="0" mode="file" file="out.bin" chunk_size="256" '
            'seq_len="2" size="1024" checksum="crc32"/>'
            '</step></test>')
        cfg = load_config(CONFIG)
        dlg = ScriptEditorDialog(cfg, path=None)
        dlg.xml_edit.setPlainText(xml)
        self.assertEqual(dlg.validate_text(dlg._current_text()), "")
        self.assertTrue(dlg._sync_text_to_graph())
        dlg.tabs.setCurrentIndex(0)              # 图形 -> XML
        out = dlg.xml_edit.toPlainText()

        def shape(text):
            r = ET.fromstring(text)
            return [(e.tag, tuple(sorted(e.attrib.items()))) for e in r.iter()]
        self.assertEqual(shape(xml), shape(out))
        dlg.deleteLater()


@unittest.skipUnless(PYSIDE6_AVAILABLE, "未安装 PySide6，跳过脚本编辑器测试")
class TestConnectDynamicFields(unittest.TestCase):
    def _select_connect(self, dlg):
        top = dlg.tree.topLevelItem(0)
        for i in range(top.childCount()):
            it = top.child(i)
            if it.data(0, Qt.UserRole).tag == "connect":
                dlg.tree.setCurrentItem(it)
                return it
        raise AssertionError("未找到 connect 节点")

    def test_fields_follow_protocol(self):
        cfg = load_config(CONFIG)
        xml = ('<?xml version="1.0"?>'
               '<test name="T" version="1.0">'
               '<connect resource="rs485" protocol="serial" port="COM3" '
               'baudrate="115200"/>'
               '</test>')
        dlg = ScriptEditorDialog(cfg, path=None)
        dlg.xml_edit.setPlainText(xml)
        self.assertTrue(dlg._sync_text_to_graph())
        item = self._select_connect(dlg)

        # serial：串口参数可见，CAN/Modbus 参数隐藏
        self.assertFalse(dlg._form_widgets["port"].isHidden())
        self.assertFalse(dlg._form_widgets["baudrate"].isHidden())
        self.assertTrue(dlg._form_widgets["interface"].isHidden())
        self.assertTrue(dlg._form_widgets["host"].isHidden())

        # 切到 can：只剩 CAN 参数，且串口属性从元素中清除
        proto = dlg._form_widgets["protocol"]
        proto.setCurrentIndex(proto.findData("can"))
        self.assertTrue(dlg._form_widgets["port"].isHidden())
        self.assertFalse(dlg._form_widgets["interface"].isHidden())
        el = item.data(0, Qt.UserRole)
        self.assertNotIn("port", el.attrib)
        self.assertNotIn("baudrate", el.attrib)

        # 切到 mock：只剩 mock_script
        proto.setCurrentIndex(proto.findData("mock"))
        self.assertFalse(dlg._form_widgets["mock_script"].isHidden())
        self.assertTrue(dlg._form_widgets["interface"].isHidden())
        dlg.deleteLater()

    def test_modbus_mode_follows_tcp(self):
        cfg = load_config(CONFIG)
        xml = ('<?xml version="1.0"?>'
               '<test name="T" version="1.0">'
               '<connect resource="mb" protocol="modbus" modbus_mode="rtu" '
               'port="COM4" unit="3"/>'
               '</test>')
        dlg = ScriptEditorDialog(cfg, path=None)
        dlg.xml_edit.setPlainText(xml)
        self.assertTrue(dlg._sync_text_to_graph())
        item = self._select_connect(dlg)
        self.assertFalse(dlg._form_widgets["port"].isHidden())
        self.assertTrue(dlg._form_widgets["host"].isHidden())

        mode = dlg._form_widgets["modbus_mode"]
        mode.setCurrentIndex(mode.findData("tcp"))
        self.assertTrue(dlg._form_widgets["port"].isHidden())
        self.assertFalse(dlg._form_widgets["host"].isHidden())
        self.assertFalse(dlg._form_widgets["tcp_port"].isHidden())
        dlg._form_widgets["host"].setText("192.168.1.20")
        dlg._apply_form_to_element()
        el = item.data(0, Qt.UserRole)
        self.assertNotIn("port", el.attrib)
        self.assertEqual(el.get("host"), "192.168.1.20")
        dlg.deleteLater()

    def test_station_type_fallback(self):
        # bms_ft.xml 的 connect 未写 protocol，工位配置 can_main 的 type=mock
        dlg = make_dlg()
        dlg.tabs.setCurrentIndex(1)
        self._select_connect(dlg)
        self.assertFalse(dlg._form_widgets["mock_script"].isHidden())
        self.assertTrue(dlg._form_widgets["interface"].isHidden())
        self.assertTrue(dlg._form_widgets["port"].isHidden())
        dlg.deleteLater()

    def test_unknown_resource_hint(self):
        cfg = load_config(CONFIG)
        xml = ('<?xml version="1.0"?>'
               '<test name="T" version="1.0">'
               '<connect resource="not_in_station"/>'
               '</test>')
        dlg = ScriptEditorDialog(cfg, path=None)
        dlg.xml_edit.setPlainText(xml)
        self.assertTrue(dlg._sync_text_to_graph())
        self._select_connect(dlg)
        self.assertIn("未指定协议", dlg._connect_hint.text())
        for key in ("interface", "port", "host", "mock_script"):
            self.assertTrue(dlg._form_widgets[key].isHidden())
        dlg.deleteLater()


if __name__ == "__main__":
    unittest.main()
