"""健康检查（详设 4.10.2、方案 E7、契约 C-02～C-04、O-13、O-14）：分存活和就绪。

- `/api/health/live`：进程能响应就是 ok，不探测依赖；容器 HEALTHCHECK 用它；
- `/api/health/ready`：探测模型网关、auth-server（JWKS）、沙箱网关、连接器网关、磁盘、git、装配目录；
  关键依赖失败或启动恢复没做完返回 503；匿名请求只给每项的名称和状态；
- `/api/health`：旧接口，响应不变。

探测结果缓存 30 秒；插件运行时等模块用 `register_check` 加自己的探测项（装配目录由 B3a 接入）。
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config

router = APIRouter(tags=["health"])

PROBE_TTL_SECONDS = 30
SLOW_MS = 1500
DISK_WARN_PCT = 15
DISK_WARN_BYTES = 10 * 1024**3
DISK_FAIL_BYTES = 512 * 1024**2

_started_at = time.time()
_startup_done = False
_cache: tuple[float, list[dict]] | None = None
_lock = threading.Lock()

# 名称 → (探测方式说明, 探测函数, 是否关键)；探测函数返回 (status, detail)，status 取 ok|warn|fail
_checks: dict[str, tuple[str, Callable[[], tuple[str, str | None]], bool]] = {}


def register_check(name: str, probe_label: str, probe: Callable[[], tuple[str, str | None]], critical: bool = False) -> None:
    _checks[name] = (probe_label, probe, critical)


def mark_startup_done() -> None:
    global _startup_done
    _startup_done = True


def startup_done() -> bool:
    return _startup_done


def uptime_ms() -> int:
    return int((time.time() - _started_at) * 1000)


def started_at() -> float:
    return _started_at


# ---- 内置探测 ----


def disk_usage() -> dict:
    disk = shutil.disk_usage(config.DATA_DIR if os.path.isdir(config.DATA_DIR) else "/")
    free_pct = disk.free / disk.total * 100 if disk.total else 0
    return {
        "total": disk.total, "free": disk.free, "free_pct": round(free_pct, 1),
        "warn": free_pct < DISK_WARN_PCT or disk.free < DISK_WARN_BYTES,
    }


def _probe_disk():
    disk = disk_usage()
    if disk["free"] < DISK_FAIL_BYTES:
        return "fail", f"可用空间只剩 {disk['free'] // 1024**2} MiB"
    return ("warn", f"可用 {disk['free_pct']}%") if disk["warn"] else ("ok", None)


def _probe_git():
    return ("ok", None) if shutil.which("git") else ("fail", "找不到 git")


def _probe_auth_server():
    from ..services import auth

    if not auth.ENABLED:
        return "ok", "本地模式，未启用"
    try:
        with urllib.request.urlopen(f"{auth.AUTH_URL}/jwks.json", timeout=3) as response:
            return ("ok", None) if response.status == 200 else ("fail", f"JWKS 返回 {response.status}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return "fail", f"JWKS 不可达：{type(exc).__name__}"


def _probe_model_gateway():
    from ..services import model_settings

    admin = model_settings.masked()
    # 任一引擎在界面或部署环境变量里接上了模型就算配置了（只配内测引擎的部署原来报「未配置」）
    configured = (admin.get("configured") or (admin.get("jiuwen") or {}).get("configured")
                  or any(model_settings.deploy_config(engine)["configured"] for engine in ("claude", "jiuwen")))
    return ("ok", None) if configured else ("warn", "未配置模型接入，使用部署机默认配置")


def _probe_sandbox():
    from ..services import sandbox_status

    status = sandbox_status.status()
    if status.get("connected"):
        return "ok", None
    # 连不上沙箱时脚本运行验证回退到主容器内执行，不算不可用
    return "warn", str(status.get("detail") or "未连接")


def _probe_connector_gateway():
    """连接器网关（ADR-0073）：资料转文本靠它；不可用时资料等待转换、恢复后补转，不挡就绪。"""
    from ..services.material_conversion import gateway

    status = gateway.health(force=True)
    if status["status"] == "ok":
        return "ok", f"已加载 {status['converters']} 个转换器"
    return ("warn" if status["status"] == "not_configured" else "fail"), status.get("detail")


def _probe_assembly():
    return "ok", "插件装配未启用"


register_check("model_gateway", "配置检查", _probe_model_gateway)
register_check("auth_server", "GET /jwks.json", _probe_auth_server, critical=True)
register_check("sandbox_gateway", "GET /v1/status", _probe_sandbox)
register_check("connector_gateway", "GET /health", _probe_connector_gateway)
register_check("disk", "数据目录可用空间", _probe_disk, critical=True)
register_check("git", "git 可执行", _probe_git, critical=True)
register_check("assembly", "装配目录核对", _probe_assembly, critical=True)


def probe(force: bool = False) -> list[dict]:
    """全部探测：[{name, probe, status, latency_ms, detail, critical}]，缓存 30 秒。"""
    global _cache
    with _lock:
        if not force and _cache and time.time() - _cache[0] < PROBE_TTL_SECONDS:
            return _cache[1]
        results = []
        for name, (label, check, critical) in list(_checks.items()):
            started = time.monotonic()
            try:
                status, detail = check()
            except Exception as exc:  # 探测本身出错算失败，不让健康页 500
                status, detail = "fail", f"探测出错：{type(exc).__name__}"
            latency = int((time.monotonic() - started) * 1000)
            if status == "ok" and latency > SLOW_MS:
                status = "slow"
            results.append({"name": name, "probe": label, "status": status, "latency_ms": latency,
                            "detail": detail, "critical": critical})
        _cache = (time.time(), results)
        return results


def readiness(force: bool = False) -> tuple[str, list[dict]]:
    """(ready|degraded|not_ready, 探测结果)。"""
    checks = probe(force)
    if not _startup_done or any(item["status"] == "fail" and item["critical"] for item in checks):
        return "not_ready", checks
    if any(item["status"] != "ok" for item in checks):
        return "degraded", checks
    return "ready", checks


def _authenticated(request: Request) -> bool:
    from ..services import auth

    if not auth.ENABLED:
        return True
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    return bool(token) and auth.verify_access_token(token) is not None


# ---- 路由（两个监听都挂） ----


@router.get("/api/health")
def health_legacy():
    """C-02：永久保留的旧健康检查，响应不变。"""
    from ..routers.auth_routes import drawio_available
    from ..services.agent_sessions import SDK_AVAILABLE

    return {"ok": True, "agent_sdk": SDK_AVAILABLE, "drawio": drawio_available()}


@router.get("/api/health/live")
def health_live():
    """C-03 存活：只要进程能响应就是 ok，不探测依赖（容器 HEALTHCHECK 用它）。"""
    from ..api.deps import iso

    return {"status": "ok", "uptime_ms": uptime_ms(), "started_at": iso(_started_at)}


@router.get("/api/health/ready")
def health_ready(request: Request):
    """C-04 就绪：关键依赖不可用或启动恢复没做完时 503。"""
    status, checks = readiness()
    full = _authenticated(request)
    items = [
        {"name": item["name"], "status": item["status"],
         **({"latency_ms": item["latency_ms"], "detail": item["detail"]} if full else {})}
        for item in checks
    ]
    if not _startup_done and full:
        items.append({"name": "startup_recovery", "status": "fail", "latency_ms": 0, "detail": "启动恢复还没完成"})
    body = {"status": status, "checks": items}
    return JSONResponse(status_code=503 if status == "not_ready" else 200, content=body)
