"""脚本执行器：顺序执行、拼帧收发、字段解析、timeout/retry/停止。

执行器不依赖 UI，通过 EngineListener 回调对外通知。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import __version__
from ..communication.base import Communication, Frame
from ..communication.can import CanCommunication
from ..communication.mock import MockCommunication
from ..communication.modbus import ModbusCommunication
from ..communication.serial import SerialCommunication
from ..config_loader import StationConfig
from ..errors import (
    CommunicationError,
    EngineBug,
    ExtensionError,
    RuntimeAteError,
    SimpleAteError,
    TimeoutAteError,
)
from ..logging_conf import TraceLogger
from ..storage.base import ResultStore, RunMeta, RunResult
from . import frame_io
from .context import RunContext
from .extension import ExtensionLoader, resolve_kwargs
from .frame_io import do_send, do_wait
from .judge import judge, resolve
from .model import (
    Action,
    ConnectStmt,
    DelayStmt,
    DisconnectStmt,
    ItemResult,
    ManualJudgeRequest,
    Script,
    Send,
    Step,
    Wait,
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class EngineListener:
    def on_run_start(self, ctx: RunContext, script: Script) -> None: ...
    def on_step_start(self, step: Step, attempt: int) -> None: ...
    def on_log(self, level: str, message: str) -> None: ...
    def on_trace(self, direction: str, resource: str, frame: Frame) -> None: ...
    def on_step_finish(self, step: Step, item: ItemResult) -> None: ...
    def on_run_finish(self, result: RunResult) -> None: ...

    def on_manual_judge(self, request: ManualJudgeRequest) -> bool | None:
        """人工判定（`<limit mode="manual">`）。

        返回 True=合格、False=不合格、None=中止（Run 最终为 ABORT）。
        基类默认返回 None（无人工判定能力时安全中止）。
        """
        return None


@dataclass
class _CollectingListener(EngineListener):
    """默认监听器：转 logger。"""

    logger: Any = None

    def on_log(self, level, message):
        if self.logger:
            getattr(self.logger, level.lower(), self.logger.info)(message)


@dataclass
class Engine:
    config: StationConfig
    store: ResultStore
    listener: EngineListener
    extensions_dir: str | Path = "extensions"
    stop_event: threading.Event = field(default_factory=threading.Event)
    trace: TraceLogger | None = None
    extensions: ExtensionLoader | None = None

    # ------------------------------------------------------------ 资源工厂
    def _build_resource(self, stmt: ConnectStmt) -> Communication:
        """按 <connect> 与工位配置合并后的结果创建通信资源。

        优先级：脚本 <connect> 属性 > 工位配置 [resources.xxx]；
        protocol 未显式给出时取工位配置的 type。
        """
        name = stmt.resource
        spec = self.config.resources.get(name)
        options: dict[str, Any] = dict(spec.options) if spec else {}
        options.update(stmt.options_dict())
        proto = stmt.protocol or (spec.type if spec else None)
        if proto is None:
            raise CommunicationError(
                f"资源 {name!r} 未在工位配置中定义，且 <connect> 未指定 protocol")
        if proto == "mock":
            mock_script = options.get("mock_script", "")
            if mock_script:
                p = Path(mock_script)
                if not p.is_absolute():
                    p = self.config.base_dir / p
                mock_script = str(p.resolve())
            return MockCommunication(name, script_path=mock_script)
        if proto == "can":
            return CanCommunication(
                name, interface=str(options.get("interface", "")),
                channel=str(options.get("channel", "")),
                bitrate=int(options.get("bitrate", 500000)))
        if proto == "serial":
            return SerialCommunication(name, **self._serial_options(options))
        if proto == "usb":
            opts = self._serial_options(options)
            opts["resolve_usb"] = True
            return SerialCommunication(name, **opts)
        if proto == "modbus":
            return ModbusCommunication(name, **self._modbus_options(options))
        raise CommunicationError(f"未知资源类型/协议: {proto}")

    @staticmethod
    def _serial_options(options: dict) -> dict:
        keys = ("port", "baudrate", "bytesize", "parity", "stopbits",
                "flowcontrol", "read_timeout", "frame_gap", "max_frame",
                "vid", "pid", "serial_number")
        return {k: options[k] for k in keys if k in options}

    @staticmethod
    def _modbus_options(options: dict) -> dict:
        keys = ("modbus_mode", "port", "baudrate", "bytesize", "parity",
                "stopbits", "flowcontrol", "read_timeout", "frame_gap",
                "max_frame", "host", "tcp_port", "unit")
        return {k: options[k] for k in keys if k in options}

    # ------------------------------------------------------------ 停止
    def request_stop(self) -> None:
        self.stop_event.set()

    def _check_stop(self) -> bool:
        return self.stop_event.is_set()

    def _interruptible_sleep(self, seconds: float) -> None:
        """每 50ms 响应停止标志。"""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._check_stop():
                return
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))

    # ------------------------------------------------------------ Run
    def run(self, script: Script, sn: str, trace_path: str | Path | None = None) -> RunResult:
        self.stop_event.clear()
        self.extensions = ExtensionLoader(
            self.extensions_dir, self.config.extensions_allowed)

        ctx = RunContext(sn=sn, station_id=self.config.station_id,
                         logger=_get_logger(self.listener),
                         script_dir=Path(script.path).parent if script.path
                         else self.config.base_dir)
        self.trace = TraceLogger(trace_path) if trace_path else None

        start_time = now_iso()
        meta = RunMeta(
            sn=sn, script_name=script.name, script_version=script.version,
            script_sha256=script.sha256, software_version=__version__,
            station_id=self.config.station_id, start_time=start_time,
            script_path=script.path)
        handle = self.store.create_run(meta)
        items: list[ItemResult] = []
        seq = 0

        self.listener.on_run_start(ctx, script)
        ctx.logger.info(f"[RUN] sn={sn} script={script.name} v{script.version} started")

        final_result = "PASS"
        aborted = False

        try:
            for stmt in script.statements:
                if self._check_stop():
                    aborted = True
                    break

                if isinstance(stmt, ConnectStmt):
                    try:
                        self._do_connect(ctx, stmt)
                    except CommunicationError as exc:
                        # 资源打开失败属链路异常 E302，记录日志并终止 Run
                        ctx.logger.error(f"[{stmt.resource}] 打开失败 {exc.code}: {exc.message}")
                        final_result = "ERROR"
                        break
                elif isinstance(stmt, DisconnectStmt):
                    self._do_disconnect(ctx, stmt)
                elif isinstance(stmt, DelayStmt):
                    self._interruptible_sleep(stmt.ms / 1000.0)
                    if self._check_stop():
                        aborted = True
                        break
                elif isinstance(stmt, Step):
                    seq += 1
                    item = self._run_step(ctx, stmt, seq)
                    items.append(item)
                    self.store.append_item(handle, item)
                    self.listener.on_step_finish(stmt, item)

                    if item.result == "ERROR":
                        final_result = "ERROR"
                        if stmt.on_fail == "abort":
                            ctx.logger.error(
                                f"[STEP] {stmt.name} ERROR（abort），终止测试")
                            break
                    elif item.result == "FAIL":
                        if final_result != "ERROR":
                            final_result = "FAIL"
                        if stmt.on_fail == "abort":
                            ctx.logger.warning(
                                f"[STEP] {stmt.name} FAIL（abort），终止测试")
                            break
        finally:
            # 无论正常/异常/中止，关闭所有资源
            for name, comm in list(ctx.resources.items()):
                try:
                    comm.close()
                    ctx.logger.info(f"[{name}] resource closed")
                except Exception as exc:  # noqa: BLE001
                    ctx.logger.error(f"[{name}] close 失败: {exc}")
            if self.trace:
                self.trace.close()

        if aborted or self._check_stop():
            final_result = "ABORT"

        end_time = now_iso()
        result = self.store.finish_run(handle, final_result, end_time)
        ctx.logger.info(f"[RUN] sn={sn} finished -> {final_result}")
        self.listener.on_run_finish(result)
        return result

    # ------------------------------------------------------------ 资源语句
    def _do_connect(self, ctx: RunContext, stmt: ConnectStmt) -> None:
        if stmt.resource in ctx.resources:
            raise SimpleAteError(f"资源 {stmt.resource} 重复 connect")
        comm = self._build_resource(stmt)
        comm.open()
        ctx.resources[stmt.resource] = comm
        ctx.logger.info(f"[{stmt.resource}] opened")

    def _do_disconnect(self, ctx: RunContext, stmt: DisconnectStmt) -> None:
        comm = ctx.resources.pop(stmt.resource, None)
        if comm is not None:
            comm.close()
            ctx.logger.info(f"[{stmt.resource}] closed")
        else:
            ctx.logger.warning(f"[{stmt.resource}] disconnect 时资源未打开（忽略）")

    # ------------------------------------------------------------ step
    def _select_resource(self, ctx: RunContext, step: Step) -> Communication:
        if step.resource:
            return ctx.resource(step.resource)
        if len(ctx.resources) == 1:
            return next(iter(ctx.resources.values()))
        raise EngineBug(f"step {step.name} 资源无法确定（解析期应已拦截）")

    def _run_step(self, ctx: RunContext, step: Step, seq: int) -> ItemResult:
        attempts = step.retry + 1
        last_error: RuntimeAteError | None = None
        t0 = time.monotonic()

        for attempt in range(1, attempts + 1):
            if self._check_stop():
                break
            self.listener.on_step_start(step, attempt)
            ctx.logger.info(f"[STEP] {step.name} start (attempt {attempt}/{attempts})")
            try:
                value, unit = self._run_attempt(ctx, step)
                elapsed = time.monotonic() - t0

                # 人工判定：不重试，不按帧自动判定
                if step.limit is not None and step.limit.mode == "manual":
                    judged_value = value
                    if step.limit.value is not None:
                        judged_value = _jsonable(
                            resolve(step.limit.value, ctx.variables))
                    unit = step.limit.unit or unit
                    request = ManualJudgeRequest(
                        seq=seq, step_name=step.name,
                        prompt=step.limit.prompt or step.name,
                        value=judged_value, unit=unit)
                    decision = self.listener.on_manual_judge(request)
                    if decision is None:
                        self.stop_event.set()
                        return self._item(step, seq, judged_value, unit, "ERROR",
                                          elapsed, attempt - 1,
                                          error_code="E310",
                                          message="人工判定被取消（Run 中止）",
                                          judge_mode="manual")
                    return self._item(
                        step, seq, judged_value, unit,
                        "PASS" if decision else "FAIL", elapsed, attempt - 1,
                        message="人工判定：合格" if decision else "人工判定：不合格",
                        judge_mode="manual")

                # 自动判定
                if step.limit is not None:
                    passed, judged = judge(step.limit, ctx.variables)
                    value = judged
                    unit = step.limit.unit or unit
                    if not passed:
                        return self._item(step, seq, value, unit, "FAIL",
                                          elapsed, attempt - 1,
                                          message="测量值超出限值")
                return self._item(step, seq, value, unit, "PASS",
                                  elapsed, attempt - 1, message=None)
            except RuntimeAteError as exc:
                last_error = exc
                if not exc.retryable or attempt == attempts:
                    break
                ctx.logger.warning(
                    f"[STEP] {step.name} {exc.code}（第 {attempt} 次尝试）：{exc.message}，重试")
                self._interruptible_sleep(step.retry_interval)
            except Exception as exc:  # noqa: BLE001 - 未预期异常 = 引擎/扩展 bug
                ctx.logger.exception(f"[STEP] {step.name} 未预期异常")
                return self._item(step, seq, None, None, "ERROR",
                                  time.monotonic() - t0, attempt - 1,
                                  error_code="E303", message=f"未预期异常: {exc}")

        # 重试用尽 / 不可重试的运行期错误
        code = getattr(last_error, "code", "E303")
        message = getattr(last_error, "message", str(last_error))
        ctx.logger.error(f"[STEP] {step.name} -> ERROR {code}: {message}")
        return self._item(step, seq, None, None, "ERROR",
                          time.monotonic() - t0, step.retry,
                          error_code=code, message=message)

    def _run_attempt(self, ctx: RunContext, step: Step):
        """执行一个 step 内的全部指令，返回 (最后产出的测量值, unit)。"""
        comm = self._select_resource(ctx, step)
        value: Any = None
        unit: str | None = None
        # drain 语义：wait 的 drain="before"（默认）表示在与之配对的 send 之前清空残留帧。
        # 前向扫描：若下一个 wait 需要 drain，则在本条 send 前清空。
        instrs = step.instructions
        for idx, instr in enumerate(instrs):
            if self._check_stop():
                break
            if isinstance(instr, Send):
                nxt = instrs[idx + 1] if idx + 1 < len(instrs) else None
                if isinstance(nxt, Wait) and nxt.drain == "before":
                    frame_io.drain(comm)
                if instr.mode == "file":
                    for frame in frame_io.iter_send_file(
                            instr, comm, ctx.script_dir, self._check_stop):
                        self._emit_tx(ctx, comm, frame)
                else:
                    self._emit_tx(ctx, comm, do_send(instr, comm, ctx.variables))
            elif isinstance(instr, Wait):
                # 独立 wait（前面没有配对 send）：在等待开始时按 drain 处理
                prev = instrs[idx - 1] if idx > 0 else None
                if not isinstance(prev, Send) and instr.drain == "before":
                    frame_io.drain(comm)
                if instr.mode == "file":
                    stats = frame_io.do_wait_file(
                        instr, comm, ctx.variables, ctx.script_dir, step.timeout,
                        should_cancel=self._check_stop,
                        on_frame=lambda fr: self._emit_rx(ctx, comm, fr))
                    value = stats.bytes_received
                    unit = "B"
                    ctx.logger.info(
                        f"[RX][{comm.name}] file {stats.bytes_received} bytes / "
                        f"{stats.chunks} chunks -> {stats.path}")
                else:
                    frame, extracted = do_wait(instr, comm, ctx.variables,
                                               default_timeout=step.timeout,
                                               should_cancel=self._check_stop)
                    self._emit_rx(ctx, comm, frame)
                    ctx.logger.info(f"[RX][{comm.name}] {frame} fields={extracted}")
                    if extracted:
                        # 取本 wait 最后一个字段作为 step 测量值候选
                        last_field = instr.fields[-1]
                        value = extracted[last_field.var]
                        unit = last_field.unit
            elif isinstance(instr, DelayStmt):
                self._interruptible_sleep(instr.ms / 1000.0)
            elif isinstance(instr, Action):
                value = self._run_action(ctx, comm, instr)

        return value, unit

    def _emit_tx(self, ctx: RunContext, comm: Communication, frame: Frame) -> None:
        self.listener.on_trace("TX", comm.name, frame)
        if self.trace:
            self.trace.tx(comm.name, frame)
        ctx.logger.info(f"[TX][{comm.name}] {frame}")

    def _emit_rx(self, ctx: RunContext, comm: Communication, frame: Frame) -> None:
        self.listener.on_trace("RX", comm.name, frame)
        if self.trace:
            self.trace.rx(comm.name, frame)
        ctx.logger.info(f"[RX][{comm.name}] {frame}")

    def _run_action(self, ctx: RunContext, comm: Communication, instr: Action):
        assert self.extensions is not None
        func = self.extensions.load(instr.handler)
        kwargs = resolve_kwargs(instr.kwargs, ctx.variables)
        try:
            ret = func(ctx, comm, **kwargs)
        except (TimeoutAteError, CommunicationError, ExtensionError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise ExtensionError(f"扩展 {instr.handler} 抛出异常: {exc}") from exc
        if instr.var:
            ctx.variables[instr.var] = ret
        return ret

    @staticmethod
    def _item(step: Step, seq: int, value, unit, result: str, elapsed: float,
              retries: int, error_code: str | None = None,
              message: str | None = None,
              judge_mode: str = "auto") -> ItemResult:
        lo = step.limit.min if step.limit else None
        hi = step.limit.max if step.limit else None
        return ItemResult(
            seq=seq, step_name=step.name,
            value=_jsonable(value), unit=unit,
            low_limit=lo, high_limit=hi,
            result=result, duration_ms=int(elapsed * 1000),
            retries=retries, error_code=error_code, message=message,
            judge_mode=judge_mode)


def _jsonable(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)) or value is None:
        return value
    return str(value)


def _get_logger(listener: EngineListener):
    """从 listener 上取 logger（CLI 注入），没有则用标准 logger。"""
    import logging
    found = getattr(listener, "logger", None)
    if found is not None:
        return found
    return logging.getLogger("simple_ate")
