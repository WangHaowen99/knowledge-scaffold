"""自主构建（契约 3.22 AB-01～AB-18）与平台限制、模型单价（SY-17、SY-18）。

逻辑在 `services/autobuild/`；这里只做鉴权、请求体检查、错误码与专项审计。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from ..api import deps
from ..api.errors import ApiError
from ..services import audit, model_pricing
from ..services.autobuild import limits, plan, policy, store, views
from ..services.autobuild.runner import run_view, runner

router = APIRouter(prefix="/api/v1/projects/{project_id}/autobuild", tags=["autobuild"])
admin_router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def owner_of(project_id: str) -> str:
    return str((deps.workspaces.get_project(project_id) or {}).get("owner") or "")


def _special_audit(request: Request, project_id: str, action: str, summary: str, **detail) -> None:
    audit.record(type="autobuild", action=action, actor=deps.actor(request) or "system", project_id=project_id,
                 summary=summary, detail={key: value for key, value in detail.items() if value is not None} or None)


# ---- AB-01、AB-02 策略 ----


def _policy_inputs(project_id: str) -> tuple[list[str], list[str]]:
    """策略校验要的：分段编号、流程节点编号。"""
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
        details = {"fields": exc.fields, **({"min": limits.MAX_RUNS_MIN} if exc.status == 422 else {})}
        raise ApiError("invalid_argument", str(exc), exc.status, details) from exc


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


# ---- AB-03～AB-09、AB-18 运行 ----


def _run_or_404(project_id: str, run_id: str) -> dict:
    run = store.get(project_id, run_id)
    if run is None:
        raise ApiError("run_not_found", "运行不存在", 404)
    return run


def preview_for(project_id: str, overrides: dict | None) -> dict:
    """AB-03 与 BP-03 `next=L4` 共用：策略并上临时改的、校验、算范围与预算。"""
    return runner.preview(project_id, normalized_policy(project_id, overrides))


@router.post("/runs/preview")
async def preview_run(project_id: str, request: Request):
    """AB-03。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    overrides = body.get("policy_overrides")
    if overrides is not None and not isinstance(overrides, dict):
        raise ApiError("invalid_argument", "policy_overrides 要是对象", 400, {"fields": {"policy_overrides": "object"}})
    return await asyncio.to_thread(preview_for, project_id, overrides)


@router.post("/runs", status_code=201)
async def start_run(project_id: str, request: Request):
    """AB-04：开始（或排队）；运行在自己的对话里。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    run = await runner.start(project_id, str(body.get("preview_token") or ""), actor=deps.actor(request),
                             owner=owner_of(project_id))
    _special_audit(request, project_id, "autobuild.start", f"运行 #{run['run_no']}", run_id=run["run_id"])
    return run


@router.get("/runs")
def list_runs(project_id: str, request: Request, state: str = "", cursor: str = "", limit: int = 0):
    """AB-18：`state=running` 取没结束、还在跑的（含排队、正在去安全点的）。"""
    deps.require_owner(project_id, request)
    items = store.runs(project_id)
    if state == "running":
        items = [run for run in items if run["state"] in ("queued", *store.RUNNING)]
    elif state:
        items = [run for run in items if run["state"] == state]
    paged = deps.page([views.summary(run) for run in items], cursor, limit, binding={"state": state})
    return {"items": paged["items"], "next_cursor": paged["next_cursor"]}


@router.get("/runs/{run_id}")
def get_run(project_id: str, run_id: str, request: Request):
    """AB-05。"""
    deps.require_owner(project_id, request)
    return run_view(_run_or_404(project_id, run_id))


async def _control(project_id: str, run_id: str, request: Request, op: str):
    deps.require_writable(project_id, request)
    _run_or_404(project_id, run_id)
    run = await runner.control(project_id, run_id, op)
    _special_audit(request, project_id, "autobuild.control", f"运行 #{run['run_no']} {op}", run_id=run_id, op=op)
    return run


@router.post("/runs/{run_id}/pause")
async def pause_run(project_id: str, run_id: str, request: Request):
    """AB-06：当前这一步做完后暂停。"""
    return await _control(project_id, run_id, request, "pause")


@router.post("/runs/{run_id}/stop-at-safe-point")
async def stop_run(project_id: str, run_id: str, request: Request):
    """AB-07：做完这一段再停。"""
    return await _control(project_id, run_id, request, "stop")


@router.post("/runs/{run_id}/convert-to-l2")
async def convert_run(project_id: str, run_id: str, request: Request):
    """AB-08：这一段做完后转为陪伴；项目默认构建方式不变。"""
    return await _control(project_id, run_id, request, "convert")


@router.post("/runs/{run_id}/resume")
async def resume_run(project_id: str, run_id: str, request: Request):
    """AB-09：暂停或中断的运行接着跑，预算按剩余额度。"""
    return await _control(project_id, run_id, request, "resume")


# ---- AB-10～AB-17 台账、挂起、报告、按段撤回 ----


def _refresh(project_id: str) -> None:
    """复核之后：运行条的数字、待处理、流程（「需复核」标记）跟着变。"""
    from ..services import project_events
    from ..services.flow import inbox

    run = store.runs(project_id)
    if run:
        from ..services import streams

        streams.publish_project(project_id, "autobuild.run.updated", views.summary(run[0]))
    inbox.refresh(deps.workspaces, project_id)
    project_events.schedule_flow_refresh(project_id)


@router.get("/runs/{run_id}/ledger")
def get_ledger(project_id: str, run_id: str, request: Request, review_state: str = "", below_threshold: str = "",
               cursor: str = "", limit: int = 0):
    """AB-10。"""
    from ..services.autobuild import review

    deps.require_owner(project_id, request)
    if review_state not in ("", "pending", "accepted", "rejudged"):
        raise ApiError("invalid_argument", "review_state 取值不合法", 400, {"fields": {"review_state": "enum"}})
    below = None if below_threshold == "" else below_threshold.lower() in ("1", "true", "yes")
    data = review.ledger(project_id, run_id, review_state=review_state, below_threshold=below)
    paged = deps.page(data["items"], cursor, limit, binding={"review_state": review_state, "below": below_threshold})
    return {"items": paged["items"], "next_cursor": paged["next_cursor"], "counts": data["counts"]}


@router.post("/ledger/{entry_id}/accept")
async def accept_entry(project_id: str, entry_id: str, request: Request):
    """AB-11。"""
    from ..services.autobuild import review

    deps.require_writable(project_id, request)
    entry = await asyncio.to_thread(review.accept, project_id, entry_id)
    _refresh(project_id)
    return entry


@router.post("/ledger/{entry_id}/rejudge", status_code=202)
async def rejudge_entry(project_id: str, entry_id: str, request: Request):
    """AB-12：运行还在跑时改判排到结束后（`command_task.state=queued`）。"""
    from ..services.autobuild import review

    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    result = await review.rejudge(project_id, entry_id, body)
    _refresh(project_id)
    return result


@router.post("/runs/{run_id}/ledger/accept-high-confidence")
async def accept_high(project_id: str, run_id: str, request: Request):
    """AB-13。"""
    from ..services.autobuild import review

    deps.require_writable(project_id, request)
    result = await asyncio.to_thread(review.accept_high, project_id, run_id)
    _refresh(project_id)
    return result


@router.get("/runs/{run_id}/suspensions")
def get_suspensions(project_id: str, run_id: str, request: Request):
    """AB-14。"""
    from ..services.autobuild import review

    deps.require_owner(project_id, request)
    return review.suspensions(project_id, run_id)


@router.post("/suspensions/{suspension_id}/answer")
async def answer_suspension(project_id: str, suspension_id: str, request: Request):
    """AB-15。"""
    from ..services.autobuild import review

    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    result = await review.answer(project_id, suspension_id, body)
    _refresh(project_id)
    return result


@router.get("/runs/{run_id}/report")
def get_report(project_id: str, run_id: str, request: Request):
    """AB-16：运行结束后才有。"""
    deps.require_owner(project_id, request)
    run = _run_or_404(project_id, run_id)
    if run["state"] in store.ACTIVE:
        raise ApiError("autobuild_running", "运行还没结束", 409, {"run_id": run_id})
    return views.report(run)


@router.post("/runs/{run_id}/slices/{slice_id}/revert")
async def revert_slice(project_id: str, run_id: str, slice_id: str, request: Request):
    """AB-17：反向提交，只动这一段。"""
    from ..services.agent_sessions import manager
    from ..services.autobuild import review

    deps.require_writable(project_id, request)
    _run_or_404(project_id, run_id)
    result = await asyncio.to_thread(review.revert_slice, project_id, run_id, slice_id, actor=deps.actor(request))
    _special_audit(request, project_id, "autobuild.revert_slice", f"{run_id} {slice_id}", run_id=run_id,
                   slice_id=slice_id, commit=result["commit"])
    run = store.get(project_id, run_id)
    try:
        manager.append_record(project_id, run["conversation_id"], {"type": "card_updated",
                                                                    "payload": views.report_card(run)})
    except Exception:  # noqa: BLE001 - 对话删了：报告卡不更新
        pass
    _refresh(project_id)
    return result
