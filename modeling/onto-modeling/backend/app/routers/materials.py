"""资料分析（API 契约 3.6）：R-07 资料地图、R-08 候选、R-09 核验、R-10 资料问题转待答；R-11 / R-12 外部反馈。

数据取资料分析自己的产出（`research/` 下的报告、验收回执、建模蓝图），见 `services/research_view.py`；
外部反馈是工作区 `feedback/` 下的批次（X-05 写入），处理状态见 `services/feedback_inbox.py`。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..api import deps
from ..api.errors import ApiError
from ..services import feedback_inbox, project_events, research_view

router = APIRouter(prefix="/api/v1/projects/{project_id}/materials", tags=["materials"])
feedback_router = APIRouter(prefix="/api/v1/projects/{project_id}/feedback", tags=["materials"])


@router.get("/map")
def material_map(project_id: str, request: Request):
    deps.require_owner(project_id, request)
    return research_view.material_map(deps.workspaces, project_id)


@router.get("/candidates")
def candidates(project_id: str, request: Request, after: str = "", limit: int = 0):
    deps.require_owner(project_id, request)
    return research_view.candidates(deps.workspaces, project_id, after=after, limit=limit)


@router.get("/verification")
def verification(project_id: str, request: Request):
    deps.require_owner(project_id, request)
    data = research_view.verification(deps.workspaces, project_id)
    return {key: data[key] for key in ("checked_at", "total", "passed", "problems", "items")}


@router.post("/problems/{problem_id}/promote")
async def promote(project_id: str, problem_id: str, request: Request):
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    result = research_view.promote(deps.workspaces, project_id, problem_id, str(body.get("as") or ""),
                                   actor=deps.username(request))
    # 问题进了蓝图的「待你回答」，待处理跟着重算
    project_events.schedule_flow_refresh(project_id)
    return result


# ---- R-11、R-12 外部反馈 ----


@feedback_router.get("")
def list_feedback(project_id: str, request: Request, state: str = "", cursor: str = "", limit: int = 0):
    """反馈演进包没启用时空列表，带 plugin_disabled。"""
    root = deps.require_owner(project_id, request)
    if state not in ("", "open", "handled"):
        raise ApiError("invalid_argument", "state 只能是 open 或 handled", 400, {"fields": {"state": "enum"}})
    if not research_view.pack_enabled(root, feedback_inbox.EVOLUTION_PACK):
        return {"items": [], "next_cursor": None, "plugin_disabled": True}
    page = deps.page(feedback_inbox.list_feedback(root, state), cursor, limit, {"state": state})
    return {"items": page["items"], "next_cursor": page["next_cursor"]}


@feedback_router.patch("/{feedback_id}")
async def set_feedback_state(project_id: str, feedback_id: str, request: Request):
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    try:
        view = feedback_inbox.set_state(deps.workspaces, project_id, feedback_id, str(body.get("state") or ""),
                                        by=deps.username(request))
    except feedback_inbox.FeedbackRejected as exc:
        raise ApiError(exc.code, str(exc), exc.status, exc.details) from exc
    # 待处理不再列处理过的批次
    project_events.schedule_flow_refresh(project_id)
    return view
