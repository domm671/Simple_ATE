"""脚本引擎包：XML 解析、顺序执行、判定、上下文与扩展加载。

对应《设计说明.md》第 3 章总体架构中的 Script Engine 层
（Parser → Executor → Judge → Context），不依赖任何 UI 框架。
"""

from .executor import Engine, EngineListener
from .parser import ScriptParser

__all__ = ["Engine", "EngineListener", "ScriptParser"]
