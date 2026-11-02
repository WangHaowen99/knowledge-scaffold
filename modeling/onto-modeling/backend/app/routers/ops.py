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
import sys
import threading
import time
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
from ..governance import health as health_checks
from ..governance import lifecycle, roles
from ..services import audit, auth, conversation_store, error_log
from ..services import backup as backup_service
from ..services.agent_sessions import SDK_AVAILABLE, manager
from ..services.workspace import META_FILE, WorkspaceError
from .admin import ONLINE_WINDOW_SECONDS, auth_admin_available, list_auth_users, workspace_size
from .auth_routes import BILLING_CURRENCY

router = APIRouter(prefix="/api/v1/ops", tags=["ops"])

DISK_WARN_PCT = health_checks.DISK_WARN_PCT
USAGE_RANGES = {"today": 1, "7d": 7, "14d": 14}
# 备份范围（O-36 scope）：与 services/backup.py 打进包里的条目一一对应
BACKUP_SCOPE = (
    "workspaces", "session_history", "session_index", "config", "mcp_tokens", "service_tokens",
    "jiuwen_archives", "studio_drafts", "releases", "packs", "audit", "cli_transcripts",
)


def _read(request: Request) -> None:
    """运维面只读接口：ops、admin（契约 3.25）。"""
    roles.require_ops_read(request)


def _write(request: Request, admin_too: bool = False) -> None:
    """运维面写接口：只有 ops（契约 3.25）；契约另说明 admin 也可的接口传 admin_too。"""
    roles.require_ops_write(request, admin_too=admin_too)


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
    return health_checks.disk_usage()


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
            "autobuild": bool(getattr(session, "run_id", None)),
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
    from ..services.autobuild import limits

    autobuild_runs = _autobuild_runs()
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
            "autobuild": {"running": len(autobuild_runs), "limit": limits.max_runs()},
            "cost": {"total": money(total_cost), "this_month": month_usage(now=now)["cost"]},
            "disk": {
                "free_pct": disk["free_pct"], "free_bytes": disk["free"],
                "threshold_pct": DISK_WARN_PCT, "warn": disk["warn"],
            },
        },
        "advice": advice,
        "usage_daily": usage_report(USAGE_RANGES[range]),
        "alerts": [],
        "autobuild_runs": autobuild_runs,
        "autobuild_today": _autobuild_today(now),
        "startup": _startup_summary(),
        "users": users,
        "projects": projects,
    }


def _autobuild_runs() -> list[dict]:
    """O-01 运行中的自主构建（元数据视图，不含项目内容）。"""
    from ..services.autobuild import budget, store, views

    items = []
    for run in store.all_runs(("queued", *store.RUNNING)):
        meta = _meta_or_none(run["project_id"])
        if meta is None:
            continue
        summary = views.summary(run)
        done, total = summary["slices"]["done"], summary["slices"]["total"]
        items.append({
            "project": project_meta_view(meta, deps.workspaces.project_root(run["project_id"])),
            "run_no": run["run_no"], "elapsed_ms": summary["elapsed_ms"],
            "progress": {"done": done, "total": total, "pct": round(done * 100 / total) if total else 0},
            "budget_used_pct": round(max(budget.used_pct(run).values() or [0]), 1),
            "cost": summary["cost"]["used"], "budget": summary["cost"]["budget"], "state": run["state"],
        })
    return items


def _autobuild_today(now: float) -> dict:
    """O-01 今天结束的自主构建：完成数、平均用时与费用、因预算停下的次数。"""
    from ..services.autobuild import budget, store, views

    start = now - (now % 86400)
    ended = [run for run in store.all_runs(store.ENDED) if float(run.get("ended_at") or 0) >= start]
    completed = [run for run in ended if run["state"] == "completed"]
    currency = views.currency_of(ended[0]) if ended else BILLING_CURRENCY
    durations = [budget.elapsed_ms(run) for run in completed]
    costs = [float((run.get("budget") or {}).get("cost") or 0) for run in completed]
    return {"completed": len(completed),
            "avg_duration_ms": int(sum(durations) / len(durations)) if durations else None,
            "avg_cost": {"amount": round(sum(costs) / len(costs), 2) if costs else None, "currency": currency},
            "stopped_by_budget": sum(1 for run in ended if run["state"] == "budget_exhausted")}


# ---- O-13 健康详情 ----

_cli_version_cache: str | None = None
# 就绪探测的状态 → O-13 依赖的状态（契约 O-13 用 ready 表示正常）
_DEPENDENCY_STATUS = {"ok": "ready", "slow": "slow", "warn": "warn", "fail": "fail"}


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


def dependencies(force: bool = False) -> list[dict]:
    """依赖探测结果（与就绪检查同一份探测，缓存 30 秒；O-14 强制重测）。"""
    return [
        {"name": item["name"], "probe": item["probe"], "latency_ms": item["latency_ms"],
         "status": _DEPENDENCY_STATUS.get(item["status"], item["status"]), "last_issue": item["detail"]}
        for item in health_checks.probe(force)
    ]


def last_exit_view() -> dict | None:
    last = lifecycle.last_exit()
    if not last:
        return None
    return {"at": deps.iso(last.get("at")), "reason": last.get("reason"), "code": last.get("code")}


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
    ready_status, _checks = health_checks.readiness()
    deps_status = dependencies()
    passed = sum(1 for item in deps_status if item["status"] == "ready")
    failed = [item["name"] for item in deps_status if item["status"] == "fail"]
    warned = [item["name"] for item in deps_status if item["status"] in ("warn", "slow")]
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
            "uptime_ms": health_checks.uptime_ms(),
            "started_at": deps.iso(health_checks.started_at()),
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
            "last_exit": last_exit_view(),
            "restarts": lifecycle.restarts(),
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
        "last_exit": last_exit_view() if report else None,
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
    """O-37：立即备份（ops，admin 也可）。备份同步打完再回 202（任务中心 O-40 属于以后阶段）。"""
    _write(request, admin_too=True)
    if not _backup_lock.acquire(blocking=False):
        raise ApiError("backup_in_progress", "已有备份在进行", 409)
    try:
        name = await asyncio.to_thread(backup_service.run_backup, deps.workspaces)
    finally:
        _backup_lock.release()
    return JSONResponse(status_code=202, content={"task_id": f"backup-{uuid.uuid4().hex[:12]}", "name": name})


# ---- O-25～O-28 插件运维（O-29 熔断在 ops_control；逻辑在 services/packs/governance.py） ----


def _pack_ops(call, *args, **kwargs):
    from ..services.packs import governance
    from ..services.packs.repository import RepositoryError

    try:
        return call(deps.workspaces, *args, **kwargs)
    except governance.OpsError as exc:
        raise ApiError(exc.code, str(exc), exc.status, exc.details) from exc
    except RepositoryError as exc:
        raise ApiError(exc.code, str(exc), exc.status, exc.details) from exc


@router.get("/packs")
def ops_packs(request: Request):
    """O-25：插件运维列表（通道上的、项目在用的、禁用的版本各一行）。"""
    _read(request)
    from ..services.packs import governance

    return {"items": _pack_ops(governance.ops_items)}


class RolloutBody(BaseModel):
    percent: int
    by: str = "project"
    targets: list[str] = []


@router.put("/packs/{name}/{version}/rollout")
def ops_pack_rollout(name: str, version: str, body: RolloutBody, request: Request):
    """O-26：调灰度（只做按项目；按租户回 501 rollout_not_available）。"""
    _write(request)
    if not 0 <= body.percent <= 100:
        raise ApiError("invalid_argument", "比例是 0–100 的整数", 400, {"fields": {"percent": "range"}})
    from ..services.packs import governance

    item = _pack_ops(governance.set_rollout, name, version, percent=body.percent, by=body.by,
                     targets=body.targets, actor=deps.actor(request))
    audit.log(deps.actor(request), "PUT", request.url.path, {"plugin.rollout": f"{name}@{version}",
                                                             "percent": body.percent, "by": body.by})
    return item


class ChannelRollbackBody(BaseModel):
    channel: str
    to_version: str


@router.post("/packs/{name}/rollback")
def ops_pack_rollback(name: str, body: ChannelRollbackBody, request: Request):
    """O-27：把通道指针改回旧版本。"""
    _write(request)
    if body.channel not in ("stable", "beta"):
        raise ApiError("invalid_argument", "channel 只能是 stable 或 beta", 400, {"fields": {"channel": "enum"}})
    from ..services.packs import governance

    item = _pack_ops(governance.rollback_channel, name, channel=body.channel, to_version=body.to_version,
                     actor=deps.actor(request))
    audit.log(deps.actor(request), "POST", request.url.path, {"plugin.rollback": f"{name}@{body.to_version}",
                                                              "channel": body.channel})
    return item


class DisableVersionBody(BaseModel):
    reason: str = ""


@router.post("/packs/{name}/{version}/disable")
def ops_pack_disable(name: str, version: str, body: DisableVersionBody, request: Request):
    """O-28：禁用版本。"""
    _write(request)
    if not body.reason.strip():
        raise ApiError("invalid_argument", "要写禁用原因", 400, {"fields": {"reason": "required"}})
    from ..services.packs import governance

    item = _pack_ops(governance.disable_version, name, version, reason=body.reason.strip()[:300],
                     actor=deps.actor(request))
    audit.log(deps.actor(request), "POST", request.url.path, {"plugin.disable": f"{name}@{version}"})
    return item


# ---- O-30～O-34 core 默认版本与包仓库备份（包仓库承接原资产包，services/packs/channels.py） ----


def _core_view() -> dict:
    from ..services.packs import channels

    deps.workspaces.packs.core_version()
    view = channels.core_default_view(deps.workspaces.packs.repository)
    view["versions"] = [{**item, "released_at": deps.iso(item["released_at"])} for item in view["versions"]]
    return view


@router.get("/core-default")
def core_default(request: Request):
    """O-30：core 默认版本（stable 通道指向的版本）与包仓库里的全部 core 版本。"""
    _read(request)
    return _core_view()


class RollbackBody(BaseModel):
    version: str


def _set_core_default(version: str, request: Request, *, backup_first: bool) -> dict:
    from ..services.packs import channels
    from ..services.packs.repository import RepositoryError

    if not version or "/" in version or ".." in version:
        raise ApiError("invalid_argument", "版本号不合法", 400, {"fields": {"version": "format"}})
    try:
        channels.set_core_default(deps.workspaces.packs.repository, version, by=deps.actor(request),
                                  backup_first=backup_first)
    except RepositoryError as exc:
        raise ApiError("not_found" if exc.status == 404 else exc.code, str(exc), exc.status) from exc
    return _core_view()


@router.post("/core-default/promote")
def core_default_promote(body: RollbackBody, request: Request):
    """O-31：推广到 stable。各项目仍按自己锁定的版本运行，跟随默认的项目下一条消息生效。"""
    _write(request)
    return _set_core_default(body.version, request, backup_first=False)


@router.post("/core-default/rollback")
def core_default_rollback(body: RollbackBody, request: Request):
    """O-32：回滚默认版本：先自动备份包仓库，再把 stable 指回旧版本（旧版本一直在包仓库里）。"""
    _write(request)
    return _set_core_default(body.version, request, backup_first=True)


@router.get("/pack-repo/backups")
def pack_repo_backups(request: Request):
    """O-33：包仓库备份。"""
    _read(request)
    from ..services.packs import channels

    items = channels.list_backups(deps.workspaces.packs.repository)
    return {"items": [{**item, "created_at": deps.iso(item["created_at"])} for item in items],
            "keep": channels.BACKUP_KEEP}


@router.post("/pack-repo/backups")
def pack_repo_backup_now(request: Request):
    """O-34：立即备份包仓库（同步打完再回 202，任务中心 O-40 以后做）。"""
    _write(request)
    from ..services.packs import channels

    name = channels.backup(deps.workspaces.packs.repository)
    return JSONResponse(status_code=202, content={"task_id": f"pack-backup-{uuid.uuid4().hex[:12]}", "name": name})


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
    """审计记录 → AuditEntry（契约 7.16）。新记录（services/audit.record）已是这个形状；
    旧版记录（user、method、path、detail）按动作名与路径推断类型。"""
    if "actor" in raw:
        ts = raw.get("ts")
        return {
            "id": "aud-" + hashlib.sha256(f"{index}:{ts}".encode()).hexdigest()[:12],
            "ts": deps.iso(ts),
            **{key: raw.get(key) for key in (
                "actor", "actor_kind", "type", "action", "method", "path", "status", "project_id",
                "summary", "trailer", "ip", "trace_id",
            )},
            "summary": raw.get("summary") or "",
        }
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
        kind = classify_path(path)
    project_id = detail.get("project") if isinstance(detail.get("project"), str) else None
    if not project_id and path:
        project_id = project_of_path(path)
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


classify_path = audit.classify_path


def project_of_path(path: str) -> str | None:
    found = _PROJECT_IN_PATH.search(path or "")
    return (found.group(1) or found.group(2)) if found else None


def _split(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def filtered_audit(request: Request, type: str = "", q: str = "") -> list[tuple[int, dict]]:  # noqa: A002
    """按 type（多值）、q（操作者、项目、路径、摘要、动作）、from/to 筛审计，新的在前（O-48、O-49 共用）。

    from 是 Python 关键字，from/to 直接从查询串取。
    """
    params = request.query_params
    types = _split(type)
    unknown = [item for item in types if item not in AUDIT_TYPES]
    if unknown:
        raise ApiError("invalid_argument", "未知的审计类型", 400, {"fields": {"type": ",".join(unknown)}})
    since = deps.parse_time(params["from"], "from") if params.get("from") else None
    until = deps.parse_time(params["to"], "to") if params.get("to") else None
    needle = q.strip().lower()
    matched = []
    for index, raw in audit.read_all():
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
    return matched


@router.get("/audit")
def audit_entries(request: Request, type: str = "", q: str = "", cursor: str = "", limit: int = 0):  # noqa: A002
    """O-48：审计日志，默认每页 100 条。"""
    _read(request)
    matched = filtered_audit(request, type, q)
    params = request.query_params
    binding = {"type": sorted(_split(type)), "q": q.strip().lower(), "from": params.get("from", ""),
               "to": params.get("to", "")}
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
    """O-53：恢复到所有者的项目列表（ops、admin）；原 ID 被占用时换新 ID。"""
    _write(request, admin_too=True)
    try:
        meta = deps.workspaces.restore_trash(entry)
    except WorkspaceError as exc:
        if exc.status == 404:
            raise ApiError("trash_entry_not_found", "回收站条目不存在", 404) from exc
        raise
    return project_meta_view(meta, deps.workspaces.project_root(meta["id"]))
