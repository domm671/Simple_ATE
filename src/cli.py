"""命令行入口（M1 无头模式）。

用法:
    python -m simple_ate run <script.xml> --sn <SN> [--config config/station.toml]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config_loader import load_config
from .engine import Engine, EngineListener
from .engine.parser import ScriptParser
from .logging_conf.logger import run_logging, trace_path_for
from .storage.file_store import FileResultStore

_RESULT_EXIT = {"PASS": 0, "FAIL": 1, "ERROR": 2, "ABORT": 3}


class CliListener(EngineListener):
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def on_manual_judge(self, request):
        """无头模式：在终端询问操作员；非交互式环境返回 None（中止）。"""
        prompt = f"人工判定 [{request.step_name}]：{request.prompt}"
        if request.value is not None:
            prompt += f"（当前值 {request.value}{request.unit or ''}）"
        if not sys.stdin or not sys.stdin.isatty():
            self.logger.error(f"{prompt} —— 非交互式环境，无法人工判定，中止")
            return None
        try:
            answer = input(prompt + " [y=合格 / n=不合格 / 其它=中止] ").strip().lower()
        except EOFError:
            return None
        if answer in ("y", "yes", "1"):
            return True
        if answer in ("n", "no", "0"):
            return False
        return None

    def on_run_start(self, ctx, script):
        self.logger.info(f"脚本 {script.name} v{script.version}，sha256={script.sha256[:12]}…")

    def on_step_finish(self, step, item):
        mark = {"PASS": "✓", "FAIL": "✗", "ERROR": "!"}[item.result]
        val = "" if item.value is None else f" = {item.value}{item.unit or ''}"
        extra = f" [{item.error_code}: {item.message}]" if item.error_code else ""
        self.logger.info(f"  {mark} {item.step_name}{val} -> {item.result}{extra}")

def _cmd_run(args) -> int:
    cfg = load_config(args.config)
    sn = args.sn.strip()
    if message := cfg.sn.validation_error(sn):
        print(message, file=sys.stderr)
        return 2
    log_dir = cfg.resolve_path(cfg.storage.log_dir)
    result_dir = cfg.resolve_path(cfg.storage.result_dir)

    script_path = Path(args.script)
    parser = ScriptParser(
        station_resources=cfg.resource_names(),
        allowed_extensions=set(cfg.extensions_allowed),
        station_protocols={name: spec.type
                           for name, spec in cfg.resources.items()})
    script = parser.parse_file(str(script_path))

    with run_logging(log_dir) as (logger, log_path):
        logger.info(f"simple_ATE {cfg.software_version} | station={cfg.station_id} | log={log_path}")
        store = FileResultStore(result_dir, mes_enabled=cfg.mes.enabled)
        recovered = store.recover_aborted()
        if recovered:
            logger.warning(f"启动恢复：{recovered} 个未完成 Run 标记为 ABORT")
        engine = Engine(config=cfg, store=store, listener=CliListener(logger),
                        extensions_dir=cfg.resolve_path(cfg.extensions_dir))
        result = engine.run(script, sn=sn, trace_path=trace_path_for(log_path, sn))
        logger.info(f"最终结论: {result.result}")
        return _RESULT_EXIT[result.result]


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
