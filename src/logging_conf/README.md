# logging_conf 模块说明（日志配置）

> 对应《设计说明.md》第 4.4 节日志设计。Logger 是被所有层使用的**横切模块**。
> 导入路径：`simple_ate.logging_conf.*`。

## 1. 模块定位

本模块负责两类严格分开的日志：

1. **运行日志 run log**：记录 Run 生命周期、资源开关、每个测试项的结果与错误，
   文本文件 + UI 日志区；
2. **原始通信 trace**：逐帧记录 TX/RX（时间、方向、资源、ID、扩展帧、数据字节），
   每次 Run 单独一个文件，用于协议排查与质量追溯，不进入 UI。

## 2. 文件组成

| 文件 | 职责 |
|---|---|
| `logger.py` | `setup_run_logger()` 运行日志配置；`TraceLogger` 逐帧 trace |
| `__init__.py` | 导出 `setup_run_logger` / `TraceLogger` |

## 3. 运行方式（logger.py）

### 3.1 setup_run_logger(log_dir)

- 创建日志目录，生成文件 `run_<yyyyMMdd_HHmmss>_<唯一后缀>.log`；
- 每次调用创建独立的 logger。CLI 写入文件（UTF-8）与控制台，GUI 写入文件并经信号更新运行日志区；
- 返回 `(logger, log_path)`。

典型内容：

```text
2026-09-09 12:00:03 INFO  [STEP] Voltage = 12.03 V  -> PASS
2026-09-09 12:00:04 ERROR [STEP] CAN_COMM timeout -> ERROR
```

### 3.2 TraceLogger(trace_path)

- 独占创建 `trace_<SN>_<时间戳>_<唯一后缀>.log`（路径由 CLI/GUI 在 Run 开始前确定，
  位于 `<log_dir>/trace/`）；
- `tx(resource, frame)` / `rx(resource, frame)`：逐帧写入，每行立即 flush；
- `note(text)`：写入备注行；
- `close()`：关闭文件（由 `Engine.run` 的 finally 统一调用）。

典型内容：

```text
[12:00:03] TX can_main id=0x18FF50E5 ext=1 data=02 01 00 00 00 00 00 00
[12:00:03] RX can_main id=0x18FF50E6 ext=1 data=B3 04 00 00 00 00 00 00
```

## 4. 与其他模块的关系

- 引擎内部日志走 `ctx.logger`，Run 结束后 CLI 和 GUI 释放日志文件句柄；
- 引擎在每次 send/recv 后调用 `TraceLogger.tx/rx`，并同时经
  `on_trace` 事件通知监听方（GUI 默认不显示 trace，只写文件）；
- trace 文件与 run log 均**不进入**结果存储。

## 5. 运行产物位置

- 运行日志：`data/logs/run_*.log`；
- 通信 trace：`data/logs/trace/trace_*.log`；
- `storage.trace_retain_days` 为预留配置，当前版本保留生成的 trace。
