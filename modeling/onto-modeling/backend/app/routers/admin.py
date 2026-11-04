import json
import os
import shutil
import tarfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import config
from ..services import audit, auth, conversation_store, error_log, modeling_cleanup
from ..services.agent_sessions import manager, usage_summary
from ..services.auth import AuthError
from .projects import workspaces

router = APIRouter(prefix="/api/admin", tags=["admin"])

ONLINE_WINDOW_SECONDS = 600
MAX_SIZE_SCAN_FILES = 20000


def _require_admin(request: Request) -> None:
    if not auth.ENABLED:
        return  # 本地开发未开鉴权时放开
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        raise AuthError("需要管理员权限", 403)


def _workspace_size(root: Path) -> int:
    total = 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for filename in filenames:
            count += 1
            if count > MAX_SIZE_SCAN_FILES:
                return total
            try:
                total += (Path(dirpath) / filename).stat().st_size
            except OSError:
                continue
    return total


@router.get("/overview")
def overview(request: Request):
    _require_admin(request)
    now = time.time()

    disk = shutil.disk_usage(config.DATA_DIR)
    try:
        load = os.getloadavg()
    except OSError:
        load = (0, 0, 0)

    recent_users = sorted(auth.LAST_SEEN.items(), key=lambda item: -item[1])
    users = [
        {
            "username": username,
            "last_seen": last_seen,
            "online": now - last_seen < ONLINE_WINDOW_SECONDS,
        }
        for username, last_seen in recent_users
    ]

    sessions = [
        {
            "project_id": pid,
            "conversation_id": s.conversation_id,
            "state": s.state,
            "events": len(s.events),
            "connected": s.client is not None,
            "last_activity": s.last_activity,
        }
        for pid, s in manager._sessions.items()
    ]

    projects = []
    total_cost = 0.0
    for meta in workspaces.list_projects():
        usage = usage_summary(meta["id"])
        total_cost += usage["total_cost_usd"]
        root = workspaces.project_root(meta["id"])
        projects.append(
            {
                **meta,
                "usage": usage,
                "size_bytes": _workspace_size(root),
            }
        )

    try:
        from importlib.metadata import version as pkg_version

        sdk_version = pkg_version("claude-agent-sdk")
    except Exception:
        sdk_version = ""

    return {
        "system": {
            "disk_total": disk.total,
            "disk_free": disk.free,
            "load": load,
            "sdk_version": sdk_version,
        },
        "users": users,
        "online_count": sum(1 for u in users if u["online"]),
        "sessions": sessions,
        "session_pool": manager.pool_status(),
        "projects": projects,
        "total_cost_usd": round(total_cost, 4),
        "errors": error_log.read(100),
        "usage_daily": _usage_daily(),
        "backups": _list_backups(),
        "user_admin_available": bool(config.AUTH_ADMIN_TOKEN and auth.ENABLED),
    }


def _usage_daily(days: int = 14) -> list[dict]:
    """按日聚合全部项目的模型用量（读会话事件历史）。"""
    buckets: dict[str, dict] = {}
    sessions_dir = Path(config.DATA_DIR) / "sessions"
    if sessions_dir.is_dir():
        # 递归：多会话的历史与已删除会话留存的用量都在 sessions/{项目}/ 下
        for history_file in sessions_dir.rglob("*.jsonl"):
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
                day = datetime.fromtimestamp(event["ts"]).strftime("%Y-%m-%d")
                bucket = buckets.setdefault(
                    day, {"date": day, "cost": 0.0, "output_tokens": 0, "calls": 0}
                )
                if event.get("type") == "result":
                    bucket["cost"] += event.get("total_cost_usd") or 0
                    bucket["calls"] += 1
                bucket["output_tokens"] += usage.get("output_tokens") or 0
    series = sorted(buckets.values(), key=lambda b: b["date"], reverse=True)[:days]
    for bucket in series:
        bucket["cost"] = round(bucket["cost"], 4)
    return series


BACKUP_KEEP = 3


def _backups_dir() -> Path:
    return Path(config.DATA_DIR) / "backups"


def _list_backups() -> list[dict]:
    directory = _backups_dir()
    if not directory.is_dir():
        return []
    return [
        {"name": p.name, "size": p.stat().st_size, "ts": p.stat().st_mtime}
        for p in sorted(directory.glob("*.tar.gz"), reverse=True)
    ]


def run_backup() -> str:
    """打包全部 workspace（保留最近 BACKUP_KEEP 份）；手动按钮与定时调度共用。"""
    directory = _backups_dir()
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / f"workspaces-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(workspaces.workspaces_dir, arcname="workspaces")
        from ..services import knowledge_hub
        tasks = Path(config.DATA_DIR) / 'hub-tasks'
        if tasks.exists():
            for project in sorted(tasks.iterdir()):
                if project.is_dir() and project.name != 'cleanup':
                    with knowledge_hub.lock(project.name):
                        if project.exists():
                            tar.add(project, arcname=f'hub-tasks/{project.name}')
            if (tasks / 'cleanup').exists():
                tar.add(tasks / 'cleanup', arcname='hub-tasks/cleanup')
    for old in sorted(directory.glob("*.tar.gz"), reverse=True)[BACKUP_KEEP:]:
        old.unlink(missing_ok=True)
    return dest.name


async def backup_scheduler_loop():
    """定期备份（ONTO_WEB_BACKUP_INTERVAL_HOURS，0 关闭）；打包放线程避免卡事件循环。"""
    import asyncio

    interval = config.BACKUP_INTERVAL_HOURS
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval * 3600)
        try:
            await asyncio.to_thread(run_backup)
        except Exception:
            pass  # 单次失败不终止调度


@router.post("/backup")
def backup(request: Request):
    _require_admin(request)
    return {"created": run_backup(), "backups": _list_backups()}


@router.post("/sessions/{project_id}/recycle")
async def recycle_session(project_id: str, request: Request):
    """手动回收某项目的会话子进程（续接点保留，用户下次发消息自动恢复）。"""
    _require_admin(request)
    recycled = await manager.recycle_project(project_id)
    return {"recycled": recycled, "pool": manager.pool_status()}


_cli_version_cache: str | None = None


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
        result = subprocess.run(
            [cli, "--version"], capture_output=True, text=True, timeout=15
        )
        _cli_version_cache = result.stdout.strip()[:80] if result.returncode == 0 else ""
    except Exception:
        _cli_version_cache = ""
    return _cli_version_cache


@router.get("/health")
def health_detail(request: Request):
    """健康页：SDK/CLI 版本、磁盘水位（含告警）、负载、会话池、备份调度。"""
    _require_admin(request)
    disk = shutil.disk_usage(config.DATA_DIR)
    free_pct = disk.free / disk.total * 100 if disk.total else 0
    try:
        load = os.getloadavg()
    except OSError:
        load = (0, 0, 0)
    try:
        from importlib.metadata import version as pkg_version

        sdk_version = pkg_version("claude-agent-sdk")
    except Exception:
        sdk_version = ""
    backups = _list_backups()
    return {
        "sdk_version": sdk_version,
        "cli_version": _cli_version(),
        "disk": {
            "total": disk.total,
            "free": disk.free,
            "free_pct": round(free_pct, 1),
            # 磁盘水位告警：可用低于 15% 或 10G
            "warn": free_pct < 15 or disk.free < 10 * 1024**3,
        },
        "load": load,
        "pool": manager.pool_status(),
        "backup": {
            "interval_hours": config.BACKUP_INTERVAL_HOURS,
            "last": backups[0] if backups else None,
        },
    }


@router.get("/trash")
def list_trash(request: Request):
    _require_admin(request)
    return {"entries": workspaces.list_trash()}


class RestoreBody(BaseModel):
    name: str


@router.post("/trash/restore")
def restore_trash(request: Request, body: RestoreBody):
    _require_admin(request)
    return workspaces.restore_trash(body.name)


@router.get("/audit")
def audit_log(request: Request):
    _require_admin(request)
    return {"entries": audit.read(200)}


# ---- 用户管理（代理 auth-server Admin API）----


class UserUpdateBody(BaseModel):
    password: str | None = None
    role: str | None = None


def _auth_admin_request(method: str, path: str, payload: dict | None = None):
    if not config.AUTH_ADMIN_TOKEN:
        raise AuthError("未配置认证服务管理令牌", 503)
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{auth.AUTH_URL}{path}",
        data=data,
        method=method,
        headers={
            # auth-server 管理 API 以 X-Admin-Token 头鉴权
            "X-Admin-Token": config.AUTH_ADMIN_TOKEN,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {"error": "认证服务返回异常"}
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AuthError("认证服务不可达", 502) from exc


@router.get("/users")
def list_users(request: Request):
    _require_admin(request)
    status_code, data = _auth_admin_request("GET", "/admin/users")
    return JSONResponse(status_code=status_code, content=data)


@router.put("/users/{username}")
def update_user(username: str, body: UserUpdateBody, request: Request):
    _require_admin(request)
    status_code, data = _auth_admin_request(
        "PUT", f"/admin/users/{username}", body.model_dump(exclude_none=True)
    )
    return JSONResponse(status_code=status_code, content=data)


@router.delete("/users/{username}")
def delete_user(username: str, request: Request):
    _require_admin(request)
    status_code, data = _auth_admin_request("DELETE", f"/admin/users/{username}")
    if 200 <= status_code < 300:
        from ..services import personal_model_settings

        personal_model_settings.delete_user(username)
    return JSONResponse(status_code=status_code, content=data)


@router.get("/modeling-cleanups")
def list_modeling_cleanups(request: Request):
    _require_admin(request)
    return {"tasks": modeling_cleanup.list_tasks()}


@router.post("/modeling-cleanups/{task_id}/retry")
def retry_modeling_cleanup(task_id: str, request: Request):
    _require_admin(request)
    if not modeling_cleanup.retry_task(task_id):
        return JSONResponse(status_code=404, content={"error": "清理任务不存在"})
    return {"queued": True, "id": task_id}
