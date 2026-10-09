# engine 模块说明（脚本引擎）

> 对应《设计说明.md》第 3 章总体架构中的 **Script Engine** 层、第 4.1 节模块设计。
> 导入路径：`simple_ate.engine.*`。

## 1. 模块定位

脚本引擎是软件的核心层，负责把 XML 脚本解析为内存对象，并严格按顺序执行测试流程：
拼帧发送、等待应答、字段提取、限值判定、重试、延时、人工停止。

**本包不 import 任何 PySide6/Qt 模块**，可在命令行下独立运行。引擎通过
`EngineListener` 回调接口把运行事件对外通知，CLI 与 GUI 各自实现监听，
展示逻辑不进入引擎。

## 2. 文件组成

| 文件 | 职责 |
|---|---|
| `model.py` | 全部脚本语句与结果的内存模型，`@dataclass(frozen=True)`；连接参数、文件传输字段、人工判定、`ItemResult` 及其 JSON 序列化 |
| `parser.py` | XML → `Script`；加载期完成全部静态校验，错误聚合一次报出（`ScriptParseErrors`） |
| `executor.py` | `Engine` 类：顺序执行、retry/on_fail、停止标志、人工判定回调、文件传输、资源生命周期（注意：类名是 `Engine`） |
| `frame_io.py` | 内建原语的落地：send 拼帧、wait 匹配轮询、field 提取换算、drain、文件分块收发与校验 |
| `judge.py` | limit 判定：min/max 闭区间、eq 等值、变量取值、类型相容性检查 |
| `context.py` | `RunContext`：一次 Run 的 SN、变量表、已打开资源句柄、logger |
| `extension.py` | `ExtensionLoader`：importlib 加载扩展模块 + 工位配置白名单；`resolve_kwargs` 入参绑定 |
| `__init__.py` | 包入口，导出 `Engine` / `EngineListener` / `ScriptParser` |

## 3. 关键数据模型（model.py）

- 脚本层：`Script`（含 `name/version/sha256/statements/path`）；
- 顶层语句 `Statement`：`ConnectStmt` / `DisconnectStmt` / `DelayStmt` / `Step`；
  `ConnectStmt` 携带 `protocol` 与 `options`（串口/Modbus/USB/CAN 参数）；
- step 内指令 `Instruction`：`Send` / `Wait` / `DelayStmt` / `Action`；
- `Send`/`Wait` 均有 `mode`（`single`/`file`）：文件模式携带 `file`/`chunk_size`/`seq_len`/
  `header`/`checksum`/`size` 等；
- `Wait` 内可有多个 `Field`；`Step` 至多一个 `Limit` 且必须位于最后；
  `Limit.mode` 为 `auto`（默认）或 `manual`（人工）；
- `ManualJudgeRequest` 为传给 `EngineListener.on_manual_judge` 的快照；
- `VarRef` 表示属性值中的 `${name}` 引用；
- 结果模型 `ItemResult`（可变 dataclass），提供 `to_dict()/from_dict()`，
  字段名即结果 JSON / MES payload 契约，**新增只追加不更名**（已新增 `judge_mode`）。

## 4. 运行机制

### 4.1 唯一执行入口

```python
Engine(config, store, listener, extensions_dir).run(script, sn, trace_path) -> RunResult
```

`run()` 的执行顺序：

1. 清空停止标志，构造 `ExtensionLoader` 与 `RunContext`；
2. 由 `RunMeta` 经 `store.create_run()` 创建结果文件；
3. 回调 `on_run_start`，随后逐条遍历顶层语句：
   - `connect`：经资源工厂创建通信对象并 `open()`，失败按 E302 终止；
   - `disconnect`：关闭并移除资源；
   - `delay`：可中断延时；
   - `step`：执行（见 4.2），每步完成即 `store.append_item()` 落盘并回调 `on_step_finish`；
4. `finally` 中**无条件关闭所有已打开资源**、关闭 trace；
5. `store.finish_run()` 写入最终结论，回调 `on_run_finish`。

### 4.2 Step 与重试语义

- 总尝试次数 = `retry + 1`，重试粒度是整个 step；
- **仅 `retryable=True` 的 `RuntimeAteError` 触发重试**（E301/E302/E305/E306），
  尝试之间执行可中断的 `retry_interval`；
- 判定 **FAIL 不重试**；E307（send data 非法）、E209（判定类型错误）不重试；
  未预期异常记 **E303**；
- step 内指令顺序执行：配对 `wait` 的 `drain="before"` 在其**前一条 send 之前**
  清空缓冲；独立 wait 在等待开始时清空（drain 不能放在 `do_wait` 内，
  否则会删掉同步 mock 中 send 即刻入队的应答）；
- field 提取：无符号整数按 offset/length/endian 取 raw，
  `value = raw*gain + bias`（`raw="true"` 跳过换算），越界抛 E305；
  浮点结果经 `_round_engineering()` 消除尾数噪声；
- 人工判定：`<limit mode="manual">` 时调 `listener.on_manual_judge(request)`，
  返回 True/False/None 分别对应 PASS/FAIL/ABORT；不重试；
- 文件传输：`<send mode="file">` 按 `header`+序号+分块逐帧发送（`iter_send_file`）；
  `<wait mode="file">` 按 `size`/`chunks`/`idle_gap` 终止并原子落盘（`do_wait_file`），
  失败抛 E308（可重试整步）。

### 4.3 Run 结论聚合

任一步 **ERROR → Run=ERROR**（ERROR 优先于 FAIL，工装异常不计产品不良）；
否则任一 FAIL → FAIL；全 PASS → PASS；人工停止 → ABORT（已有明细保留）。

### 4.4 停止机制

`request_stop()` 设置 `threading.Event`，为协作式中断（不 kill 线程）。
检查点：顶层语句与指令边界、Attempt 之间、`delay` 每 50ms
（`_interruptible_sleep`）、wait 轮询每 50ms（`do_wait` 的 `should_cancel`）。

### 4.5 变量与扩展

- 变量为 Run 级全局，存入 `ctx.variables`；`${name}` 仅简单取值，无表达式；
  解析期做“定义先于引用”检查；
- `<action handler="module:func">`：模块必须在工位配置
  `[extensions] allowed` 白名单内，否则 E201；`resolve_kwargs`
  把入参（含 `${var}`）统一转成字符串后调用
  `func(ctx, resource, **kwargs)`；返回值可写入 `var` 指定变量；
- 资源在 `Engine._build_resource(stmt)` 构造：脚本 `<connect>` 参数覆盖工位配置，
  `protocol` 未给时回退到工位 `type`。

## 5. 事件回调接口（EngineListener）

```python
on_run_start(ctx, script)
on_step_start(step, attempt)          # attempt 为 1 基
on_log(level, message)
on_trace(direction, resource, frame)  # TX/RX
on_step_finish(step, item_result)
on_run_finish(run_result)
on_manual_judge(request) -> bool|None # 人工判定：True/False/None(中止)
```

CLI（`simple_ate.cli.CliListener`）直接打印；GUI 由
`simple_ate.ui.engine_bridge.EngineBridge` 转成 Qt 信号投递主线程。

## 6. 设计取舍

- **脚本即协议**：CAN ID、字节布局、gain/bias 全部声明在脚本中，普通产品测试
  无需编写任何 Python 代码；多帧/校验和/UDS 等复杂场景才走扩展 action；
- **错误尽早暴露**：结构、类型、资源、变量、扩展白名单等全部在加载期校验，
  解析器一次收集全部错误而非遇错即停；
- 资源类型（mock/can/serial/usb/modbus）默认由工位配置决定；
  v2.1 起 `<connect>` 可用 `protocol` + 参数内联声明并覆盖工位配置，
  以便单脚本自描述串口工位。

## 7. 相关测试

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_parser.py` | 解析与全量静态校验、错误码 |
| `tests/test_executor.py` | 执行、retry/on_fail、停止、资源生命周期 |
| `tests/test_frame_judge.py` | 拼帧、ID/mask 匹配、字段提取、判定 |
| `tests/test_extension.py` | 扩展白名单加载与入参绑定 |
| `tests/test_file_transfer.py` | 文件分块收发、序号重排、校验、Modbus 组帧 |
