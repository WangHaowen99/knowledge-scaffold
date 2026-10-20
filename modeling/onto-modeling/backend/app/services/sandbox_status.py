"""配置中心「沙箱」：经脚本验证网关查 onto-agent-jiuwenbox 是否可用（只读状态，无需配置）。

onto-modeling 主容器不挂 jiuwenbox socket，连接、检测都由脚本验证网关完成（backend/app/sandbox_gateway.py）。
客户端以 workspace-toolkit/scripts/jiuwenbox_client.py 为单一事实源，脚本运行验证 script_verify.py
用的是同一份实现；这里只负责加载并补上检测时间。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

from .. import config


def _load_client():
    path = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts" / "jiuwenbox_client.py"
    spec = importlib.util.spec_from_file_location("onto_workspace_jiuwenbox_client", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


client = _load_client()


def status() -> dict:
    """经网关检测：返回 {connected, gateway, socket, detail, path, python, pip, packages, checked_at}。"""
    return {**client.gateway_status(), "checked_at": datetime.now().astimezone().isoformat(timespec="seconds")}
