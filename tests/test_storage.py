"""文件结果存储测试：同 SN 多 Run 不覆盖、增量保存、崩溃恢复。"""

import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from simple_ate.engine.model import ItemResult
from simple_ate.storage.base import RunMeta
from simple_ate.storage.file_store import FAILED, FileResultStore, PENDING, UPLOADED


def meta(sn="SN1"):
    return RunMeta(sn=sn, script_name="T", script_version="1.0", script_sha256="abc",
                   software_version="0.1", station_id="ST", start_time="t0")


def item(result="PASS", name="S"):
    return ItemResult(seq=1, step_name=name, value=1.0, unit="V",
                      low_limit=0.0, high_limit=2.0, result=result,
                      duration_ms=5, retries=0)


class TestFileStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_storage_can_be_imported_before_engine(self):
        result = subprocess.run(
            [sys.executable, "-c", "from simple_ate.storage.file_store import FileResultStore"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mes_disabled_goes_uploaded(self):
        store = FileResultStore(self.tmp, mes_enabled=False)
        h = store.create_run(meta())
        store.append_item(h, item())
        r = store.finish_run(h, "PASS", "t1")
        self.assertEqual(r.result, "PASS")
        files = list((self.tmp / UPLOADED).glob("*.json"))
        self.assertEqual(len(files), 1)

    def test_mes_enabled_goes_pending(self):
        store = FileResultStore(self.tmp, mes_enabled=True)
        h = store.create_run(meta())
        store.finish_run(h, "PASS", "t1")
        self.assertEqual(len(list((self.tmp / PENDING).glob("*.json"))), 1)

    def test_same_sn_multiple_runs_no_overwrite(self):
        def run(_):
            store = FileResultStore(self.tmp)
            h = store.create_run(meta("SNX"))
            store.finish_run(h, "PASS", "t")
            return h.path

        with patch("simple_ate.storage.file_store._stamp", return_value="20260101_090000"):
            with ThreadPoolExecutor(max_workers=3) as pool:
                paths = list(pool.map(run, range(3)))
        self.assertEqual(len(set(paths)), 3)
        self.assertEqual(len(list((self.tmp / UPLOADED).glob("SNX_*.json"))), 3)

    def test_sn_is_kept_in_json_but_cannot_change_output_directory(self):
        store = FileResultStore(self.tmp)
        sn = r"..\..\DUT/01?"
        handle = store.create_run(meta(sn))
        result = store.finish_run(handle, "PASS", "t1")
        self.assertEqual(Path(handle.path).parent, self.tmp / UPLOADED)
        self.assertEqual(result.meta.sn, sn)

    def test_incremental_persist_each_item(self):
        store = FileResultStore(self.tmp, mes_enabled=False)
        h = store.create_run(meta())
        store.append_item(h, item(name="A"))
        # finish 之前文件已存在且有 1 条 item，但 result 为 null
        doc = json.loads(Path(h.path).read_text(encoding="utf-8"))
        self.assertIsNone(doc["result"])
        self.assertEqual(len(doc["items"]), 1)
        store.append_item(h, item(name="B"))
        doc = json.loads(Path(h.path).read_text(encoding="utf-8"))
        self.assertEqual(len(doc["items"]), 2)
        store.finish_run(h, "FAIL", "t1")
        doc = json.loads(Path(h.path).read_text(encoding="utf-8"))
        self.assertEqual(doc["result"], "FAIL")

    def test_crash_recovery_marks_abort(self):
        store = FileResultStore(self.tmp, mes_enabled=False)
        # 手动制造一个没有 result 的残留文件
        h = store.create_run(meta("CRASH"))
        store.append_item(h, item())
        # 另一个进程启动时不能把正在执行的 Run 当成崩溃残留。
        other = FileResultStore(self.tmp)
        self.assertEqual(other.recover_aborted(), 0)
        h.token.close()  # 模拟进程退出释放文件锁，但结论仍为 None
        n = other.recover_aborted()
        self.assertEqual(n, 1)
        doc = json.loads(Path(h.path).read_text(encoding="utf-8"))
        self.assertEqual(doc["result"], "ABORT")
        # 再扫一次不应重复处理
        self.assertEqual(store.recover_aborted(), 0)

    def test_recovery_skips_invalid_json_and_reads_legacy_results(self):
        store = FileResultStore(self.tmp)
        directory = self.tmp / UPLOADED
        (directory / "invalid.json").write_text("[]", encoding="utf-8")
        legacy = directory / "legacy.json"
        legacy.write_text('{"sn":"LEGACY","items":[]}', encoding="utf-8")
        self.assertEqual(store.recover_aborted(), 1)
        self.assertEqual(json.loads(legacy.read_text(encoding="utf-8"))["result"], "ABORT")


if __name__ == "__main__":
    unittest.main()
