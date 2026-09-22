"""日志配置包：运行日志（run log）与原始通信 trace。

对应《设计说明.md》第 4.4 节，Logger 为被所有层使用的横切模块。
"""

from .logger import TraceLogger, setup_run_logger

__all__ = ["TraceLogger", "setup_run_logger"]
