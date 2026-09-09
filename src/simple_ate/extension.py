"""可选扩展 action 的加载（importlib + 工位配置白名单）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from .errors import ExtensionError
from .model import VarRef


def resolve_kwargs(raw: dict, variables: dict) -> dict:
    """把 action 入参中的 VarRef 替换为运行期值（统一转成字符串传入扩展函数）。"""
    out: dict[str, str] = {}
    for key, val in raw.items():
        if isinstance(val, VarRef):
            out[key] = str(variables[val.name])
        else:
            out[key] = str(val)
    return out


class ExtensionLoader:
    def __init__(self, extensions_dir: str | Path, allowed: tuple[str, ...] = ()):
        self.dir = Path(extensions_dir)
        self.allowed = set(allowed)
        self._cache: dict[str, object] = {}

    def load(self, handler: str):
        """返回 handler "module:func" 指向的可调用对象。"""
        module_name, _, func_name = handler.partition(":")
        if module_name not in self.allowed:
            raise ExtensionError(f"扩展模块 {module_name!r} 不在白名单中")
        module = self._import(module_name)
        func = getattr(module, func_name, None)
        if not callable(func):
            raise ExtensionError(f"扩展 {handler} 不存在或不可调用")
        return func

    def _import(self, module_name: str):
        if module_name in self._cache:
            return self._cache[module_name]
        path = self.dir / f"{module_name}.py"
        if not path.exists():
            raise ExtensionError(f"扩展模块文件不存在: {path}")
        unique = f"simple_ate_ext_{module_name}"
        spec = importlib.util.spec_from_file_location(unique, path)
        if spec is None or spec.loader is None:
            raise ExtensionError(f"无法加载扩展模块: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique] = module
        spec.loader.exec_module(module)
        self._cache[module_name] = module
        return module
