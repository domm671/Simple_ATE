"""一次 Run 的运行上下文。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..communication.base import Communication


@dataclass
class RunContext:
    sn: str
    station_id: str
    logger: logging.Logger
    variables: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Communication] = field(default_factory=dict)
    # 脚本所在目录：文件传输的相对路径以此为基准
    script_dir: Path = field(default_factory=lambda: Path("."))

    def resource(self, name: str) -> Communication:
        return self.resources[name]
