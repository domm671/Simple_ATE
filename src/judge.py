"""limit 判定。"""

from __future__ import annotations

from .errors import JudgeTypeError
from .model import Limit, VarRef


def resolve(operand, variables: dict):
    if isinstance(operand, VarRef):
        return variables.get(operand.name)
    return operand


def _as_number(value, what: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JudgeTypeError(f"{what} = {value!r} 不是数值，无法做数值判定")
    return float(value)


def judge(spec: Limit, variables: dict) -> tuple[bool, object]:
    """返回 (是否PASS, 被判定值)。"""
    value = resolve(spec.value, variables)

    if spec.eq is not None:
        expected = resolve(spec.eq, variables)
        # int/float 相容，其余按相等比较
        compatible = (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and isinstance(expected, (int, float)) and not isinstance(expected, bool)
            or type(value) == type(expected)
        )
        if not compatible:
            raise JudgeTypeError(
                f"eq 判定类型不相容：value={value!r}({type(value).__name__}), "
                f"eq={expected!r}({type(expected).__name__})")
        return value == expected, value

    if value is None:
        raise JudgeTypeError("判定值为空（变量未定义或 action/field 未产出）")
    num = _as_number(value, "value")
    if spec.min is not None and num < spec.min:
        return False, value
    if spec.max is not None and num > spec.max:
        return False, value
    return True, value
