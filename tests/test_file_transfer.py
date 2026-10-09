"""文件传输与 Modbus 组帧的单元测试。"""

import tempfile
import unittest
from pathlib import Path

from simple_ate.communication.base import Frame
from simple_ate.communication.modbus import (
    build_ascii_adu,
    build_rtu_adu,
    build_tcp_adu,
    crc16_modbus,
    lrc,
    parse_ascii_adu,
    parse_rtu_adu,
    parse_tcp_adu,
)
from simple_ate.errors import CommunicationError, FileTransferError, TimeoutAteError
from simple_ate.engine.frame_io import (
    compute_checksum,
    do_wait_file,
    iter_send_file,
)
from simple_ate.engine.model import Send as SendSpec
from simple_ate.engine.model import Wait as WaitSpec


class FakeComm:
    """按预置帧列表应答的假通信。"""

    name = "fake"

    def __init__(self, frames=None):
        self.frames = list(frames or [])
        self.sent = []

    def open(self):
        pass

    def close(self):
        pass

    def send(self, frame):
        self.sent.append(frame)

    def recv(self, timeout):
        if not self.frames:
            raise TimeoutAteError("no frame")
        return self.frames.pop(0)


class TestChecksum(unittest.TestCase):
    def test_crc32(self):
        self.assertEqual(compute_checksum(b"123456789", "crc32"),
                         bytes.fromhex("2639F4CB"))

    def test_crc16_modbus(self):
        self.assertEqual(compute_checksum(b"123456789", "crc16_modbus"),
                         bytes.fromhex("374B"))

    def test_sum8(self):
        self.assertEqual(compute_checksum(b"\x01\x02\x03", "sum8"), b"\x06")


class TestModbusFraming(unittest.TestCase):
    def test_rtu_roundtrip(self):
        pdu = bytes([0x03, 0x00, 0x00, 0x00, 0x02])
        adu = build_rtu_adu(1, pdu)
        unit, back = parse_rtu_adu(adu)
        self.assertEqual(unit, 1)
        self.assertEqual(back, pdu)

    def test_rtu_bad_crc(self):
        adu = bytearray(build_rtu_adu(1, b"\x03\x02\x00\x01"))
        adu[-1] ^= 0xFF
        with self.assertRaises(CommunicationError):
            parse_rtu_adu(bytes(adu))

    def test_ascii_roundtrip(self):
        pdu = bytes([0x03, 0x02, 0x00, 0x0A])
        adu = build_ascii_adu(2, pdu)
        self.assertTrue(adu.startswith(b":"))
        unit, back = parse_ascii_adu(adu)
        self.assertEqual(unit, 2)
        self.assertEqual(back, pdu)

    def test_tcp_roundtrip(self):
        pdu = bytes([0x03, 0x02, 0x00, 0x0A])
        adu = build_tcp_adu(0x1234, 5, pdu)
        txid, unit, back = parse_tcp_adu(adu)
        self.assertEqual(txid, 0x1234)
        self.assertEqual(unit, 5)
        self.assertEqual(back, pdu)

    def test_lrc(self):
        self.assertEqual(lrc(bytes([0x01, 0x03, 0x00, 0x00])), 0xFC)
        self.assertEqual(crc16_modbus(b"123456789"), bytes.fromhex("374B"))


class TestFileTransfer(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_send_and_receive(self):
        payload = bytes(range(10))
        src = self.tmp / "src.bin"
        dst = self.tmp / "dst.bin"
        src.write_bytes(payload)

        comm = FakeComm()
        send = SendSpec(id=0x10, mode="file", file="src.bin",
                        chunk_size=4, seq_len=1)
        frames = list(iter_send_file(send, comm, self.tmp))
        self.assertEqual(len(frames), 3)          # 4 + 4 + 2

        comm.frames = frames
        wait = WaitSpec(id=0x10, mode="file", file="dst.bin",
                        chunk_size=4, seq_len=1, size=10)
        stats = do_wait_file(wait, comm, {}, self.tmp, 1.0)
        self.assertEqual(stats.bytes_received, 10)
        self.assertEqual(stats.chunks, 3)
        self.assertEqual(dst.read_bytes(), payload)

    def test_sequence_reorder(self):
        payload = bytes(range(8))
        src = self.tmp / "src.bin"
        dst = self.tmp / "dst.bin"
        src.write_bytes(payload)

        comm = FakeComm()
        send = SendSpec(id=0x10, mode="file", file="src.bin",
                        chunk_size=4, seq_len=1)
        frames = list(iter_send_file(send, comm, self.tmp))
        comm.frames = list(reversed(frames))      # 乱序到达
        wait = WaitSpec(id=0x10, mode="file", file="dst.bin",
                        chunk_size=4, seq_len=1, size=8)
        do_wait_file(wait, comm, {}, self.tmp, 1.0)
        self.assertEqual(dst.read_bytes(), payload)

    def test_checksum_mismatch(self):
        comm = FakeComm([Frame(id=0x10, data=b"\x00\x01\x02\x03")])
        wait = WaitSpec(id=0x10, mode="file", file="out.bin", size=4,
                        checksum="sum8", checksum_value="FF")
        with self.assertRaises(FileTransferError):
            do_wait_file(wait, comm, {}, self.tmp, 1.0)

    def test_max_size_guard(self):
        comm = FakeComm([Frame(id=0x10, data=bytes(16))])
        wait = WaitSpec(id=0x10, mode="file", file="out.bin", max_size=8)
        with self.assertRaises(FileTransferError):
            do_wait_file(wait, comm, {}, self.tmp, 1.0)

    def test_idle_gap_terminates(self):
        payload = b"abcdefgh"
        src = self.tmp / "src.bin"
        dst = self.tmp / "dst.bin"
        src.write_bytes(payload)
        comm = FakeComm()
        send = SendSpec(id=0x10, mode="file", file="src.bin",
                        chunk_size=8, seq_len=0)
        frames = list(iter_send_file(send, comm, self.tmp))
        comm.frames = frames
        wait = WaitSpec(id=0x10, mode="file", file="dst.bin",
                        chunk_size=8, seq_len=0, idle_gap=0.05, timeout=1.0)
        stats = do_wait_file(wait, comm, {}, self.tmp, 1.0)
        self.assertEqual(stats.bytes_received, 8)
        self.assertEqual(dst.read_bytes(), payload)

    def test_header_and_checksum_ok(self):
        payload = b"hello world"
        src = self.tmp / "src.bin"
        dst = self.tmp / "dst.bin"
        src.write_bytes(payload)
        comm = FakeComm()
        send = SendSpec(id=0x10, mode="file", file="src.bin", chunk_size=4,
                        header=(0xAA,), seq_len=1)
        frames = list(iter_send_file(send, comm, self.tmp))
        self.assertTrue(frames[0].data.startswith(b"\xAA"))
        comm.frames = frames
        wait = WaitSpec(id=0x10, mode="file", file="dst.bin", chunk_size=4,
                        header=(0xAA,), seq_len=1, size=len(payload),
                        checksum="crc32",
                        checksum_value=compute_checksum(payload, "crc32").hex())
        stats = do_wait_file(wait, comm, {}, self.tmp, 1.0)
        self.assertEqual(stats.bytes_received, len(payload))
        self.assertEqual(dst.read_bytes(), payload)

    def test_send_missing_file(self):
        comm = FakeComm()
        send = SendSpec(id=0x10, mode="file", file="nope.bin")
        with self.assertRaises(FileTransferError):
            list(iter_send_file(send, comm, self.tmp))


if __name__ == "__main__":
    unittest.main()
