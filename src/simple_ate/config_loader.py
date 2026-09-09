"""工位配置加载（config/station.toml）。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .communication.base import ResourceConfig


@dataclass
class SnConfig:
    pattern: str = ".*"
    auto_start: bool = True


@dataclass
class StorageConfig:
    sink: str = "file"
    result_dir: str = "./data/results"
    log_dir: str = "./data/logs"
    trace_retain_days: int = 30


@dataclass
class MesConfig:
    enabled: bool = False
    uploader: str = ""                      # "module:class"，定制方提供
    retry_intervals: tuple[int, ...] = (10, 60, 300, 1800)
    max_retry: int = 8


@dataclass
class StationConfig:
    station_id: str = "UNKNOWN"
    software_version: str = "0.1.0"
    sn: SnConfig = field(default_factory=SnConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    mes: MesConfig = field(default_factory=MesConfig)
    resources: dict[str, ResourceConfig] = field(default_factory=dict)
    extensions_allowed: tuple[str, ...] = ()
    extensions_dir: str = "extensions"
    base_dir: Path = Path(".")

    def resource_names(self) -> set[str]:
        return set(self.resources.keys())


def load_config(path: str | Path) -> StationConfig:
    path = Path(path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    cfg = StationConfig(
        station_id=raw.get("station_id", "UNKNOWN"),
        software_version=raw.get("software_version", "0.1.0"),
        base_dir=path.parent.resolve(),
    )

    if "sn" in raw:
        cfg.sn = SnConfig(
            pattern=raw["sn"].get("pattern", ".*"),
            auto_start=raw["sn"].get("auto_start", True),
        )

    if "storage" in raw:
        cfg.storage = StorageConfig(
            sink=raw["storage"].get("sink", "file"),
            result_dir=raw["storage"].get("result_dir", "./data/results"),
            log_dir=raw["storage"].get("log_dir", "./data/logs"),
            trace_retain_days=raw["storage"].get("trace_retain_days", 30),
        )

    if "mes" in raw:
        cfg.mes = MesConfig(
            enabled=raw["mes"].get("enabled", False),
            uploader=raw["mes"].get("uploader", ""),
            retry_intervals=tuple(raw["mes"].get("retry_intervals", [10, 60, 300, 1800])),
            max_retry=raw["mes"].get("max_retry", 8),
        )

    for name, spec in raw.get("resources", {}).items():
        cfg.resources[name] = ResourceConfig(
            name=name, type=spec.get("type", "mock"), options=dict(spec))

    ext = raw.get("extensions", {})
    cfg.extensions_allowed = tuple(ext.get("allowed", []))
    cfg.extensions_dir = ext.get("dir", "extensions")
    return cfg
