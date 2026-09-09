"""命令行入口（M1 无头模式）。

用法:
    python -m simple_ate run <script.xml> --sn <SN> [--config config/station.toml]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from .config_loader import load_config
from .engine import Engine, EngineListener
from .logging_conf import TraceLogger, setup_run_logger
from .parser import ScriptParser
from .storage.file_store import FileResultStore

_RESULT_EXIT = {"PASS": 0, "FAIL": 1, "ERROR": 2, "ABORT": 3}


class CliListener(EngineListener):
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def on_run_start(self, ctx, script):
        self.logger.info(f"脚本 {script.name} v{script.version}，sha256={script.sha256[:12]}…")

    def on_step_start(self, step, attempt):
        pass

    def on_log(self, level, message):
        getattr(self.logger, level, self.logger.info)(message)

    def on_trace(self, direction, resource, frame):
        pass

    def on_step_finish(self, step, item):
        mark = {"PASS": "✓", "FAIL": "✗", "ERROR": "!"}[item.result]
        val = "" if item.value is None else f" = {item.value}{item.unit or ''}"
        extra = f" [{item.error_code}: {item.message}]" if item.error_code else ""
        self.logger.info(f"  {mark} {item.step_name}{val} -> {item.result}{extra}")

    def on_run_finish(self, result):
        pass


def _cmd_run(args) -> int:
    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)

    log_dir = _resolve(cfg, cfg.storage.log_dir)
    result_dir = _resolve(cfg, cfg.storage.result_dir)

    logger, log_path = setup_run_logger(log_dir)
    logger.info(f"simple_ATE {cfg.software_version} | station={cfg.station_id} | log={log_path}")

    script_path = Path(args.script)
    parser = ScriptParser(
        station_resources=cfg.resource_names(),
        allowed_extensions=set(cfg.extensions_allowed))
    script = parser.parse_file(str(script_path))

    store = FileResultStore(result_dir, mes_enabled=cfg.mes.enabled)
    recovered = store.recover_aborted()
    if recovered:
        logger.warning(f"启动恢复：{recovered} 个未完成 Run 标记为 ABORT")

    trace_dir = log_dir / "trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trace_path = trace_dir / f"trace_{args.sn}_{stamp}.log"

    listener = CliListener(logger)
    engine = Engine(config=cfg, store=store, listener=listener,
                    extensions_dir=cfg.base_dir / cfg.extensions_dir)
    result = engine.run(script, sn=args.sn, trace_path=trace_path)

    logger.info(f"最终结论: {result.result}")
    return _RESULT_EXIT.get(result.result, 2)


def _resolve(cfg, path: str) -> Path:
    """配置中的相对路径以配置文件所在目录为基准。"""
    p = Path(path)
    return p if p.is_absolute() else cfg.base_dir / p


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="simple-ate", description="simple_ATE 无头运行器")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("run", help="执行测试脚本")
    pr.add_argument("script", help="XML 脚本路径")
    pr.add_argument("--sn", required=True, help="产品序列号")
    pr.add_argument("--config", default="config/station.toml", help="工位配置路径")
    pr.set_defaults(func=_cmd_run)

    pg = sub.add_parser("gui", help="启动 PySide6 图形界面")
    pg.add_argument("--config", default="config/station.toml", help="工位配置路径")
    pg.set_defaults(func=_cmd_gui)
    return p


def _cmd_gui(args) -> int:
    try:
        from .ui.app import main as gui_main
    except ImportError:
        print("未安装 PySide6。请先安装 GUI 依赖：pip install -e .[gui]",
              file=sys.stderr)
        return 2
    return gui_main(["--config", args.config])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
