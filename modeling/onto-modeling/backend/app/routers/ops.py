"""运维管理面（API 契约 3.25）：由旧 /api/admin 运维接口与 /api/settings/assets 资产包接口改形状而来。

本阶段先挂在主监听上；独立运维监听与 ONTO_WEB_OPS_ON_MAIN 开关属于 E8（P1）。项目一律只给
元数据视图（ProjectMetaView），不读内容。契约里没有旧接口、属于以后阶段的运维能力（指标、
链路、日志、告警、维护模式、插件运维、任务、诊断包等）这里不做。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import config
from ..api import deps
from ..api.errors import ApiError
from ..api.views import engine_label, project_meta_view
from ..services import audit, auth, conversation_store, error_log
from ..services import backup as backup_service
from ..services.agent_sessions import SDK_AVAILABLE, manager
from ..services.workspace import META_FILE, WorkspaceError
from .admin import ONLINE_WINDOW_SECONDS, auth_admin_available, list_auth_users, workspace_size
from .auth_routes import BILLING_CURRENCY

router = APIRouter(prefix="/api/v1/ops", tags=["ops"])

_STARTED_AT = time.time()
DISK_WARN_PCT = 15
DISK_WARN_BYTES = 10 * 1024**3
DEPENDENCY_PROBE_TTL_SECONDS = 30
USAGE_RANGES = {"today": 1, "7d": 7, "14d": 14}
# 备份范围（O-36 scope）：与 services/backup.py 打进包里的条目一一对应
BACKUP_SCOPE = (
    "workspaces", "session_history", "session_index", "config", "mcp_tokens", "service_tokens",
    "jiuwen_archives", "studio_drafts", "releases", "audit", "cli_transcripts",
)


def _read(request: Request) -> None:
    """运维面只读接口：ops、admin（契约 3.25）。"""
    deps.require_role(request, "ops", "admin")


def _write(request: Request) -> None:
    """运维面写接口：契约只给 ops。auth-server 的 ops 角色属于 E3（P1），过渡期 admin 也放行。"""
    deps.require_role(request, "ops", "admin")


def money(amount: float | None) -> dict:
    """Money（契约 1.9）：模型单价表（B14）落地前是引擎 SDK 报告的美元费用。"""
    return {"amount": None if amount is None else round(amount, 2), "currency": BILLING_CURRENCY}


def _pct_change(current: float, previous: float) -> float | None:
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


# ---- 模型用量（读会话事件历史） ----


def usage_events(project_ids: set[str] | None = None):
    """遍历会话事件历史里的用量事件，产出 (项目, 时间, 是否一轮, 费用, 输出 token, 输入 token)。

    递归：多会话的历史与已删除会话留存的用量都在 sessions/{项目}/ 下；默认会话是 sessions/{项目}.jsonl。
    """
    sessions_dir = Path(config.DATA_DIR) / "sessions"
    if not sessions_dir.is_dir():
        return
    for history_file in sessions_dir.rglob("*.jsonl"):
        rel = history_file.relative_to(sessions_dir)
        project = rel.parts[0] if len(rel.parts) > 1 else history_file.stem
        if project_ids is not None and project not in project_ids:
            continue
        try:
            lines = history_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if '"result"' not in line and '"model_usage"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = conversation_store.counted_usage(event)
            if usage is None or not event.get("ts"):
                continue
            is_call = event.get("type") == "result"
            yield (
                project,
                event["ts"],
                is_call,
                (event.get("total_cost_usd") or 0) if is_call else 0,
                usage.get("output_tokens") or 0,
                (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0),
            )


def _usage_buckets(project_ids: set[str] | None = None) -> dict[str, dict]:
    buckets: dict[str, dict] = {}
    for _project, ts, is_call, cost, output_tokens, _input in usage_events(project_ids):
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        bucket = buckets.setdefault(day, {"date": day, "cost": 0.0, "output_tokens": 0, "calls": 0})
        bucket["cost"] += cost
        bucket["calls"] += 1 if is_call else 0
        bucket["output_tokens"] += output_tokens
    return buckets


def _usage_daily(days: int = 14) -> list[dict]:
    """按日聚合全部项目的模型用量：有用量的最近 days 天，新的在前（费用为美元原值）。"""
    series = sorted(_usage_buckets().values(), key=lambda b: b["date"], reverse=True)[:days]
    for bucket in series:
        bucket["cost"] = round(bucket["cost"], 4)
    return series


def usage_report(span: int, today: date | None = None) -> dict:
    """O-01 usage_daily：最近 span 天逐日（含零值，按日期升序），合计、日均、峰值与上一周期对比。"""
    today = today or date.today()
    buckets = _usage_buckets()
    empty = {"calls": 0, "cost": 0.0, "output_tokens": 0}

    def window(end: date) -> list[dict]:
        result = []
        for offset in range(span - 1, -1, -1):
            day = (end - timedelta(days=offset)).strftime("%Y-%m-%d")
            result.append({"date": day, **{k: buckets.get(day, empty)[k] for k in empty}})
        return result

    current, previous = window(today), window(today - timedelta(days=span))

    def total(rows: list[dict]) -> dict:
        return {k: sum(row[k] for row in rows) for k in empty}

    def shape(row: dict) -> dict:
        return {"calls": row["calls"], "cost": money(row["cost"]), "output_tokens": row["output_tokens"]}

    now_total, prev_total = total(current), total(previous)
    peak = max(current, key=lambda row: (row["calls"], row["output_tokens"]))
    return {
        "days": [{"date": row["date"], **shape(row)} for row in current],
        "totals": shape(now_total),
        "avg": {
            "calls": round(now_total["calls"] / span, 1),
            "cost": money(now_total["cost"] / span),
            "output_tokens": round(now_total["output_tokens"] / span),
        },
        "peak": {"date": peak["date"], **shape(peak)} if peak["calls"] or peak["output_tokens"] else None,
        "vs_previous_pct": {k: _pct_change(now_total[k], prev_total[k]) for k in empty},
    }


def month_usage(project_ids: set[str] | None = None, now: float | None = None) -> dict:
    """本自然月的用量：{output_tokens, input_tokens, cost}（A-07 与运维总览共用）。"""
    start = datetime.fromtimestamp(now or time.time()).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    since = start.timestamp()
    output_tokens = input_tokens = 0
    cost = 0.0
    for _project, ts, _is_call, event_cost, out, inp in usage_events(project_ids):
        if ts >= since:
            output_tokens += out
            input_tokens += inp
            cost += event_cost
    return {"output_tokens": output_tokens, "input_tokens": input_tokens, "cost": money(cost)}


# ---- 磁盘、会话池 ----


def _disk() -> dict:
    disk = shutil.disk_usage(config.DATA_DIR)
    free_pct = disk.free / disk.total * 100 if disk.total else 0
    return {
        "total": disk.total,
        "free": disk.free,
        "free_pct": round(free_pct, 1),
        # 磁盘水位告警：可用低于 15% 或 10G
        "warn": free_pct < DISK_WARN_PCT or disk.free < DISK_WARN_BYTES,
    }


def pool_view() -> dict:
    status = manager.pool_status()
    return {
        "max_sessions": status["max"],
        "active": status["active"],
        "queued": len(status["waiting"]),
        "idle_ttl_s": status["idle_ttl_seconds"],
    }


def _meta_or_none(project_id: str) -> dict | None:
    try:
        return deps.workspaces.get_project(project_id)
    except WorkspaceError:
        return None


def _project_view(project_id: str) -> dict:
    meta = _meta_or_none(project_id)
    if meta is None:
        # 项目刚被删除、会话还没回收：只给 ID
        return {"id": project_id, "name": project_id, "owner": None, "stage_label": "", "build_mode": "L2",
                "archived": False, "created_at": None, "last_activity_at": None}
    return project_meta_view(meta, deps.workspaces.project_root(project_id))


def _session_state(session) -> str:
    if session.state in ("running", "queued"):
        return session.state
    return "idle"


@router.get("/sessions")
def sessions(request: Request):
    """O-15：会话池。"""
    _read(request)
    now = time.time()
    pool = pool_view()
    items = []
    for project_id, session in list(manager._sessions.items()):
        state = _session_state(session)
        engine = getattr(session.engine, "name", "") or (_meta_or_none(project_id) or {}).get("engine") or "claude"
        items.append({
            "project": _project_view(project_id),
            "engine_label": engine_label(engine),
            "state": state,
            "conversation_id": session.conversation_id,
            "connected": session.client is not None,
            "events": len(session.events),
            "idle_ms": int((now - session.last_activity) * 1000) if state == "idle" else None,
            "queue_position": manager.queue_position(project_id),
            "autobuild": False,
        })
    return {
        "config": {
            "max_sessions": pool["max_sessions"],
            "idle_ttl_s": pool["idle_ttl_s"],
            "overflow_policy": "evict_idle_then_queue",
        },
        "active": pool["active"],
        "queued": pool["queued"],
        "items": items,
    }


@router.post("/sessions/{project_id}/recycle")
async def recycle_session(project_id: str, request: Request):
    """O-17：回收会话子进程，续接点保留，用户下次发消息自动恢复。运行中的不回收。"""
    _write(request)
    from ..services.agent_sessions import _busy

    session = manager.get(project_id)
    if session is None:
        raise ApiError("session_not_found", "该项目没有在线会话", 404)
    if _busy(session) or session.pending_prompts:
        raise ApiError("session_running", "会话正在运行，不能回收", 409)
    recycled = await manager.recycle_project(project_id)
    return {"recycled": recycled, "pool": pool_view()}


# ---- O-01 运维总览 ----


def _online_users(now: float) -> list[dict]:
    recent = sorted(auth.LAST_SEEN.items(), key=lambda item: -item[1])
    return [
        {
            "username": username,
            "display_name": username,
            "online": now - last_seen < ONLINE_WINDOW_SECONDS,
            "last_seen_at": deps.iso(last_seen),
            "calls_today": None,  # 用量事件不带发起人，按人统计属于以后阶段
        }
        for username, last_seen in recent
    ]


def _startup_summary() -> dict | None:
    from ..services import startup_report

    report = startup_report.latest()
    if not report:
        return None
    return {
        "at": deps.iso(report.get("at")),
        "items": [{"label": item.get("title"), "result": item.get("result")} for item in report.get("items") or []],
    }


@router.get("/overview")
def overview(request: Request, range: str = "14d"):  # noqa: A002  契约查询参数名
    """O-01：运维总览。告警、自主构建属于以后阶段，给空值。"""
    _read(request)
    if range not in USAGE_RANGES:
        raise ApiError("invalid_argument", "range 只能是 today、7d、14d", 400, {"fields": {"range": "enum"}})
    now = time.time()
    disk = _disk()
    users = _online_users(now)
    accounts = None
    if auth_admin_available():
        try:
            accounts = len(list_auth_users())
        except ApiError:
            accounts = None
    from ..services.agent_sessions import usage_summary

    projects, total_cost = [], 0.0
    active = archived = 0
    for meta in deps.workspaces.list_projects():
        total_cost += usage_summary(meta["id"])["total_cost_usd"]
        root = deps.workspaces.project_root(meta["id"])
        archived += 1 if meta.get("archived") else 0
        active += 0 if meta.get("archived") else 1
        projects.append(project_meta_view(meta, root, disk_bytes=workspace_size(root)))
    projects.sort(key=lambda item: item.get("disk_bytes") or 0, reverse=True)
    pool = pool_view()
    advice = []
    if disk["warn"]:
        advice.append({"level": "warning", "text": f"磁盘可用空间 {disk['free_pct']}%，低于告警线，请清理或扩容"})
    trash = deps.workspaces.list_trash()
    if trash:
        advice.append({"level": "info", "text": f"回收站里有 {len(trash)} 个旧版删除的项目，可恢复或手工清理"})
    return {
        "stats": {
            "online_users": sum(1 for user in users if user["online"]),
            "accounts": accounts,
            "sessions": {
                "active": pool["active"], "max": pool["max_sessions"],
                "queued": pool["queued"], "idle_ttl_s": pool["idle_ttl_s"],
            },
            "projects": {"total": active + archived, "active": active, "archived": archived},
            "autobuild": {"running": 0, "limit": None},
            "cost": {"total": money(total_cost), "this_month": month_usage(now=now)["cost"]},
            "disk": {
                "free_pct": disk["free_pct"], "free_bytes": disk["free"],
                "threshold_pct": DISK_WARN_PCT, "warn": disk["warn"],
            },
        },
        "advice": advice,
        "usage_daily": usage_report(USAGE_RANGES[range]),
        "alerts": [],
        "autobuild_runs": [],
        "autobuild_today": {"completed": 0, "avg_duration_ms": None, "avg_cost": money(None), "stopped_by_budget": 0},
        "startup": _startup_summary(),
        "users": users,
        "projects": projects,
    }


# ---- O-13 健康详情 ----

_cli_version_cache: str | None = None
_probe_cache: tuple[float, list[dict]] | None = None
_probe_lock = threading.Lock()


def _package_version(name: str) -> str:
    try:
        from importlib.metadata import version as pkg_version

        return pkg_version(name)
    except Exception:
        return ""


def _cli_version() -> str:
    global _cli_version_cache
    if _cli_version_cache is not None:
        return _cli_version_cache
    import subprocess

    cli = config.CLI_PATH
    if not cli:
        try:
            import claude_agent_sdk

            bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
            cli = str(bundled) if bundled.is_file() else ""
        except ImportError:
            cli = ""
    if not cli:
        _cli_version_cache = ""
        return ""
    try:
        result = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=15)
        _cli_version_cache = result.stdout.strip()[:80] if result.returncode == 0 else ""
    except Exception:
        _cli_version_cache = ""
    return _cli_version_cache


def _timed(probe) -> tuple[str, str | None, int]:
    started = time.monotonic()
    try:
        status, issue = probe()
    except Exception as exc:  # 探测本身出错算失败，不让健康页 500
        status, issue = "fail", f"探测出错：{type(exc).__name__}"
    return status, issue, int((time.monotonic() - started) * 1000)


def _probe_auth_server() -> tuple[str, str | None]:
    if not auth.ENABLED:
        return "ready", "本地模式，未启用"
    try:
        with urllib.request.urlopen(f"{auth.AUTH_URL}/health", timeout=3) as response:
            return ("ready", None) if response.status == 200 else ("fail", f"返回 {response.status}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return "fail", f"不可达：{type(exc).__name__}"


def _probe_sandbox() -> tuple[str, str | None]:
    from ..services import sandbox_status

    status = sandbox_status.status()
    if status.get("connected"):
        return "ready", None
    # 连不上沙箱时脚本运行验证回退到主容器内执行，不算不可用
    return "warn", str(status.get("detail") or "未连接")


def _probe_model_gateway() -> tuple[str, str | None]:
    from ..services import model_settings

    admin = model_settings.masked()
    configured = admin.get("configured") or os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_API_KEY")
    return ("ready", None) if configured else ("warn", "未配置模型接入，使用部署机默认配置")


def _probe_disk() -> tuple[str, str | None]:
    disk = _disk()
    return ("warn", f"可用 {disk['free_pct']}%") if disk["warn"] else ("ready", None)


def _probe_git() -> tuple[str, str | None]:
    return ("ready", None) if shutil.which("git") else ("fail", "找不到 git")


DEPENDENCY_PROBES = (
    ("model_gateway", "配置检查", _probe_model_gateway),
    ("auth_server", "GET /health", _probe_auth_server),
    ("sandbox_gateway", "GET /v1/status", _probe_sandbox),
    ("disk", "数据目录可用空间", _probe_disk),
    ("git", "git 可执行", _probe_git),
)


def dependencies(force: bool = False) -> list[dict]:
    """依赖探测结果，缓存 30 秒（契约 O-13「每 30 秒探测一次」，按需刷新）。"""
    global _probe_cache
    with _probe_lock:
        if not force and _probe_cache and time.time() - _probe_cache[0] < DEPENDENCY_PROBE_TTL_SECONDS:
            return _probe_cache[1]
        results = []
        for name, probe_label, probe in DEPENDENCY_PROBES:
            status, issue, latency = _timed(probe)
            results.append({"name": name, "probe": probe_label, "latency_ms": latency, "status": status, "last_issue": issue})
        _probe_cache = (time.time(), results)
        return results


def _memory() -> dict | None:
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    values = {}
    for line in lines:
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * 1024
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    if not total or available is None:
        return None
    return {"used": total - available, "total": total}


@router.get("/health")
def health(request: Request):
    """O-13：存活、就绪、依赖探测与运行环境。"""
    _read(request)
    started = time.monotonic()
    deps_status = dependencies()
    passed = sum(1 for item in deps_status if item["status"] == "ready")
    failed = [item["name"] for item in deps_status if item["status"] == "fail"]
    warned = [item["name"] for item in deps_status if item["status"] in ("warn", "slow")]
    ready_status = "not_ready" if failed else "degraded" if warned else "ready"
    summary = (
        f"{'、'.join(failed)} 不可用" if failed
        else f"{'、'.join(warned)} 降级" if warned
        else "全部依赖就绪"
    )
    disk = _disk()
    try:
        load = list(os.getloadavg())
    except OSError:
        load = [0, 0, 0]
    from ..services.engines import jiuwen

    engines = [
        {
            "id": "claude", "label": engine_label("claude"),
            "runtime_version": _package_version("claude-agent-sdk"), "cli_version": _cli_version(),
            "available": SDK_AVAILABLE, "note": None if SDK_AVAILABLE else "claude-agent-sdk 未安装",
        },
        {
            "id": "jiuwen", "label": engine_label("jiuwen"),
            "runtime_version": _package_version("openjiuwen"), "cli_version": None,
            "available": jiuwen.JIUWEN_AVAILABLE,
            "note": None if jiuwen.JIUWEN_AVAILABLE else jiuwen.UNAVAILABLE_MESSAGE,
        },
    ]
    return {
        "live": {
            "status": "ok", "path": "/api/health/live",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "uptime_ms": int((time.time() - _STARTED_AT) * 1000),
            "started_at": deps.iso(_STARTED_AT),
        },
        "ready": {
            "status": ready_status, "path": "/api/health/ready",
            "passed": passed, "total": len(deps_status), "summary": summary,
        },
        "dependencies": deps_status,
        "runtime": {
            "engines": engines,
            "disk": {"total": disk["total"], "used_pct": round(100 - disk["free_pct"], 1), "warn": disk["warn"]},
            "load": load,
            "cpu_cores": os.cpu_count(),
            "memory": _memory(),
            "process": {"cmd": os.path.basename(sys.argv[0] or "python"), "pid": os.getpid(), "init_managed": os.getpid() != 1},
            "last_exit": None,
        },
    }


# ---- O-35 启动自检 ----


@router.get("/startup-report")
def startup_report_view(request: Request):
    """O-35：最近一次启动自检报告。没有报告（还没启动过）时 at 为 null、items 为空。"""
    _read(request)
    from ..services import startup_report

    report = startup_report.latest() or {}
    return {
        "at": deps.iso(report.get("at")),
        "last_exit": report.get("last_exit"),
        "items": report.get("items") or [],
    }


# ---- O-36、O-37 数据备份 ----

_backup_lock = threading.Lock()


def _backup_items() -> list[dict]:
    return [
        {
            "name": entry["name"], "size": entry["size"], "created_at": deps.iso(entry["ts"]),
            "scope": "full", "scope_items": list(BACKUP_SCOPE),
        }
        for entry in backup_service.list_backups()
    ]


@router.get("/backups")
def backups(request: Request):
    """O-36：备份列表。现在按间隔定时（ONTO_WEB_BACKUP_INTERVAL_HOURS），没有固定时刻。"""
    _read(request)
    interval = config.BACKUP_INTERVAL_HOURS
    next_due = deps.iso(time.time() + backup_service.seconds_until_due(interval * 3600)) if interval > 0 else None
    return {
        "schedule": {
            "daily_at": None,
            "interval_hours": interval,
            "next_due_at": next_due,
            "keep": backup_service.BACKUP_KEEP,
            "dir_volume": str(backup_service.backups_dir()),
        },
        "scope": list(BACKUP_SCOPE),
        "items": _backup_items(),
        "last_drill": None,
    }


@router.post("/backups")
async def run_backup(request: Request):
    """O-37：立即备份。备份同步打完再回 202（任务中心 O-40 属于以后阶段）。"""
    _write(request)
    if not _backup_lock.acquire(blocking=False):
        raise ApiError("backup_in_progress", "已有备份在进行", 409)
    try:
        name = await asyncio.to_thread(backup_service.run_backup, deps.workspaces)
    finally:
        _backup_lock.release()
    return JSONResponse(status_code=202, content={"task_id": f"backup-{uuid.uuid4().hex[:12]}", "name": name})


# ---- O-30、O-32、O-33 core 默认版本（过渡：现有提示词资产包） ----

CURRENT_ASSETS_VERSION = "current"


def core_default_view() -> dict:
    """O-30 形状。包仓库（B3）之前 core 只有一份资产：当前版本叫 current，资产备份按备份名列为 retired。"""
    from ..services import assets_admin

    info = assets_admin.info()
    versions = [{
        "version": CURRENT_ASSETS_VERSION, "channel": "stable",
        "released_at": deps.iso(info.get("updated_at")), "is_default": True,
    }] if info.get("present") else []
    versions += [
        {"version": entry["name"], "channel": "retired", "released_at": deps.iso(entry["time"]), "is_default": False}
        for entry in assets_admin.list_backups()
    ]
    return {"stable": CURRENT_ASSETS_VERSION if info.get("present") else None, "beta": None, "versions": versions}


def _asset_error(exc) -> ApiError:
    if exc.status == 404:
        return ApiError("not_found", str(exc), 404)
    return ApiError("invalid_argument", str(exc), exc.status if 400 <= exc.status < 500 else 400)


@router.get("/core-default")
def core_default(request: Request):
    """O-30：core 默认版本（过渡实现，见 core_default_view）。"""
    _read(request)
    return core_default_view()


class RollbackBody(BaseModel):
    version: str


@router.post("/core-default/rollback")
def core_default_rollback(body: RollbackBody, request: Request):
    """O-32：回滚到某份资产备份；当前资产先移入备份，再恢复所选版本。"""
    _write(request)
    from ..services import assets_admin

    try:
        assets_admin.rollback(body.version)
    except assets_admin.AssetError as exc:
        raise _asset_error(exc) from exc
    return core_default_view()


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


@router.get("/pack-repo/backups")
def pack_repo_backups(request: Request):
    """O-33：包仓库备份（过渡：资产包备份目录，现在不自动清理，keep 为 null）。"""
    _read(request)
    from ..services import assets_admin

    directory = Path(config.DATA_DIR) / "asset-backups"
    return {
        "items": [
            {"name": entry["name"], "size": _dir_size(directory / entry["name"]), "created_at": deps.iso(entry["time"])}
            for entry in assets_admin.list_backups()
        ],
        "keep": None,
    }


# ---- 追加写日志的游标分页（审计、错误留档） ----


def _read_log(path: Path) -> list[tuple[int, dict]]:
    """读 JSONL 日志：(行号, 记录)，新的在前。行号是文件里的绝对位置，只追加写所以稳定。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            entries.append((index, record))
    entries.reverse()
    return entries


def log_page(entries: list[tuple[int, dict]], cursor: str, limit: int, binding: dict, default: int) -> tuple[list, str | None]:
    """按行号翻页：游标记住上一页最后一行，下一页从它往前取，翻页期间新写入不会让条目错位。"""
    key = hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    limit = max(1, min(int(limit or default), deps.PAGE_MAX))
    if cursor:
        try:
            data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            before = int(data["i"])
            valid = data.get("b") == key
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise ApiError("invalid_cursor", "游标无法解析，或筛选条件变了", 400)
        entries = [entry for entry in entries if entry[0] < before]
    page = entries[:limit]
    next_cursor = None
    if len(entries) > limit:
        token = json.dumps({"i": page[-1][0], "b": key}).encode()
        next_cursor = base64.urlsafe_b64encode(token).decode().rstrip("=")
    return page, next_cursor


# ---- O-48 审计 ----

AUDIT_TYPES = (
    "write", "mcp", "commit", "plugin", "evidence", "ops", "auth", "publish", "changeset",
    "autobuild", "user", "contract",
)
_PROJECT_IN_PATH = re.compile(r"^/api(?:/v1)?/(?:projects|onto-mcp)/([a-z0-9][a-z0-9-]*)(?:/|$)|^/projects/([a-z0-9][a-z0-9-]*)/")
_TYPE_BY_PATH = (
    (re.compile(r"^/api/v1/ops/|^/api/admin/(?:backup|sessions|trash)"), "ops"),
    (re.compile(r"^/api(?:/v1)?/auth/|^/api/v1/me/password"), "auth"),
    (re.compile(r"^/api(?:/v1)?/admin/users|^/api/v1/admin/projects/[^/]+/owner|^/api/admin/projects/[^/]+/owner"), "user"),
    (re.compile(r"/(?:releases|publish)(?:/|$)"), "publish"),
    (re.compile(r"/changesets(?:/|$)"), "changeset"),
    (re.compile(r"/autobuild(?:/|$)"), "autobuild"),
    (re.compile(r"/(?:packs|plugins)(?:/|$)"), "plugin"),
    (re.compile(r"/evidence(?:/|$)"), "evidence"),
    (re.compile(r"^/api(?:/v1)?/onto-mcp/|/mcp(?:/|$)"), "mcp"),
)
# 旧审计记录里的动作名 → 契约动作名
_LEGACY_ACTIONS = {"set_owner": "project.set_owner", "remove_members": "project.remove_members"}


def _audit_action(detail: dict) -> str | None:
    action = detail.get("action")
    if isinstance(action, str) and action:
        return _LEGACY_ACTIONS.get(action, action)
    # B1 的契约审计把动作写成键名，如 {"publish.download": 版本}
    return next((key for key in detail if "." in key), None)


def _audit_summary(detail: dict) -> str:
    """审计摘要只取短的标量值（键、版本、用户名、计数），不带正文。"""
    parts = []
    for key, value in detail.items():
        if key == "action" or isinstance(value, (dict, list)):
            continue
        text = "" if value is None else str(value)
        if len(text) <= 80:
            parts.append(f"{key}={text}")
    return "，".join(parts)[:300]


def audit_entry(index: int, raw: dict) -> dict:
    """audit.jsonl 的一行 → AuditEntry（契约 7.16）。"""
    actor = str(raw.get("user") or "anonymous")
    method = str(raw.get("method") or "") or None
    path = str(raw.get("path") or "") or None
    detail = raw.get("detail") if isinstance(raw.get("detail"), dict) else {}
    action = _audit_action(detail)
    if actor.startswith("service:"):
        actor_kind, kind = "service", "contract"
    elif method == "MCP":
        actor_kind, kind = "mcp", "mcp"
    elif actor == "system" or method == "MIGRATE":
        actor_kind, kind = "system", "write"
    else:
        actor_kind, kind = "user", "write"
    prefix = (action or "").split(".", 1)[0]
    if prefix in AUDIT_TYPES and kind not in ("contract", "mcp"):
        kind = prefix
    elif prefix == "project" and action == "project.set_owner":
        kind = "user"
    elif kind == "write" and path:
        kind = next((name for pattern, name in _TYPE_BY_PATH if pattern.search(path)), "write")
    project_id = detail.get("project") if isinstance(detail.get("project"), str) else None
    if not project_id and path:
        found = _PROJECT_IN_PATH.search(path)
        project_id = (found.group(1) or found.group(2)) if found else None
    ts = raw.get("ts")
    return {
        "id": "aud-" + hashlib.sha256(f"{index}:{ts}".encode()).hexdigest()[:12],
        "ts": deps.iso(ts),
        "actor": actor,
        "actor_kind": actor_kind,
        "type": kind,
        "action": action or (f"http.{method.lower()}" if method else "unknown"),
        "method": method,
        "path": path,
        "status": raw.get("status"),
        "project_id": project_id,
        "summary": _audit_summary(detail),
        "trailer": None,
        "ip": raw.get("ip"),
        "trace_id": raw.get("trace_id"),
    }


def _split(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


@router.get("/audit")
def audit_entries(request: Request, type: str = "", q: str = "", cursor: str = "", limit: int = 0):  # noqa: A002
    """O-48：审计日志，按 type（多值）、q（操作者、项目、路径、摘要）、from/to 过滤，默认每页 100 条。

    from 是 Python 关键字，from/to 直接从查询串取。
    """
    _read(request)
    params = request.query_params
    types = _split(type)
    unknown = [item for item in types if item not in AUDIT_TYPES]
    if unknown:
        raise ApiError("invalid_argument", "未知的审计类型", 400, {"fields": {"type": ",".join(unknown)}})
    since = deps.parse_time(params["from"], "from") if params.get("from") else None
    until = deps.parse_time(params["to"], "to") if params.get("to") else None
    needle = q.strip().lower()
    matched = []
    for index, raw in _read_log(audit._file()):
        ts = raw.get("ts") or 0
        if (since is not None and ts < since) or (until is not None and ts > until):
            continue
        entry = audit_entry(index, raw)
        if types and entry["type"] not in types:
            continue
        if needle and not any(
            needle in str(entry.get(field) or "").lower() for field in ("actor", "project_id", "path", "summary", "action")
        ):
            continue
        matched.append((index, entry))
    binding = {"type": sorted(types), "q": needle, "from": params.get("from", ""), "to": params.get("to", "")}
    page, next_cursor = log_page(matched, cursor, limit, binding, default=100)
    return {"items": [entry for _index, entry in page], "next_cursor": next_cursor, "total": len(matched)}


# ---- O-50 错误留档 ----

_ERROR_ID = re.compile(r"^\[(err-[0-9a-f]{4,8})\]\s*")
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|authorization)\s*[=:]\s*)[^\s,;'\"]+"),
    re.compile(r"\b(?:sk|onto_pat|onto_svc)[-_][A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"),
)
_ORIGINS = {
    "kernel": "kernel", "project_delete": "kernel", "eval_retirement": "kernel",
    "publish": "publish", "collection": "collection", "assembly": "assembly", "gate": "gate",
}
_FRIENDLY = {
    "session": "会话出错，已记录原始错误",
    "publish": "发布出错，已记录原始错误",
    "kernel": "服务内部错误，已记录原始错误",
}


def redact(text: str) -> str:
    """错误原文脱敏：去掉令牌、密钥、连接串里的口令（契约 1.3 决定 #29）。"""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.re.groups else "") + "***", text)
    return text


def error_record(index: int, raw: dict, metas: dict[str, dict]) -> dict:
    """errors.jsonl 的一行 → ErrorRecord（契约 7.16）。"""
    text = str(raw.get("raw") or "")
    found = _ERROR_ID.match(text)
    ts = raw.get("ts")
    error_id = found.group(1) if found else "err-" + hashlib.sha256(f"{index}:{ts}".encode()).hexdigest()[:6]
    source = str(raw.get("source") or "")
    origin = _ORIGINS.get(source) or ("session" if source.startswith(("session", "interrupt")) or source else "kernel")
    project_id = str(raw.get("project_id") or "")
    meta = metas.get(project_id)
    return {
        "id": error_id,
        "at": deps.iso(ts),
        "project": project_meta_view(meta) if meta else None,
        "project_id": project_id or None,
        "origin": origin,
        "source": source or None,
        "pack": None,
        "node": None,
        "raw_message": redact(_ERROR_ID.sub("", text)),
        "friendly_message": _FRIENDLY.get(origin, _FRIENDLY["kernel"]),
        "trace_id": raw.get("trace_id"),
    }


@router.get("/errors")
def errors(request: Request, cursor: str = "", limit: int = 0):
    """O-50：错误留档，新的在前，默认每页 20 条。"""
    _read(request)
    metas = {meta["id"]: meta for meta in deps.workspaces.list_projects()}
    entries = _read_log(error_log._file())
    page, next_cursor = log_page(entries, cursor, limit, {}, default=20)
    return {
        "items": [error_record(index, raw, metas) for index, raw in page],
        "next_cursor": next_cursor,
        "total": len(entries),
    }


# ---- O-52、O-53 回收站 ----


def _trash_meta(entry: Path) -> dict:
    try:
        meta = json.loads((entry / META_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        meta = {}
    return meta if isinstance(meta, dict) else {}


@router.get("/trash")
def trash(request: Request):
    """O-52：回收站。删除已改为永久删除，这里只有旧版软删除留下的条目，不自动清理。"""
    _read(request)
    directory = deps.workspaces.trash_dir()
    items = []
    for entry in deps.workspaces.list_trash():
        path = directory / entry["name"]
        meta = {"id": entry["name"], "name": entry["project_name"], "owner": entry["owner"], **_trash_meta(path)}
        items.append({
            "entry": entry["name"],
            "project": project_meta_view(meta),
            "deleted_at": deps.iso(entry["deleted_at"]),
            "size": workspace_size(path),
            "purge_at": None,
        })
    return {"retention_days": None, "items": items}


@router.post("/trash/{entry}/restore")
def restore_trash(entry: str, request: Request):
    """O-53：恢复到所有者的项目列表；原 ID 被占用时换新 ID。"""
    _write(request)
    try:
        meta = deps.workspaces.restore_trash(entry)
    except WorkspaceError as exc:
        if exc.status == 404:
            raise ApiError("trash_entry_not_found", "回收站条目不存在", 404) from exc
        raise
    return project_meta_view(meta, deps.workspaces.project_root(meta["id"]))
