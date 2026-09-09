"""日志：运行日志（标准 logging）+ 原始通信 trace。"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from .communication.base import Frame

_LOGGER_NAME = "simple_ate"


def setup_run_logger(log_dir: str | Path, level=logging.INFO) -> tuple[logging.Logger, Path]:
    """配置按 Run 输出的运行日志，返回 (logger, 日志文件路径)。"""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = log_dir / f"run_{stamp}.log"

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    logger.propagate = False
    return logger, path


class TraceLogger:
    """逐帧记录原始通信：trace_<标识>.log。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w", encoding="utf-8")

    def tx(self, resource: str, frame: Frame) -> None:
        self._write("TX", resource, frame)

    def rx(self, resource: str, frame: Frame) -> None:
        self._write("RX", resource, frame)

    def note(self, text: str) -> None:
        self._fh.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self._fh.flush()

    def _write(self, direction: str, resource: str, frame: Frame) -> None:
        data = " ".join(f"{b:02X}" for b in frame.data)
        self._fh.write(
            f"[{time.strftime('%H:%M:%S')}] {direction} {resource} "
            f"id=0x{frame.id:X} ext={int(frame.ext)} data={data}\n")
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()
