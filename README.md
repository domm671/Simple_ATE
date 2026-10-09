# Simple_ATE

一个尽量简洁易用的产线自动测试（ATE / FT）软件。俺希望能让使用的人变为单纯的测试脚本小子。

## 目录

- [项目进度](#项目进度)
- [环境安装](#环境安装)
- [运行](#运行)
- [测试](#测试)
- [目录结构](#目录结构)
- [产物文件](#产物文件)

详见仓库中的 `设计说明.md` 与 `ATE脚本格式规范.md`。

---

## 项目进度

| 里程碑 | 内容 | 状态 | 备注 |
|---|---|---|---|
| **M1** 无头引擎 + Mock | XML 解析与全量静态校验（错误码 E1xx/E2xx）、顺序执行、send/wait/field 原语、自定义 action 白名单加载、limit 判定 / timeout / retry / delay / 停止标志、Mock 通信（JSON 应答脚本）、结果每步即时保存、run log 与逐帧 trace、CLI 入口 | ✅ 已完成 |
| **M2** PySide6 界面 | 五区域主窗口、引擎运行于独立 QThread（Qt 信号回主线程刷新）、扫码自动启动、SN 可选正则校验、实时进度与结论配色、内置脚本编辑器（XML 原文 / 图形化树形双页签同步，保存前全量校验） | ✅ 已完成 ||
| **M3** 真实总线联调 | `python-can`、pyserial 串口/USB CDC、Modbus RTU/ASCII/TCP 适配代码已就位（未装库时有明确报错），真机联调与参数固化待做 | ⬜ 进行中 |
| **M4** MES 扩展点与 Outbox | 已定义 `MesUploader` 协议与 `NullUploader`；上传、退避重试、手动重传、定制类配置化加载待做 | ⬜ 未开始 |

其他说明：

- 累计 154 个单元测试（解析 / 执行 / 帧判定 / 扩展 / 存储 / 文件传输与 Modbus / 串口 Mock 共 129 个，UI 与脚本编辑器 25 个），开发环境全部通过；
- 脚本连接支持 CAN / 串口 / USB CDC / Modbus（脚本 `<connect>` 内联或工位配置）；支持人工判定与文件分块传输；
- 进程退出码约定：`PASS=0`、`FAIL=1`、`ERROR=2`、`ABORT=3`；
- 明确的非目标：不做通用 DSL / 表达式引擎、不内置数据库与 ORM、不内置具体 MES 协议、
  不做多设备并行测试（均通过扩展点交由现场定制）。

---

## 环境安装

### 1. 前置条件

- Python **3.11 或更高版本**（开发与验证环境为 Python 3.13.2）；
- Windows 工位机为主目标环境；命令示例为 `cmd`（`.bat`）写法，Linux/macOS 等价调整即可；
- 仅跑无头引擎 + Mock：**无需任何第三方依赖**；
- 需要界面：安装可选依赖组 `[gui]`（PySide6 ≥ 6.5）；
- 需要真实 CAN：自行安装 `python-can` 及硬件厂商驱动（M3）；
- 需要串口 / USB CDC / 串口 Modbus：安装可选依赖组 `[serial]`（pyserial，M3）。

### 2. 安装方式

开发模式安装（推荐，会注册 `simple-ate` / `simple-ate-gui` 命令）：

```bat
:: 仅无头引擎 / Mock
pip install -e .

:: 含 PySide6 界面
pip install -e .[gui]

:: 含 pyserial（串口 / USB CDC / Modbus RTU/ASCII）
pip install -e .[serial]
```

或者不安装，直接把源码目录加入 `PYTHONPATH`：

```bat
set PYTHONPATH=源码目录
```

### 3. 工位配置

复制并按需修改 `config/station.toml`：工位号、SN 校验规则与扫码自动启动、
结果/日志目录、通信资源（M1 用 `type = "mock"`，M3 真机改为 `type = "can"`
并填写 interface / channel / bitrate）、扩展 action 白名单等均在该文件中配置。

---

## 运行

### 无头模式跑一次测试（M1，无需界面 / 硬件）

```bat
python -m simple_ate run scripts/bms_ft.xml --sn BMS20260909001 --config config/station.toml
```

退出码：`PASS=0`、`FAIL=1`、`ERROR=2`、`ABORT=3`，便于产线脚本与调度系统判定。

串口/Modbus 的无硬件 Mock 演示（无需 pyserial 与串口硬件）：

```bat
python -m simple_ate run scripts/serial_demo.xml --sn SERDEMO001 --config config/station.toml
```

该脚本用 `<connect protocol="serial" mock_script="mock/serial_demo.json">` 模拟设备应答；
真机部署时去掉 `mock_script`、改为 `port="COMx"` 即可。

### 启动图形界面（M2）

```bat
simple-ate-gui --config config/station.toml
:: 或
python -m simple_ate gui --config config/station.toml
```

单窗口五区域：脚本选择、SN 扫码输入（回车启动，受 `sn.auto_start` 控制）、
Start / Stop / Reset、逐项进度表、结果大字（PASS 绿 / FAIL 红 / ERROR 黄 /
ABORT 灰）与日志区。引擎在独立 QThread 运行，事件经 Qt 信号回主线程刷新，
界面不直接接触通信层。

脚本栏除选择脚本外，还提供**脚本编辑器**（“新建脚本…”/“编辑脚本…”）：

- **XML 编辑**：直接编写 Script XML 原文；
- **图形化编辑**：树形结构增删 / 上下移节点
  （connect / step / send / wait / field / limit / action / delay），
  右侧表单按标签类型编辑属性，无需记忆标签名；
- 两个页签互相同步；保存前用正式解析器做完整静态校验（E1xx/E2xx，
  错误码 + 行列 + 说明一次列出），校验不过不保存。
  注意：经图形页签往返会丢弃 XML 注释，需保留注释请在 XML 页签中修改。

---

## 测试

全部测试使用标准库 `unittest`，无需 pytest 等第三方框架。

```bat
:: 引擎 / Mock / 存储等（无 GUI 依赖，任意环境可跑）
python -m unittest tests.test_parser tests.test_executor tests.test_frame_judge ^
                   tests.test_extension tests.test_storage -v

:: 全部（含 UI 测试）：Qt 程序需指定 offscreen 平台插件
set QT_QPA_PLATFORM=offscreen
python -m unittest discover -s tests -v
```

测试文件：

| 文件 | 覆盖范围 |
|---|---|
| `tests/test_parser.py` | XML 解析、全量静态校验与错误码 |
| `tests/test_executor.py` | 引擎执行、retry / on_fail、停止与资源生命周期 |
| `tests/test_frame_judge.py` | 拼帧、ID/mask 匹配、字段提取、limit 判定 |
| `tests/test_extension.py` | `<action handler="module:func">` 白名单加载 |
| `tests/test_storage.py` | JSON 结果保存与 Outbox 目录 |
| `tests/test_ui.py` | 主窗口、引擎桥接、信号刷新 |
| `tests/test_script_editor.py` | 脚本编辑器双向同步 / 增删移节点 / 校验拦截 |
| `tests/test_serial_communication.py` | 串口 Mock、分帧/超时、USB 找口、Modbus RTU/ASCII/TCP 模拟 |

> 若未安装 PySide6，UI 相关 25 个测试会被**自动跳过（skipped）**而非报错，
> 安装 `pip install -e .[gui]` 后即自动执行，不影响引擎部分测试。

也可用 Mock 应答做一次端到端手工验证：先执行上文“无头模式跑一次测试”，
再检查 `data/results/` 与 `data/logs/` 下的产物。

---

## 目录结构

```
Simple_ATE/
├─ src/                         导入名 simple_ate（pyproject package-dir 映射）
│  ├─ __main__.py / cli.py      python -m simple_ate 入口（run / gui 子命令）
│  ├─ config_loader.py          station.toml 加载
│  ├─ errors.py                 统一异常体系与错误码（E1xx/E2xx/E3xx，被所有层引用）
│  ├─ engine/                   脚本引擎包（不依赖 UI）
│  │  ├─ README.md              本模块设计与运行说明
│  │  ├─ parser.py              XML 脚本解析 + 全量静态校验（错误聚合，E1xx/E2xx）
│  │  ├─ executor.py            顺序执行、retry/on_fail、停止标志、资源生命周期
│  │  ├─ frame_io.py            send/wait/field 拼帧、ID/mask 匹配、字段提取
│  │  ├─ judge.py               limit 判定（min/max、eq）
│  │  ├─ extension.py           <action handler="module:func"> 白名单加载
│  │  ├─ context.py             Run 级上下文（SN、变量表、资源句柄）
│  │  └─ model.py               脚本 / 语句 / 结果的 dataclass 模型
│  ├─ logging_conf/             日志配置包（横切模块）
│  │  ├─ README.md              本模块设计与运行说明
│  │  └─ logger.py              run log 与 CAN trace 配置
│  ├─ communication/            通信层
│  │  ├─ README.md              本模块设计与运行说明
│  │  └─ base/mock/can/serial   抽象接口 / mock / can（python-can）/ serial（pyserial）/ modbus / mock_serial（伪串口）
│  ├─ storage/                  结果存储
│  │  ├─ README.md              本模块设计与运行说明
│  │  └─ base/file_store        ResultStore 协议 + FileResultStore（JSON + Outbox 目录）
│  ├─ mes/                      MES 扩展点
│  │  ├─ README.md              本模块设计与运行说明
│  │  └─ base                   MesUploader 协议 + NullUploader（M4 实现上传）
│  └─ ui/                       PySide6 界面（M2）
│     ├─ README.md              本模块设计与运行说明
│     └─ app / main_window / engine_bridge / worker(QThread) / script_editor
├─ scripts/                     测试脚本（XML），如 bms_ft.xml
├─ config/
│  ├─ station.toml              工位配置（SN、存储、通信资源、扩展白名单）
│  └─ mock/                     Mock 应答脚本（JSON）
├─ extensions/                  可选自定义 action 示例（sample_ext.py）
├─ tests/                       单元测试（unittest）
├─ data/                        运行产物（results / logs / trace，运行后生成）
├─ pyproject.toml               包配置（依赖、入口点、package-dir 映射）
├─ ATE脚本格式规范.md             Script XML 标签与属性完整规范
├─ LICENSE
└─ README.md
```

### 数据记录文件

- `data/results/uploaded/*.json`：每步即时保存的完整 Run 结果
  （MES 禁用时直接归档 uploaded；启用时先入 outbox，M4 实现上传）；
- `data/results/{failed,pending}/`：FAIL / 待传 MES 的结果归档；
- `data/logs/run_*.log`：运行日志；
- `data/logs/trace/trace_*.log`：逐帧 CAN trace（TX/RX、ID、ext、data），
  保留天数由 `storage.trace_retain_days` 控制。
