"""配额接口（契约 SY-15、SY-16；详设 4.10.1、方案 E9）：默认限额与单独设置。

读：admin、ops；写：只有 admin，立即生效，都记审计。限额、用量台账与检查在 governance/quota。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..api import deps
from ..api.errors import ApiError
from ..governance import quota
from ..services import audit, ownership

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

REASON_MAX = 200
_GROUPS = (("per_user", "user"), ("per_project", "project"))


def _override_view(item: dict) -> dict:
    """QuotaOverride（契约 7.16）。"""
    subject = item["subject"]
    return {
        "id": item["id"],
        "subject": {"kind": subject["kind"], "id": subject["id"], "label": subject.get("label") or subject["id"]},
        "item": item["item"],
        "value": item["value"],
        "reason": item.get("reason") or "",
        "created_at": deps.iso(item.get("created_at")),
        "created_by": item.get("created_by") or "",
    }


def _view(limits: dict) -> dict:
    return {
        "per_user": dict(limits["per_user"]),
        "per_project": dict(limits["per_project"]),
        "overrides": [_override_view(item) for item in limits["overrides"]],
    }


def _audit(request: Request, action: str, **detail) -> None:
    detail = {key: value for key, value in detail.items() if value is not None}
    audit.log(deps.actor(request), request.method, request.url.path, detail={"action": action, **detail})


def _invalid(fields: dict, message: str = "配额设置不合法") -> ApiError:
    return ApiError("invalid_argument", message, 400, {"fields": fields})


def _limits_from(body: dict) -> dict[str, dict]:
    """PUT 请求体 → 各组要改的项；online_engines 固定 1，带了也忽略；overrides 另有接口，忽略。"""
    fields, result = {}, {}
    for group, kind in _GROUPS:
        section = body.get(group)
        result[group] = {}
        if section is None:
            continue
        if not isinstance(section, dict):
            fields[group] = "object"
            continue
        for key, value in section.items():
            if key in quota.READ_ONLY_ITEMS:
                continue
            if key not in quota.ITEMS[kind]:
                fields[f"{group}.{key}"] = "unknown"
            elif not quota.valid_value(value):
                fields[f"{group}.{key}"] = "non_negative_int"
            else:
                result[group][key] = value
    if fields:
        raise _invalid(fields, "配额必须是不小于 0 的整数（0 表示不限）")
    return result


@router.get("/quotas")
def get_quotas(request: Request):
    """SY-15：默认限额与单独设置（admin；ops 只读）。"""
    deps.require_role(request, "admin", "ops")
    return _view(quota.service().limits())


@router.put("/quotas")
async def put_quotas(request: Request):
    """SY-15：只改 per_user、per_project，立即生效。"""
    deps.require_role(request, "admin")
    changes = _limits_from(await deps.json_body(request))
    quotas = quota.service()
    before = quotas.limits()
    limits = quotas.set_limits(changes["per_user"], changes["per_project"])
    changed = {
        f"{group}.{key}": value
        for group, _kind in _GROUPS
        for key, value in limits[group].items()
        if before[group].get(key) != value
    }
    _audit(request, "quota.update", **changed)
    return _view(limits)


def _subject_label(kind: str, subject_id: str) -> str:
    """单独设置对象的显示名：项目取名称（不存在时 404），用户取用户名。"""
    if kind == "project":
        return str(deps.workspaces.get_project(subject_id).get("name") or subject_id)
    if not ownership.OWNER_NAME_RE.fullmatch(subject_id):
        raise _invalid({"subject.id": "format"}, "用户名不合法")
    return subject_id


@router.post("/quotas/overrides")
async def create_override(request: Request):
    """SY-16：给某个用户或项目单独设一项限额；同一对象的同一项再设时替换旧的。"""
    deps.require_role(request, "admin")
    body = await deps.json_body(request)
    subject = body.get("subject") if isinstance(body.get("subject"), dict) else {}
    kind = subject.get("kind")
    subject_id = subject.get("id").strip() if isinstance(subject.get("id"), str) else ""
    item, value = body.get("item"), body.get("value")
    reason = body.get("reason").strip() if isinstance(body.get("reason"), str) else ""
    fields = {}
    if kind not in quota.ITEMS:
        fields["subject.kind"] = "enum"
    elif item not in quota.ITEMS[kind]:
        fields["item"] = "enum"
    if not subject_id:
        fields["subject.id"] = "required"
    if not quota.valid_value(value):
        fields["value"] = "non_negative_int"
    if not reason:
        fields["reason"] = "required"
    elif len(reason) > REASON_MAX:
        fields["reason"] = "length"
    if fields:
        raise _invalid(fields)
    label = _subject_label(kind, subject_id)
    entry, replaced = quota.service().add_override(kind, subject_id, label, item, value, reason, deps.actor(request))
    _audit(request, "quota.override.create", override=entry["id"], subject_kind=kind, subject_id=subject_id,
           project=subject_id if kind == "project" else None, item=item, value=value, reason=reason,
           replaced=replaced)
    return JSONResponse(status_code=201, content=_override_view(entry))


@router.delete("/quotas/overrides/{override_id}", status_code=204)
def delete_override(override_id: str, request: Request):
    """SY-16：删掉单独设置，回到默认限额。"""
    deps.require_role(request, "admin")
    removed = quota.service().delete_override(override_id)
    if removed is None:
        raise ApiError("not_found", "单独设置不存在或已删除", 404)
    subject = removed["subject"]
    _audit(request, "quota.override.delete", override=override_id, subject_kind=subject["kind"],
           subject_id=subject["id"], project=subject["id"] if subject["kind"] == "project" else None,
           item=removed["item"])
    return Response(status_code=204)
