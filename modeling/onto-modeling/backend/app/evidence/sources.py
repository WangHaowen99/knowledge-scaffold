"""数据源登记与申请（契约 EV-01～EV-05、EV-03、EV-30；`design/evidence-collection.md` §6「自助登记」）。

登记存 `DATA_DIR/evidence/sources.json`（平台级，不在工作区）。只存凭据引用和不敏感的连接参数。

- 管理员可以登记任何已安装的提供方；
- 提供方 `self_service=true`（不要凭据、不连业务网络，如 REA）时登录用户也能登记，`owner` 固定为本人，登记只有本人和
  管理员看得到，本人才能授权给自己的项目；
- 别人登记的自助类数据源不出现在列表里，也不能授权。
"""

from __future__ import annotations

import re
import threading
import time
import uuid

from ..api.errors import ApiError
from . import grants, storage
from .registry import SOURCE_ID_PATTERN

_lock = threading.Lock()
SOURCE_ID = re.compile(SOURCE_ID_PATTERN)
NAME_MAX = 80
NOTE_MAX = 2000
ENVIRONMENTS = ("production", "test")
# 自助类（REA）的缺省：任务说明里的预算按 spec §4 的例子
SELF_SERVICE_BUDGET = {"max_rows": 200000, "max_bytes": 268435456, "max_duration_s": 1800}
SELF_SERVICE_RATE = {"concurrency": 1, "qps": 1, "query_timeout_s": 1800}
ARTIFACT_KEYS = ("artifact_sha256", "artifact_filename", "artifact_size")
# 登记后不能改的字段
FIXED = ("id", "provider", "owner", "self_service", "registered_by_admin", "created_at")


def _path():
    return storage.root() / "sources.json"


def _load() -> list[dict]:
    data = storage.read_json(_path(), {}) or {}
    return [dict(item) for item in data.get("items") or [] if isinstance(item, dict) and item.get("id")]


def _save(items: list[dict]) -> None:
    storage.write_json(_path(), {"items": items})


def all_sources() -> list[dict]:
    return _load()


def get(source_id: str) -> dict | None:
    return next((item for item in _load() if item["id"] == source_id), None)


def visible(registration: dict, user: str, admin: bool) -> bool:
    return admin or registration.get("registered_by_admin", True) or registration.get("owner") == user


def can_manage(registration: dict, user: str, admin: bool) -> bool:
    """EV-04、EV-05：管理员，或自助类数据源的登记人。"""
    return admin or bool(registration.get("self_service") and registration.get("owner") == user)


def can_grant(registration: dict, user: str, admin: bool) -> bool:
    """授权给自己的项目：管理员登记的谁都能授权（项目所有者自己决定），自助登记的只有登记人。"""
    return bool(registration.get("registered_by_admin", True) or registration.get("owner") == user)


def require_visible(source_id: str, user: str, admin: bool) -> dict:
    registration = get(source_id)
    if registration is None or not visible(registration, user, admin):
        raise ApiError("source_not_found", "数据源不存在", 404)
    return registration


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _invalid(field: str, reason: str, message: str = "") -> ApiError:
    return ApiError("invalid_argument", message or f"{field} 不合法", 400, {"fields": {field: reason}})


def _positive_ints(value, field: str, keys: tuple[str, ...]) -> dict:
    if not isinstance(value, dict):
        raise _invalid(field, "object")
    out = {}
    for key in keys:
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, (int, float)) or item < 0:
            raise _invalid(f"{field}.{key}", "non_negative_number")
        out[key] = int(item) if isinstance(item, int) or float(item).is_integer() else float(item)
    return out


def _strings(value, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise _invalid(field, "string_list")
    return [item.strip() for item in value][:500]


def _egress(value) -> list[dict]:
    if not isinstance(value, list):
        raise _invalid("egress", "list")
    out = []
    for item in value:
        port = item.get("port") if isinstance(item, dict) else None
        if not isinstance(item, dict) or not str(item.get("host") or "").strip() or isinstance(port, bool) \
                or not isinstance(port, int) or not 0 < port < 65536:
            raise _invalid("egress", "host_port")
        out.append({"host": str(item["host"]).strip(), "port": port})
    return out


def _window(value) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _invalid("access_window", "object")
    clock = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
    days = value.get("days", "daily")
    if not (days == "daily" or isinstance(days, list) and all(isinstance(day, str) for day in days)):
        raise _invalid("access_window.days", "daily_or_list")
    if not clock.match(str(value.get("start") or "")) or not clock.match(str(value.get("end") or "")):
        raise _invalid("access_window", "hh_mm")
    return {"days": days, "start": value["start"], "end": value["end"], "tz": str(value.get("tz") or "UTC")}


def _artifact_connection(value) -> dict:
    if not isinstance(value, dict):
        raise _invalid("connection", "object")
    sha256 = str(value.get("artifact_sha256") or "").lower()
    size = storage.blob_size(sha256)
    if size is None:
        raise ApiError("artifact_not_found", "目标程序还没上传完", 422)
    filename = str(value.get("artifact_filename") or "").strip()[:200] or sha256[:12]
    return {"artifact_sha256": sha256, "artifact_filename": filename, "artifact_size": size}


def _connection(value) -> dict:
    if not isinstance(value, dict):
        raise _invalid("connection", "object")
    out = {}
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise _invalid(f"connection.{key}", "scalar")
        out[str(key)] = item
    return out


def _clean(body: dict, info: dict, *, creating: bool, current: dict | None = None) -> dict:
    """按提供方校验登记字段；更新时只校验带上的字段。"""
    self_service = bool(info.get("self_service"))
    artifact_input = info.get("input") == "artifact"
    out: dict = {}

    def given(key: str) -> bool:
        return key in body and (creating or body[key] is not None or key == "access_window")

    if creating or given("display_name"):
        name = str(body.get("display_name") or "").strip()
        if not name or len(name) > NAME_MAX:
            raise _invalid("display_name", "required", f"显示名必填，最多 {NAME_MAX} 字")
        out["display_name"] = name
    if creating or given("environment"):
        environment = body.get("environment") or ("test" if self_service and creating else None)
        if environment not in ENVIRONMENTS:
            raise _invalid("environment", "enum")
        out["environment"] = environment
    if creating or given("connection"):
        out["connection"] = _artifact_connection(body.get("connection")) if artifact_input \
            else _connection(body.get("connection") if body.get("connection") is not None else {})
    for key, parse, default in (
        ("scope_allowlist", lambda value: _strings(value, "scope_allowlist"), []),
        ("egress", _egress, []),
        ("budget", lambda value: _positive_ints(value, "budget", ("max_rows", "max_bytes", "max_duration_s")),
         dict(SELF_SERVICE_BUDGET)),
        ("rate_limit", lambda value: _positive_ints(value, "rate_limit", ("concurrency", "qps", "query_timeout_s")),
         dict(SELF_SERVICE_RATE)),
    ):
        if given(key) and body.get(key) is not None:
            out[key] = parse(body[key])
        elif creating:
            if not self_service:
                raise _invalid(key, "required")
            out[key] = default
    if given("access_window"):
        out["access_window"] = _window(body.get("access_window"))
    elif creating:
        out["access_window"] = None
    if given("masking_policy") or creating:
        policy = str(body.get("masking_policy") or ("none" if self_service else "")).strip()
        if not policy:
            raise _invalid("masking_policy", "required")
        out["masking_policy"] = policy[:40]
    if given("authorization_note") or creating:
        note = str(body.get("authorization_note") or "").strip()[:NOTE_MAX] or None
        out["authorization_note"] = note
    note = out.get("authorization_note", (current or {}).get("authorization_note"))
    if info.get("kind") == "rea" and not note:
        raise _invalid("authorization_note", "required", "逆向类数据源要写软件授权说明")
    return out


def _provider_info(provider_id: str) -> dict:
    from . import collector

    providers = collector.catalog()
    if providers is None:
        raise ApiError("gateway_unavailable", "连接器网关不可达", 503)
    info = next((item for item in providers if item.get("id") == provider_id), None)
    if info is None or not info.get("installed", True):
        raise ApiError("provider_not_installed", "提供方没有安装", 409, {"provider": provider_id})
    return info


# ---------------------------------------------------------------------------
# EV-02、EV-04、EV-05
# ---------------------------------------------------------------------------


def register(body: dict, *, user: str, admin: bool) -> dict:
    source_id = str(body.get("id") or "")
    if not SOURCE_ID.match(source_id):
        raise _invalid("id", "pattern", "ID 只能用小写字母、数字和连字符，2～41 位")
    provider_id = str(body.get("provider") or "").strip()
    if not provider_id:
        raise _invalid("provider", "required")
    if body.get("secret"):
        raise ApiError("evidence_not_available", "这一版还不能写入凭据", 501, {"api": "EV-06"})
    info = _provider_info(provider_id)
    self_service = bool(info.get("self_service"))
    if not admin and not self_service:
        raise ApiError("forbidden", "这类数据源要管理员登记", 403, {"required_role": "admin"})
    fields = _clean(body, info, creating=True)
    owner = user if self_service or not admin else str(body.get("owner") or "").strip() or user
    now = time.time()
    registration = {
        "id": source_id, "provider": provider_id, "provider_version": str(info.get("version") or ""),
        "provider_label": str(info.get("label") or provider_id), "kind": str(info.get("kind") or ""),
        **fields, "cred_ref": None, "owner": owner, "self_service": self_service,
        "registered_by_admin": admin and not self_service, "created_at": now, "updated_at": now,
        "secret_written_at": {},
    }
    with _lock:
        items = _load()
        if any(item["id"] == source_id for item in items):
            raise ApiError("source_id_taken", "数据源 ID 已被占用", 409)
        items.append(registration)
        _save(items)
    return registration


def update(source_id: str, body: dict, *, user: str, admin: bool) -> dict:
    if body.get("secret"):
        raise ApiError("evidence_not_available", "这一版还不能写入凭据", 501, {"api": "EV-06"})
    current = require_visible(source_id, user, admin)
    if not can_manage(current, user, admin):
        raise ApiError("forbidden", "只有登记人和管理员能改", 403, {"required_role": "admin"})
    info = {"self_service": current.get("self_service"), "kind": current.get("kind"),
            "input": "artifact" if "artifact_sha256" in (current.get("connection") or {}) else "connection"}
    changes = _clean({key: value for key, value in body.items() if key not in FIXED}, info, creating=False,
                     current=current)
    if admin and not current.get("self_service") and str(body.get("owner") or "").strip():
        changes["owner"] = str(body["owner"]).strip()
    with _lock:
        items = _load()
        for index, item in enumerate(items):
            if item["id"] == source_id:
                items[index] = {**item, **changes, "updated_at": time.time()}
                _save(items)
                return items[index]
    raise ApiError("source_not_found", "数据源不存在", 404)


def delete(source_id: str, *, user: str, admin: bool) -> dict:
    current = require_visible(source_id, user, admin)
    if not can_manage(current, user, admin):
        raise ApiError("forbidden", "只有登记人和管理员能删", 403, {"required_role": "admin"})
    from . import jobs

    active = jobs.active_for_source(source_id)
    if active:
        raise ApiError("job_state_invalid", "这个数据源有正在跑的采集任务", 409, {"state": active[0]["state"]})
    with _lock:
        _save([item for item in _load() if item["id"] != source_id])
    grants.drop_source(source_id)
    return current


# ---------------------------------------------------------------------------
# 视图（EvidenceSource）
# ---------------------------------------------------------------------------


def ref(registration: dict) -> dict:
    return {"id": registration["id"], "display_name": registration.get("display_name") or registration["id"],
            "environment": registration.get("environment") or "test"}


def scope_summary(registration: dict) -> str:
    connection = registration.get("connection") or {}
    if "artifact_filename" in connection:
        return str(connection.get("artifact_filename") or "")
    scope = registration.get("scope_allowlist") or []
    return "、".join(scope[:3]) + (f" 等 {len(scope)} 项" if len(scope) > 3 else "") if scope else "全部"


def view(registration: dict, *, user: str, admin: bool, project_id: str | None = None,
         provider_info: dict | None = None, last_job: dict | None = None) -> dict:
    """EvidenceSource。普通用户看别人登记的数据源时，连接参数、出口、凭据引用都不给。"""
    from ..api import deps

    detailed = admin or registration.get("owner") == user
    info = provider_info or {}
    secret_fields = [
        {"name": field["name"], "set": field["name"] in (registration.get("secret_written_at") or {}),
         "written_at": deps.iso((registration.get("secret_written_at") or {}).get(field["name"]))}
        for field in info.get("cred_fields") or [] if isinstance(field, dict) and field.get("secret")
    ]
    authorized = None
    if project_id:
        authorized = registration["id"] in grants.authorized_ids(project_id)
    return {
        **ref(registration),
        "provider": {"id": registration["provider"], "version": str(info.get("version") or
                                                                      registration.get("provider_version") or ""),
                     "label": str(info.get("label") or registration.get("provider_label") or registration["provider"])},
        "connection": dict(registration.get("connection") or {}) if detailed else {},
        "cred_ref": registration.get("cred_ref") if detailed else None,
        "secret_fields": secret_fields,
        "scope_allowlist": list(registration.get("scope_allowlist") or []),
        "scope_summary": scope_summary(registration),
        "egress": list(registration.get("egress") or []) if detailed else [],
        "access_window": registration.get("access_window"),
        "budget": dict(registration.get("budget") or {}),
        "rate_limit": dict(registration.get("rate_limit") or {}),
        "masking_policy": registration.get("masking_policy") or "none",
        "authorization_note": registration.get("authorization_note"),
        "owner": registration.get("owner") or "",
        "self_service": bool(registration.get("self_service")),
        "last_job": {"at": deps.iso(last_job.get("ended_at") or last_job.get("created_at")),
                     "state": last_job["state"]} if last_job else None,
        "authorized_for_project": authorized,
    }


# ---------------------------------------------------------------------------
# EV-03、EV-30：申请与通知管理员
# ---------------------------------------------------------------------------


def save_request(kind: str, by: str, payload: dict) -> str:
    request_id = f"esr-{uuid.uuid4().hex[:12]}"
    storage.write_json(storage.root() / "requests" / f"{request_id}.json",
                       {"id": request_id, "kind": kind, "by": by, "at": time.time(), "state": "pending", **payload})
    return request_id


def notify_admins(title: str, body: str, *, project_id: str | None = None) -> int:
    """给管理员发 `request_pending` 通知，返回发出的条数。本地模式发给本地用户；取不到账号列表时只存不发。"""
    from ..services import auth, notifications

    if not auth.ENABLED:
        return 1 if notifications.notify("local", "request_pending", title, body, project_id=project_id) else 0
    try:
        from ..routers.admin import auth_admin_available, list_auth_users

        admins = [user["username"] for user in list_auth_users() if user.get("role") == "admin"] \
            if auth_admin_available() else []
    except Exception:  # noqa: BLE001 - 认证服务不通：申请已经存在 requests/ 下，只是不发通知
        admins = []
    sent = 0
    for name in admins:
        if notifications.notify(name, "request_pending", title, body, project_id=project_id):
            sent += 1
    return sent
