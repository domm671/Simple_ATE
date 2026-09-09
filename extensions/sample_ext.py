"""扩展 action 示例。

仅演示自定义 handler 的签名。普通产品测试不需要写扩展。
在脚本中使用（需在 station.toml [extensions] allowed 中放行 "sample_ext"）：

    <step name="ExtDemo" timeout="2">
        <action handler="sample_ext:read_status" address="1" var="st"/>
        <limit value="${st}" eq="0"/>
    </step>
"""

from __future__ import annotations


def read_status(ctx, resource, address: str = "0") -> int:
    """读取状态字：示例中不真正收发，直接返回 0。

    真实实现应使用 resource.send/resource.recv 完成多帧交互；
    超时请抛 TimeoutAteError / CommunicationError（可重试）。
    """
    ctx.logger.info(f"[sample_ext] read_status address={address}")
    return 0
