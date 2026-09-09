"""字段提取、拼帧与判定单元测试。"""

import unittest

from simple_ate.communication.base import Frame
from simple_ate.errors import FieldExtractError, SendDataError
from simple_ate.frame_io import build_data, extract_field
from simple_ate.judge import judge
from simple_ate.model import Field as F
from simple_ate.model import Limit, Send as SendSpec, VarRef


def frame(data: bytes) -> Frame:
    return Frame(id=0x100, data=data, ext=False)


class TestBuildData(unittest.TestCase):
    def test_literals(self):
        spec = SendSpec(id=0x100, ext=False, data=(0x01, 0x0A))
        self.assertEqual(build_data(spec.data, {}), b"\x01\x0a")

    def test_var_byte(self):
        self.assertEqual(build_data((VarRef("ch"),), {"ch": 5}), b"\x05")

    def test_var_out_of_range(self):
        with self.assertRaises(SendDataError):
            build_data((VarRef("ch"),), {"ch": 256})
        with self.assertRaises(SendDataError):
            build_data((VarRef("ch"),), {"ch": 1.5})
        with self.assertRaises(SendDataError):
            build_data((VarRef("ch"),), {"ch": None})


class TestExtract(unittest.TestCase):
    data = bytes([0xB3, 0x04, 0x00, 0x00])

    def test_little_endian_gain(self):
        f = F(var="v", offset=0, length=2, endian="little", gain=0.01, unit="V")
        self.assertAlmostEqual(extract_field(frame(self.data), f), 12.03)

    def test_big_endian(self):
        f = F(var="v", offset=0, length=2, endian="big", gain=1.0, bias=0.0)
        self.assertEqual(extract_field(frame(self.data), f), 0xB304)

    def test_bias(self):
        f = F(var="v", offset=0, length=1, gain=1.0, bias=-40.0)
        self.assertEqual(extract_field(frame(bytes([100])), f), 60.0)

    def test_raw(self):
        f = F(var="v", offset=1, length=1, raw=True)
        self.assertIsInstance(extract_field(frame(self.data), f), int)
        self.assertEqual(extract_field(frame(self.data), f), 0x04)

    def test_out_of_range(self):
        f = F(var="v", offset=4, length=1)
        with self.assertRaises(FieldExtractError):
            extract_field(frame(self.data), f)
        f2 = F(var="v", offset=3, length=2)
        with self.assertRaises(FieldExtractError):
            extract_field(frame(self.data), f2)


class TestJudge(unittest.TestCase):
    def test_min_max_pass_fail(self):
        lim = Limit(value=VarRef("v"), min=11.5, max=12.5)
        self.assertTrue(judge(lim, {"v": 12.0})[0])
        self.assertFalse(judge(lim, {"v": 11.4})[0])
        self.assertFalse(judge(lim, {"v": 12.6})[0])

    def test_eq_int_float_compatible(self):
        lim = Limit(value=VarRef("v"), eq=1)
        self.assertTrue(judge(lim, {"v": 1.0})[0])
        self.assertFalse(judge(lim, {"v": 2})[0])

    def test_eq_string(self):
        lim = Limit(value=VarRef("s"), eq="OK")
        self.assertTrue(judge(lim, {"s": "OK"})[0])

    def test_type_mismatch(self):
        lim = Limit(value=VarRef("s"), eq=1)
        with self.assertRaises(Exception):
            judge(lim, {"s": "OK"})

    def test_missing_variable(self):
        lim = Limit(value=VarRef("v"), min=0)
        with self.assertRaises(Exception):
            judge(lim, {})


if __name__ == "__main__":
    unittest.main()
