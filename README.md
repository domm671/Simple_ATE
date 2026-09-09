# simple_ATE

刻意保持小的产线自动测试（ATE）框架。v0.1：XML 测试脚本 + 内建 CAN 帧收发原语 +
同步执行引擎 + JSON 结果存储。引擎不依赖 Qt，UI 在后续里程碑接入。

- Python ≥ 3.11（开发环境 3.13.2）
- 运行期零第三方依赖（标准库）；测试使用内置 `unittest`
- 真实 CAN（M3）通过可选依赖 `python-can` 接入

## 安装（开发模式）

```bat
pip install -e .
```

或不安装，直接设置源码路径：

```bat
set PYTHONPATH=F:\my_project\Simple_ATE\src
```

## 运行一次测试（M1 无头 + Mock）

```bat
python -m simple_ate run scripts/bms_ft.xml --sn BMS20260909001 --config config/station.toml
```

进程退出码：`PASS=0`、`FAIL=1`、`ERROR=2`、`ABORT=3`。

产物：

- `data/results/uploaded/*.json`：每步即时落盘的完整 Run 结果（MES 禁用时直接归档 uploaded）
- `data/logs/run_*.log`：运行日志
- `data/logs/trace/trace_*.log`：逐帧 CAN trace（TX/RX、ID、ext、data）

## 运行测试

```bat
python -m unittest discover -s tests -v
```

## 目录

```
src/simple_ate/
  parser.py          XML 脚本解析 + 全量静态校验（错误聚合，错误码 E1xx/E2xx）
  model.py           脚本/语句/结果的 dataclass 模型
  engine.py          顺序执行、retry/on_fail、停止标志、资源生命周期
  frame_io.py        send/wait/field 拼帧、ID/mask 匹配、字段提取
  judge.py           limit 判定（min/max、eq）
  extension.py       <action handler="module:func"> 白名单加载
  config_loader.py   station.toml
  communication/     base 抽象、mock（JSON 应答）、can（python-can，M3）、serial（占位）
  storage/           ResultStore 协议 + FileResultStore（JSON + Outbox 目录）
  mes/               MesUploader 协议 + NullUploader（M4 实现上传）
scripts/             测试脚本示例（XML）
config/              station.toml 与 mock 应答脚本
extensions/          可选自定义 action（示例 sample_ext.py）
tests/               单元测试
```

详见 `设计说明.md` 与 `ATE脚本格式规范.md`。
