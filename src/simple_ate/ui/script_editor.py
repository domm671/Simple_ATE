"""测试脚本编辑器对话框：支持 XML 原文编辑与图形化编辑两种方式。

- 两个页签共享同一份脚本内容，切换页签时互相同步：
  * XML 页签：纯文本（QPlainTextEdit）；
  * 图形页签：以 xml.etree.ElementTree.Element 为后台模型，QTreeWidget 展示
    脚本结构，右侧属性表单按标签类型动态生成，支持添加/删除/上移/下移节点。
- 保存前复用 ScriptParser 做完整的加载期静态校验（E1xx/E2xx），
  校验不通过时在对话框底部列出全部错误（错误码 + 行列 + 说明），不写文件。
- 注意：经图形页签往返一次会丢弃 XML 注释（ElementTree 默认不保留注释），
  页签内有明确提示；仅在 XML 页签编辑则原文不动。
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config_loader import StationConfig
from ..errors import ScriptParseErrors
from ..parser import ScriptParser

# ---------------------------------------------------------------- 标签元数据
TAG_CN = {
    "test": "脚本",
    "connect": "连接资源",
    "disconnect": "断开资源",
    "step": "测试步",
    "delay": "延时",
    "send": "发送帧",
    "wait": "等待应答",
    "field": "字段提取",
    "limit": "判定",
    "action": "扩展动作",
}

# 属性表单规格：(属性名, 中文标签, 控件类型, 是否必填)
# 控件类型：str / int / float / bool / endian / drain / onfail
ATTR_SPEC: dict[str, list[tuple[str, str, str, bool]]] = {
    "test": [
        ("name", "脚本名称", "str", True),
        ("version", "版本（如 1.0）", "str", True),
    ],
    "connect": [
        ("resource", "逻辑资源名", "str", True),
        ("timeout", "打开超时（秒）", "float", False),
    ],
    "disconnect": [
        ("resource", "逻辑资源名", "str", True),
    ],
    "delay": [
        ("ms", "延时（毫秒）", "int", True),
    ],
    "step": [
        ("name", "测试项名称", "str", True),
        ("resource", "逻辑资源（单资源可省略）", "str", False),
        ("timeout", "单次等待超时（秒）", "float", False),
        ("retry", "通信异常重试次数", "int", False),
        ("retry_interval", "重试间隔（秒）", "float", False),
        ("on_fail", "失败策略", "onfail", False),
    ],
    "send": [
        ("id", "CAN ID（如 0x18FF50E5）", "str", True),
        ("id_mask", "匹配掩码 ID", "str", False),
        ("ext", "是否扩展帧（29 位）", "bool", False),
        ("data", "数据字节（空格分隔，如 02 01 00）", "str", False),
    ],
    "wait": [
        ("id", "应答 CAN ID", "str", True),
        ("id_mask", "匹配掩码 ID", "str", False),
        ("ext", "是否扩展帧（29 位）", "bool", False),
        ("timeout", "本次等待超时（秒）", "float", False),
        ("drain", "缓冲清空方式", "drain", False),
        ("min_len", "应答最小数据长度", "int", False),
    ],
    "field": [
        ("var", "变量名", "str", True),
        ("offset", "起始字节偏移（从 0 开始）", "int", True),
        ("length", "占用字节数（1–8）", "int", False),
        ("endian", "多字节字节序", "endian", False),
        ("gain", "缩放系数 gain", "float", False),
        ("bias", "偏移量 bias", "float", False),
        ("raw", "取原始整数（忽略 gain/bias）", "bool", False),
        ("unit", "工程单位（仅记录）", "str", False),
    ],
    "limit": [
        ("value", "被判定值（数值或 ${变量}）", "str", True),
        ("min", "下限（闭区间）", "float", False),
        ("max", "上限（闭区间）", "float", False),
        ("eq", "等值判据（与 min/max 互斥）", "str", False),
        ("unit", "单位（仅记录）", "str", False),
    ],
    "action": [
        ("handler", "处理器（模块名:函数名）", "str", True),
        ("var", "接收返回值的变量名", "str", False),
    ],
}

# 各容器允许添加的子标签
CHILD_TAGS: dict[str, tuple[str, ...]] = {
    "test": ("connect", "disconnect", "delay", "step"),
    "step": ("send", "wait", "delay", "action", "limit"),
    "wait": ("field",),
}
STEP_CHILD_TAGS = set(CHILD_TAGS["step"])

_ENUM_OPTIONS = {
    "bool": (("", "（默认）"), ("true", "true"), ("false", "false")),
    "endian": (("", "（默认 little）"), ("little", "little 小端"), ("big", "big 大端")),
    "drain": (("", "（默认 before）"), ("before", "before 发前清空"), ("off", "off 不清空")),
    "onfail": (("", "（默认 abort）"), ("abort", "abort 终止 Run"),
               ("continue", "continue 记录后继续")),
}

# 新建节点时给出的可直接编辑的初始属性
_NEW_DEFAULTS = {
    "connect": {"resource": ""},
    "disconnect": {"resource": ""},
    "delay": {"ms": "100"},
    "step": {"name": "NEW_STEP"},
    "send": {"id": "0x100"},
    "wait": {"id": "0x100"},
    "field": {"var": "new_var", "offset": "0"},
    "limit": {"value": "${new_var}", "min": "0"},
    "action": {"handler": "module:func"},
}

XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>\n'


def new_script_template(resource: str = "") -> str:
    """新建脚本的初始内容。"""
    res = resource or "can_main"
    return (
        XML_DECLARATION
        + f"""<test name="NEW_SCRIPT" version="1.0">
    <connect resource="{res}"/>
    <step name="Step1" timeout="2" retry="1" on_fail="abort">
        <send id="0x100" data="01"/>
        <wait id="0x101"/>
    </step>
    <disconnect resource="{res}"/>
</test>
"""
    )


def _clean_whitespace(el: ET.Element) -> None:
    """删除仅用于缩进的 text/tail，供 ET.indent 重新排版。"""
    if el.text is not None and not el.text.strip():
        el.text = None
    if el.tail is not None and not el.tail.strip():
        el.tail = None
    for child in el:
        _clean_whitespace(child)


def serialize(root: ET.Element) -> str:
    """将元素树序列化为带 XML 声明、带缩进的脚本文本。"""
    _clean_whitespace(root)
    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return XML_DECLARATION + body + "\n"


def node_label(el: ET.Element) -> str:
    """树节点的可读标题。"""
    tag = el.tag
    a = el.attrib
    if tag == "test":
        return f"脚本 · {a.get('name', '(未命名)')}  v{a.get('version', '?')}"
    if tag == "connect":
        return f"连接资源 · {a.get('resource', '(未选择)')}"
    if tag == "disconnect":
        return f"断开资源 · {a.get('resource', '(未选择)')}"
    if tag == "delay":
        return f"延时 · {a.get('ms', '?')} ms"
    if tag == "step":
        extra = " continue" if a.get("on_fail") == "continue" else ""
        return f"测试步 · {a.get('name', '(未命名)')}{extra}"
    if tag == "send":
        ext = " 扩展帧" if a.get("ext") == "true" else ""
        data = f"  data=[{a['data']}]" if a.get("data") else ""
        return f"发送 → {a.get('id', '?')}{ext}{data}"
    if tag == "wait":
        n = len(el)
        fields = f"  字段×{n}" if n else ""
        return f"等待 ← {a.get('id', '?')}{fields}"
    if tag == "field":
        return f"字段 · {a.get('var', '?')}[{a.get('offset', '?')}]"
    if tag == "limit":
        if a.get("eq") is not None:
            crit = f"== {a['eq']}"
        else:
            lo, hi = a.get("min"), a.get("max")
            crit = f"[{lo if lo is not None else '−∞'}, {hi if hi is not None else '+∞'}]"
        return f"判定 · {a.get('value', '?')} {crit}"
    if tag == "action":
        return f"扩展动作 · {a.get('handler', '?')}"
    return TAG_CN.get(tag, tag)


class ScriptEditorDialog(QDialog):
    """脚本编辑器对话框。

    返回值：exec() == Accepted 且 self.saved_path 指向写入的文件。
    """

    def __init__(self, config: StationConfig, path: Path | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.config = config
        self.path = path
        self.saved_path: Path | None = None
        self.root: ET.Element | None = None
        self._dirty = False
        self._form_widgets: dict[str, QWidget] = {}

        first_resource = next(iter(config.resources), "")
        if path is not None:
            self._initial_text = path.read_text(encoding="utf-8")
            title = f"编辑脚本 — {path.name}"
        else:
            self._initial_text = new_script_template(first_resource)
            title = "新建脚本"

        self.setWindowTitle(title)
        self.resize(900, 680)
        self._build_ui()
        self.xml_edit.setPlainText(self._initial_text)
        self._dirty = False

    # ============================================================ UI 构建
    def _build_ui(self) -> None:
        root_lay = QVBoxLayout(self)

        self.tabs = QTabWidget()
        root_lay.addWidget(self.tabs)

        # --- XML 页签 --------------------------------------------------
        xml_tab = QWidget()
        xv = QVBoxLayout(xml_tab)
        self.xml_edit = QPlainTextEdit()
        font = self.xml_edit.font()
        font.setFamily("Consolas, Courier New, monospace")
        self.xml_edit.setFont(font)
        self.xml_edit.setPlaceholderText("在此直接编辑 Script XML…")
        self.xml_edit.textChanged.connect(self._on_text_changed)
        xv.addWidget(self.xml_edit)
        self.tabs.addTab(xml_tab, "XML 编辑")

        # --- 图形页签 ---------------------------------------------------
        graph_tab = QWidget()
        gv = QVBoxLayout(graph_tab)
        splitter = QSplitter(Qt.Horizontal)

        # 左：树 + 结构操作按钮
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("脚本结构")
        self.tree.currentItemChanged.connect(self._on_tree_select)
        lv.addWidget(self.tree, stretch=1)

        add_row = QHBoxLayout()
        self.add_combo = QComboBox()
        self.add_combo.setMinimumWidth(140)
        add_row.addWidget(self.add_combo, stretch=1)
        btn_add = QPushButton("添加")
        btn_add.clicked.connect(self._add_node)
        add_row.addWidget(btn_add)
        lv.addLayout(add_row)

        mov_row = QHBoxLayout()
        self.btn_del = QPushButton("删除节点")
        self.btn_del.clicked.connect(self._delete_node)
        self.btn_up = QPushButton("上移")
        self.btn_up.clicked.connect(lambda: self._move_node(-1))
        self.btn_down = QPushButton("下移")
        self.btn_down.clicked.connect(lambda: self._move_node(1))
        mov_row.addWidget(self.btn_del)
        mov_row.addWidget(self.btn_up)
        mov_row.addWidget(self.btn_down)
        lv.addLayout(mov_row)
        splitter.addWidget(left)

        # 右：属性表单
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.node_title = QLabel("请选择左侧节点")
        f = self.node_title.font()
        f.setBold(True)
        self.node_title.setFont(f)
        rv.addWidget(self.node_title)

        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.form_holder: QWidget | None = None
        rv.addWidget(self.form_scroll, stretch=1)

        self.action_extra: QPlainTextEdit | None = None
        hint = QLabel("提示：图形化编辑会保留节点结构与属性，但不保留 XML 注释；"
                      "需要保留注释请在“XML 编辑”页签中修改。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#806000;")
        rv.addWidget(hint)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        gv.addWidget(splitter)
        self.tabs.addTab(graph_tab, "图形化编辑")

        self.tabs.currentChanged.connect(self._on_tab_changed)

        # --- 底部：校验结果 + 按钮 -------------------------------------
        self.error_view = QPlainTextEdit()
        self.error_view.setReadOnly(True)
        self.error_view.setMaximumBlockCount(100)
        self.error_view.setFixedHeight(92)
        self.error_view.setStyleSheet("color:#D02424;")
        self.error_view.setPlaceholderText("校验通过后此处无提示。")
        root_lay.addWidget(self.error_view)

        buttons = QDialogButtonBox()
        self.btn_check = buttons.addButton("校验", QDialogButtonBox.ActionRole)
        self.btn_save = buttons.addButton("保存", QDialogButtonBox.AcceptRole)
        self.btn_cancel = buttons.addButton("取消", QDialogButtonBox.RejectRole)
        self.btn_check.clicked.connect(self._on_check)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_cancel.clicked.connect(self.reject)
        root_lay.addWidget(buttons)

    # ============================================================ 页签同步
    def _on_tab_changed(self, index: int) -> None:
        if index == 1:  # 切到图形页签：XML 文本 -> 元素树
            self._sync_text_to_graph()
        else:           # 切到 XML 页签：元素树 -> 文本
            self._sync_graph_to_text()

    def _sync_text_to_graph(self) -> bool:
        try:
            root = ET.fromstring(self.xml_edit.toPlainText())
        except ET.ParseError as e:
            QMessageBox.warning(self, "无法图形化编辑",
                                f"XML 非良构，请先在“XML 编辑”中修正：\n{e}")
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(0)
            self.tabs.blockSignals(False)
            return False
        if root.tag != "test":
            QMessageBox.warning(self, "无法图形化编辑",
                                f"根节点必须是 <test>，实际为 <{root.tag}>。")
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(0)
            self.tabs.blockSignals(False)
            return False
        self.root = root
        self._rebuild_tree()
        return True

    def _sync_graph_to_text(self) -> None:
        if self.root is None:
            return
        self._apply_form_to_element()           # 属性表单可能有未落盘编辑
        self.xml_edit.blockSignals(True)
        self.xml_edit.setPlainText(serialize(copy.deepcopy(self.root)))
        self.xml_edit.blockSignals(False)

    def _on_text_changed(self) -> None:
        self._dirty = True
        self.error_view.clear()

    # ============================================================ 树
    def _rebuild_tree(self, select_el: ET.Element | None = None) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        top = self._make_item(self.root)
        self.tree.addTopLevelItem(top)
        self._fill_children(top, self.root)
        top.setExpanded(True)
        self.tree.expandAll()

        target = top
        if select_el is not None:
            found = self._find_item(top, select_el)
            if found is not None:
                target = found
        self.tree.setCurrentItem(target)
        self.tree.blockSignals(False)
        self._on_tree_select(target, None)

    def _make_item(self, el: ET.Element) -> QTreeWidgetItem:
        item = QTreeWidgetItem([node_label(el)])
        item.setData(0, Qt.UserRole, el)
        return item

    def _fill_children(self, item: QTreeWidgetItem, el: ET.Element) -> None:
        allowed = CHILD_TAGS.get(el.tag, ())
        for child in el:
            if child.tag not in allowed:
                continue
            child_item = self._make_item(child)
            item.addChild(child_item)
            self._fill_children(child_item, child)

    def _find_item(self, item: QTreeWidgetItem, el: ET.Element) -> QTreeWidgetItem | None:
        if item.data(0, Qt.UserRole) is el:
            return item
        for i in range(item.childCount()):
            found = self._find_item(item.child(i), el)
            if found is not None:
                return found
        return None

    def _current(self) -> tuple[QTreeWidgetItem | None, ET.Element | None]:
        item = self.tree.currentItem()
        if item is None:
            return None, None
        return item, item.data(0, Qt.UserRole)

    def _on_tree_select(self, current: QTreeWidgetItem | None,
                        _previous: QTreeWidgetItem | None) -> None:
        self._refresh_add_combo(current)
        if current is None:
            self.btn_del.setEnabled(False)
            self.btn_up.setEnabled(False)
            self.btn_down.setEnabled(False)
            self._rebuild_form(None)
            return
        el = current.data(0, Qt.UserRole)
        is_root = el.tag == "test"
        self.btn_del.setEnabled(not is_root)
        self.btn_up.setEnabled(not is_root)
        self.btn_down.setEnabled(not is_root)
        self._rebuild_form(el)

    def _container_for(self, item: QTreeWidgetItem, el: ET.Element
                       ) -> tuple[ET.Element, str] | None:
        """返回 (容器元素, 容器类型)。容器类型为 test/step/wait。"""
        if el.tag in CHILD_TAGS:
            return el, el.tag
        parent_item = item.parent()
        if parent_item is not None:
            parent_el = parent_item.data(0, Qt.UserRole)
            if parent_el.tag in CHILD_TAGS:
                return parent_el, parent_el.tag
        return None

    def _refresh_add_combo(self, item: QTreeWidgetItem | None) -> None:
        self.add_combo.clear()
        if item is None:
            self.add_combo.setEnabled(False)
            return
        el = item.data(0, Qt.UserRole)
        info = self._container_for(item, el)
        if info is None:
            self.add_combo.setEnabled(False)
            return
        _, kind = info
        for tag in CHILD_TAGS[kind]:
            self.add_combo.addItem(f"{TAG_CN[tag]} <{tag}>", tag)
        self.add_combo.setEnabled(True)

    # ============================================================ 结构操作
    def _add_node(self) -> None:
        item, el = self._current()
        if item is None or el is None:
            return
        info = self._container_for(item, el)
        if info is None:
            return
        container, _kind = info
        tag = self.add_combo.currentData()
        if tag is None:
            return
        new_el = ET.Element(tag, attrib=dict(_NEW_DEFAULTS.get(tag, {})))
        # 新建测试步自动带一个等待指令，保证至少含一条通信指令（否则 E108）
        if tag == "step":
            new_el.append(ET.Element("wait", attrib={"id": "0x100"}))
        # limit 必须在最后；其它指令若已有 limit，则插到 limit 之前
        if tag == "limit":
            container.append(new_el)
        elif len(container) and container[-1].tag == "limit":
            container.insert(len(container) - 1, new_el)
        else:
            container.append(new_el)
        self._dirty = True
        self._rebuild_tree(select_el=new_el)

    def _delete_node(self) -> None:
        item, el = self._current()
        if item is None or el is None or el.tag == "test":
            return
        parent_item = item.parent()
        if parent_item is None:
            return
        parent_el = parent_item.data(0, Qt.UserRole)
        if QMessageBox.question(
                self, "删除节点",
                f"确定删除 <{el.tag}> 及其全部子节点？") != QMessageBox.StandardButton.Yes:
            return
        parent_el.remove(el)
        self._dirty = True
        self._rebuild_tree(select_el=parent_el)

    def _move_node(self, delta: int) -> None:
        item, el = self._current()
        if item is None or el is None or el.tag == "test":
            return
        parent_item = item.parent()
        if parent_item is None:
            return
        parent_el = parent_item.data(0, Qt.UserRole)
        idx = list(parent_el).index(el)
        new_idx = idx + delta
        if not 0 <= new_idx < len(parent_el):
            return
        parent_el.remove(el)
        parent_el.insert(new_idx, el)
        self._dirty = True
        self._rebuild_tree(select_el=el)

    # ============================================================ 属性表单
    def _rebuild_form(self, el: ET.Element | None) -> None:
        # 先把旧表单对旧元素的修改落盘（正常选择切换时旧表单仍显示旧值）
        self._form_widgets = {}
        self.action_extra = None

        old = self.form_scroll.takeWidget()
        if old is not None:
            old.deleteLater()

        holder = QWidget()
        form = QFormLayout(holder)
        form.setLabelAlignment(Qt.AlignRight)
        self.form_scroll.setWidget(holder)
        self.form_holder = holder

        if el is None:
            self.node_title.setText("请选择左侧节点")
            return
        self.node_title.setText(f"<{el.tag}>　{TAG_CN.get(el.tag, '')}")

        for attr, label, kind, required in ATTR_SPEC.get(el.tag, []):
            widget = self._make_value_widget(kind)
            self._set_widget_value(widget, kind, el.attrib.get(attr, ""))
            self._connect_widget(widget, lambda _v=None: self._on_form_edited())
            mark = "* " if required else ""
            form.addRow(mark + label, widget)
            self._form_widgets[attr] = widget

        # action 的扩展入参：白名单外的任意属性，每行 key=value
        if el.tag == "action":
            self.action_extra = QPlainTextEdit()
            self.action_extra.setFixedHeight(90)
            lines = [f"{k}={v}" for k, v in el.attrib.items()
                     if k not in ("handler", "var")]
            self.action_extra.setPlainText("\n".join(lines))
            self.action_extra.textChanged.connect(self._on_form_edited)
            form.addRow("扩展入参（每行 key=value）", self.action_extra)

    def _make_value_widget(self, kind: str) -> QWidget:
        if kind in _ENUM_OPTIONS:
            combo = QComboBox()
            for value, text in _ENUM_OPTIONS[kind]:
                combo.addItem(text, value)
            return combo
        edit = QLineEdit()
        return edit

    def _set_widget_value(self, widget: QWidget, kind: str, value: str) -> None:
        if isinstance(widget, QComboBox):
            idx = widget.findData(value)
            widget.setCurrentIndex(idx if idx >= 0 else 0)
        else:
            widget.setText(value)

    def _widget_value(self, widget: QWidget) -> str:
        if isinstance(widget, QComboBox):
            return widget.currentData() or ""
        return widget.text().strip()

    def _connect_widget(self, widget: QWidget, slot) -> None:
        if isinstance(widget, QComboBox):
            widget.currentIndexChanged.connect(slot)
        else:
            widget.textChanged.connect(slot)

    def _on_form_edited(self) -> None:
        self._apply_form_to_element()
        self._dirty = True
        item, _el = self._current()
        if item is not None:
            el = item.data(0, Qt.UserRole)
            item.setText(0, node_label(el))

    def _apply_form_to_element(self) -> None:
        item = self.tree.currentItem() if hasattr(self, "tree") else None
        if item is None:
            return
        el = item.data(0, Qt.UserRole)
        for attr, widget in self._form_widgets.items():
            value = self._widget_value(widget)
            if value == "":
                el.attrib.pop(attr, None)
            else:
                el.set(attr, value)
        if el.tag == "action" and self.action_extra is not None:
            for k in list(el.attrib):
                if k not in ("handler", "var"):
                    del el.attrib[k]
            for line in self.action_extra.toPlainText().splitlines():
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k and k not in ("handler", "var"):
                    el.set(k, v.strip())

    # ============================================================ 校验 / 保存
    def _current_text(self) -> str:
        if self.tabs.currentIndex() == 1 and self.root is not None:
            self._apply_form_to_element()
            return serialize(copy.deepcopy(self.root))
        return self.xml_edit.toPlainText()

    def validate_text(self, text: str) -> str:
        """用正式解析器做静态校验，返回错误信息文本（空串表示通过）。"""
        parser = ScriptParser(
            station_resources=self.config.resource_names(),
            allowed_extensions=set(self.config.extensions_allowed))
        try:
            parser.parse_bytes(text.encode("utf-8"), source_path=str(self.path or ""))
        except ScriptParseErrors as exc:
            return "\n".join(str(item) for item in exc.errors)
        except Exception as exc:  # noqa: BLE001 - 其它异常同样展示给用户
            return str(exc)
        return ""

    def _on_check(self) -> bool:
        text = self._current_text()
        msg = self.validate_text(text)
        if msg:
            self.error_view.setPlainText(msg)
            return False
        self.error_view.setPlainText("✔ 校验通过：脚本符合 Script XML v1 规范。")
        return True

    def _on_save(self) -> None:
        text = self._current_text()
        msg = self.validate_text(text)
        if msg:
            self.error_view.setPlainText(msg)
            QMessageBox.warning(self, "校验未通过",
                                "脚本存在错误，已在下方列出，修正后才能保存。")
            return

        path = self.path
        if path is None:
            start_dir = str(self.config.base_dir.parent / "scripts")
            chosen, _ = QFileDialog.getSaveFileName(
                self, "保存新脚本", start_dir, "测试脚本 (*.xml)")
            if not chosen:
                return
            path = Path(chosen)
            if path.suffix.lower() != ".xml":
                path = path.with_suffix(".xml")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.path = path
        self.saved_path = path
        self._dirty = False
        self.accept()

    # ============================================================ 取消确认
    def reject(self) -> None:
        if self._dirty:
            ans = QMessageBox.question(
                self, "放弃修改", "当前修改尚未保存，确定放弃并关闭？")
            if ans != QMessageBox.StandardButton.Yes:
                return
        super().reject()
