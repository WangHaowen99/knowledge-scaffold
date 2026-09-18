import os
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Request

from .. import config
from ..services import auth, error_log
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

    return {
        "system": {
            "disk_total": disk.total,
            "disk_free": disk.free,
            "load": load,
        },
        "users": users,
        "online_count": sum(1 for u in users if u["online"]),
        "sessions": sessions,
        "projects": projects,
        "total_cost_usd": round(total_cost, 4),
        "errors": error_log.read(100),
    }
