# communication 模块说明（通信层）

> 对应《设计说明.md》第 3 章总体架构最底层、第 4.3 节通信层设计。
> 导入路径：`simple_ate.communication.*`。

## 1. 模块定位

通信层只负责**帧（Frame）的收发**，不理解任何产品协议——CAN ID 含义、
字节布局、应答匹配全部由引擎（frame_io）与脚本声明。
本层对上提供统一的同步接口，使真实设备与 Mock 设备可等价替换。

**v0.1 采用同步接口，不使用 asyncio**：底层 python-can/pyserial 均为同步库，
FT 测试是严格顺序流程，引入异步只会增加复杂度。

## 2. 文件组成

| 文件 | 职责 |
|---|---|
| `base.py` | `Frame` 帧对象、`Communication` 同步协议（Protocol）、`ResourceConfig` 资源配置 |
| `mock.py` | `MockCommunication`：按 JSON 应答脚本回复请求，供无硬件开发与单元测试 |
| `can.py` | `CanCommunication`：基于 python-can 的真实 CAN（M3 联调） |
| `serial.py` | `SerialCommunication`：v0.1 占位，内建帧原语不支持串口 |
| `__init__.py` | 导出 `Communication` / `Frame` / `ResourceConfig` |

## 3. 核心接口（base.py）

```python
@dataclass(frozen=True)
class Frame:
    id: int                  # CAN ID / 地址
    data: bytes = b""        # 数据字节
    ext: bool = False        # 扩展帧标志
    timestamp: float = 0.0

class Communication(Protocol):
    name: str
    def open(self) -> None: ...
    def close(self) -> None: ...
    def send(self, frame: Frame) -> None: ...
    def recv(self, timeout: float) -> Frame: ...   # 超时必须抛 TimeoutAteError(E301)
```

`ResourceConfig` 是 `station.toml` 中单个 `[resources.xxx]` 的解析结果
（`name` / `type` / 全部原始选项 `options`），由 `Engine._build_resource`
据此创建具体实现。

## 4. 各实现的运行方式

### 4.1 MockCommunication（mock.py）

- `open()`：读取 `type="mock"` 资源配置的 `mock_script`（JSON），加载规则列表；
- `send(frame)`：按 `request_id`（可选 `request_data`、`ext`）匹配规则；
  命中则按 `delay` 延时后把应答帧放入内部队列；**无匹配规则即不产生应答**，
  上层 wait 最终超时（E301）；
- `recv(timeout)`：从队列取帧，队列为空抛 `TimeoutAteError`；
- 应答支持两种写法：`response_id/response_data`，或
  `response_computed: {id, bytes, ext}`；
- 另提供 `inject(frame)`，用于模拟设备主动上报。

### 4.2 CanCommunication（can.py）

- `open()`：延迟导入 python-can，未安装时给出明确中文错误；
  按工位配置的 `interface/channel/bitrate` 创建总线；
- `send()`：把 `Frame` 转为 `can.Message`（含扩展帧标志）发出；
- `recv()`：调用总线阻塞接收，返回 `None`（超时）时抛 `TimeoutAteError`，
  否则转换回统一 `Frame`；
- `close()`：`bus.shutdown()`。

### 4.3 SerialCommunication（serial.py）

v0.1 占位：`open/send/recv` 均抛 `CommunicationError`，提示需要在
扩展 action 中自行使用 pyserial。

## 5. 与其他模块的关系

```text
engine.frame_io  ──send/recv──▶  communication 实现  ──▶ 被测装备
```

- 引擎通过 `Communication` Protocol 编程，不依赖具体后端；
- 本层异常统一走 `errors.py`：超时 E301、链路异常 E302，
  均属可重试（`retryable=True`）的运行期错误；
- 资源由 `Engine._build_resource` 创建、在 `Engine.run` 的 `finally`
  中无条件 `close()`。

## 6. 新增通信后端

实现 `Communication` Protocol（`open/close/send/recv`），并在
`Engine._build_resource` 注册新的 `type`；脚本与引擎其他部分无需改动。

## 7. 相关测试

- `tests/test_executor.py`：Mock 资源下的端到端收发；
- `tests/test_frame_judge.py`：基于 `Frame` 的帧构造与解析。
