# simple_ATE 测试脚本格式规范

- 文档版本：v2.0（声明式帧模型，替代 v1.0 的驱动方法模型）
- 适用软件：simple_ATE v0.1
- 脚本语言版本：**Script XML v1**（对应根节点 `<test version="1.x">`）
- 状态：评审通过

---

## 1. 概述

### 1.1 目的

本规范定义 simple_ATE 测试脚本（Script XML）的完整语法与执行语义，是以下工作的唯一依据：

- 测试工程师编写装备测试脚本（**只写 XML，不需要修改项目代码**）；
- 引擎 `parser.py` / `executor.py` / `frame_io.py` 的实现；
- 脚本静态校验与错误提示；
- 可选扩展 action 的编写。

### 1.2 核心理念：脚本即协议

CAN 帧的 ID、数据、应答匹配方式、字段解析与换算全部以标签**声明在脚本中**。引擎负责：

```text
按 <send> 拼帧发出 → 在 <wait> 时限内匹配应答 → 按 <field> 解析出变量 → <limit> 判定
```

脚本作者同时掌握被测装备协议，协议细节写在脚本里即改即测。仅当协议涉及多帧握手、校验和、UDS、特殊串口报文等内建原语无法描述的场景时，才需要写 Python 扩展（`<action>`，见第 9 章）。

设计约束：

1. 节点即语句类型，属性即参数；
2. 只允许本规范定义的标签与属性；
3. v0.1 内建帧原语面向**经典 CAN**（11/29 位 ID，数据 0–8 字节）的顺序"发—等—判"流程；串口/TCP 报文原语在后续版本提供；
4. 工位硬件参数（接口型号、通道、波特率）不属于脚本，放工位配置；
5. v1 仅顺序执行，无条件分支、循环、子脚本；
6. 变量仅支持 `${name}` 简单取值，**不支持表达式运算**；脚本中不允许出现任意 Python 代码（扩展白名单内的命名函数除外）。

### 1.3 术语

| 术语 | 含义 |
|---|---|
| Script | 一个 XML 测试脚本文件 |
| Statement | 顶层语句：`connect` / `disconnect` / `step` / `delay` |
| Step | 一个测试步，包含若干 send/wait/delay/action 与至多一个 limit |
| Frame | 一条 CAN 帧：ID、扩展帧标志、数据字节 |
| Field | 从应答帧中按字节偏移提取、换算得到的测量值 |
| Handler | `extensions/` 目录中经配置放行的 Python 函数，供 `<action>` 调用 |
| Run | 扫描一个 SN 后对脚本的一次完整执行 |
| Attempt | Step 的一次执行尝试；重试时产生新的 Attempt |

---

## 2. 文件约定

| 项 | 规定 |
|---|---|
| 编码 | UTF-8（必须含 XML 声明） |
| 扩展名 | `.xml` |
| 存放目录 | `scripts/`（UI 从该目录列出可选脚本） |
| 文件名 | 小写产品/工序名，如 `bms_ft.xml`；不同产品规格分别建文件，如 `bms_ft_100ah.xml` |
| 注释 | 允许标准 XML 注释 `<!-- ... -->`，不影响解析 |
| 元素文本 | 所有元素均为空元素或只含规定子元素，不允许自由文本（纯空白除外） |

---

## 3. 文档结构

```text
<?xml ...?>
<test name version>                    根节点，恰好一个
    <connect .../>                     打开逻辑通信资源（可多个）
    <step ...>                         测试步（可多个，顺序执行）
        <send .../>                    发送帧
        <wait ...>                     等待应答帧
            <field .../>               从应答中提取字段（可多个）
        </wait>
        <delay .../>
        <action .../>                  可选：扩展函数
        <limit .../>                   至多一个，且必须在最后
    </step>
    <delay .../>                       顶层延时
    <disconnect .../>                  关闭逻辑资源
</test>
```

- 顶层允许的子元素**仅**：`connect`、`disconnect`、`step`、`delay`；
- `step` 内允许的子元素**仅**：`send`、`wait`、`delay`、`action`、`limit`；
- `wait` 内允许的子元素**仅**：`field`。

---

## 4. 形式化语法（EBNF）

```ebnf
script      = "<test" test-attr ">" statement* "</test>" ;
statement   = connect | disconnect | step | delay-stmt ;

connect     = "<connect" resource-attr timeout-attr? "/>" ;
disconnect  = "<disconnect" resource-attr "/>" ;

step        = "<step" step-attr ">"
              (send | wait | action | delay-stmt)* limit?
              "</step>" ;
(* 语义约束：step 至少含一条通信指令 send/wait/action；limit 必须在最后 *)

send        = "<send" id-attr ext-attr? data-attr? "/>" ;
wait        = "<wait" wait-attr ">" field* "</wait>" ;
field       = "<field" field-attr "/>" ;
action      = "<action" handler-attr var-attr? param-attr* "/>" ;
limit       = "<limit" value-attr criterion-attr* unit-attr? "/>" ;
delay-stmt  = "<delay ms=\"<non-negative-int>\"/>" ;

step-attr   = "name=\"" step-name "\"" resource-attr?
                timeout-attr? retry-attr? retry-interval-attr? onfail-attr? ;
id-attr     = "id=\"" can-id "\"" "id_mask=\"" can-id "\""? ;
data-attr   = "data=\"" {byte-token}+"\"" ;
ext-attr    = "ext=\"" bool "\"" ;
wait-attr   = id-attr ext-attr? timeout-attr? drain-attr? minlen-attr? ;
field-attr  = "var=\"" variable-name "\""
              offset-attr length-attr? endian-attr?
              gain-attr? bias-attr? raw-attr? field-unit-attr? ;
byte-token  = hex-byte | variable-ref ;       # data 中每个 token 占一个字节
```

---

## 5. 元素与属性规范

### 5.1 `<test>`（根节点）

| 属性 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `name` | 是 | 标识符 | 正则 `^[A-Za-z0-9_-]+$` |
| `version` | 是 | 版本串 | 正则 `^\d+\.\d+(\.\d+)?$`，如 `1.0`；脚本改动应升版本 |

引擎运行时另记脚本文件 SHA-256 校验和，与版本一起写入结果文件。

### 5.2 `<connect>` / `<disconnect>`

| 元素 | 属性 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `connect` | `resource` | 是 | — | 工位配置中定义的逻辑资源名，如 `can_main` |
| `connect` | `timeout` | 否 | `3.0` | 打开资源超时（秒，float>0） |
| `disconnect` | `resource` | 是 | — | 逻辑资源名 |

- 同一资源重复 `connect`：运行期错误 E304；
- 资源在工位配置中不存在：加载期 E204；
- `connect` 失败属于设备异常，Run 直接以 **ERROR** 结束；
- `disconnect` 幂等；**即使脚本不写，引擎也在 Run 结束的 `finally` 中关闭所有已打开资源**。

### 5.3 `<step>`（测试步）

| 属性 | 必填 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `name` | 是 | 字符串 | — | 测试项名称，同一脚本内唯一；显示在 UI、日志、结果中 |
| `resource` | 否 | 标识符 | 见下 | 本 step 收发使用的逻辑资源；**仅打开了一个资源时可省略**，同时打开多个资源又省略 → E210 |
| `timeout` | 否 | 秒(float>0) | `5.0` | **单次 Attempt** 内每条 `<wait>` 的默认等待时限 |
| `retry` | 否 | int(≥0) | `0` | 通信类异常时的额外重试次数，总尝试 = retry+1 |
| `retry_interval` | 否 | 秒(≥0) | `0.0` | 两次 Attempt 间等待，可被停止中断 |
| `on_fail` | 否 | 枚举 | `abort` | `abort` / `continue` |

结构约束（加载期校验）：

- 至少含一条 `send`/`wait`/`action`，否则 E108；
- 至多一个 `limit`，且必须是最后一个子元素，否则 E109/E110；
- `resource` 必须已在本 step 之前 `connect`，否则 E204/E210；一个 step 内只允许使用这一个资源。

### 5.4 `<send>`（发送帧）

| 属性 | 必填 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `id` | 是 | CAN ID | — | `0x` 前缀十六进制，如 `0x18FF50E5`；或十进制 |
| `ext` | 否 | bool | `false` | `true`=29 位扩展帧，`false`=11 位标准帧 |
| `data` | 否 | 字节串 | 空（0 字节帧） | 空格分隔的字节 token，见下 |
| `id_mask` | 否 | CAN ID | — | 发送侧一般不用，保留与 wait 一致的解析 |

`data` 规则：

- 每个 token 占**恰好一个字节**：十六进制字面量（`00`–`FF`，大小写不敏感）或单字节变量引用 `${name}`；
- token 总数 0–8，超过 8 报 E115；
- 字面量超出 `0xFF` 报 E105；
- 变量必须在**本 step 之前**已由 `field` 或 `action var` 定义，且运行期值为 0–255 的 int；否则分别报 E208 / E307。

```xml
<send id="0x18FF50E5" ext="true" data="02 01 00"/>
<send id="0x123" data="${ch}"/>          <!-- 单字节变量拼入 -->
```

> v0.1 不支持一个变量占多字节（如 16 位拼包），多字节发送请用扩展 action；后续版本增加 `<arg>` 编码声明。

### 5.5 `<wait>`（等待并匹配应答帧）

| 属性 | 必填 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `id` | 是 | CAN ID | — | 期望的应答帧 ID |
| `ext` | 否 | bool | `false` | 期望帧的 ID 类型，须与总线上的帧一致 |
| `id_mask` | 否 | CAN ID | 精确匹配 | 匹配掩码：`(recv_id & id_mask) == (id & id_mask)` 即命中 |
| `timeout` | 否 | 秒(float>0) | 取 step 的 `timeout` | 本次等待的时限 |
| `drain` | 否 | 枚举 | `before` | `before`：等待前清空接收缓冲；`off`：不清空（可等到更早到达的帧） |
| `min_len` | 否 | int(0–8) | 0 | 应答帧最小数据长度；收到的帧不足视为无效帧继续等 |

语义：

1. `drain="before"`（默认）先排空接收队列，避免收到上一交互的残留帧；
2. 在剩余 timeout 内循环接收，按 ID 类型 + ID（+mask）匹配，**无关帧丢弃但全部写 trace**；
3. 命中但长度不足 `min_len`：丢弃并继续等；
4. 超时未收到有效帧 → 通信异常 E301，触发 retry；
5. 空体 `<wait/>` 只做"应答到达"检查，step 无 limit 时即 PASS；
6. 含 `field` 时，对命中的那一帧依次提取。

### 5.6 `<field>`（字段提取与换算）

| 属性 | 必填 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `var` | 是 | 变量名 | — | 提取结果写入的变量，正则 `^[a-zA-Z_][a-zA-Z0-9_]*$` |
| `offset` | 是 | int(≥0) | — | 起始字节偏移（从 0 开始） |
| `length` | 否 | int(1–8) | `1` | 占用字节数 |
| `endian` | 否 | 枚举 | `little` | `little` / `big`：多字节原始整数的字节序 |
| `gain` | 否 | float | `1.0` | 缩放系数 |
| `bias` | 否 | float | `0.0` | 偏移量 |
| `raw` | 否 | bool | `false` | `true` 时忽略 gain/bias，直接取原始整数 |
| `unit` | 否 | 字符串 | — | 工程单位记录（如 `V`、`uA`），供结果展示 |

换算：

```text
raw_int = 按 endian 组合 data[offset .. offset+length-1] 的无符号整数
value   = raw_int                     (raw="true")
value   = raw_int * gain + bias       (默认)
```

规则：

- 提取范围超出应答帧实际长度 → 运行期 E305（通信类异常，可重试）；
- 同一 wait 可提取多个 field；同一 step 内变量可被后续 send/limit 引用；
- v0.1 只支持**无符号整数字节段**；有符号、BCD、位域、ASCII 在后续版本或用扩展 action；
- `unit` 仅记录用，不参与判定。

### 5.7 `<limit>`（判定）

| 属性 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `value` | 是 | 数值字面量或 `${var}` | 被判定值 |
| `min` | 条件必填 | float | 下限（闭区间） |
| `max` | 条件必填 | float | 上限（闭区间） |
| `eq` | 条件必填 | 字面量 | 等值判据，面向整数状态码 |
| `unit` | 否 | 字符串 | 单位，仅记录 |

- `min`/`max`/`eq` 至少一个（E111）；`eq` 与 `min`/`max` 互斥（E112）；
- 同时给 min、max 时须 `min ≤ max`（E113）；
- 数值位属性必须整体是数值字面量或单个 `${var}`，不允许混合文本（E114）。

### 5.8 `<delay>`（延时）

| 属性 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `ms` | 是 | int(≥0) | 延时毫秒，以 50ms 粒度响应停止标志 |

可出现在顶层或 step 内；step 内的 delay 计入该 Attempt。

---

## 6. 变量系统

1. 变量由 `field` 提取或 `<action var>` 产生，属于 **Run 级全局上下文**，同名后赋值覆盖；
2. 不跨 Run 保留；
3. 引用语法 `${name}`：
   - `send data` 中：单字节 token（要求 int 0–255）；
   - `limit value` 中：整体引用一个数值变量；
   - `action` 入参中：见第 9 章；
4. v0.1 顺序执行，加载期做"定义先于引用"检查，引用点之前没有任何 `field`/`action var` 定义该变量 → E208；
5. 失败 Attempt 中已提取的变量会留在上下文，重试时重新提取——脚本不应依赖失败 Attempt 的中间值。

---

## 7. 判定规则

1. step 内全部通信指令成功且无 limit → **PASS**；
2. 有 limit：所有 send/wait/action 成功后判定一次。

| 判据 | PASS 条件 |
|---|---|
| 仅 min | `value ≥ min` |
| 仅 max | `value ≤ max` |
| min + max | `min ≤ value ≤ max` |
| eq | `value == eq`（int/float 相容；状态码场景使用，浮点测量请用 min/max） |

- 超限 → **FAIL，不重试**；
- 变量运行期类型与判据不相容（如字符串参与数值比较）→ E209；
- 明细记录 `value`、`low_limit`、`high_limit`、`unit`。

---

## 8. 执行语义

### 8.1 顺序

`connect` 资源 → 严格顺序执行顶层语句 → 每步产生一条 ItemResult 并即时写结果文件 → 汇总 → 关闭资源 → 归档/MES Outbox。

### 8.2 Step 状态机

```text
PENDING → RUNNING ─ 全部通信成功且判定通过 ───────────────► PASS
             │
             ├────────── 判定不合格 ──────────────────────► FAIL（不重试）
             │
             ├─ 通信类异常 & 剩余重试>0 ─ 等 retry_interval ─► RUNNING（新 Attempt）
             │
             └─ 通信类异常且重试耗尽 ─────────────────────► ERROR
                脚本/扩展错误（E209、E303 等）──────────────► 终止 Run
```

### 8.3 timeout

- step 的 `timeout` 是其内部每个 `<wait>` 的**默认**等待时限；`<wait timeout>` 可覆盖；
- 每次重试获得满额新预算；
- 等待在通信层 `recv(timeout)` 中阻塞返回，引擎不 kill 线程；
- wait 超时属于通信类异常（E301），触发 retry。

### 8.4 retry 的异常分类

| 异常 | 错误码 | 是否重试 |
|---|---|---|
| 应答超时、ID 不匹配超时、帧长不足超时 | E301 | 是 |
| 链路异常（掉线、发送失败） | E302 | 是 |
| field 提取越界（本帧数据不完整） | E305 | 是 |
| send data 变量运行期越界/类型错 | E307 | 否，终止 Run（脚本缺陷） |
| 判定 FAIL | — | **否** |
| 判定值类型错误 | E209 | 否，终止 Run |
| 扩展 action 抛出异常 | E306 | 是（按通信类对待，函数应自行区分业务失败） |
| 引擎/扩展编程错误 | E303 | 否，Run=ERROR，堆栈入日志 |

- 重试粒度为**整个 step**：已发的帧、已等的应答全部重来；
- `retry_interval` 期间同样响应停止。

### 8.5 on_fail 与 Run 结论

| step 结果 | `on_fail="abort"`（默认） | `on_fail="continue"` |
|---|---|---|
| PASS | 继续 | 继续 |
| FAIL | 终止，Run=FAIL | 记录并继续 |
| ERROR | 终止，Run=ERROR | 记录并继续 |

走完所有语句时：① 任一步 ERROR → Run=ERROR；② 否则任一 FAIL → FAIL；③ 全 PASS → PASS。
**ERROR 优先于 FAIL：工装/通信异常绝不计为产品不良。**

### 8.6 人工停止（ABORT）

停止标志在顶层语句之间、send/wait/action 之间、Attempt 之间、delay 每 50ms 被检查；正在进行的 recv 等其自身超时返回。停止后不再重试、不再执行后续语句，Run 固定为 **ABORT**，已有明细保留，随后关闭资源。

---

## 9. 扩展 action（可选，复杂协议专用）

当测试步无法用 send/wait/field 表达（多帧分包、校验和、UDS 握手、轮询状态位等），可调用扩展函数：

```xml
<action handler="my_uds:enter_extended_session" var="session_st" timeout="2"/>
```

| 属性 | 必填 | 说明 |
|---|---|---|
| `handler` | 是 | `模块名:函数名`，模块位于 `extensions/` 目录 |
| `var` | 否 | 接收函数返回值 |
| 其它属性 | 否 | 字符串关键字入参（支持 `${var}` 替换） |

函数约定：

```python
# extensions/my_uds.py
def enter_extended_session(ctx, resource, **params):
    """
    ctx:      运行上下文（SN、变量表、日志）
    resource: 该 step 的 resource 指向的通信资源句柄（单资源时 step.resource 可省略）
    return:   存入 var；无 var 可返回 None
    超时/通信问题抛 TimeoutError / CommunicationError（可重试）；
    其它异常视为编程错误，Run=ERROR。
    """
```

规则：

1. 扩展模块必须在 `station.toml` 的 `[extensions] allowed` 白名单中，否则加载期 E201；
2. `handler` 模块不存在、函数不存在 → 加载期 E202；
3. 入参全部以字符串传入（经过 `${}` 替换），函数自行做类型转换；
4. 扩展 action 与内建原语混用在同一 step，顺序执行，适用同一套 step timeout/retry/on_fail；
5. **普通产品测试不应需要扩展**；扩展是给现场集成复杂工装用的例外通道。

---

## 10. 校验规则与错误码

### 10.1 加载期（脚本不开始执行，应一次报出全部错误，带文件名/行/列）

| 码 | 含义 |
|---|---|
| E101 | XML 非良构 |
| E102 | 未知标签 |
| E103 | 未知属性 |
| E104 | 缺少必填属性 |
| E105 | 属性值类型/取值非法（含 data 字节越界） |
| E106 | step `name` 重复 |
| E107 | 使用了 v2 预留标签 `if`/`loop`/`call` |
| E108 | step 内无 send/wait/action |
| E109 | step 内多个 limit |
| E110 | limit 不是 step 最后一个子元素，或 limit 之后还有指令 |
| E111 | limit 未给出任何判据 |
| E112 | eq 与 min/max 混用 |
| E113 | min > max，或限值非数值 |
| E114 | 数值位属性出现混合文本 |
| E115 | send data 超过 8 字节 |
| E116 | CAN ID 超出对应帧类型范围（标准帧 ≤0x7FF） |
| E117 | field 的 offset/length 静态越界（length>8 等） |
| E201 | 扩展模块未在白名单中 |
| E202 | 扩展模块或 handler 函数不存在 |
| E204 | 资源未在工位配置中定义 |
| E208 | 变量在定义之前被引用 / 未定义 |
| E210 | 同时打开多个资源但 step 未指定 `resource`（或指定了未连接的资源） |

### 10.2 运行期

| 码 | 含义 |
|---|---|
| E301 | 应答超时（含 ID/掩码未命中、min_len 不足） |
| E302 | 通信链路异常（掉线、发送失败） |
| E303 | 引擎/扩展未预期异常（堆栈写运行日志） |
| E304 | 同一资源重复 connect |
| E305 | field 提取超出实际帧长度 |
| E306 | 扩展 action 抛出异常 |
| E307 | send data 变量运行期值不是 0–255 的整数 |
| E209 | 判定值与判据类型不相容 |

---

## 11. v2 预留标签

v1 解析到以下标签显式报 E107：

```xml
<if var="x" gt="10"> … </if>
<loop count="3"> … </loop>
<call script="COMMON"/>
```

v1 同样禁止：CAN ID 用变量、一个变量占多字节发送、位域/有符号/BCD 提取、一个 step 跨多个资源。

---

## 12. 脚本编写风格建议

1. 第一步做通信自检（发已知请求帧、等应答、不判定字段），`on_fail="abort"`；
2. 测量类步用 `on_fail="continue"` 一次收集全部不良项；上下电、继电器等安全相关步用 `abort`；
3. 限值、timeout、retry 写在脚本中；规格差异用不同脚本文件承载；
4. 变量名带工程含义（`vbat`、`i_sleep`、`relay_st`）；
5. 所有 wait 默认 `drain="before"`，仅在有意监听异步推送帧时用 `off`；
6. 修改脚本同步提升根节点 `version`；文件头用注释记录协议假设与变更历史。

---

## 13. 完整示例

### 13.1 最小冒烟脚本

```xml
<?xml version="1.0" encoding="UTF-8"?>
<test name="SMOKE" version="1.0">
    <connect resource="can_main"/>
    <step name="Ping" timeout="2" retry="1" on_fail="abort">
        <send id="0x100" data="01 00"/>
        <wait id="0x101"/>
    </step>
    <disconnect resource="can_main"/>
</test>
```

### 13.2 BMS FT（扩展帧、请求/应答 ID 不同、缩放换算、状态码）

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!-- 产品：BMS 主控板终检 | 工装：FT_LINE1 CAN | 1.0 初版 -->
<test name="BMS_FT" version="1.0">

    <connect resource="can_main" timeout="3"/>

    <step name="CAN_COMM" timeout="2" retry="2" retry_interval="0.5" on_fail="abort">
        <send id="0x18FF50E5" ext="true" data="01 00"/>
        <wait id="0x18FF50E6" ext="true"/>
    </step>

    <step name="Voltage" timeout="5" on_fail="continue">
        <send id="0x18FF50E5" ext="true" data="02 01 00"/>
        <wait id="0x18FF50E6" ext="true">
            <field var="vbat" offset="0" length="2" endian="little" gain="0.01" unit="V"/>
        </wait>
        <limit value="${vbat}" min="11.5" max="12.5" unit="V"/>
    </step>

    <step name="SleepCurrent" timeout="5" on_fail="continue">
        <send id="0x18FF50E5" ext="true" data="03 02"/>
        <wait id="0x18FF50E6" ext="true">
            <field var="i_sleep" offset="0" length="4" endian="little" gain="0.1" unit="uA"/>
        </wait>
        <limit value="${i_sleep}" max="1000" unit="uA"/>
    </step>

    <step name="RelayCheck" timeout="3" retry="1" on_fail="abort">
        <send id="0x18FF50E5" ext="true" data="10 01"/>
        <delay ms="200"/>
        <send id="0x18FF50E5" ext="true" data="11 01"/>
        <wait id="0x18FF50E6" ext="true">
            <field var="relay_st" offset="0" length="1"/>
        </wait>
        <limit value="${relay_st}" eq="1"/>
    </step>

    <disconnect resource="can_main"/>
</test>
```

### 13.3 典型结果对照

| 场景 | 现象 | Step | Run |
|---|---|---|---|
| 正常 | 应答及时、字段在限值内 | PASS | PASS |
| 电压超限 | 13.0V，FAIL 不重试，继续后续步 | FAIL | FAIL |
| CAN 无应答 | 3 次尝试全超时，abort 终止 | ERROR | ERROR |
| 人工 Stop | 已完成明细保留 | — | ABORT |
| SleepCurrent 超时(continue) + Voltage 超限 | ERROR 与 FAIL 并存 | ERROR+FAIL | ERROR（ERROR 优先） |

---

## 附录 A：XSD 1.0（静态结构校验）

> 属性间条件约束（E108–E117、变量数据流）由解析器语义校验负责。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           elementFormDefault="qualified">

  <xs:element name="test">
    <xs:complexType>
      <xs:sequence>
        <xs:choice minOccurs="0" maxOccurs="unbounded">
          <xs:element name="connect"    type="connectType"/>
          <xs:element name="disconnect" type="disconnectType"/>
          <xs:element name="delay"      type="delayType"/>
          <xs:element name="step"       type="stepType"/>
        </xs:choice>
      </xs:sequence>
      <xs:attribute name="name"    type="xs:string" use="required"/>
      <xs:attribute name="version" type="xs:string" use="required"/>
    </xs:complexType>
  </xs:element>

  <xs:complexType name="connectType">
    <xs:attribute name="resource" type="xs:string" use="required"/>
    <xs:attribute name="timeout"  type="xs:float"/>
  </xs:complexType>

  <xs:complexType name="disconnectType">
    <xs:attribute name="resource" type="xs:string" use="required"/>
  </xs:complexType>

  <xs:complexType name="delayType">
    <xs:attribute name="ms" type="xs:nonNegativeInteger" use="required"/>
  </xs:complexType>

  <xs:complexType name="stepType">
    <xs:sequence>
      <xs:choice minOccurs="0" maxOccurs="unbounded">
        <xs:element name="send"   type="sendType"/>
        <xs:element name="wait"   type="waitType"/>
        <xs:element name="action" type="actionType"/>
        <xs:element name="delay"  type="delayType"/>
        <xs:element name="limit"  type="limitType"/>
      </xs:choice>
    </xs:sequence>
    <xs:attribute name="name"           type="xs:string" use="required"/>
    <xs:attribute name="resource"       type="xs:string"/>
    <xs:attribute name="timeout"        type="xs:float"/>
    <xs:attribute name="retry"          type="xs:nonNegativeInteger"/>
    <xs:attribute name="retry_interval" type="xs:float"/>
    <xs:attribute name="on_fail"        type="onFailEnum"/>
  </xs:complexType>

  <xs:simpleType name="onFailEnum">
    <xs:restriction base="xs:string">
      <xs:enumeration value="abort"/>
      <xs:enumeration value="continue"/>
    </xs:restriction>
  </xs:simpleType>

  <xs:complexType name="sendType">
    <xs:attribute name="id"      type="xs:string" use="required"/>
    <xs:attribute name="id_mask" type="xs:string"/>
    <xs:attribute name="ext"     type="xs:boolean"/>
    <xs:attribute name="data"    type="xs:string"/>
  </xs:complexType>

  <xs:complexType name="waitType">
    <xs:sequence>
      <xs:element name="field" type="fieldType" minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="id"       type="xs:string" use="required"/>
    <xs:attribute name="id_mask"   type="xs:string"/>
    <xs:attribute name="ext"       type="xs:boolean"/>
    <xs:attribute name="timeout"   type="xs:float"/>
    <xs:attribute name="drain"     type="drainEnum"/>
    <xs:attribute name="min_len"   type="xs:nonNegativeInteger"/>
  </xs:complexType>

  <xs:simpleType name="drainEnum">
    <xs:restriction base="xs:string">
      <xs:enumeration value="before"/>
      <xs:enumeration value="off"/>
    </xs:restriction>
  </xs:simpleType>

  <xs:complexType name="fieldType">
    <xs:attribute name="var"    type="xs:string" use="required"/>
    <xs:attribute name="offset" type="xs:nonNegativeInteger" use="required"/>
    <xs:attribute name="length" type="xs:nonNegativeInteger"/>
    <xs:attribute name="endian" type="endianEnum"/>
    <xs:attribute name="gain"   type="xs:float"/>
    <xs:attribute name="bias"   type="xs:float"/>
    <xs:attribute name="raw"    type="xs:boolean"/>
    <xs:attribute name="unit"   type="xs:string"/>
  </xs:complexType>

  <xs:simpleType name="endianEnum">
    <xs:restriction base="xs:string">
      <xs:enumeration value="little"/>
      <xs:enumeration name="big"/>
    </xs:restriction>
  </xs:simpleType>

  <xs:complexType name="actionType">
    <xs:anyAttribute processContents="skip"/>
    <xs:attribute name="handler" type="xs:string" use="required"/>
    <xs:attribute name="var"     type="xs:string"/>
  </xs:complexType>

  <xs:complexType name="limitType">
    <xs:attribute name="value" type="xs:string" use="required"/>
    <xs:attribute name="min"   type="xs:float"/>
    <xs:attribute name="max"   type="xs:float"/>
    <xs:attribute name="eq"    type="xs:string"/>
    <xs:attribute name="unit"  type="xs:string"/>
  </xs:complexType>

</xs:schema>
```

---

## 附录 B：解析器输出的内存模型

```text
Script(name, version, sha256, statements[])
├── ConnectStmt(resource, timeout=3.0)
├── DisconnectStmt(resource)
├── DelayStmt(ms)
└── Step(name, resource?, timeout=5.0, retry=0, retry_interval=0.0, on_fail=ABORT, instructions[])
    ├── Send(id:int, ext:bool, data: list[int|VarRef])
    ├── Wait(id, id_mask?, ext, timeout?, drain=BEFORE, min_len=0, fields[])
    │     └── Field(var, offset, length=1, endian=LITTLE, gain=1.0, bias=0.0,
    │               raw=False, unit?)
    ├── DelayStmt(ms)
    ├── Action(handler="module:func", var?, kwargs: dict[str,str|VarRef])
    └── Limit(value: float|VarRef, min?, max?, eq?, unit?)   # 必为最后一条
```

错误结构：

```python
@dataclass
class ScriptErrorInfo:
    code: str     # 如 "E105"
    line: int
    column: int
    message: str  # 中文说明
```
