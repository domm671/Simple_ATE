"""内置 JSON 文本结果存储 + Outbox 目录布局（设计说明第 9 章）。

目录::

    result_dir/
      pending/<SN>_<stamp>.json     待上传 MES（MES 禁用时直接归档 uploaded）
      uploaded/...                  已上传或无需上传
      failed/...                    超过最大重试（M4 处理）

每步完成即整体覆写落盘并 flush，崩溃残留（无 result 字段）在下次扫描时标记 ABORT。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from ..model import ItemResult
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
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class FileResultStore:
    def __init__(self, result_dir: str | Path, mes_enabled: bool = False):
        self.root = Path(result_dir)
        self.mes_enabled = mes_enabled
        for sub in (PENDING, UPLOADED, FAILED):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 写
    def _target_dir(self) -> str:
        return PENDING if self.mes_enabled else UPLOADED

    def _unique_path(self, directory: Path, sn: str) -> Path:
        base = f"{sn}_{_stamp()}"
        path = directory / f"{base}.json"
        seq = 1
        while path.exists():
            path = directory / f"{base}_{seq}.json"
            seq += 1
        return path

    def create_run(self, meta: RunMeta) -> RunHandle:
        path = self._unique_path(self.root / self._target_dir(), meta.sn)
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
        _atomic_write_json(path, payload)
        return RunHandle(token=str(path), path=str(path))

    def append_item(self, handle: RunHandle, item: ItemResult) -> None:
        path = Path(handle.path)
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        payload["items"].append(item.to_dict())
        _atomic_write_json(path, payload)

    def finish_run(self, handle: RunHandle, result: str, end_time: str) -> RunResult:
        path = Path(handle.path)
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        payload["end_time"] = end_time
        payload["result"] = result
        _atomic_write_json(path, payload)
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
                    with open(path, "r", encoding="utf-8") as f:
                        p = json.load(f)
                except (json.JSONDecodeError, OSError):
                    continue
                if not p.get("result"):
                    p["result"] = "ABORT"
                    p["end_time"] = p.get("end_time") or datetime.now().isoformat(timespec="seconds")
                    _atomic_write_json(path, p)
                    count += 1
        return count

    def iter_results(self, subdir: str = PENDING) -> list[RunResult]:
        out = []
        for path in sorted((self.root / subdir).glob("*.json")):
            with open(path, "r", encoding="utf-8") as f:
                out.append(self._result_from_payload(json.load(f)))
        return out
