"""串口 / Modbus 通信的 Mock 模拟测试（无需真实硬件）。

覆盖：
- MockSerialPort：JSON 规则匹配、应答、主动上报、超时；
- SerialCommunication：注入 Mock 后的收发、分帧、超时、USB 自动找口；
- ModbusCommunication：RTU / ASCII（Mock 串口）与 TCP（本地回环服务）；
- Engine：脚本 `<connect protocol="serial" mock_script=...>` 端到端跑通。
"""

import json
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

from simple_ate.communication.base import Frame
from simple_ate.communication.mock_serial import MockSerialPort, parse_data
from simple_ate.communication.modbus import (
    ModbusCommunication,
    build_ascii_adu,
    build_rtu_adu,
)
from simple_ate.communication.serial import SerialCommunication, resolve_usb_port
from simple_ate.config_loader import StationConfig
from simple_ate.engine import Engine, EngineListener
from simple_ate.engine.parser import ScriptParser
from simple_ate.errors import CommunicationError, TimeoutAteError
from simple_ate.storage.file_store import FileResultStore


def write_rule_file(directory: Path, name: str, rules: list[dict],
                    default_timeout: float = 0.2) -> str:
    path = directory / name
    path.write_text(json.dumps({"default_timeout": default_timeout,
                                "rules": rules}), encoding="utf-8")
    return str(path)


class TestMockSerialPort(unittest.TestCase):
    def test_parse_data(self):
        self.assertEqual(parse_data("01 0A FF"), b"\x01\x0a\xff")
        self.assertEqual(parse_data([1, 2]), b"\x01\x02")
        self.assertEqual(parse_data(None), b"")

    def test_rule_response(self):
        port = MockSerialPort(rules=[{"request_data": "AA 01",
                                      "response_data": "BB 02 03"}],
                              default_timeout=0.05)
        port.open()
        port.write(b"\xAA\x01")
        port.timeout = 0.2
        self.assertEqual(port.read(3), b"\xBB\x02\x03")
        self.assertEqual(port.written, [b"\xAA\x01"])

    def test_no_rule_times_out(self):
        port = MockSerialPort(rules=[], default_timeout=0.05)
        port.open()
        port.write(b"\x01")
        port.timeout = 0.02
        self.assertEqual(port.read(1), b"")

    def test_wildcard_rule_matches_any(self):
        port = MockSerialPort(rules=[{"response_data": "AA 55"}],
                              default_timeout=0.05)
        port.open()
        port.write(b"\x01\x02\x03")
        port.timeout = 0.1
        self.assertEqual(port.read(2), b"\xAA\x55")

    def test_inject_and_framing(self):
        port = MockSerialPort(default_timeout=0.02)
        port.open()
        port.inject(b"\x01\x02")
        time.sleep(0.05)
        port.inject(b"\x03")
        port.timeout = 0.1
        self.assertEqual(port.read(2), b"\x01\x02")
        port.timeout = 0.1
        self.assertEqual(port.read(1), b"\x03")

    def test_from_file(self):
        tmp = Path(tempfile.mkdtemp())
        path = write_rule_file(tmp, "s.json",
                               [{"request_data": "01", "response_data": "02"}])
        port = MockSerialPort.from_file(path)
        port.open()
        port.write(b"\x01")
        port.timeout = 0.1
        self.assertEqual(port.read(1), b"\x02")

    def test_from_file_missing(self):
        with self.assertRaises(CommunicationError):
            MockSerialPort.from_file("/no/such/mock.json")


class TestSerialCommunication(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _open_serial(self, rules, **kwargs):
        path = write_rule_file(self.tmp, "serial.json", rules)
        comm = SerialCommunication("rs485", mock_script=path, frame_gap=0.02,
                                   **kwargs)
        comm.open()
        return comm

    def test_send_receive_roundtrip(self):
        comm = self._open_serial([{"request_data": "AA 01",
                                   "response_data": "BB 02 03"}])
        comm.send(Frame(id=0, data=b"\xAA\x01"))
        frame = comm.recv(0.5)
        self.assertEqual(frame.id, 0)
        self.assertEqual(frame.data, b"\xBB\x02\x03")
        comm.close()

    def test_receive_timeout(self):
        comm = self._open_serial([])
        comm.send(Frame(id=0, data=b"\x00"))
        with self.assertRaises(TimeoutAteError):
            comm.recv(0.02)
        comm.close()

    def test_frame_split_by_gap(self):
        comm = self._open_serial([])
        port: MockSerialPort = comm._ser
        port.timeout = 0.02
        port.inject(b"\x01\x02")
        # 第二段在 frame_gap 之后才到达 -> 应被识别为下一帧
        timer = threading.Timer(0.1, lambda: port.inject(b"\x03"))
        timer.start()
        self.assertEqual(comm.recv(0.2).data, b"\x01\x02")
        timer.join()
        self.assertEqual(comm.recv(0.2).data, b"\x03")
        comm.close()

    def test_send_before_open_errors(self):
        comm = SerialCommunication("rs485", mock_script="x")
        with self.assertRaises(CommunicationError):
            comm.send(Frame(id=0, data=b"\x01"))

    def test_open_without_port_errors(self):
        comm = SerialCommunication("rs485", serial_factory=lambda **k: None)
        with self.assertRaises(CommunicationError):
            comm.open()

    def test_injected_factory_used(self):
        captured = {}

        class FakePort:
            timeout = 0.0
            def write(self, data): captured["write"] = data; return len(data)
            def read(self, size=1): return b""
            @property
            def in_waiting(self): return 0
            def flush(self): pass
            def close(self): pass

        def factory(**kwargs):
            captured.update(kwargs)
            return FakePort()

        comm = SerialCommunication("rs485", port="COM9", baudrate=115200,
                                   parity="even", serial_factory=factory)
        comm.open()
        self.assertEqual(captured["port"], "COM9")
        self.assertEqual(captured["baudrate"], 115200)
        self.assertEqual(captured["parity"], "E")
        comm.close()


class TestUsbPortResolution(unittest.TestCase):
    def setUp(self):
        self.saved = {}

    def _install_fake_list_ports(self, infos):
        serial_mod = types.ModuleType("serial")
        tools_mod = types.ModuleType("serial.tools")
        lp_mod = types.ModuleType("serial.tools.list_ports")
        lp_mod.comports = lambda: infos
        tools_mod.list_ports = lp_mod
        serial_mod.tools = tools_mod
        for name, mod in (("serial", serial_mod), ("serial.tools", tools_mod),
                          ("serial.tools.list_ports", lp_mod)):
            self.saved[name] = sys.modules.get(name)
            sys.modules[name] = mod

    def tearDown(self):
        for name, mod in self.saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def test_resolve_by_vid_pid(self):
        class Info:
            def __init__(self, device, vid, pid, sn="SN"):
                self.device, self.vid, self.pid, self.serial_number = \
                    device, vid, pid, sn
        self._install_fake_list_ports([
            Info("COM1", 0x1234, 0x0001),
            Info("COM7", 0x0483, 0x5740),
        ])
        self.assertEqual(resolve_usb_port(vid=0x0483, pid=0x5740), "COM7")

    def test_resolve_by_serial_number(self):
        class Info:
            def __init__(self, device, vid, pid, sn):
                self.device, self.vid, self.pid, self.serial_number = \
                    device, vid, pid, sn
        self._install_fake_list_ports([
            Info("COM7", 0x0483, 0x5740, "DUT-002"),
        ])
        self.assertEqual(resolve_usb_port(serial_number="DUT-002"), "COM7")
        self.assertEqual(resolve_usb_port(vid=0xFFFF), "")


class TestModbusSerial(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _mock_modbus(self, mode, rules):
        path = write_rule_file(self.tmp, f"mb_{mode}.json", rules)
        comm = ModbusCommunication("mb", modbus_mode=mode, unit=3,
                                   mock_script=path)
        comm.open()
        return comm

    def test_rtu_roundtrip(self):
        request_pdu = b"\x03\x00\x00\x00\x02"
        # 从站 3 的应答：功能码 03，字节数 4，两个寄存器 0x000B / 0x000C
        response_pdu = b"\x03\x04\x00\x0B\x00\x0C"
        rules = [{"request_data": build_rtu_adu(3, request_pdu).hex(" "),
                  "response_data": build_rtu_adu(3, response_pdu).hex(" ")}]
        comm = self._mock_modbus("rtu", rules)
        comm.send(Frame(id=3, data=request_pdu))
        frame = comm.recv(0.5)
        self.assertEqual(frame.id, 3)
        self.assertEqual(frame.data, response_pdu)
        comm.close()

    def test_rtu_exception_response(self):
        request_pdu = b"\x03\x00\x00\x00\x02"
        response_pdu = b"\x83\x02"                      # 非法数据地址
        rules = [{"request_data": build_rtu_adu(3, request_pdu).hex(" "),
                  "response_data": build_rtu_adu(3, response_pdu).hex(" ")}]
        comm = self._mock_modbus("rtu", rules)
        comm.send(Frame(id=3, data=request_pdu))
        self.assertEqual(comm.recv(0.5).data, response_pdu)
        comm.close()

    def test_rtu_bad_crc_detected(self):
        request_pdu = b"\x03\x00\x00\x00\x02"
        bad = bytearray(build_rtu_adu(3, b"\x03\x02\x00\x01"))
        bad[-1] ^= 0xFF
        rules = [{"request_data": build_rtu_adu(3, request_pdu).hex(" "),
                  "response_data": bytes(bad).hex(" ")}]
        comm = self._mock_modbus("rtu", rules)
        comm.send(Frame(id=3, data=request_pdu))
        with self.assertRaises(CommunicationError):
            comm.recv(0.5)
        comm.close()

    def test_rtu_timeout(self):
        comm = self._mock_modbus("rtu", [])
        comm.send(Frame(id=3, data=b"\x03\x00\x00\x00\x02"))
        with self.assertRaises(TimeoutAteError):
            comm.recv(0.05)
        comm.close()

    def test_ascii_roundtrip(self):
        request_pdu = b"\x03\x00\x00\x00\x02"
        response_pdu = b"\x03\x02\x00\x0A"
        rules = [{"request_data": build_ascii_adu(3, request_pdu).hex(" "),
                  "response_data": build_ascii_adu(3, response_pdu).hex(" ")}]
        comm = self._mock_modbus("ascii", rules)
        comm.send(Frame(id=3, data=request_pdu))
        self.assertEqual(comm.recv(0.5).data, response_pdu)
        comm.close()


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("对端关闭")
        buf += chunk
    return bytes(buf)


class _ModbusTcpSlave:
    """本地回环的极简 Modbus TCP 从站，用于无硬件模拟。"""

    def __init__(self, unit: int, response_pdu: bytes):
        self.unit = unit
        self.response_pdu = response_pdu
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        try:
            head = _recv_exact(conn, 7)
            length = int.from_bytes(head[4:6], "big")
            _recv_exact(conn, max(0, length - 1))
            payload = bytes([self.unit]) + self.response_pdu
            resp = head[0:2] + b"\x00\x00" + len(payload).to_bytes(2, "big") + payload
            conn.sendall(resp)
        finally:
            conn.close()
            self.sock.close()


class TestModbusTcp(unittest.TestCase):
    def test_tcp_roundtrip(self):
        response_pdu = b"\x03\x04\x00\x0B\x00\x0C"
        slave = _ModbusTcpSlave(unit=3, response_pdu=response_pdu).start()
        comm = ModbusCommunication("mb", modbus_mode="tcp",
                                   host="127.0.0.1", tcp_port=slave.port,
                                   unit=3, read_timeout=1.0)
        comm.open()
        comm.send(Frame(id=3, data=b"\x03\x00\x00\x00\x02"))
        frame = comm.recv(1.0)
        self.assertEqual(frame.id, 3)
        self.assertEqual(frame.data, response_pdu)
        comm.close()
        slave.thread.join(timeout=2)

    def test_tcp_connect_failure(self):
        comm = ModbusCommunication("mb", modbus_mode="tcp",
                                   host="127.0.0.1", tcp_port=1,
                                   read_timeout=0.2)
        with self.assertRaises(CommunicationError):
            comm.open()


class TestEngineSerialMock(unittest.TestCase):
    """脚本级：<connect protocol="serial" mock_script=...> 端到端跑通。"""

    XML = ('<test name="SER" version="1.0">'
           '<connect resource="rs485" protocol="serial" '
           'mock_script="serial_mock.json"/>'
           '<step name="Read" timeout="1" on_fail="continue">'
           '<send id="0" data="AA 01"/>'
           '<wait id="0" min_len="2">'
           '<field var="v" offset="0" length="1"/>'
           '</wait>'
           '<limit value="${v}" eq="187" unit=""/>'
           '</step></test>')

    def test_script_runs_pass(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "serial_mock.json").write_text(json.dumps({
            "default_timeout": 0.2,
            "rules": [{"request_data": "AA 01", "response_data": "BB 02"}],
        }), encoding="utf-8")
        cfg = StationConfig(station_id="UT", base_dir=tmp)
        parser = ScriptParser(allowed_extensions=set())
        script = parser.parse_bytes(self.XML.encode("utf-8"),
                                    source_path=str(tmp / "ser.xml"))
        engine = Engine(config=cfg, store=FileResultStore(tmp / "results"),
                        listener=EngineListener(), extensions_dir=tmp / "ext")
        result = engine.run(script, sn="SER001")
        self.assertEqual(result.result, "PASS")
        self.assertEqual(result.items[0].value, 187)

    def test_build_resource_resolves_mock_script(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "serial_mock.json").write_text(json.dumps({"rules": []}),
                                              encoding="utf-8")
        cfg = StationConfig(station_id="UT", base_dir=tmp)
        engine = Engine(config=cfg, store=FileResultStore(tmp / "results"),
                        listener=EngineListener())
        from simple_ate.engine.model import ConnectStmt
        stmt = ConnectStmt(resource="rs485", protocol="serial",
                           options=(("mock_script", "serial_mock.json"),))
        comm = engine._build_resource(stmt)
        self.assertEqual(Path(comm.mock_script), tmp / "serial_mock.json")
        comm.open()
        comm.close()


if __name__ == "__main__":
    unittest.main()
