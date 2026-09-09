"""解析器静态校验测试。"""

import unittest

from simple_ate.errors import ScriptParseErrors
from simple_ate.parser import ScriptParser

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


if __name__ == "__main__":
    unittest.main()
