"""会话宿主的接口投影（契约 7.7）：Conversation、PoolStatus，接口层与项目流（EP-21）共用。"""

from __future__ import annotations

from datetime import datetime, timezone

# 会话层状态 → 契约 ConversationState.state
STATE = {"connecting": "connecting", "running": "running", "idle": "idle", "closed": "closed",
         "error": "error", "queued": "queued", "waiting_user": "waiting_user"}


def iso(value) -> str | None:
    if value in (None, "", 0):
        return None
    moment = datetime.fromtimestamp(float(value), timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def pool() -> dict:
    from ..agent_sessions import manager

    status = manager.pool_status()
    return {"max": status["max"], "active": status["active"], "queued": len(status["waiting"]),
            "idle_ttl_s": status["idle_ttl_seconds"]}


def conversation_view(view: dict, live: dict | None) -> dict:
    is_live = bool(live and live["conversation_id"] == view["id"])
    return {
        "id": view["id"], "title": view["title"], "title_manual": view["title_manual"],
        "created_at": iso(view["created_at"]), "updated_at": iso(view["updated_at"]),
        "live": is_live, "state": STATE.get(live["state"], live["state"]) if is_live else "idle",
        "kind": view.get("kind") or "chat", "run_id": view.get("run_id"), "plugin": view.get("plugin"),
    }


def conversations(project_id: str) -> list[dict]:
    from .. import conversation_store
    from ..agent_sessions import manager

    live = manager.live_status(project_id)
    return [conversation_view(view, live) for view in conversation_store.list_conversations(project_id)]


def conversation(project_id: str, conversation_id: str) -> dict | None:
    return next((item for item in conversations(project_id) if item["id"] == conversation_id), None)
