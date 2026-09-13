# AGENTS.md

本文件面向在本仓库工作的 AI 编码助手（也适合新加入的人类开发者），说明项目是什么、如何构建/测试、代码如何组织、以及改动时必须遵守的约束。

> 权威文档：`README.md`（上手与运行）、`设计说明.md`（架构与设计决策）、`ATE脚本格式规范.md`（Script XML v1 唯一语法权威）。修改脚本语法时必须同步更新规范文档。

---

## 1. 项目是什么

Simple_ATE 是一个尽量小的产线自动测试（ATE/FT）执行软件，运行在 Windows 工位机上，按 **XML 声明式脚本** 通过 CAN（预留 serial/TCP）与被测装备交互，完成顺序测试、限值判定、结果记录与 MES 上传扩展点。

核心理念：

- **脚本即协议，写脚本不改代码**：CAN ID、数据、应答匹配、字段解析全部声明在 XML 中；引擎内建拼帧/收发/解析，普通产品测试不需要任何 Python 代码。
- **先无头后界面**：引擎不依赖 UI，可 CLI 独立运行（M1），PySide6 界面（M2）只是引擎的一个前端。
- **真机/Mock 等价**：`type="mock"` 用 JSON 应答脚本即可无硬件开发测试。
- **结果绝不丢失**：每步即时落 JSON 并 flush，MES 故障不阻塞生产。
- **刻意保持小**：无 DSL/表达式引擎、无数据库/ORM、无插件系统、无多设备并行（见 `设计说明.md` 第 13 章非目标）。

当前里程碑：M1（无头引擎 + Mock）✅、M2（PySide6 GUI 含脚本编辑器）✅、M3（真实 CAN 联调，适配代码已就位）⬜、M4（MES Outbox 上传）⬜。

## 2. 技术栈与依赖纪律

- **Python ≥ 3.11**（开发验证环境 3.13），扁平 src-layout：代码文件直接放在 `src/` 下（导入名仍是包 `simple_ate`，由 `pyproject.toml` 的 `tool.setuptools.package-dir` 把 `simple_ate` 映射到物理目录 `src`）。
- 引擎核心**零第三方依赖**，全部使用标准库（`xml.etree.ElementTree`、`tomllib`、`dataclasses`、`importlib`、`logging`、`unittest` 等）。
- 可选依赖：`gui = ["PySide6>=6.5"]`；真实 CAN 由使用方自行安装 `python-can`。
- **新增依赖前先想能否用标准库解决**；任何新依赖都属于重要架构决策，需有充分理由并更新 `pyproject.toml` 与 `设计说明.md` 第 11 章。
- License：Apache-2.0。

## 3. 常用命令

安装（Windows cmd 写法，Linux/macOS 等价调整）：

```bat
pip install -e .            :: 仅引擎/Mock
pip install -e .[gui]       :: 含 PySide6 界面
```

无头运行一次测试（无需硬件、无需第三方库）：

```bat
python -m simple_ate run scripts/bms_ft.xml --sn BMS20260909001 --config config/station.toml
```

启动 GUI：

```bat
simple-ate-gui --config config/station.toml
:: 或 python -m simple_ate gui --config config/station.toml
```

进程退出码（产线脚本依赖，勿改语义）：**PASS=0、FAIL=1、ERROR=2、ABORT=3**（见 `cli.py` 的 `_RESULT_EXIT`）。

测试只用标准库 `unittest`，不用 pytest：

```bat
:: 引擎相关（任意环境可跑）
python -m unittest tests.test_parser tests.test_executor tests.test_frame_judge ^
                   tests.test_extension tests.test_storage -v

:: 全部（含 UI），Qt 需指定 offscreen 平台
set QT_QPA_PLATFORM=offscreen
python -m unittest discover -s tests -v
```

- 未安装 PySide6 时 `tests/test_ui.py`、`tests/test_script_editor.py` 无法收集属正常，不代表引擎回归。
- 当前约 83 个测试；**改动引擎/解析器后必须跑全量引擎测试，改动 UI 后必须在 offscreen 下跑 UI 测试，并随功能补充测试**。

端到端手工验证：跑无头命令后检查 `data/results/uploaded/*.json`、`data/logs/run_*.log`、`data/logs/trace/trace_*.log`（`data/` 已 gitignore，为运行产物）。

## 4. 目录结构（实际布局）

```text
Simple_ATE/
├─ src/                         导入名 simple_ate（pyproject package-dir 映射）
│  ├─ __main__.py / cli.py      入口与 run/gui 子命令、CliListener、退出码
│  ├─ parser.py                 XML → Script；全量静态校验，错误聚合一次报出（E1xx/E2xx）
│  ├─ model.py                  frozen dataclass：脚本语句模型 + ItemResult
│  ├─ engine.py                 Engine：顺序执行、retry/on_fail、停止标志、资源生命周期
│  ├─ frame_io.py               send 拼帧、wait 匹配轮询、field 提取换算、drain
│  ├─ judge.py                  limit 判定（min/max 闭区间、eq）
│  ├─ extension.py              <action handler="module:func"> importlib 加载 + 白名单
│  ├─ context.py                RunContext（SN、变量表、资源句柄、logger）
│  ├─ errors.py                 异常体系与错误码（E1xx/E2xx 加载期，E3xx 运行期）
│  ├─ config_loader.py          station.toml → StationConfig（tomllib）
│  ├─ logging_conf.py           run log（logging）+ TraceLogger（逐帧 TX/RX）
│  ├─ communication/            base(Protocol+Frame) / mock / can(python-can) / serial(占位)
│  ├─ storage/                  base(ResultStore/RunResult) + file_store(原子写+Outbox 目录)
│  ├─ mes/                      MesUploader 协议 + NullUploader（M4 才实现上传）
│  └─ ui/                       app / main_window / engine_bridge / worker / script_editor
├─ scripts/                     产品测试脚本（*.xml），UI 默认列出此目录
├─ config/station.toml          工位配置（随工位部署，不含产品参数）
├─ config/mock/*.json           Mock 应答脚本（请求 id[/data] → 应答帧）
├─ extensions/                  现场定制 action（如 sample_ext.py）
├─ tests/                       unittest 测试（7 个模块）
├─ data/                        运行时生成：results/{pending,uploaded,failed}、logs/、logs/trace/
├─ pyproject.toml
├─ 设计说明.md
├─ ATE脚本格式规范.md
├─ README.md
└─ LICENSE（Apache-2.0）
```

## 5. 架构与依赖规则（改动时不要破坏）

分层依赖方向只能自上而下，**引擎层禁止 import 任何 PySide6/Qt 模块**：

```text
ui  ──Qt Signal(queued)──▶ EngineListener 回调  ──▶ engine ──▶ frame_io/judge/extension
                                                                      │
                                                                      ├─▶ communication（同步 Protocol）
                                                                      ├─▶ storage（ResultStore Protocol）
                                                                      └─▶ mes（MesUploader Protocol，仅扩展点）
```

关键事实：

- `Engine.run(script, sn, trace_path)` 是唯一执行入口；通过 `EngineListener`（`on_run_start/on_step_start/on_step_finish/on_log/on_trace/on_run_finish`）对外发事件。CLI 与 GUI 各自实现监听，不要把展示逻辑塞进引擎。
- 通信为**同步接口**（`open/close/send/recv(timeout)`），`recv` 超时必须抛 `errors.TimeoutAteError`（E301）。不使用 asyncio。
- 资源由 `Engine._build_resource` 按 `station.toml` 的 `[resources.xxx] type` 创建（mock/can/serial）；脚本只引用逻辑资源名，硬件参数绝不进脚本。
- 停止通过 `threading.Event` 协作式中断（`request_stop()`），不 kill 线程；指令边界、Attempt 之间、`delay` 每 50ms（`_interruptible_sleep`）、wait 轮询（50ms 粒度）检查停止标志。
- 所有已打开资源在 `Engine.run` 的 `finally` 中无条件 `close()`。
- GUI 线程模型：`RunWorker` 把 `_Runner` `moveToThread` 到 `QThread`；`EngineBridge`（QObject + EngineListener）在工作线程被回调，经 Qt 信号自动以 queued connection 投递主线程。**UI 线程绝不直接调用通信层或 Engine 阻塞方法**；桥接信号只传快照/基本类型，不传 `RunContext` 等带句柄对象。
- 配置中的相对路径以**配置文件所在目录**（`cfg.base_dir`）为基准解析；脚本目录默认可用环境变量 `SIMPLE_ATE_SCRIPTS` 覆盖。

## 6. 执行语义（改动引擎时务必保持）

- Run 结论聚合：任一步 **ERROR → Run=ERROR**（ERROR 优先于 FAIL，工装异常绝不计产品不良）；否则任一 FAIL → FAIL；全 PASS → PASS；人工停止 → ABORT（已有明细保留）。
- step 的 `on_fail`：`abort`（默认）FAIL/ERROR 即终止；`continue` 记录后继续。
- **retry 只针对 `retryable=True` 的 `RuntimeAteError`**（E301/E302/E305/E306），重试粒度是整个 step（总尝试 = retry+1）；**判定 FAIL、E209、E307 不重试**；未预期异常记 E303。
- `wait drain="before"`（默认）在**配对 send 之前**清空接收缓冲（`frame_io.drain`）；独立 wait 在等待开始时清空。注意 drain 不能放在 `do_wait` 内，否则会删掉同步 mock 中 send 即刻入队的应答。
- field：无符号整数按 offset/length/endian 取 raw，`value = raw*gain+bias`（`raw="true"` 跳过换算）；越界 E305（可重试）。浮点结果经 `_round_engineering` 消除尾数噪声。
- 变量为 Run 级全局，`${name}` 仅简单取值，无表达式；解析期做"定义先于引用"检查。
- 结果文件每步完成即整体覆写（临时文件 + `os.replace` 原子替换 + fsync）；启动时 `FileResultStore.recover_aborted()` 把缺 `result` 字段的残留文件标记为 ABORT；同名 SN 用时间戳 + 序号保证永不覆盖历史。

## 7. 代码风格约定

- 注释、docstring、用户可见信息（日志/错误提示/UI 文案）使用**中文**；标识符用英文。
- 每个模块顶部写说明用途的模块 docstring；复杂设计取舍在注释中解释"为什么"（可参考 `frame_io.py`、`engine_bridge.py`、`file_store.py`）。
- 统一 `from __future__ import annotations`；完整类型注解；脚本/语句模型用 `@dataclass(frozen=True)`，配置用普通 dataclass。
- 对外抽象用 `typing.Protocol`（`Communication`、`ResultStore`、`MesUploader`）。
- 异常必须走 `errors.py` 体系并带错误码，不要裸抛 `Exception` 表达业务错误；捕获未知异常处保留现有 `# noqa: BLE001` 风格并记 E303。
- 引擎内日志走 `ctx.logger` / listener，不直接 print；CLI 入口错误才可 print 到 stderr。
- 缩进 4 空格，UTF-8，保持与周围代码一致；不引入格式化/ lint 配置以外的工具。
- 不修改 `data/` 下的产物文件；不要把运行产物、`*.log` 提交入库。

## 8. 脚本/配置/Mock 格式要点

- Script XML v1：根 `<test name version>`；顶层仅 `connect/disconnect/step/delay`；step 内仅 `send/wait/delay/action`，且至多一个 `limit` 且必须在最后，至少含一条 send/wait/action；`if/loop/call` 是 v2 预留标签，解析到报 E107。
- 解析器设计为**一次收集全部静态错误**（`ScriptParseErrors` 带错误码/行/列/中文说明），新增校验时沿用 `_Errors.add`，不要遇到第一个错误就返回。
- 错误码段位：E1xx 结构/类型、E2xx 语义/资源/变量/扩展（加载期）；E3xx 运行期。完整清单见《ATE脚本格式规范.md》第 10 章。
- `station.toml` 用标准库 tomllib；SN 默认不校验（仅非空），启用 `sn.validation_enabled` + `pattern` 后才拦；扫码回车自启受 `sn.auto_start` 控制。
- Mock JSON：规则按 `request_id`（可选 `request_data`、`ext`）匹配，应答用 `response_id/response_data` 或 `response_computed: {id, bytes, ext}`，支持 `delay`；无匹配规则即模拟超时。
- 图形脚本编辑器经 ElementTree 往返**会丢失 XML 注释**（属已知约束，UI 中有提示）；改动编辑器时保留该提示。

## 9. 扩展点（定制方通道，勿与核心耦合）

- **自定义 action**：在 `extensions/<mod>.py` 写普通函数 `def f(ctx, resource, **params)`，脚本 `<action handler="mod:func" var=".." 参数..>`；模块必须列入 `station.toml [extensions] allowed`，否则 E201；入参统一为字符串（`${var}` 已替换，见 `resolve_kwargs`）；超时/通信问题抛 `TimeoutAteError/CommunicationError` 可重试，其余异常 E306/E303。
- **新通信后端**：实现 `Communication` Protocol 并在 `Engine._build_resource` 注册新 type。
- **新结果存储**：实现 `ResultStore`（`create_run/append_item/finish_run`），配置 `storage.sink="custom"`，装配处注册；引擎/UI/MES 不应改动。
- **MES uploader（M4）**：实现 `MesUploader.upload(result: RunResult)`（成功返回，失败抛异常），以 `"module:class"` 配置；默认 `NullUploader`。MES 协议、鉴权、字段映射一律不进本仓库。

## 10. 常见改动的检查清单

- **新增/修改 XML 标签或属性**：同时改 ① `model.py` 数据结构；② `parser.py` 的 `ALLOWED_ATTRS/REQUIRED_ATTRS`、解析与校验（分配/复用错误码）；③ `ui/script_editor.py` 的 `TAG_CN/ATTR_SPEC` 及树形增删/序列化逻辑；④ `engine.py`/`frame_io.py` 执行语义；⑤ `ATE脚本格式规范.md`（含 EBNF/XSD/错误码表/示例）与 `设计说明.md`；⑥ 补 parser/executor（必要时 editor）测试。
- **改判定/重试/结论/退出码**：先读规范第 7、8 章；退出码与四态语义被产线依赖，原则上只修 bug 不改语义。
- **改结果 JSON 格式**：`model.ItemResult.to_dict/from_dict`、`RunResult.to_dict`、`file_store` 读写与 `recover_aborted` 要成对兼容；字段名即 MES payload 契约，新增只追加不更名。
- **改 GUI**：业务逻辑放引擎，UI 只经 `EngineBridge` 信号刷新；结束后回到 IDLE、清空 SN 并聚焦以支持连续扫码；提示用非阻塞方式（`_notify` 状态栏），避免模态框卡住产线。
- **新增依赖/新协议/数据库/并行测试**：这些属于 `设计说明.md` 明确的非目标，默认不做，需先与项目维护者确认并更新设计文档。

## 11. 提交前自查

1. `python -m unittest tests.test_parser tests.test_executor tests.test_frame_judge tests.test_extension tests.test_storage -v` 全绿；
2. 动了 UI 则 `set QT_QPA_PLATFORM=offscreen` 后 `python -m unittest discover -s tests -v` 全绿；
3. Mock 下无头跑通 `scripts/bms_ft.xml`，结论 PASS 且产物 JSON/log/trace 正常；
4. 脚本语法/错误码/配置项变更已同步三份文档（README、设计说明、脚本格式规范）；
5. 引擎代码未引入 PySide6，UI 代码未直接触碰通信层，未新增不必要的第三方依赖。
