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
from ..services import audit, auth, error_log
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

    users = [
        {
            "username": username,
            "last_seen": last_seen,
            "online": now - last_seen < ONLINE_WINDOW_SECONDS,
        }
        for username, last_seen in sorted(
            auth.LAST_SEEN.items(), key=lambda item: -item[1]
        )
    ]

    sessions = [
        {"project_id": pid, "state": s.state, "events": len(s.events)}
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
        for history_file in sessions_dir.glob("*.jsonl"):
            try:
                lines = history_file.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                if '"result"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "result" or not event.get("ts"):
                    continue
                day = datetime.fromtimestamp(event["ts"]).strftime("%Y-%m-%d")
                bucket = buckets.setdefault(
                    day, {"date": day, "cost": 0.0, "output_tokens": 0, "calls": 0}
                )
                bucket["cost"] += event.get("total_cost_usd") or 0
                bucket["output_tokens"] += (event.get("usage") or {}).get(
                    "output_tokens"
                ) or 0
                bucket["calls"] += 1
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


@router.post("/backup")
def backup(request: Request):
    _require_admin(request)
    directory = _backups_dir()
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / f"workspaces-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(workspaces.workspaces_dir, arcname="workspaces")
    for old in sorted(directory.glob("*.tar.gz"), reverse=True)[BACKUP_KEEP:]:
        old.unlink(missing_ok=True)
    return {"created": dest.name, "backups": _list_backups()}


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
    return JSONResponse(status_code=status_code, content=data)
