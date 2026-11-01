"""配置中心「沙箱」：经脚本验证网关查 onto-agent-jiuwenbox 是否可用（只读状态，无需配置）。

onto-modeling 主容器不挂 jiuwenbox socket，连接、检测都由脚本验证网关完成（backend/app/sandbox_gateway.py）。
客户端以 workspace-toolkit/scripts/jiuwenbox_client.py 为单一事实源，脚本运行验证 script_verify.py
用的是同一份实现；这里只负责加载并补上检测时间。
"""

from __future__ import annotations

from datetime import datetime


def _load_client():
    # 经共享契约库加载（方案 A16）：与脚本运行验证 script_verify.py 用同一份实现
    from .packs import contracts

    return contracts.default().load("jiuwenbox_client")


client = _load_client()


def status() -> dict:
    """经网关检测：返回 {connected, gateway, socket, detail, path, python, pip, packages, checked_at}。"""
    return {**client.gateway_status(), "checked_at": datetime.now().astimezone().isoformat(timespec="seconds")}
