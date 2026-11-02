"""资料分析（API 契约 3.6）：R-07 资料地图、R-08 候选、R-09 核验、R-10 资料问题转待答。

数据取资料分析自己的产出（`research/` 下的报告、验收回执、建模蓝图），见 `services/research_view.py`。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..api import deps
from ..services import project_events, research_view

router = APIRouter(prefix="/api/v1/projects/{project_id}/materials", tags=["materials"])


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
