"""工位配置加载（config/station.toml）。"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from .communication.base import ResourceConfig
from .errors import ScriptError


_HARDWARE_PROTOCOLS = {"can", "serial", "usb", "modbus"}


@dataclass
class SnConfig:
    # 默认不对 SN 做 pattern 校验；使用方有需要时置 validation_enabled=true 并配置 pattern
    validation_enabled: bool = False
    pattern: str = ".*"
    auto_start: bool = True

    def validation_error(self, sn: str) -> str:
        if not sn.strip():
            return "SN 不能为空"
        if self.validation_enabled:
            try:
                if re.fullmatch(self.pattern, sn) is None:
                    return f"SN 不符合规则：{self.pattern}"
            except (re.error, TypeError):
                return f"工位配置中的 SN 规则无效：{self.pattern}"
        return ""


def sn_filename(sn: str) -> str:
    """生成跨平台的文件名片段，原始 SN 仍保存到结果 JSON。"""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", sn)[:100].strip(" .") or "SN"
    return f"_{safe}" if PureWindowsPath(safe).is_reserved() else safe


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
    mode: str = "debug"
    config_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in ("debug", "production"):
            raise ValueError('工位 mode 必须为 "debug" 或 "production"')

    @property
    def is_production(self) -> bool:
        return self.mode == "production"

    def check_connection(self, resource: str, protocol: str | None,
                         inline_parameters: bool) -> None:
        """生产脚本声明协议要求，设备与后端由工位配置决定。"""
        if not self.is_production:
            return
        spec = self.resources.get(resource)
        if spec is None:
            raise ScriptError(f"生产资源 {resource!r} 未在工位配置中定义", "E204")
        if inline_parameters:
            raise ScriptError("生产脚本的 connect 只允许 resource 和 protocol", "E211")
        if spec.type not in _HARDWARE_PROTOCOLS or spec.options.get("mock_script"):
            raise ScriptError(f"生产资源 {resource!r} 必须绑定真实通信后端", "E211")
        if protocol is not None and protocol != spec.type:
            raise ScriptError(f"资源 {resource!r} 要求协议 {protocol}，工位配置为 {spec.type}", "E211")

    def resource_names(self) -> set[str]:
        return set(self.resources.keys())

    def resolve_path(self, path: str) -> Path:
        return self.base_dir / path


def load_config(path: str | Path) -> StationConfig:
    path = Path(path)
    data = path.read_bytes()
    raw = tomllib.loads(data.decode("utf-8"))

    cfg = StationConfig(
        station_id=raw.get("station_id", "UNKNOWN"),
        software_version=raw.get("software_version", "0.1.0"),
        base_dir=path.parent.resolve(),
        mode=raw.get("mode", "debug"),
        config_sha256=hashlib.sha256(data).hexdigest(),
    )

    if "sn" in raw:
        cfg.sn = SnConfig(
            validation_enabled=raw["sn"].get("validation_enabled", False),
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
