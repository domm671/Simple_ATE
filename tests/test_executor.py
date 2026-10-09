"""执行器端到端测试：使用内存 Mock，覆盖 PASS/FAIL/ERROR/ABORT、重试、停止、存储。"""

import threading
import time
import unittest
from pathlib import Path
import tempfile

from simple_ate.communication.base import Communication, Frame, ResourceConfig
from simple_ate.communication.mock import MockCommunication
from simple_ate.config_loader import StationConfig
from simple_ate.engine import Engine, EngineListener
from simple_ate.engine.parser import ScriptParser
from simple_ate.storage.file_store import FileResultStore


def make_cfg(resources: dict[str, MockCommunication], tmpdir: Path) -> StationConfig:
    cfg = StationConfig(station_id="UT", base_dir=tmpdir)
    cfg.resources = {
        name: ResourceConfig(name=name, type="mock", options={})
        for name in resources
    }
    return cfg


class ScriptedMock(MockCommunication):
    """直接注入规则、不走 JSON。"""

    def __init__(self, name, rules=None):
        super().__init__(name)
        self._rules = rules or []
        self.open()


def parse(xml: str, resources=("can",), source_path: str = ""):
    p = ScriptParser(set(resources), allowed_extensions=set())
    return p.parse_bytes(xml.encode("utf-8"), source_path=source_path)


class EngineHarness:
    def __init__(self, mocks: dict[str, MockCommunication], tmpdir: Path):
        self.tmpdir = tmpdir
        self.cfg = make_cfg(mocks, tmpdir)
        self.store = FileResultStore(tmpdir / "results")
        self.engine = Engine(
            config=self.cfg, store=self.store,
            listener=EngineListener(),
            extensions_dir=tmpdir / "extensions")
        self._mocks = mocks

    def run(self, xml: str, sn: str = "SN1"):
        # 替换资源工厂：返回预置 mock（无需 JSON）
        engine = self.engine
        mocks = self._mocks

        def build(stmt):
            return mocks[stmt.resource]
        engine._build_resource = build
        # source_path 决定文件传输相对路径基准（脚本所在目录）
        script = parse(xml, tuple(mocks), source_path=str(self.tmpdir / "script.xml"))
        return engine.run(script, sn=sn)


class TestExecution(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def mock(self, rules):
        return ScriptedMock("can", rules)

    XML = """<test name="T" version="1.0">
      <connect resource="can"/>
      <step name="Volt" timeout="1" retry="2" retry_interval="0" on_fail="continue">
        <send id="0x10" data="01"/>
        <wait id="0x11">
          <field var="v" offset="0" length="2" endian="little" gain="0.01" unit="V"/>
        </wait>
        <limit value="${v}" min="11.5" max="12.5" unit="V"/>
      </step>
    </test>"""

    def test_pass(self):
        mock = self.mock([{"request_id": "0x10", "request_data": "01",
                          "response_id": "0x11",
                          "response_computed": {"id": "0x11", "bytes": "B3 04"}}])
        h = EngineHarness({"can": mock}, self.tmp)
        r = h.run(self.XML)
        self.assertEqual(r.result, "PASS")
        self.assertAlmostEqual(r.items[0].value, 12.03)
        self.assertEqual(r.items[0].retries, 0)

    def test_fail_not_retried(self):
        # 应答 10.00V（0x03E8=1000），超出下限；FAIL 不重试 -> retries=0
        mock = self.mock([{"request_id": "0x10",
                          "response_id": "0x11",
                          "response_computed": {"id": "0x11", "bytes": "E8 03"}}])
        h = EngineHarness({"can": mock}, self.tmp)
        r = h.run(self.XML)
        self.assertEqual(r.result, "FAIL")
        self.assertEqual(r.items[0].result, "FAIL")
        self.assertEqual(r.items[0].retries, 0)

    def test_timeout_error_retried(self):
        # 无应答规则 -> 超时，重试 2 次（共 3 次尝试），ERROR，retries=2
        mock = self.mock([])
        h = EngineHarness({"can": mock}, self.tmp)
        r = h.run(self.XML)
        self.assertEqual(r.result, "ERROR")
        self.assertEqual(r.items[0].error_code, "E301")
        self.assertEqual(r.items[0].retries, 2)

    def test_abort_on_fail_halts_later_steps(self):
        xml = """<test name="T" version="1.0"><connect resource="can"/>
          <step name="A" timeout="0.2" retry="0" on_fail="abort">
            <send id="0x10" data="01"/><wait id="0x11"/></step>
          <step name="B" timeout="0.2" retry="0" on_fail="continue">
            <send id="0x10" data="02"/><wait id="0x11"/></step></test>"""
        mock = self.mock([])
        h = EngineHarness({"can": mock}, self.tmp)
        r = h.run(xml)
        self.assertEqual(r.result, "ERROR")
        self.assertEqual([i.step_name for i in r.items], ["A"])

    def test_external_stop(self):
        xml = """<test name="T" version="1.0"><connect resource="can"/>
          <step name="A" timeout="5" retry="0"><send id="0x10"/><wait id="0x11"/></step>
        </test>"""
        mock = self.mock([])
        h = EngineHarness({"can": mock}, self.tmp)

        def stopper():
            time.sleep(0.2)
            h.engine.request_stop()
        t0 = time.monotonic()
        threading.Thread(target=stopper, daemon=True).start()
        r = h.run(xml)
        elapsed = time.monotonic() - t0
        self.assertEqual(r.result, "ABORT")
        # 应在数百毫秒内响应停止，而不是等满 5s timeout
        self.assertLess(elapsed, 1.0)

    def test_id_mask_match(self):
        xml = """<test name="T" version="1.0"><connect resource="can"/>
          <step name="A" timeout="0.5" retry="0">
            <send id="0x100"/><wait id="0x200" id_mask="0x700">
              <field var="x" offset="0" length="1"/>
            </wait><limit value="${x}" eq="7"/></step></test>"""
        # 应答 0x255 & 0x700 = 0x200，匹配
        mock = self.mock([{"request_id": "0x100",
                          "response_computed": {"id": "0x255", "bytes": "07"}}])
        h = EngineHarness({"can": mock}, self.tmp)
        r = h.run(xml)
        self.assertEqual(r.result, "PASS")

    def test_overall_fail_then_pass_is_fail(self):
        xml = """<test name="T" version="1.0"><connect resource="can"/>
          <step name="A" timeout="0.3" retry="0" on_fail="continue">
            <send id="0x10"/><wait id="0x11">
              <field var="x" offset="0" length="1"/></wait>
            <limit value="${x}" max="0"/></step>
          <step name="B" timeout="0.3" retry="0" on_fail="continue">
            <send id="0x20"/><wait id="0x22">
              <field var="y" offset="0" length="1"/></wait>
            <limit value="${y}" eq="1"/></step></test>"""
        mock = self.mock([
            {"request_id": "0x10", "response_computed": {"id": "0x11", "bytes": "05"}},
            {"request_id": "0x20", "response_computed": {"id": "0x22", "bytes": "01"}},
        ])
        h = EngineHarness({"can": mock}, self.tmp)
        r = h.run(xml)
        self.assertEqual([i.result for i in r.items], ["FAIL", "PASS"])
        self.assertEqual(r.result, "FAIL")

    def test_result_json_persisted(self):
        mock = self.mock([{"request_id": "0x10",
                          "response_computed": {"id": "0x11", "bytes": "B3 04"}}])
        h = EngineHarness({"can": mock}, self.tmp)
        r = h.run(self.XML, sn="SNXYZ")
        files = list((self.tmp / "results" / "uploaded").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.startswith("SNXYZ_"))
        import json
        doc = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(doc["result"], "PASS")
        self.assertEqual(doc["sn"], "SNXYZ")
        self.assertEqual(doc["items"][0]["step_name"], "Volt")


class TestFieldExtractAtRuntime(unittest.TestCase):
    def test_field_beyond_frame_length_is_error(self):
        tmp = Path(tempfile.mkdtemp())
        xml = """<test name="T" version="1.0"><connect resource="can"/>
          <step name="A" timeout="0.5" retry="0">
            <send id="0x10"/><wait id="0x11">
              <field var="x" offset="0" length="4"/>
            </wait></step></test>"""
        mock = ScriptedMock("can", [{"request_id": "0x10",
                                    "response_computed": {"id": "0x11", "bytes": "01"}}])
        h = EngineHarness({"can": mock}, tmp)
        r = h.run(xml)
        self.assertEqual(r.result, "ERROR")
        self.assertEqual(r.items[0].error_code, "E305")


    def test_connect_failure_is_error(self):
        # connect 抛 CommunicationError(E302) -> Run 以 ERROR 正常收尾，不崩
        xml = """<test name="T" version="1.0"><connect resource="can"/>
          <step name="A" timeout="0.2"><send id="0x10"/><wait id="0x11"/></step>
          </test>"""
        tmp = Path(tempfile.mkdtemp())
        from simple_ate.communication.base import Communication
        from simple_ate.errors import CommunicationError

        class DeadComm(MockCommunication):
            def open(self):
                raise CommunicationError("设备未连接")

        mock = DeadComm("can")
        h = EngineHarness({"can": mock}, tmp)
        r = h.run(xml)
        self.assertEqual(r.result, "ERROR")
        self.assertEqual(r.items, [])


class ManualDecisionListener(EngineListener):
    """人工判定测试桩：固定返回一个结果，并记录请求。"""

    def __init__(self, decision):
        self.decision = decision
        self.requests = []

    def on_manual_judge(self, request):
        self.requests.append(request)
        return self.decision


class TestManualJudge(unittest.TestCase):
    XML = """<test name="T" version="1.0"><connect resource="can"/>
      <step name="Led" timeout="0.5" retry="0" on_fail="continue">
        <send id="0x10"/><wait id="0x11"/>
        <limit mode="manual" prompt="LED 是否亮绿？" unit="color"/>
      </step></test>"""

    def _run(self, decision):
        tmp = Path(tempfile.mkdtemp())
        mock = ScriptedMock("can", [{"request_id": "0x10",
                                     "response_id": "0x11"}])
        h = EngineHarness({"can": mock}, tmp)
        listener = ManualDecisionListener(decision)
        h.engine.listener = listener
        result = h.run(self.XML)
        return result, listener

    def test_manual_pass(self):
        result, listener = self._run(True)
        self.assertEqual(result.result, "PASS")
        self.assertEqual(result.items[0].judge_mode, "manual")
        self.assertEqual(listener.requests[0].prompt, "LED 是否亮绿？")

    def test_manual_fail_not_retried(self):
        result, _ = self._run(False)
        self.assertEqual(result.result, "FAIL")
        self.assertEqual(result.items[0].result, "FAIL")
        self.assertEqual(result.items[0].judge_mode, "manual")
        self.assertEqual(result.items[0].retries, 0)

    def test_manual_cancel_aborts_run(self):
        result, _ = self._run(None)
        self.assertEqual(result.result, "ABORT")


class LoopbackCommunication:
    """把发出去的帧原样回灌的假通信，用于文件传输端到端测试。"""

    def __init__(self, name="can"):
        import queue
        self.name = name
        self._q = queue.Queue()
        self.sent = []

    def open(self):
        pass

    def close(self):
        pass

    def send(self, frame):
        self.sent.append(frame)
        self._q.put(frame)

    def recv(self, timeout):
        import queue
        from simple_ate.errors import TimeoutAteError
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutAteError(f"[{self.name}] 超时") from exc


class TestFileTransferExecution(unittest.TestCase):
    XML = """<test name="T" version="1.0"><connect resource="can"/>
      <step name="FileX" timeout="3" retry="0">
        <send id="0x10" mode="file" file="payload.bin" chunk_size="7" seq_len="1"/>
        <wait id="0x10" mode="file" file="recv.bin" chunk_size="7" seq_len="1"
              size="20"/>
      </step></test>"""

    def test_send_wait_roundtrip(self):
        tmp = Path(tempfile.mkdtemp())
        payload = bytes(range(20))
        (tmp / "payload.bin").write_bytes(payload)
        comm = LoopbackCommunication("can")
        h = EngineHarness({"can": comm}, tmp)
        result = h.run(self.XML)
        self.assertEqual(result.result, "PASS")
        self.assertEqual(result.items[0].value, 20)
        self.assertEqual((tmp / "recv.bin").read_bytes(), payload)

    def test_send_missing_file_is_error(self):
        tmp = Path(tempfile.mkdtemp())
        comm = LoopbackCommunication("can")
        h = EngineHarness({"can": comm}, tmp)
        result = h.run(self.XML)
        self.assertEqual(result.result, "ERROR")
        self.assertEqual(result.items[0].error_code, "E308")


class TestBuildResourceOptions(unittest.TestCase):
    def test_script_inline_serial_overrides_station(self):
        from simple_ate.communication.base import ResourceConfig
        from simple_ate.engine.model import ConnectStmt
        from simple_ate.communication.serial import SerialCommunication
        tmp = Path(tempfile.mkdtemp())
        cfg = StationConfig(station_id="UT", base_dir=tmp)
        cfg.resources = {"rs485": ResourceConfig(
            name="rs485", type="serial",
            options={"type": "serial", "port": "COM1", "baudrate": 9600})}
        engine = Engine(config=cfg, store=FileResultStore(tmp / "r"),
                        listener=EngineListener())
        stmt = ConnectStmt(
            resource="rs485", protocol="serial",
            options=(("port", "COM9"), ("baudrate", 115200), ("parity", "even")))
        comm = engine._build_resource(stmt)
        self.assertIsInstance(comm, SerialCommunication)
        self.assertEqual(comm.port, "COM9")       # 脚本覆盖工位配置
        self.assertEqual(comm.baudrate, 115200)
        self.assertEqual(comm.parity, "even")

    def test_script_inline_modbus_unknown_resource(self):
        from simple_ate.engine.model import ConnectStmt
        from simple_ate.communication.modbus import ModbusCommunication
        tmp = Path(tempfile.mkdtemp())
        cfg = StationConfig(station_id="UT", base_dir=tmp)
        engine = Engine(config=cfg, store=FileResultStore(tmp / "r"),
                        listener=EngineListener())
        stmt = ConnectStmt(
            resource="mb", protocol="modbus",
            options=(("modbus_mode", "rtu"), ("port", "COM2"), ("unit", 3)))
        comm = engine._build_resource(stmt)
        self.assertIsInstance(comm, ModbusCommunication)
        self.assertEqual(comm.mode, "rtu")
        self.assertEqual(comm.unit, 3)


if __name__ == "__main__":
    unittest.main()
