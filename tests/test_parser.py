"""解析器静态校验测试。"""

import unittest

from simple_ate.errors import ScriptParseErrors
from simple_ate.engine.parser import ScriptParser

RESOURCES = {"can_main"}
EXTS = {"sample_ext"}


def parse(xml: str, resources=None, exts=None):
    p = ScriptParser(resources or RESOURCES, set(exts if exts is not None else EXTS))
    return p.parse_bytes(xml.encode("utf-8"))


def codes(exc: ScriptParseErrors) -> set[str]:
    return {e.code for e in exc.errors}


VALID = """<?xml version="1.0"?>
<test name="T" version="1.0">
  <connect resource="can_main"/>
  <step name="S1" timeout="2" retry="1" on_fail="continue">
    <send id="0x100" data="01 00"/>
    <wait id="0x101">
      <field var="ch" offset="0" length="1"/>
    </wait>
  </step>
  <step name="S2" timeout="2">
    <send id="0x100" data="01 ${ch}"/>
    <wait id="0x101"/>
  </step>
</test>
"""


class TestParserOK(unittest.TestCase):
    def test_valid(self):
        s = parse(VALID)
        self.assertEqual(s.name, "T")
        self.assertEqual(len(s.statements), 3)   # connect + 2 step

    def test_ext_id(self):
        s = parse("""<test name="T" version="1.0">
          <connect resource="can_main"/>
          <step name="S"><send id="0x18FF50E5" ext="true" data="01"/>
          <wait id="0x18FF50E6" ext="true"/></step></test>""")
        step = s.statements[1]
        self.assertTrue(step.instructions[0].ext)

    def test_action_whitelist(self):
        s = parse("""<test name="T" version="1.0"><connect resource="can_main"/>
          <step name="S"><action handler="sample_ext:read_status" address="1" var="st"/>
          <limit value="${st}" eq="0"/></step></test>""")
        self.assertEqual(s.statements[1].instructions[0].handler,
                         "sample_ext:read_status")


class TestParserErrors(unittest.TestCase):
    def test_malformed(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("<test name=T version=1.0><step")
        self.assertIn("E101", codes(cm.exception))

    def test_unknown_tag_and_attr(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("""<test name="T" version="1.0"><connect resource="can_main"/>
              <step name="S" bogus="1"><frob id="0x100"/></step></test>""")
        c = codes(cm.exception)
        self.assertIn("E102", c)
        self.assertIn("E103", c)

    def test_missing_required(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0"><step name="S">'
                  '<send/><wait/></step></test>')
        self.assertIn("E104", codes(cm.exception))

    def test_bad_types(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("""<test name="T" version="1.0"><connect resource="can_main"/>
              <step name="S" timeout="abc" retry="-1">
                <send id="0x100"/><wait id="0x101" drain="x"/></step></test>""")
        self.assertIn("E105", codes(cm.exception))

    def test_duplicate_step_name(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("""<test name="T" version="1.0"><connect resource="can_main"/>
              <step name="S"><send id="0x1"/><wait id="0x2"/></step>
              <step name="S"><send id="0x1"/><wait id="0x2"/></step></test>""")
        self.assertIn("E106", codes(cm.exception))

    def test_v2_tag(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("""<test name="T" version="1.0"><if var="x" gt="1"/></test>""")
        self.assertIn("E107", codes(cm.exception))

    def test_step_no_comm(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0"><step name="S"><delay ms="10"/></step></test>')
        self.assertIn("E108", codes(cm.exception))

    def test_two_limits(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("""<test name="T" version="1.0"><connect resource="can_main"/>
              <step name="S"><send id="0x1"/><wait id="0x2"/>
                <limit value="1" min="0" max="2"/>
                <limit value="1" min="0"/>
              </step></test>""")
        self.assertIn("E109", codes(cm.exception))

    def test_limit_not_last(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("""<test name="T" version="1.0"><connect resource="can_main"/>
              <step name="S"><send id="0x1"/><wait id="0x2"/>
                <limit value="1" min="0" max="2"/>
                <delay ms="10"/>
              </step></test>""")
        self.assertIn("E110", codes(cm.exception))

    def test_min_greater_than_max(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("""<test name="T" version="1.0"><connect resource="can_main"/>
              <step name="S"><send id="0x1"/><wait id="0x2"/>
                <limit value="1" min="2" max="1"/></step></test>""")
        self.assertIn("E113", codes(cm.exception))

    def test_eq_min_mutex_and_no_criterion(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("""<test name="T" version="1.0"><connect resource="can_main"/>
              <step name="S"><send id="0x1"/><wait id="0x2"/>
                <limit value="1" eq="1" min="0"/></step></test>""")
        self.assertIn("E112", codes(cm.exception))
        with self.assertRaises(ScriptParseErrors) as cm2:
            parse("""<test name="T" version="1.0"><connect resource="can_main"/>
              <step name="S"><send id="0x1"/><wait id="0x2"/>
                <limit value="1"/></step></test>""")
        self.assertIn("E111", codes(cm2.exception))

    def test_data_too_long(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("""<test name="T" version="1.0"><connect resource="can_main"/>
              <step name="S"><send id="0x100" data="00 01 02 03 04 05 06 07 08"/>
                <wait id="0x101"/></step></test>""")
        self.assertIn("E115", codes(cm.exception))

    def test_standard_id_too_large(self):
        with self.assertRaises(ScriptParseErrors) as cm2:
            parse("""<test name="T" version="1.0"><connect resource="can_main"/>
              <step name="S"><send id="0x1000"/><wait id="0x101"/></step></test>""")
        self.assertIn("E116", codes(cm2.exception))
        # 扩展帧允许 29 位 ID
        parse("""<test name="T" version="1.0"><connect resource="can_main"/>
          <step name="S"><send id="0x18FF50E5" ext="true"/>
          <wait id="0x18FF50E6" ext="true"/></step></test>""")

    def test_field_range(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("""<test name="T" version="1.0"><connect resource="can_main"/>
              <step name="S"><send id="0x1"/><wait id="0x2">
                <field var="x" offset="6" length="4"/></wait></step></test>""")
        self.assertIn("E117", codes(cm.exception))

    def test_unknown_resource(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0"><connect resource="xyz"/></test>')
        self.assertIn("E204", codes(cm.exception))

    def test_undefined_var(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("""<test name="T" version="1.0"><connect resource="can_main"/>
              <step name="S"><send id="0x1" data="01 ${nope}"/><wait id="0x2"/></step>
              </test>""")
        self.assertIn("E208", codes(cm.exception))

    def test_multi_resource_requires_step_resource(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("""<test name="T" version="1.0">
              <connect resource="can_main"/><connect resource="rs485"/>
              <step name="S"><send id="0x1"/><wait id="0x2"/></step></test>""",
              resources={"can_main", "rs485"})
        self.assertIn("E210", codes(cm.exception))

    def test_duplicate_connect_and_bad_disconnect(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("""<test name="T" version="1.0">
              <connect resource="can_main"/><connect resource="can_main"/></test>""")
        self.assertIn("E210", codes(cm.exception))
        with self.assertRaises(ScriptParseErrors) as cm2:
            parse("""<test name="T" version="1.0">
              <disconnect resource="can_main"/></test>""")
        self.assertIn("E210", codes(cm2.exception))

    def test_step_after_disconnect(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("""<test name="T" version="1.0"><connect resource="can_main"/>
              <disconnect resource="can_main"/>
              <step name="S" resource="can_main"><send id="0x1"/><wait id="0x2"/></step>
              </test>""")
        self.assertIn("E210", codes(cm.exception))

    def test_extension_not_whitelisted(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse("""<test name="T" version="1.0"><connect resource="can_main"/>
              <step name="S"><action handler="evil:doit"/></step></test>""")
        self.assertIn("E201", codes(cm.exception))

    def test_multiple_errors_collected(self):
        try:
            parse('<test name="T" version="1.0"><step name="S"><bogus/></step></test>')
            self.fail("应报错")
        except ScriptParseErrors as e:
            self.assertGreaterEqual(len(e.errors), 2)


class TestProtocolExtensions(unittest.TestCase):
    # ---------------- connect：串口/Modbus/USB/CAN 配置 ----------------
    def test_connect_options(self):
        s = parse(
            '<test name="T" version="1.0">'
            '<connect resource="rs485" protocol="modbus" modbus_mode="rtu" '
            'port="COM4" baudrate="9600" parity="even" stopbits="1.5" '
            'bytesize="8" unit="3"/>'
            '</test>')
        stmt = s.statements[0]
        self.assertEqual(stmt.protocol, "modbus")
        self.assertEqual(stmt.option("baudrate"), 9600)
        self.assertEqual(stmt.option("parity"), "even")
        self.assertEqual(stmt.option("unit"), 3)
        self.assertAlmostEqual(stmt.option("stopbits"), 1.5)

    def test_connect_inline_resource_no_station(self):
        # 指定 protocol 后允许脚本内联定义资源（不必预先写在 station.toml）
        s = parse('<test name="T" version="1.0">'
                  '<connect resource="rs485" protocol="serial" port="COM1"/>'
                  '</test>')
        self.assertEqual(s.statements[0].protocol, "serial")

    def test_connect_unknown_resource_without_protocol(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0"><connect resource="nope"/></test>')
        self.assertIn("E204", codes(cm.exception))

    def test_connect_bad_protocol(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0">'
                  '<connect resource="can_main" protocol="bogus"/></test>')
        self.assertIn("E105", codes(cm.exception))

    def test_connect_bad_serial_params(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0">'
                  '<connect resource="rs485" protocol="serial" port="COM1" '
                  'bytesize="9" parity="xyz" stopbits="3"/></test>')
        self.assertIn("E105", codes(cm.exception))

    # ---------------- 串口放宽 CAN 的 8 字节限制 ----------------
    def test_serial_allows_long_data(self):
        s = parse('<test name="T" version="1.0">'
                  '<connect resource="rs485" protocol="serial" port="COM1"/>'
                  '<step name="S" resource="rs485">'
                  '<send id="0" data="00 01 02 03 04 05 06 07 08 09"/>'
                  '<wait id="0"/></step></test>')
        self.assertEqual(len(s.statements[1].instructions[0].data), 10)

    def test_can_data_too_long(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0">'
                  '<connect resource="can_main"/>'
                  '<step name="S"><send id="0x1" '
                  'data="00 01 02 03 04 05 06 07 08 09"/>'
                  '<wait id="0x2"/></step></test>')
        self.assertIn("E115", codes(cm.exception))

    # ---------------- limit：自动 / 人工判定 ----------------
    def test_limit_manual_ok(self):
        s = parse('<test name="T" version="1.0">'
                  '<connect resource="can_main"/>'
                  '<step name="S"><send id="0x1"/><wait id="0x2"/>'
                  '<limit mode="manual" prompt="LED 是否亮绿？"/></step></test>')
        limit = s.statements[1].limit
        self.assertEqual(limit.mode, "manual")
        self.assertEqual(limit.prompt, "LED 是否亮绿？")

    def test_limit_manual_requires_prompt(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0">'
                  '<connect resource="can_main"/>'
                  '<step name="S"><send id="0x1"/><wait id="0x2"/>'
                  '<limit mode="manual"/></step></test>')
        self.assertIn("E119", codes(cm.exception))

    def test_limit_manual_rejects_thresholds(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0">'
                  '<connect resource="can_main"/>'
                  '<step name="S"><send id="0x1"/><wait id="0x2"/>'
                  '<limit mode="manual" prompt="x" min="0"/></step></test>')
        self.assertIn("E120", codes(cm.exception))

    def test_limit_auto_requires_value(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0">'
                  '<connect resource="can_main"/>'
                  '<step name="S"><send id="0x1"/><wait id="0x2"/>'
                  '<limit min="0" max="1"/></step></test>')
        self.assertIn("E104", codes(cm.exception))

    # ---------------- send / wait：单次通讯 vs 文件传输 ----------------
    def test_send_file_mode(self):
        s = parse('<test name="T" version="1.0">'
                  '<connect resource="can_main"/>'
                  '<step name="S"><send id="0x1" mode="file" '
                  'file="fw.bin" chunk_size="7" seq_len="1"/></step></test>')
        send = s.statements[1].instructions[0]
        self.assertEqual(send.mode, "file")
        self.assertEqual(send.file, "fw.bin")
        self.assertEqual(send.chunk_size, 7)
        self.assertEqual(send.seq_len, 1)

    def test_send_file_requires_file(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0">'
                  '<connect resource="can_main"/>'
                  '<step name="S"><send id="0x1" mode="file"/></step></test>')
        self.assertIn("E121", codes(cm.exception))

    def test_send_single_rejects_file_attrs(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0">'
                  '<connect resource="can_main"/>'
                  '<step name="S"><send id="0x1" file="fw.bin"/></step></test>')
        self.assertIn("E121", codes(cm.exception))

    def test_send_file_chunk_exceeds_can_frame(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0">'
                  '<connect resource="can_main"/>'
                  '<step name="S"><send id="0x1" mode="file" file="f.bin" '
                  'chunk_size="8" seq_len="1"/></step></test>')
        self.assertIn("E121", codes(cm.exception))

    def test_wait_file_mode(self):
        s = parse('<test name="T" version="1.0">'
                  '<connect resource="can_main"/>'
                  '<step name="S"><wait id="0x1" mode="file" '
                  'file="recv.bin" size="100" chunk_size="8" '
                  'checksum="crc32"/></step></test>')
        wait = s.statements[1].instructions[0]
        self.assertEqual(wait.mode, "file")
        self.assertEqual(wait.size, 100)
        self.assertEqual(wait.checksum, "crc32")

    def test_wait_file_requires_termination(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0">'
                  '<connect resource="can_main"/>'
                  '<step name="S"><wait id="0x1" mode="file" '
                  'file="recv.bin"/></step></test>')
        self.assertIn("E122", codes(cm.exception))

    def test_wait_file_rejects_fields(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0">'
                  '<connect resource="can_main"/>'
                  '<step name="S"><wait id="0x1" mode="file" '
                  'file="recv.bin" size="1">'
                  '<field var="x" offset="0"/></wait></step></test>')
        self.assertIn("E122", codes(cm.exception))

    def test_wait_single_rejects_file_attrs(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0">'
                  '<connect resource="can_main"/>'
                  '<step name="S"><wait id="0x1" size="1"/></step></test>')
        self.assertIn("E122", codes(cm.exception))

    def test_checksum_value_bad_hex(self):
        with self.assertRaises(ScriptParseErrors) as cm:
            parse('<test name="T" version="1.0">'
                  '<connect resource="can_main"/>'
                  '<step name="S"><wait id="0x1" mode="file" file="r.bin" '
                  'size="1" checksum="sum8" checksum_value="ZZ"/></step></test>')
        self.assertIn("E122", codes(cm.exception))


if __name__ == "__main__":
    unittest.main()
