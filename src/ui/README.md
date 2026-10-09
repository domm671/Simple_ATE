# ui 模块说明（PySide6 图形界面）

> 对应《设计说明.md》第 3 章总体架构最上层、第 4.7 节 UI、第 8 章线程模型。
> 导入路径：`simple_ate.ui.*`。PySide6 为可选依赖（`pip install -e .[gui]`）。

## 1. 模块定位

UI 是引擎的一个前端，**只做展示与操作，所有业务逻辑在引擎**。v0.1 为单窗口
五个区域：脚本选择、SN 输入、Start/Stop/Reset、逐项进度表、结果大字与日志。
引擎运行在独立工作线程，UI 线程绝不直接调用通信层或引擎阻塞方法。

## 2. 文件组成

| 文件 | 职责 |
|---|---|
| `app.py` | GUI 入口（`simple-ate-gui`）：解析参数、加载配置、创建 QApplication 与主窗口 |
| `main_window.py` | `MainWindow`：五区域主界面、状态机（IDLE/RUNNING）、桥接信号的界面刷新 |
| `engine_bridge.py` | `EngineBridge`：实现 `EngineListener`，把引擎回调转为 Qt 信号 |
| `worker.py` | `RunWorker` + `_Runner`：QThread + moveToThread 在工作线程运行引擎 |
| `script_editor.py` | `ScriptEditorDialog`：XML 原文 / 图形化双页签脚本编辑器 |
| `__init__.py` | 包标识 |

## 3. 线程模型与事件流

```text
UI 主线程 (Qt event loop)
   │ Start
   ▼
RunWorker 创建 QThread，_Runner moveToThread
   │ 工作线程：解析脚本 → 构造 Engine → engine.run()（全部同步调用）
   │              EngineBridge 在工作线程被回调
   ▼
Qt Signal（自动 QueuedConnection）投递回主线程刷新控件
```

### 3.1 engine_bridge.py（事件适配）

- `EngineBridge(QObject, EngineListener)`：在工作线程被引擎回调；
- 把事件转为信号：`runStarted/runFinished/stepStarted/stepFinished/
  logMessage/traceFrame/manualJudgeRequested`；
- **只传快照与基本类型**（ItemResult、RunResult、ManualJudgeRequest、字符串），
  不传 RunContext 等带资源句柄的对象，避免跨线程共享可变状态；
- **人工判定**：`on_manual_judge(request)` 在工作线程被引擎回调，
  发 `manualJudgeRequested` 信号后阻塞在 `threading.Event`；
  主线程弹窗完成时调 `resolve_manual_judge(decision)` 置位事件，
  工作线程随即返回 True/False/None。

### 3.2 worker.py（线程管理）

- `_Runner.run()`（工作线程槽）：用 `ScriptParser` 解析脚本、构造
  `FileResultStore` 与 `Engine`、确定 trace 路径后执行 `engine.run()`；
  启动/解析阶段任何异常经 `failed` 信号回传；
- `RunWorker`：UI 持有的句柄，负责接线（started/finished/failed）、
  线程结束后自动 `quit` 与 `deleteLater`，并暴露 `start()` /
  `request_stop()` / `wait()`。

### 3.3 main_window.py（主窗口）

- 界面状态机：`IDLE → RUNNING → IDLE`，由 `_set_state()` 统一切换控件可用性
  （运行中禁用 Start/SN/脚本选择，Stop 可用）；
- SN：扫码枪回车自启受 `sn.auto_start` 控制；默认仅要求非空，启用
  `sn.validation_enabled` + `pattern` 后做正则校验；
- 结果配色：PASS 绿 / FAIL 红 / ERROR 黄（琥珀）/ ABORT 灰；
- Run 结束后回到 IDLE、刷新“待传 MES”计数、**清空 SN 并聚焦**，支持连续扫码；
- 提示用非阻塞方式（`_notify` 状态栏），避免模态框卡住产线；
- **人工判定弹窗**（`_ask_manual_judge`）：显示测试项/提示/当前值，
  提供 合格(Yes) / 不合格(No) / 中止(Cancel)，选择结果回填给阻塞的工作线程；
  该方法独立成函数以便自动化测试替换；
- 关闭窗口时若仍在运行，先请求停止并等待线程安全退出；
- trace 不显示在主日志区（仅留钩子）。

### 3.4 script_editor.py（脚本编辑器）

- 双页签共享一份脚本：XML 原文（QPlainTextEdit）↔ 图形化（Element 元素树 +
  QTreeWidget + 动态属性表单），切换页签互相同步；
- 图形页支持按容器规则添加/删除/上移/下移节点（connect/step/send/wait/
  field/limit/action/delay），action 额外入参以 `key=value` 逐行编辑；
  属性表单已覆盖串口/Modbus/USB 连接参数、文件传输（`mode=file`）、
  人工判定（`mode=manual`）等新属性；
- **`<limit>` 表单随判定方式联动**：`mode=auto` 只显示 `value/min/max/eq/unit`，
  `mode=manual` 只显示 `value/prompt/unit`；切换时隐藏行并自动从元素中
  清除互斥属性（避免隐藏的 min/max/eq 残留导致 E120）；
- 保存前复用 `ScriptParser` 做全量静态校验，错误码/行列/说明一次列出，
  校验不过不写文件；
- **已知约束**：经图形页往返会丢失 XML 注释（界面中有提示）。

### 3.5 app.py（入口）

加载工位配置、解析日志/结果/脚本目录（脚本目录可用环境变量
`SIMPLE_ATE_SCRIPTS` 覆盖），创建主窗口并进入 Qt 事件循环；
未安装 PySide6 时给出安装提示。

## 4. 设计约束

- 业务逻辑放引擎，UI 只经 `EngineBridge` 信号刷新；
- UI 线程不直接触碰通信层；
- 引擎代码不引入任何 Qt 模块，本包是 Qt 依赖的唯一所在。

## 5. 相关测试

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_ui.py` | 主窗口、桥接、信号刷新、SN 校验、状态切换 |
| `tests/test_script_editor.py` | 双向同步、增删移节点、action 入参、校验拦截、新建与原地保存 |

> 运行 UI 测试需设置 `QT_QPA_PLATFORM=offscreen`；未安装 PySide6 时
> UI 用例自动跳过（结果为 OK/skipped），安装后自动执行。
