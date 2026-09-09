"""脚本解析器：XML -> Script。

设计要点（见《ATE脚本格式规范.md》）：
- 加载期完成全部静态校验，收集尽量多的错误一次性报出（ScriptParseErrors）；
- 属性类型解析、结构约束、变量"先定义后使用"数据流检查、资源检查、扩展白名单；
- 运行期才可能知道的事情（实际帧长、变量值）不在这里。
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET

from .errors import ScriptError, ScriptParseErrors
from .model import (
    Action,
    ConnectStmt,
    DelayStmt,
    DisconnectStmt,
    Field,
    Limit,
    Script,
    Send,
    Step,
    VarRef,
    Wait,
)

_IDENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_VAR_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")
_VARREF_RE = re.compile(r"^\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}$")
_BYTE_TOKEN_RE = re.compile(r"^[0-9A-Fa-f]{1,2}$")

TOP_TAGS = {"connect", "disconnect", "step", "delay"}
STEP_TAGS = {"send", "wait", "delay", "action", "limit"}
WAIT_TAGS = {"field"}
BOOL_TRUE = {"true", "1"}
BOOL_FALSE = {"false", "0"}

# 各标签允许的属性白名单（action 除外，其扩展属性作为入参）
ALLOWED_ATTRS: dict[str, set[str]] = {
    "test": {"name", "version"},
    "connect": {"resource", "timeout"},
    "disconnect": {"resource"},
    "delay": {"ms"},
    "step": {"name", "resource", "timeout", "retry", "retry_interval", "on_fail"},
    "send": {"id", "id_mask", "ext", "data"},
    "wait": {"id", "id_mask", "ext", "timeout", "drain", "min_len"},
    "field": {"var", "offset", "length", "endian", "gain", "bias", "raw", "unit"},
    "limit": {"value", "min", "max", "eq", "unit"},
    "action": {"handler", "var"},
}
REQUIRED_ATTRS: dict[str, set[str]] = {
    "test": {"name", "version"},
    "connect": {"resource"},
    "disconnect": {"resource"},
    "delay": {"ms"},
    "step": {"name"},
    "send": {"id"},
    "wait": {"id"},
    "field": {"var", "offset"},
    "limit": {"value"},
    "action": {"handler"},
}


class _Errors:
    """收集解析错误。"""

    def __init__(self):
        self.items: list[ScriptError] = []

    def add(self, message: str, code: str, el: ET.Element | None = None,
            line: int = 0, column: int = 0):
        ln = line or getattr(el, "_line", 0)
        col = column or 0
        self.items.append(ScriptError(message, code, ln, col))

    def raise_if_any(self):
        if self.items:
            raise ScriptParseErrors(self.items)


def _pos(el: ET.Element) -> tuple[int, int]:
    return getattr(el, "_line", 0), 0


def _parse_bool(value: str) -> bool:
    low = value.strip().lower()
    if low in BOOL_TRUE:
        return True
    if low in BOOL_FALSE:
        return False
    raise ValueError(f"布尔值只接受 true/false/1/0，实际为 {value!r}")


def _parse_can_id(value: str) -> int:
    v = value.strip().lower()
    try:
        if v.startswith("0x"):
            return int(v, 16)
        return int(v)
    except ValueError as exc:
        raise ValueError(f"非法 CAN ID: {value!r}") from exc


def _attr(el: ET.Element, name: str, default=None):
    return el.attrib.get(name, default)


def _resolve_value_or_ref(raw: str) -> VarRef | str | int | float:
    """limit value：整体变量引用 / 数值字面量 / 普通字符串。"""
    m = _VARREF_RE.match(raw.strip())
    if m:
        return VarRef(m.group(1), *())  # line 由调用方补
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


class ScriptParser:
    def __init__(self, station_resources: set[str] | None = None,
                 allowed_extensions: set[str] | None = None):
        self.station_resources = station_resources or set()
        self.allowed_extensions = allowed_extensions or set()

    def parse_file(self, path: str) -> Script:
        with open(path, "rb") as f:
            data = f.read()
        return self.parse_bytes(data, source_path=path)

    def parse_bytes(self, data: bytes, source_path: str = "") -> Script:
        sha = hashlib.sha256(data).hexdigest()
        errs = _Errors()

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            errs.add("脚本文件不是有效的 UTF-8 编码", "E101")
            errs.raise_if_any()
            raise  # for type checkers

        try:
            parser = _LineNumberingParser()
            root = ET.fromstring(text, parser=parser)
        except ET.ParseError as e:
            line, col = e.position
            errs.add(f"XML 非良构: {e.msg}", "E101", line=line, column=col)
            errs.raise_if_any()
            raise  # unreachable

        if root.tag != "test":
            errs.add(f"根节点必须是 <test>，实际为 <{root.tag}>", "E102", root)
            errs.raise_if_any()

        name = _attr(root, "name", "")
        version = _attr(root, "version", "")
        self._check_attrs(root, errs)
        if not name or not _IDENT_RE.match(name or ""):
            errs.add("test 的 name 非法（需匹配 ^[A-Za-z0-9_-]+$）", "E105", root)
        if not _VERSION_RE.match(version or ""):
            errs.add("test 的 version 非法，需形如 1.0 / 1.2.1", "E105", root)

        statements: list = []
        step_names: set[str] = set()
        connected: set[str] = set()          # 当前位置之前已经 connect 的资源
        defined_vars: set[str] = set()       # 当前位置之前已产出的变量

        for el in root:
            tag = el.tag
            if tag not in TOP_TAGS:
                code = "E107" if tag in {"if", "loop", "call"} else "E102"
                hint = "（v2 预留标签，v1 不支持）" if code == "E107" else ""
                errs.add(f"顶层出现未知标签 <{tag}>{hint}", code, el)
                continue

            if tag == "connect":
                stmt = self._parse_connect(el, errs)
                if stmt:
                    if stmt.resource in connected:
                        errs.add(f"资源 {stmt.resource} 重复 connect", "E210", el)
                    connected.add(stmt.resource)
                    statements.append(stmt)
            elif tag == "disconnect":
                stmt = self._parse_disconnect(el, errs)
                if stmt:
                    if stmt.resource not in connected:
                        errs.add(
                            f"资源 {stmt.resource} disconnect 时未连接", "E210", el)
                    connected.discard(stmt.resource)
                    statements.append(stmt)
            elif tag == "delay":
                statements.append(self._parse_delay(el, errs))
            elif tag == "step":
                step = self._parse_step(el, errs, connected, defined_vars, step_names)
                if step:
                    statements.append(step)
                    # 本 step 产出的 field/action 变量对后续 step 可见
                    for instr in step.instructions:
                        if isinstance(instr, Wait):
                            defined_vars.update(f.var for f in instr.fields)
                        elif isinstance(instr, Action) and instr.var:
                            defined_vars.add(instr.var)

        errs.raise_if_any()
        return Script(name=name, version=version, sha256=sha,
                      statements=tuple(statements), path=source_path)

    # ------------------------------------------------------------ 语句
    def _check_attrs(self, el: ET.Element, errs: _Errors,
                     extra_allowed: set[str] | None = None) -> None:
        allowed = set(ALLOWED_ATTRS.get(el.tag, set()))
        if extra_allowed:
            allowed |= extra_allowed
        for key in el.attrib:
            if key not in allowed:
                errs.add(f"<{el.tag}> 含未知属性 {key!r}", "E103", el)
        for req in REQUIRED_ATTRS.get(el.tag, set()):
            if req not in el.attrib:
                errs.add(f"<{el.tag}> 缺少必填属性 {req!r}", "E104", el)

    def _parse_connect(self, el: ET.Element, errs: _Errors) -> ConnectStmt | None:
        self._check_attrs(el, errs)
        resource = _attr(el, "resource", "")
        timeout = 3.0
        if resource and self.station_resources and resource not in self.station_resources:
            errs.add(f"资源 {resource!r} 未在工位配置中定义", "E204", el)
        if "timeout" in el.attrib:
            try:
                timeout = float(el.attrib["timeout"])
                if timeout <= 0:
                    raise ValueError
            except ValueError:
                errs.add("connect 的 timeout 必须是正数（秒）", "E105", el)
        if not resource:
            return None
        line, _ = _pos(el)
        return ConnectStmt(resource=resource, timeout=timeout, line=line)

    def _parse_disconnect(self, el: ET.Element, errs: _Errors) -> DisconnectStmt | None:
        self._check_attrs(el, errs)
        resource = _attr(el, "resource", "")
        line, _ = _pos(el)
        return DisconnectStmt(resource=resource, line=line) if resource else None

    def _parse_delay(self, el: ET.Element, errs: _Errors) -> DelayStmt | None:
        self._check_attrs(el, errs)
        raw = _attr(el, "ms", "")
        try:
            ms = int(raw)
            if ms < 0:
                raise ValueError
        except (TypeError, ValueError):
            errs.add("delay 的 ms 必须是非负整数", "E105", el)
            return None
        line, _ = _pos(el)
        return DelayStmt(ms=ms, line=line)

    # ------------------------------------------------------------ step
    def _parse_step(self, el: ET.Element, errs: _Errors, connected: set[str],
                    defined_vars_in: set[str], step_names: set[str]) -> Step | None:
        self._check_attrs(el, errs)
        line, _ = _pos(el)

        name = _attr(el, "name", "")
        if name:
            if name in step_names:
                errs.add(f"step 名称 {name!r} 重复", "E106", el)
            step_names.add(name)

        timeout = self._num_attr(el, "timeout", 5.0, errs, positive=True)
        retry = self._int_attr(el, "retry", 0, errs, minimum=0)
        retry_interval = self._num_attr(el, "retry_interval", 0.0, errs, positive=False)
        on_fail = _attr(el, "on_fail", "abort")
        if on_fail not in ("abort", "continue"):
            errs.add("on_fail 只能是 abort 或 continue", "E105", el)
            on_fail = "abort"

        resource = _attr(el, "resource")
        if resource is not None:
            if self.station_resources and resource not in self.station_resources:
                errs.add(f"资源 {resource!r} 未在工位配置中定义", "E204", el)
            if resource not in connected:
                errs.add(f"step {name!r} 使用了尚未 connect 的资源 {resource!r}", "E210", el)
        elif len(connected) > 1:
            errs.add(
                f"step {name!r} 未指定 resource，但当前同时打开了多个资源 "
                f"{sorted(connected)}，必须显式指定", "E210", el)

        # 子元素解析
        instructions: list = []
        limit_el: ET.Element | None = None
        has_comm = False
        local_defined = set(defined_vars_in)

        children = list(el)
        for idx, child in enumerate(children):
            if child.tag not in STEP_TAGS:
                code = "E107" if child.tag in {"if", "loop", "call"} else "E102"
                hint = "（v2 预留标签，v1 不支持）" if code == "E107" else ""
                errs.add(f"step {name!r} 内出现未知标签 <{child.tag}>{hint}", code, child)
                continue

            if limit_el is not None:
                if child.tag == "limit":
                    errs.add(f"step {name!r} 中出现多个 limit（最多一个）", "E109", child)
                else:
                    errs.add(f"step {name!r} 中 limit 之后不允许再有指令", "E110", child)
                continue
            if child.tag == "limit":
                limit_el = child
                continue

            if child.tag == "send":
                instr = self._parse_send(child, errs, local_defined)
                has_comm = True
            elif child.tag == "wait":
                instr = self._parse_wait(child, errs, local_defined)
                has_comm = True
                for fld in instr.fields if instr else []:
                    local_defined.add(fld.var)
            elif child.tag == "delay":
                instr = self._parse_delay(child, errs)
            elif child.tag == "action":
                instr = self._parse_action(child, errs, local_defined)
                has_comm = True
                if instr and instr.var:
                    local_defined.add(instr.var)
            else:
                instr = None
            if instr is not None:
                instructions.append(instr)

        if not has_comm:
            errs.add(f"step {name!r} 内至少包含一条 send/wait/action", "E108", el)

        limit = self._parse_limit(limit_el, errs, local_defined, name) if limit_el is not None else None

        if not name:
            return None
        return Step(
            name=name, instructions=tuple(instructions), limit=limit,
            resource=resource, timeout=timeout, retry=retry,
            retry_interval=retry_interval, on_fail=on_fail, line=line,
        )

    # ------------------------------------------------------------ send
    def _parse_send(self, el: ET.Element, errs: _Errors, defined_vars: set[str]) -> Send | None:
        self._check_attrs(el, errs)
        line, _ = _pos(el)
        can_id = self._id_attr(el, errs)
        ext = self._bool_attr(el, "ext", False, errs)
        mask = None
        if "id_mask" in el.attrib:
            mask = self._id_attr(el, errs, attr="id_mask")
        if ext is not None and can_id is not None and not ext and can_id > 0x7FF:
            errs.add(f"标准帧 ID 不能超过 0x7FF（实际 0x{can_id:X}）", "E116", el)

        data: list = []
        raw_data = _attr(el, "data", "")
        if raw_data:
            for tok in raw_data.split():
                if _BYTE_TOKEN_RE.match(tok):
                    data.append(int(tok, 16))
                else:
                    m = _VARREF_RE.match(tok)
                    if m:
                        vname = m.group(1)
                        if vname not in defined_vars:
                            errs.add(f"send data 引用了尚未定义的变量 ${{{vname}}}", "E208", el)
                        data.append(VarRef(vname, line))
                    else:
                        errs.add(
                            f"data token {tok!r} 非法：需为 00-FF 十六进制字节或单字节 ${{var}}",
                            "E105", el)
            if len(data) > 8:
                errs.add(f"CAN data 最多 8 字节，实际 {len(data)} 字节", "E115", el)
        if can_id is None:
            return None
        return Send(id=can_id, data=tuple(data), ext=bool(ext), id_mask=mask, line=line)

    # ------------------------------------------------------------ wait / field
    def _parse_wait(self, el: ET.Element, errs: _Errors,
                    defined_vars: set[str]) -> Wait | None:
        self._check_attrs(el, errs)
        line, _ = _pos(el)
        can_id = self._id_attr(el, errs)
        ext = self._bool_attr(el, "ext", False, errs)
        mask = self._id_attr(el, errs, attr="id_mask") if "id_mask" in el.attrib else None
        if ext is not None and can_id is not None and not ext and can_id > 0x7FF:
            errs.add(f"标准帧 ID 不能超过 0x7FF（实际 0x{can_id:X}）", "E116", el)
        timeout = None
        if "timeout" in el.attrib:
            timeout = self._num_attr(el, "timeout", 5.0, errs, positive=True)
        drain = _attr(el, "drain", "before")
        if drain not in ("before", "off"):
            errs.add("wait 的 drain 只能是 before 或 off", "E105", el)
        min_len = self._int_attr(el, "min_len", 0, errs, minimum=0)
        if min_len is not None and min_len > 8:
            errs.add("min_len 不能超过 8", "E105", el)

        fields: list[Field] = []
        for child in el:
            if child.tag != "field":
                errs.add(f"<wait> 内只允许 <field>，实际出现 <{child.tag}>", "E102", child)
                continue
            fields.append(self._parse_field(child, errs))
        if can_id is None:
            return None
        return Wait(
            id=can_id, ext=bool(ext), id_mask=mask, timeout=timeout,
            drain=drain, min_len=min_len or 0, fields=tuple(f for f in fields if f),
            line=line,
        )

    def _parse_field(self, el: ET.Element, errs: _Errors) -> Field | None:
        self._check_attrs(el, errs)
        line, _ = _pos(el)
        var = _attr(el, "var", "")
        if not _VAR_RE.match(var or ""):
            errs.add(f"field 的 var 非法: {var!r}", "E105", el)
        offset = self._int_attr(el, "offset", None, errs, minimum=0)
        length = self._int_attr(el, "length", 1, errs, minimum=1)
        endian = _attr(el, "endian", "little")
        if endian not in ("little", "big"):
            errs.add("endian 只能是 little 或 big", "E105", el)
        gain = self._num_attr(el, "gain", 1.0, errs, positive=False)
        bias = self._num_attr(el, "bias", 0.0, errs, positive=False)
        raw = self._bool_attr(el, "raw", False, errs)
        unit = _attr(el, "unit")
        if length is not None and length > 8:
            errs.add("field length 不能超过 8 字节", "E117", el)
        if offset is not None and length is not None and offset + length > 8:
            errs.add(f"field {var!r} 提取范围 offset+length 超出 8 字节", "E117", el)
        if not var or offset is None:
            return None
        return Field(
            var=var, offset=offset, length=length or 1, endian=endian,  # type: ignore[arg-type]
            gain=gain if gain is not None else 1.0,
            bias=bias if bias is not None else 0.0,
            raw=bool(raw), unit=unit, line=line,
        )

    # ------------------------------------------------------------ action
    def _parse_action(self, el: ET.Element, errs: _Errors,
                      defined_vars: set[str]) -> Action | None:
        # action 的未知属性是合法入参，只检查保留属性
        for req in REQUIRED_ATTRS["action"]:
            if req not in el.attrib:
                errs.add(f"<action> 缺少必填属性 {req!r}", "E104", el)
        for reserved in ("driver", "method"):
            if reserved in el.attrib:
                errs.add(f"action 不支持属性 {reserved!r}（v1 使用 handler=\"module:func\"）", "E103", el)
        handler = _attr(el, "handler", "")
        var = _attr(el, "var")
        if var and not _VAR_RE.match(var):
            errs.add(f"action 的 var 非法: {var!r}", "E105", el)

        module = handler.split(":")[0] if handler and ":" in handler else ""
        if handler:
            if ":" not in handler or not all(part for part in handler.split(":")):
                errs.add(f"handler 格式必须是 \"module:func\"，实际 {handler!r}", "E202", el)
            elif self.allowed_extensions and module not in self.allowed_extensions:
                errs.add(f"扩展模块 {module!r} 未在工位配置 [extensions] allowed 白名单中", "E201", el)

        kwargs: dict = {}
        for key, val in el.attrib.items():
            if key in ("handler", "var"):
                continue
            m = _VARREF_RE.match(val.strip())
            if m:
                if m.group(1) not in defined_vars:
                    errs.add(f"action 入参 {key!r} 引用了尚未定义的变量 ${{{m.group(1)}}}", "E208", el)
                kwargs[key] = VarRef(m.group(1), *())
            else:
                kwargs[key] = val
        line, _ = _pos(el)
        if not handler or (":" not in handler):
            return None
        return Action(handler=handler, kwargs=kwargs, var=var, line=line)

    # ------------------------------------------------------------ limit
    def _parse_limit(self, el: ET.Element, errs: _Errors, defined_vars: set[str],
                     step_name: str) -> Limit | None:
        self._check_attrs(el, errs)
        line, _ = _pos(el)
        if not any(k in el.attrib for k in ("min", "max", "eq")):
            errs.add(f"step {step_name!r} 的 limit 未给出任何判据（min/max/eq）", "E111", el)
        if "eq" in el.attrib and ("min" in el.attrib or "max" in el.attrib):
            errs.add(f"step {step_name!r} 的 limit 中 eq 与 min/max 互斥", "E112", el)

        value = self._judge_value(el, "value", errs, defined_vars, step_name)
        eq = self._judge_value(el, "eq", errs, defined_vars, step_name) if "eq" in el.attrib else None
        lo = hi = None
        if "min" in el.attrib:
            try:
                lo = float(el.attrib["min"])
            except ValueError:
                errs.add(f"step {step_name!r} 的 min 不是数值", "E113", el)
        if "max" in el.attrib:
            try:
                hi = float(el.attrib["max"])
            except ValueError:
                errs.add(f"step {step_name!r} 的 max 不是数值", "E113", el)
        if lo is not None and hi is not None and lo > hi:
            errs.add(f"step {step_name!r} 的 min({lo}) > max({hi})", "E113", el)
        return Limit(value=value, min=lo, max=hi, eq=eq, unit=_attr(el, "unit"), line=line)

    def _judge_value(self, el: ET.Element, attr: str, errs: _Errors,
                     defined_vars: set[str], step_name: str):
        raw = el.attrib[attr]
        token = raw.strip()
        m = _VARREF_RE.match(token)
        if m:
            if m.group(1) not in defined_vars:
                errs.add(
                    f"step {step_name!r} 的 limit {attr} 引用了尚未定义的变量 ${{{m.group(1)}}}",
                    "E208", el)
            return VarRef(m.group(1), *())
        # 数值位不允许混合文本：要么整数/浮点，要么报 E114
        try:
            return int(token)
        except ValueError:
            pass
        try:
            return float(token)
        except ValueError:
            pass
        if attr == "eq":
            return token                      # eq 允许字符串/状态字面量
        errs.add(f"step {step_name!r} 的 limit {attr} 必须是数值或单个 ${{var}}", "E114", el)
        return token

    # ------------------------------------------------------------ 属性小工具
    def _num_attr(self, el, name, default, errs, positive=True) -> float | None:
        if name not in el.attrib:
            return default
        try:
            v = float(el.attrib[name])
            if positive and v <= 0:
                raise ValueError
            if not positive and v < 0:
                raise ValueError
            return v
        except ValueError:
            errs.add(f"<{el.tag}> 的 {name} 必须是{'正' if positive else '非负'}数", "E105", el)
            return default

    def _int_attr(self, el, name, default, errs, minimum=0) -> int | None:
        if name not in el.attrib:
            return default
        try:
            v = int(el.attrib[name])
            if v < minimum:
                raise ValueError
            return v
        except ValueError:
            errs.add(f"<{el.tag}> 的 {name} 必须是 >= {minimum} 的整数", "E105", el)
            return default

    def _bool_attr(self, el, name, default, errs) -> bool:
        if name not in el.attrib:
            return default
        try:
            return _parse_bool(el.attrib[name])
        except ValueError:
            errs.add(f"<{el.tag}> 的 {name} 布尔值非法（true/false/1/0）", "E105", el)
            return default

    def _id_attr(self, el, errs, attr: str = "id") -> int | None:
        if attr not in el.attrib:
            return None
        try:
            return _parse_can_id(el.attrib[attr])
        except ValueError:
            errs.add(f"<{el.tag}> 的 {attr} 非法", "E105", el)
            return None


class _LineNumberingParser(ET.XMLParser):
    """XMLParser 子类：_start 时用 expat 当前行号给元素打 _line。"""

    def _start(self, tag, attr_list):
        el = super()._start(tag, attr_list)
        el._line = self.parser.CurrentLineNumber
        return el
