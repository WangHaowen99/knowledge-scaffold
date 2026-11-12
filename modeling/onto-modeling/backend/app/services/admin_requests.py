"""管理员审核申请（契约 AD-08～AD-10）：数据源登记申请（EV-03）和知识提名（K-08）合成一个列表。

- 数据源登记申请存在 `evidence/requests/esr-*.json`（kind=register；EV-30 的「通知管理员」不是审核，不列）；
  通过 = 管理员补写凭据后按申请登记（EV-02 同一套校验），授权给申请人的项目，再记成已通过；
- 知识提名存在 `knowledge/nominations/nom-*.json`；通过、退回只记结论并通知提名人，进行业包下一版仍由行业包维护人做；
- 插件安装申请（PL-04）还没有，列表里不会出现；
- 通过、退回都通知申请人。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from ..api import deps
from ..api.errors import ApiError

KINDS = ("pack_install", "source_registration", "knowledge_nomination")
STATES = ("pending", "approved", "rejected")
REASON_MAX = 500
PAGE_MAX = 200
_NOMINATION_STATES = {"pending_review": "pending", "approved": "approved", "rejected": "rejected"}
_nomination_lock = threading.Lock()


def _project_name(project_id: str | None) -> str | None:
    if not project_id:
        return None
    try:
        return deps.workspaces.get_project(project_id).get("name")
    except Exception:  # noqa: BLE001 - 项目删了：只显示 ID
        return None


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------


_SOURCE_FIELDS = ("id", "display_name", "provider", "environment", "connection", "scope_allowlist", "egress",
                  "access_window")


def _source_view(record: dict) -> dict:
    form = record.get("body") or {}
    project_id = record.get("project_id") or None
    payload = {key: form.get(key) for key in _SOURCE_FIELDS}
    return {
        "id": record["id"], "kind": "source_registration", "state": record.get("state") or "pending",
        "requested_by": record.get("by") or "", "requested_at": deps.iso(record.get("at")),
        "project_id": project_id, "summary": form.get("display_name") or form.get("id") or "",
        "payload": {**payload, "note": form.get("authorization_note") or None,
                    "project_name": _project_name(project_id)},
        "note": record.get("note"), "reviewed_by": record.get("reviewed_by"),
        "reviewed_at": deps.iso(record.get("reviewed_at")), "source_id": record.get("source_id"),
    }


def _nomination_view(record: dict) -> dict:
    text = str(record.get("sanitized_text") or "")
    first = next((line.strip("# ").strip() for line in text.splitlines() if line.strip()), "")
    project_id = record.get("project_id") or None
    return {
        "id": record["nomination_id"], "kind": "knowledge_nomination",
        "state": _NOMINATION_STATES.get(record.get("state"), "pending"),
        "requested_by": record.get("submitted_by") or "", "requested_at": deps.iso(record.get("submitted_at")),
        "project_id": project_id, "summary": first[:60] or record.get("knowledge_id") or "",
        "payload": {"knowledge_id": record.get("knowledge_id"), "industry_pack": record.get("industry_pack"),
                    "sanitized_text": text, "project_name": _project_name(project_id)},
        "note": record.get("review_note"), "reviewed_by": record.get("reviewed_by"),
        "reviewed_at": deps.iso(record.get("reviewed_at")), "source_id": None,
    }


def _all() -> list[dict]:
    from ..evidence import sources
    from . import knowledge_layers

    items = [_source_view(record) for record in sources.all_requests("register")]
    items += [_nomination_view(record) for record in knowledge_layers.list_nominations()]
    return sorted(items, key=lambda item: item.get("requested_at") or "", reverse=True)


def list_requests(kind: str = "", state: str = "", limit: int = 50, cursor: str = "") -> dict:
    """AD-08：kind、state 可多值（逗号分隔）；cursor 是下一页起点。"""
    kinds = [item for item in kind.split(",") if item]
    states = [item for item in state.split(",") if item]
    if any(item not in KINDS for item in kinds):
        raise ApiError("invalid_argument", "kind 不合法", 400, {"fields": {"kind": "enum"}})
    if any(item not in STATES for item in states):
        raise ApiError("invalid_argument", "state 不合法", 400, {"fields": {"state": "enum"}})
    try:
        start = int(cursor or 0)
    except ValueError:
        raise ApiError("invalid_argument", "cursor 不合法", 400, {"fields": {"cursor": "invalid"}}) from None
    limit = max(1, min(int(limit or 50), PAGE_MAX))
    items = [item for item in _all()
             if (not kinds or item["kind"] in kinds) and (not states or item["state"] in states)]
    page = items[start:start + limit]
    return {"items": page, "next_cursor": str(start + limit) if start + limit < len(items) else None}


# ---------------------------------------------------------------------------
# 通过、退回
# ---------------------------------------------------------------------------


def _notify(username: str, kind: str, title: str, body: str, project_id: str | None, action: dict | None) -> None:
    from . import notifications

    try:
        notifications.notify(username or "local", kind, title, body, project_id=project_id, action=action)
    except Exception:  # noqa: BLE001 - 通知是附带信息，结论已经存下
        pass


def _reason(text) -> str:
    reason = str(text or "").strip()
    if not reason:
        raise ApiError("invalid_argument", "请写明退回原因", 400, {"fields": {"reason": "required"}})
    if len(reason) > REASON_MAX:
        raise ApiError("invalid_argument", f"退回原因最多 {REASON_MAX} 字", 400, {"fields": {"reason": "too_long"}})
    return reason


def _source_request(request_id: str) -> dict:
    from ..evidence import sources

    record = sources.get_request(request_id)
    if record is None or record.get("kind") != "register":
        raise ApiError("request_not_found", "申请不存在", 404)
    if record.get("state") != "pending":
        raise ApiError("request_state", "这条申请已经处理过了", 409, {"state": record.get("state")})
    return record


def _approve_source(request_id: str, body: dict, actor: str) -> dict:
    """先按申请登记（带上管理员写的凭据），授权给申请人的项目，再记成已通过。登记的校验和 EV-02 一样，没过回 400。"""
    from ..evidence import grants, sources

    record = _source_request(request_id)
    form = dict(record.get("body") or {})
    edits = body.get("edits") if isinstance(body.get("edits"), dict) else {}
    form.update({key: value for key, value in edits.items() if key not in ("id", "provider", "secret", "owner")})
    info = sources._provider_info(str(form.get("provider") or ""))
    needs_secret = sources.connection_type(info) and any(field.get("secret") for field in info.get("cred_fields") or [])
    secret = body.get("secret") if isinstance(body.get("secret"), dict) else None
    if needs_secret and not (secret and any(str(value or "").strip() for value in secret.values())):
        raise ApiError("invalid_argument", "通过前要写入凭据", 400, {"fields": {"secret": "required"}})
    registration = sources.register({**form, "owner": form.get("owner") or record.get("by") or actor,
                                     **({"secret": secret} if secret else {})}, user=actor, admin=True)
    project_id = record.get("project_id") or None
    granted = False
    if project_id and _project_name(project_id) is not None:
        grants.set_grant(project_id, registration["id"], True, by=actor)
        granted = True
        try:
            from . import project_events

            project_events.schedule_flow_refresh(project_id)
        except Exception:  # noqa: BLE001 - 推送是增强信息
            pass
    decided = sources.decide_request(request_id, "approved", actor, str(body.get("note") or "").strip()[:REASON_MAX],
                                     source_id=registration["id"])
    name = form.get("display_name") or registration["id"]
    _notify(record.get("by") or "", "request_reviewed", "数据源登记申请已通过",
            f"「{name}」已登记" + ("，已授权给本项目" if granted else ""),
            project_id if granted else None, {"label": "去看看", "page": "sources"} if granted else None)
    return _source_view(decided)


def _reject_source(request_id: str, reason: str, actor: str) -> dict:
    from ..evidence import sources

    record = _source_request(request_id)
    decided = sources.decide_request(request_id, "rejected", actor, reason)
    name = (record.get("body") or {}).get("display_name") or ""
    project_id = record.get("project_id") or None
    _notify(record.get("by") or "", "request_reviewed", "数据源登记申请被退回", f"「{name}」：{reason}",
            project_id, {"label": "去看看", "page": "sources"} if project_id else None)
    return _source_view(decided)


def _nomination_path(nomination_id: str) -> Path:
    from . import knowledge_layers

    return knowledge_layers.nominations_dir() / f"{nomination_id}.json"


def _decide_nomination(nomination_id: str, state: str, actor: str, note: str) -> dict:
    path = _nomination_path(nomination_id)
    with _nomination_lock:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ApiError("request_not_found", "申请不存在", 404) from None
        current = _NOMINATION_STATES.get(record.get("state"), "pending")
        if current != "pending":
            raise ApiError("request_state", "这条申请已经处理过了", 409, {"state": current})
        record.update(state=state, reviewed_by=actor, reviewed_at=time.time(), review_note=note or None)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    view = _nomination_view(record)
    title = "知识提名已通过" if state == "approved" else "知识提名被退回"
    _notify(record.get("submitted_by") or "", "nomination_reviewed", title,
            view["summary"] + (f"：{note}" if state == "rejected" and note else ""), record.get("project_id"),
            {"label": "去看看", "page": "knowledge"} if record.get("project_id") else None)
    return view


def _is_nomination(request_id: str) -> bool:
    return str(request_id).startswith("nom-") and "/" not in request_id and ".." not in request_id


def approve(request_id: str, body: dict, actor: str) -> dict:
    """AD-09。"""
    body = body if isinstance(body, dict) else {}
    if _is_nomination(request_id):
        return _decide_nomination(request_id, "approved", actor, str(body.get("note") or "").strip()[:REASON_MAX])
    return _approve_source(request_id, body, actor)


def reject(request_id: str, body: dict, actor: str) -> dict:
    """AD-10。"""
    reason = _reason((body or {}).get("reason") if isinstance(body, dict) else None)
    if _is_nomination(request_id):
        return _decide_nomination(request_id, "rejected", actor, reason)
    return _reject_source(request_id, reason, actor)
