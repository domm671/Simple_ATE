"""GUI 入口：加载工位配置，启动主窗口。

用法：
    simple-ate-gui [--config config/station.toml]
    python -m simple_ate gui [--config config/station.toml]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..config_loader import load_config


def _resolve(cfg, path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else cfg.base_dir / p


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="simple-ate-gui", description="simple_ATE 图形界面")
    parser.add_argument("--config", default="config/station.toml", help="工位配置路径")
    args = parser.parse_args(argv)

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("未安装 PySide6。请先安装 GUI 依赖：pip install -e .[gui]",
              file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    log_dir = _resolve(cfg, cfg.storage.log_dir)
    result_dir = _resolve(cfg, cfg.storage.result_dir)
    # 脚本目录约定为配置文件同级的 ../scripts，可用环境变量覆盖
    scripts_dir = Path(os.environ.get(
        "SIMPLE_ATE_SCRIPTS", cfg.base_dir.parent / "scripts"))
    log_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv)
    from .main_window import MainWindow
    win = MainWindow(cfg, log_dir, result_dir, scripts_dir)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
