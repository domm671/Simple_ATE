"""异常与错误码。

错误码分段（见《ATE脚本格式规范.md》第 10 章）：
E1xx/E2xx：加载期，脚本不会开始执行
E3xx：运行期
"""


class SimpleAteError(Exception):
    """所有 simple_ate 异常的基类。"""

    code = "E000"

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------- 加载期
class ScriptError(SimpleAteError):
    """脚本静态错误（加载期）。可附带行列号。"""

    code = "E101"

    def __init__(self, message: str, code: str = "E101", line: int = 0, column: int = 0):
        super().__init__(message)
        self.code = code
        self.line = line
        self.column = column

    def __str__(self) -> str:
        if self.line:
            return f"{self.code} (line {self.line}, col {self.column}): {self.message}"
        return f"{self.code}: {self.message}"


class ScriptParseErrors(SimpleAteError):
    """一次解析中收集到的多个错误。"""

    code = "E101"

    def __init__(self, errors: list[ScriptError]):
        self.errors = errors
        detail = "\n  ".join(str(e) for e in errors)
        super().__init__(f"脚本存在 {len(errors)} 处错误:\n  {detail}")


# ---------------------------------------------------------------- 运行期
class RuntimeAteError(SimpleAteError):
    """运行期错误基类。retryable 决定是否消耗 step 的重试次数。"""

    code = "E303"
    retryable = False


class TimeoutAteError(RuntimeAteError):
    """应答超时（含 ID/掩码未命中、min_len 一直不满足）。E301，可重试。"""

    code = "E301"
    retryable = True


class CommunicationError(RuntimeAteError):
    """链路异常（掉线、发送失败）。E302，可重试。"""

    code = "E302"
    retryable = True


class FieldExtractError(RuntimeAteError):
    """field 提取超出实际帧长度。E305，可重试。"""

    code = "E305"
    retryable = True


class ExtensionError(RuntimeAteError):
    """扩展 action 抛出的异常。E306，按通信类对待，可重试。"""

    code = "E306"
    retryable = True


class SendDataError(RuntimeAteError):
    """send data 变量运行期值不是 0..255 的整数。E307，脚本缺陷，不重试。"""

    code = "E307"
    retryable = False


class JudgeTypeError(RuntimeAteError):
    """判定值与判据类型不相容。E209，不重试。"""

    code = "E209"
    retryable = False


class EngineBug(RuntimeAteError):
    """引擎/扩展未预期异常。E303，不重试。"""

    code = "E303"
    retryable = False
