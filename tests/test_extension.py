"""扩展 action 加载与执行测试。"""

import tempfile
import unittest
from pathlib import Path

from simple_ate.communication.mock import MockCommunication
from simple_ate.config_loader import StationConfig, ResourceConfig
from simple_ate.engine import Engine, EngineListener
from simple_ate.errors import ExtensionError, ScriptParseErrors
from simple_ate.engine.extension import ExtensionLoader
from simple_ate.engine.parser import ScriptParser
from simple_ate.storage.file_store import FileResultStore

EXT_CODE = '''
from simple_ate.errors import TimeoutAteError


def echo(ctx, resource, value="0"):
    return int(value) + 1


def boom(ctx, resource):
    raise RuntimeError("boom from extension")


def comm_fail(ctx, resource):
    raise TimeoutAteError("ext timeout")


def scoped(ctx, resource, value, retry_once="false"):
    assert not ctx.variables and not ctx.variable_units, "变量或单位从上次执行泄漏"
    if retry_once == "true" and not getattr(resource, "retried", False):
        resource.retried = True
        ctx.variables["answer"] = 99
        ctx.variable_units["answer"] = "V"
        raise TimeoutAteError("首次采集超时")
    return int(value)
'''

XML = """<test name="T" version="1.0">
  <connect resource="can"/>
  <step name="Echo" timeout="1" retry="0">
    <action handler="myext:echo" value="41" var="answer"/>
    <limit value="${answer}" eq="42"/>
  </step>
</test>"""


class TestExtension(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        ext_dir = self.tmp / "extensions"
        ext_dir.mkdir()
        (ext_dir / "myext.py").write_text(EXT_CODE, encoding="utf-8")
        self.loader = ExtensionLoader(ext_dir, allowed=("myext",))

    def test_load_and_call(self):
        func = self.loader.load("myext:echo")
        self.assertEqual(func(None, None, value="1"), 2)

    def test_not_whitelisted(self):
        with self.assertRaises(ExtensionError):
            self.loader.load("evil:doit")

    def test_missing_function(self):
        with self.assertRaises(ExtensionError):
            self.loader.load("myext:nope")

    def _engine(self, xml, allowed=("myext",)):
        cfg = StationConfig(station_id="UT", base_dir=self.tmp)
        cfg.resources = {"can": ResourceConfig("can", "mock", {})}
        cfg.extensions_allowed = tuple(allowed)
        store = FileResultStore(self.tmp / "results")
        engine = Engine(config=cfg, store=store, listener=EngineListener(),
                        extensions_dir=self.tmp / "extensions")
        mock = MockCommunication("can")
        mock.open()
        engine._build_resource = lambda name: mock
        script = ScriptParser({"can"}, set(allowed)).parse_bytes(xml.encode())
        return engine.run(script, sn="SN")

    def test_action_in_step(self):
        r = self._engine(XML)
        self.assertEqual(r.result, "PASS")
        self.assertEqual(r.items[0].value, 42)

    def test_each_step_and_retry_start_with_empty_variables(self):
        result = self._engine('''<test name="T" version="1.0"><connect resource="can"/>
          <step name="A" retry="1">
            <action handler="myext:scoped" value="10" retry_once="true" var="answer"/>
            <action handler="myext:echo" value="${answer}" var="answer"/>
            <limit value="${answer}" eq="11"/>
          </step>
          <step name="B">
            <action handler="myext:scoped" value="20" var="answer"/>
            <limit value="${answer}" eq="20"/>
          </step></test>''')
        self.assertEqual(result.result, "PASS")
        self.assertEqual([(item.value, item.retries) for item in result.items], [(11, 1), (20, 0)])

    def test_missing_handler_rejected_before_run_creation(self):
        with self.assertRaises(ScriptParseErrors) as error:
            self._engine(XML.replace("myext:echo", "myext:missing"))
        self.assertEqual(error.exception.errors[0].code, "E202")
        self.assertEqual(list((self.tmp / "results").rglob("*.json")), [])

    def test_extension_runtime_error(self):
        xml = """<test name="T" version="1.0"><connect resource="can"/>
          <step name="X" timeout="1" retry="0">
            <action handler="myext:boom" var="x"/>
            <limit value="${x}" eq="1"/></step></test>"""
        r = self._engine(xml)
        self.assertEqual(r.result, "ERROR")
        self.assertEqual(r.items[0].error_code, "E306")

    def test_extension_timeout_is_retryable(self):
        xml = """<test name="T" version="1.0"><connect resource="can"/>
          <step name="X" timeout="1" retry="2" retry_interval="0">
            <action handler="myext:comm_fail" var="x"/></step></test>"""
        r = self._engine(xml)
        self.assertEqual(r.items[0].retries, 2)
        self.assertEqual(r.items[0].error_code, "E301")


if __name__ == "__main__":
    unittest.main()
