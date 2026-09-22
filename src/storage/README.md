# storage 模块说明（结果存储）

> 对应《设计说明.md》第 4.5 节存储设计、第 9 章结果存储设计。
> 导入路径：`simple_ate.storage.*`。

## 1. 模块定位

存储层负责测试结果的落盘与读取。v0.1 **不内置数据库**，以 UTF-8 JSON
文本保存：人可读、可直接作为 MES 上传 payload，日后也可批量导入任意数据库。
核心原则：**结果绝不丢失**——每步完成即整体覆写并 flush，崩溃后可恢复。

## 2. 文件组成

| 文件 | 职责 |
|---|---|
| `base.py` | `RunMeta` / `RunHandle` / `RunResult` 数据结构、`ResultStore` 抽象协议（Protocol） |
| `file_store.py` | `FileResultStore`：唯一内置实现，JSON 原子写 + pending/uploaded/failed 目录 + 崩溃恢复 |
| `__init__.py` | 导出 `ResultStore` / `RunHandle` / `RunMeta` / `RunResult` |

## 3. 核心抽象（base.py）

```python
class ResultStore(Protocol):
    def create_run(self, meta: RunMeta) -> RunHandle: ...
    def append_item(self, handle: RunHandle, item: ItemResult) -> None: ...
    def finish_run(self, handle: RunHandle, result: str, end_time: str) -> RunResult: ...
```

- `RunMeta`：Run 级元信息（SN、脚本名/版本/SHA256、软件版本、工位号、开始时间）；
- `RunHandle`：一次 Run 的存储句柄，文件实现中即文件路径；
- `RunResult`：元信息 + 结束时间 + 最终结论（PASS/FAIL/ERROR/ABORT）+ 明细列表，
  `to_dict()` 输出与结果 JSON 字段一一对应。

引擎与 MES 只依赖 `ResultStore` 抽象，不关心后端是文件还是数据库。

## 4. FileResultStore 运行机制（file_store.py）

### 4.1 Outbox 目录布局

```text
result_dir/
├── pending/      # MES 启用时新 Run 先写于此，等待上传（M4）
├── uploaded/     # MES 禁用时直接归档；或上传成功后移入
└── failed/       # 超过最大重试次数，等待人工处理（M4）
```

MES 是否启用由构造参数 `mes_enabled` 决定写入目标目录（pending 或 uploaded）。

### 4.2 写入时序（与引擎配合）

1. `create_run()`：生成唯一名 `<SN>_<yyyyMMdd_HHmmss>.json`，写入仅含头部字段、
   `result=null`、`items=[]` 的初始文件；
2. `append_item()`：**每完成一个 step** 读取现有 JSON、追加 item 后整体覆写；
3. `finish_run()`：写入 `end_time` 与最终 `result`，返回完整 `RunResult`。

### 4.3 防丢失设计

- **原子写**：临时文件 + `os.replace()` 替换 + `flush` + `fsync`，
  任何时刻掉电都不会出现半写文件；
- **文件名永不覆盖**：同一秒内重复启动时追加 `_1`、`_2` 序号，
  同一 SN 的复测历史完整保留；
- **崩溃恢复**：`recover_aborted()` 在启动时扫描 pending/uploaded，
  把缺少 `result` 字段的残留文件标记为 ABORT 并补结束时间；
- `iter_results()`：读取指定子目录下全部结果（供后续 Outbox/MES 使用）。

## 5. 与其他模块的关系

```text
engine ──create/append/finish──▶ ResultStore
mes（M4） ──读取 pending / 移动文件──▶ 同一结果目录
```

- 本层不依赖引擎实现，只引用 `engine.model.ItemResult` 的数据结构；
- MES 上传线程仅在上传成功后移动文件，与测试线程不会同时写同一文件；
- 运行日志与通信 trace **不进**结果存储。

## 6. 定制新存储后端

实现 `ResultStore`（如 `SqliteResultStore`、`MesDirectStore`），在启动装配处
注册，把 `station.toml` 的 `storage.sink` 改为 `custom`；引擎、UI、MES
均无需改动。v0.1 代码不含任何数据库驱动与 ORM。

## 7. 相关测试

- `tests/test_storage.py`：JSON 写入、唯一名、Outbox 目录、字段兼容。
