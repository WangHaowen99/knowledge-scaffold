"""自主构建（契约 3.22 AB-01～AB-18）与平台限制、模型单价（SY-17、SY-18）。

逻辑在 `services/autobuild/`；这里只做鉴权、请求体检查、错误码与专项审计。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from ..api import deps
from ..api.errors import ApiError
from ..services import audit, model_pricing
from ..services.autobuild import limits, plan, policy

router = APIRouter(prefix="/api/v1/projects/{project_id}/autobuild", tags=["autobuild"])
admin_router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def owner_of(project_id: str) -> str:
    return str((deps.workspaces.get_project(project_id) or {}).get("owner") or "")


def _special_audit(request: Request, project_id: str, action: str, summary: str, **detail) -> None:
    audit.record(type="autobuild", action=action, actor=deps.actor(request) or "system", project_id=project_id,
                 summary=summary, detail={key: value for key, value in detail.items() if value is not None} or None)


# ---- AB-01、AB-02 策略 ----


def _policy_inputs(project_id: str) -> tuple[list[str], list[str]]:
    """策略校验要的：切片编号、流程节点编号。"""
    root = deps.workspaces.project_root(project_id)
    slice_ids = [row["id"] for row in plan.slice_rows(deps.workspaces, project_id)]
    return slice_ids, plan.node_ids(plan.flow(root))


def normalized_policy(project_id: str, body: dict | None) -> dict:
    """当前策略并上请求里给的，校验后返回（AB-02、AB-03 共用）。"""
    owner = owner_of(project_id)
    merged = policy.merge(policy.load(project_id, owner), body)
    slice_ids, node_ids = _policy_inputs(project_id)
    return policy.normalize(project_id, owner, merged, slice_ids=slice_ids, node_ids=node_ids)


@router.get("/policy")
async def get_policy(project_id: str, request: Request):
    """AB-01。"""
    deps.require_owner(project_id, request)
    return await asyncio.to_thread(policy.view, project_id, owner_of(project_id))


@router.put("/policy")
async def put_policy(project_id: str, request: Request):
    """AB-02：给了哪组改哪组；`base_revision` 不对回 409。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    owner = owner_of(project_id)
    current = await asyncio.to_thread(policy.load, project_id, owner)
    expected = body.get("base_revision")
    actual = policy.revision(current)
    if expected not in (None, "") and expected != actual:
        raise ApiError("revision_conflict", "策略已被修改，请刷新", 409,
                       {"target": "autobuild_policy", "expected": expected, "actual": actual})
    fresh = await asyncio.to_thread(normalized_policy, project_id, body)
    await asyncio.to_thread(policy.save, project_id, fresh)
    return await asyncio.to_thread(policy.view, project_id, owner, fresh)


# ---- SY-17 平台限制、SY-18 模型单价 ----


@admin_router.get("/autobuild-limits")
def get_limits(request: Request):
    """SY-17（admin；ops 只读）。"""
    deps.require_role(request, "admin", "ops")
    return limits.current()


@admin_router.put("/autobuild-limits")
async def put_limits(request: Request):
    deps.require_role(request, "admin")
    body = await deps.json_body(request)
    try:
        return limits.save(body)
    except limits.LimitsError as exc:
        raise ApiError("invalid_argument", str(exc), 400, {"fields": exc.fields}) from exc


@admin_router.get("/settings/pricing")
def get_pricing(request: Request):
    """SY-18（admin；ops 只读）。"""
    deps.require_role(request, "admin", "ops")
    return model_pricing.load()


@admin_router.put("/settings/pricing")
async def put_pricing(request: Request):
    deps.require_role(request, "admin")
    body = await deps.json_body(request)
    try:
        return model_pricing.save(body)
    except model_pricing.PricingError as exc:
        raise ApiError("invalid_argument", str(exc), 400, {"fields": exc.fields}) from exc
