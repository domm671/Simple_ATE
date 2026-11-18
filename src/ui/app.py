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
from ..storage.file_store import FileResultStore


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

    try:
        cfg = load_config(args.config)
        log_dir = cfg.resolve_path(cfg.storage.log_dir)
        result_dir = cfg.resolve_path(cfg.storage.result_dir)
        recovered = FileResultStore(result_dir, mes_enabled=cfg.mes.enabled).recover_aborted()
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(f"无法初始化工位：{exc}", file=sys.stderr)
        return 2
    # 脚本目录约定为配置文件同级的 ../scripts，可用环境变量覆盖
    scripts_dir = Path(os.environ.get(
        "SIMPLE_ATE_SCRIPTS", cfg.base_dir.parent / "scripts"))
    app = QApplication.instance() or QApplication(sys.argv)
    from .main_window import MainWindow
    win = MainWindow(cfg, log_dir, result_dir, scripts_dir)
    if recovered:
        win._append_log("warning", f"启动恢复：{recovered} 个未完成 Run 标记为 ABORT")
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
