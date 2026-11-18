"""内置 JSON 文本结果存储 + Outbox 目录布局（设计说明第 9 章）。

目录::

    result_dir/
      pending/<SN>_<stamp>.json     待上传 MES（MES 禁用时直接归档 uploaded）
      uploaded/...                  已上传或无需上传
      failed/...                    超过最大重试（M4 处理）

每步完成即整体覆写保存并 flush，崩溃残留（无 result 字段）在下次扫描时标记 ABORT。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from ..config_loader import sn_filename
from ..engine.model import ItemResult
from .base import RunHandle, RunMeta, RunResult

PENDING = "pending"
UPLOADED = "uploaded"
FAILED = "failed"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _try_lock(file: BinaryIO) -> bool:
    """用操作系统锁区分仍在运行与上次崩溃的结果。"""
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        return False
    return True


class FileResultStore:
    def __init__(self, result_dir: str | Path, mes_enabled: bool = False):
        self.root = Path(result_dir)
        self.mes_enabled = mes_enabled
        for sub in (PENDING, UPLOADED, FAILED):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 写
    def _target_dir(self) -> str:
        return PENDING if self.mes_enabled else UPLOADED

    def _reserve(self, directory: Path, sn: str) -> tuple[Path, BinaryIO]:
        base = f"{sn_filename(sn)}_{_stamp()}"
        seq = 0
        while True:
            path = directory / (f"{base}_{seq}.json" if seq else f"{base}.json")
            seq += 1
            if path.exists():
                continue
            sidecar = path.with_suffix(".lock")
            try:
                lock = sidecar.open("xb")
            except FileExistsError:
                continue
            if not path.exists() and _try_lock(lock):
                return path, lock
            lock.close()
            sidecar.unlink(missing_ok=True)

    def create_run(self, meta: RunMeta) -> RunHandle:
        path, lock = self._reserve(self.root / self._target_dir(), meta.sn)
        payload = {
            "sn": meta.sn,
            "script_name": meta.script_name,
            "script_version": meta.script_version,
            "script_sha256": meta.script_sha256,
            "software_version": meta.software_version,
            "station_id": meta.station_id,
            "start_time": meta.start_time,
            "end_time": None,
            "result": None,
            "items": [],
        }
        try:
            _atomic_write_json(path, payload)
        except Exception:  # noqa: BLE001
            self._release(path, lock)
            raise
        return RunHandle(token=lock, path=str(path))

    @staticmethod
    def _release(path: Path, lock: BinaryIO) -> None:
        lock.close()
        try:
            path.with_suffix(".lock").unlink(missing_ok=True)
        except PermissionError:
            # Windows 上恢复进程可能刚打开锁文件，结果文件已经安全写入。
            pass

    def append_item(self, handle: RunHandle, item: ItemResult) -> None:
        path = Path(handle.path)
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        payload["items"].append(item.to_dict())
        _atomic_write_json(path, payload)

    def finish_run(self, handle: RunHandle, result: str, end_time: str) -> RunResult:
        path = Path(handle.path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            payload["end_time"] = end_time
            payload["result"] = result
            _atomic_write_json(path, payload)
        finally:
            self._release(path, handle.token)
        return self._result_from_payload(payload)

    # ------------------------------------------------------------ 读/恢复
    @staticmethod
    def _result_from_payload(p: dict) -> RunResult:
        meta = RunMeta(
            sn=p["sn"], script_name=p["script_name"], script_version=p["script_version"],
            script_sha256=p.get("script_sha256", ""), software_version=p.get("software_version", ""),
            station_id=p.get("station_id", ""), start_time=p.get("start_time", ""),
        )
        return RunResult(meta=meta, end_time=p.get("end_time", ""),
                         result=p.get("result", ""),
                         items=[ItemResult.from_dict(d) for d in p.get("items", [])])

    def recover_aborted(self) -> int:
        """把 pending/uploaded 中缺少结论的残留文件标记为 ABORT。返回处理个数。"""
        count = 0
        for sub in (PENDING, UPLOADED):
            for path in (self.root / sub).glob("*.json"):
                try:
                    lock = path.with_suffix(".lock").open("r+b")
                except FileNotFoundError:
                    count += self._recover_file(path)
                else:
                    with lock:
                        if not _try_lock(lock):
                            continue
                        count += self._recover_file(path)
                    self._release(path, lock)
        return count

    @staticmethod
    def _recover_file(path: Path) -> bool:
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (ValueError, OSError):
            return False
        if not isinstance(payload, dict) or "sn" not in payload or payload.get("result"):
            return False
        payload["result"] = "ABORT"
        payload["end_time"] = payload.get("end_time") or datetime.now().isoformat(timespec="seconds")
        _atomic_write_json(path, payload)
        return True

    def iter_results(self, subdir: str = PENDING) -> list[RunResult]:
        out = []
        for path in sorted((self.root / subdir).glob("*.json")):
            with open(path, "r", encoding="utf-8") as f:
                out.append(self._result_from_payload(json.load(f)))
        return out
