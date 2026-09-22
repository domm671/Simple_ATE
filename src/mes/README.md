# mes 模块说明（MES 接入扩展点）

> 对应《设计说明.md》第 4.6 节 MES 接入、第 12 章 M4 里程碑。
> 导入路径：`simple_ate.mes.*`。

## 1. 模块定位

本模块只提供 **MES 上传扩展点**。**本项目不定义、不内置任何具体 MES 接口
协议**（不规定 HTTP REST、报文格式、鉴权、字段映射），这些一律由使用本项目
的定制方实现。项目负责的是：标准化结果数据（`RunResult`/JSON）、上传时机、
Outbox 离线缓存与退避重试机制。

## 2. 文件组成

| 文件 | 职责 |
|---|---|
| `base.py` | `MesUploader` 上传协议（Protocol）、`NullUploader` 默认空实现 |
| `__init__.py` | 导出 `MesUploader` / `NullUploader` |

## 3. 核心接口（base.py）

```python
class MesUploader(Protocol):
    def upload(self, result: RunResult) -> None:
        """由定制方实现：按其 MES 接口协议上传 result。
        成功返回；失败抛异常，由 Outbox 负责重试。"""

class NullUploader:
    """默认实现：不上传，结果直接归档 uploaded。"""
    def upload(self, result: RunResult) -> None: ...
```

`upload` 的入参 `RunResult`（定义见 `storage/base.py`）即项目可向 MES
提供的全部信息，其 JSON 字段名即 payload 契约。

## 4. Outbox 运行机制（M4 实现，当前机制已由存储层就位）

1. Run 结束 → 若 `mes.enabled=true`，JSON 写入 `results/pending/`；
2. 上传线程扫描 pending，调用配置指定的定制 `MesUploader`
   （`station.toml` 的 `mes.uploader = "module:class"`）；
   成功后移动到 `results/uploaded/`；
3. 失败时文件保留在 pending，按 `retry_intervals`（默认 10s/1min/5min/30min）
   退避重试，超过 `max_retry` 移入 `results/failed/`；
4. `mes.enabled=false`（默认 NullUploader）时结果直接归档 uploaded，不扫描；
5. **MES 故障永远不阻塞测试、不改变测试结论。**

## 5. 当前状态与定制方式

- v0.1（M1/M2）：仅接口 + `NullUploader`，上传线程与退避重试在 **M4** 实现；
  GUI 已可显示“待传 MES：N”计数；
- 定制方：编写实现类（构造可接收工位配置），以 `"module:class"` 配置到
  `mes.uploader`，置 `mes.enabled=true` 即可，无需改动引擎代码。

## 6. 相关文档

- 《设计说明.md》第 4.6、9 章；
- 结果 JSON 格式见 `../storage/README.md` 与《设计说明.md》第 9.2 节。
