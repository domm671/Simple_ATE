"""日志：运行日志（标准 logging）+ 原始通信 trace。"""

from __future__ import annotations

import logging
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..communication.base import Frame
from ..config_loader import sn_filename

_LOGGER_NAME = "simple_ate"


def trace_path_for(log_path: Path, sn: str) -> Path:
    return log_path.parent / "trace" / f"trace_{sn_filename(sn)}_{log_path.name[4:]}"


def setup_run_logger(log_dir: str | Path, level: int = logging.INFO, *,
                     console: bool = True) -> tuple[logging.Logger, Path]:
    """配置按 Run 输出的运行日志，返回 (logger, 日志文件路径)。"""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with tempfile.NamedTemporaryFile(dir=log_dir, prefix=f"run_{stamp}_",
                                     suffix=".log", delete=False) as file:
        path = Path(file.name)

    logger = logging.Logger(_LOGGER_NAME, level)

    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    return logger, path


@contextmanager
def run_logging(log_dir: Path, *, console: bool = True
                ) -> Iterator[tuple[logging.Logger, Path]]:
    logger, path = setup_run_logger(log_dir, console=console)
    try:
        yield logger, path
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()


class TraceLogger:
    """逐帧记录原始通信：trace_<标识>.log。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "x", encoding="utf-8")

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
