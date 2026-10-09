"""通信层：同步接口、Frame、资源配置。"""

from .base import Communication, Frame, ResourceConfig
from .modbus import ModbusCommunication
from .serial import SerialCommunication

__all__ = ["Communication", "Frame", "ResourceConfig",
           "SerialCommunication", "ModbusCommunication"]
