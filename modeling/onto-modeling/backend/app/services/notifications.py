"""个人偏好与通知中心（契约 A-08、A-13、A-14，用户流 EU-01）。

按用户存在数据目录 `me/<用户>/`：`preferences.json`（整体替换）、`notifications.json`（最新在前，缺省保留 30 天（O-44 可改）、
最多 NOTIFICATIONS_MAX 条）。本地模式（不登录）的用户是空用户名，和用户流同一个频道。

通知由各处调 `notify` 发：存一条、往用户流推 `notification`。同一对话里还没读的同类通知（如一轮跑完）
只留最新一条，免得通知抽屉被刷屏。浏览器系统通知按偏好由前端决定弹不弹。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path

from .. import config

_log = logging.getLogger(__name__)
_LOCK = threading.Lock()

NOTIFICATIONS_MAX = 500
TITLE_MAX = 80
BODY_MAX = 300
KINDS = (
    "autobuild_finished", "autobuild_progress", "autobuild_suspension", "autobuild_stopped", "needs_input",
    "step_done", "session_error", "evidence_conflict", "publish_done", "nomination_reviewed", "request_pending",
    "upload_rejected",
)
DEFAULT_PREFERENCES = {
    "theme": "light",
    "notifications": {
        "system": True,
        "l2_step_done": True, "l2_needs_input": True, "l2_error": True,
        "l4_finished": True, "l4_new_suspension": False, "l4_error_or_stopped": True,
    },
}
_SAFE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class PreferenceError(ValueError):
    def __init__(self, message: str, fields: dict):
        super().__init__(message)
        self.fields = fields


def _dir(username: str) -> Path:
    name = username or "_local"
    if not _SAFE.fullmatch(name) or name in (".", ".."):
        name = "u-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return Path(config.DATA_DIR) / "me" / name


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---- A-08 偏好 ----


def get_preferences(username: str) -> dict:
    stored = _read(_dir(username) / "preferences.json", {})
    return normalize(stored if isinstance(stored, dict) else {})


def normalize(raw: dict) -> dict:
    notifications = raw.get("notifications") if isinstance(raw.get("notifications"), dict) else {}
    return {
        "theme": raw.get("theme") if raw.get("theme") in ("light", "dark") else DEFAULT_PREFERENCES["theme"],
        "notifications": {key: notifications[key] if isinstance(notifications.get(key), bool) else value
                          for key, value in DEFAULT_PREFERENCES["notifications"].items()},
    }


def save_preferences(username: str, body: dict) -> dict:
    """PUT 整体替换：只认契约里的键，缺的按默认，取值不对 400。"""
    if body.get("theme") not in (None, "light", "dark"):
        raise PreferenceError("theme 只能是 light 或 dark", {"theme": "enum"})
    notifications = body.get("notifications")
    if notifications is not None and not isinstance(notifications, dict):
        raise PreferenceError("notifications 要是对象", {"notifications": "object"})
    bad = {f"notifications.{key}": "bool" for key, value in (notifications or {}).items()
           if key in DEFAULT_PREFERENCES["notifications"] and not isinstance(value, bool)}
    if bad:
        raise PreferenceError("通知开关要是 true 或 false", bad)
    prefs = normalize(body)
    with _LOCK:
        _write(_dir(username) / "preferences.json", prefs)
    return prefs


# ---- A-13、A-14 通知 ----


def _load(username: str, now: float) -> list[dict]:
    items = _read(_dir(username) / "notifications.json", [])
    if not isinstance(items, list):
        return []
    keep = retention_seconds()
    return [item for item in items if isinstance(item, dict) and now - float(item.get("at") or 0) < keep]


def retention_seconds() -> int:
    """通知保留期：运维在数据保留里改过就用改过的（O-44），缺省 30 天。"""
    from . import ops_center

    return ops_center.retention_value("notifications") * 86400


def _view(item: dict) -> dict:
    from ..api import deps

    return {key: item.get(key) for key in ("id", "kind", "title", "body", "project_id", "project_name", "action", "read")} | {
        "created_at": deps.iso(item.get("at")),
    }


def unread_count(username: str) -> int:
    with _LOCK:
        return sum(1 for item in _load(username, time.time()) if not item.get("read"))


def list_notifications(username: str, *, unread_only: bool = False) -> list[dict]:
    with _LOCK:
        items = _load(username, time.time())
    return [_view(item) for item in items if not unread_only or not item.get("read")]


def mark_read(username: str, ids: list[str] | None = None, *, all_items: bool = False) -> int:
    wanted = set(ids or [])
    with _LOCK:
        items = _load(username, time.time())
        for item in items:
            if all_items or item.get("id") in wanted:
                item["read"] = True
        _write(_dir(username) / "notifications.json", items)
        return sum(1 for item in items if not item.get("read"))


def _project_name(project_id: str | None) -> str | None:
    if not project_id:
        return None
    meta = _read(Path(config.DATA_DIR) / "workspaces" / project_id / ".onto-web.json", {})
    return str(meta.get("name") or "") or None if isinstance(meta, dict) else None


def notify(username: str, kind: str, title: str, body: str = "", *, project_id: str | None = None,
           action: dict | None = None, group: str | None = None) -> dict | None:
    """存一条通知并推到用户流（EU-01）。group 相同、还没读的旧通知被这一条替换（一轮跑完、出错这类）。

    username 用项目所有者或账号名；本地模式（不登录）记 `local` 的也归到空用户名。出错只记日志。"""
    from . import auth, streams

    if kind not in KINDS:
        raise ValueError(f"通知类型 {kind} 不在契约里")
    if not auth.ENABLED and username == "local":
        username = ""
    now = time.time()
    item = {
        "id": f"ntf-{uuid.uuid4().hex[:12]}", "kind": kind, "title": title[:TITLE_MAX], "body": body[:BODY_MAX],
        "project_id": project_id, "project_name": _project_name(project_id), "action": action, "read": False,
        "at": now, **({"group": group} if group else {}),
    }
    try:
        with _LOCK:
            items, replaced = [], []
            for old in _load(username, now):
                if group and old.get("group") == group and old.get("kind") == kind and not old.get("read"):
                    replaced.append(old["id"])
                else:
                    items.append(old)
            items.insert(0, item)
            _write(_dir(username) / "notifications.json", items[:NOTIFICATIONS_MAX])
        view = _view(item)
        # replaces：这一条替换掉的同组未读通知，前端照着从列表里拿掉、未读数不重复算
        streams.publish_user(username, "notification", {**view, "replaces": replaced}, project_id=project_id)
        return view
    except Exception:  # noqa: BLE001 - 通知是附带的，失败不影响原操作
        _log.warning("给 %s 发通知失败", username or "本地用户", exc_info=True)
        return None


def forget_user(username: str) -> None:
    """删除用户时清掉他的偏好与通知。"""
    import shutil

    shutil.rmtree(_dir(username), ignore_errors=True)


# ---- 会话里的通知来源：一轮跑完、要你回答、出错（偏好里的 l2_*） ----

SESSION_EVENT_TYPES = ("result", "question_asked", "permission_request", "session_error")
_SESSION_KINDS = {
    "question_asked": ("needs_input", "需要你回答", "去回答"),
    "permission_request": ("needs_input", "需要你确认", "去确认"),
    "session_error": ("session_error", "构建出错", "去对话"),
}


def session_event(project_id: str, conversation_id: str, event: dict, *, owner: str) -> None:
    """会话宿主每条持久事件调一次：只挑通知要的几类。"""
    kind_type = event.get("type")
    if kind_type == "result":
        if event.get("is_error") or event.get("subtype") not in (None, "success"):
            return
        kind, title, label = "step_done", "一步跑完", "去对话"
    elif kind_type in _SESSION_KINDS:
        kind, title, label = _SESSION_KINDS[kind_type]
    else:
        return
    notify(owner, kind, title, _project_name(project_id) or project_id, project_id=project_id,
           action={"label": label, "page": "wb-l2", "params": {"conversation": conversation_id}},
           group=f"{project_id}:{conversation_id}")
