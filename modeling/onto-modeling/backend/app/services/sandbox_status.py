"""配置中心「沙箱」：经脚本验证网关查 onto-agent-jiuwenbox 是否可用（只读状态，无需配置）。

onto-modeling 主容器不挂 jiuwenbox socket，连接、检测都由脚本验证网关完成（backend/app/sandbox_gateway.py）。
客户端以 workspace-toolkit/scripts/jiuwenbox_client.py 为单一事实源，脚本运行验证 script_verify.py
用的是同一份实现；这里只负责加载并补上检测时间。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime


def _load_client():
    # 经共享契约库加载（方案 A16）：与脚本运行验证 script_verify.py 用同一份实现
    from .packs import contracts

    return contracts.default().load("jiuwenbox_client")


client = _load_client()


def status() -> dict:
    """经网关检测：返回 {connected, gateway, socket, detail, path, python, pip, packages, checked_at}。"""
    return {**client.gateway_status(), "checked_at": datetime.now().astimezone().isoformat(timespec="seconds")}


# 脚本页的沙箱摘要：一次检测要经网关进沙箱跑一趟（实测 2～5 秒），进页面不能每次都等它。
# 60 秒内复用上次结果；过期后先给旧结果、后台重测；同一时刻只测一次。管理员「检测」仍直接调 status()
CACHE_SECONDS = 60
_cache: tuple | None = None  # (检测函数, 检测时刻, 结果)
_cache_lock = threading.Lock()
_refreshing = threading.Event()


def _store(probe) -> dict:
    global _cache
    result = probe()
    _cache = (probe, time.monotonic(), result)
    return result


def _refresh_in_background(probe) -> None:
    if _refreshing.is_set():
        return
    _refreshing.set()

    def run():
        try:
            _store(probe)
        except Exception:  # noqa: BLE001 - 重测失败就留着旧结果，下次再测
            pass
        finally:
            _refreshing.clear()

    threading.Thread(target=run, name="sandbox-status-refresh", daemon=True).start()


def cached_status() -> dict:
    probe = status  # 测试替换了 status 时不用替换前的结果
    hit = _cache
    if hit and hit[0] is probe:
        if time.monotonic() - hit[1] >= CACHE_SECONDS:
            _refresh_in_background(probe)
        return hit[2]
    with _cache_lock:
        hit = _cache
        if hit and hit[0] is probe:
            return hit[2]
        return _store(probe)
