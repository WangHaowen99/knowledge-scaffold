"""知识包数据飞轮（ADR-0108，API 契约 3.21b FW-01～FW-14）：当前登录用户在项目里的飞轮。

状态请求同时用调用者的登录推进飞轮（见 services/flywheel），所以是打开的工作台轮询的 GET 而不是 POST：
轮询不能刷满审计日志，自动执行的步骤自己记审计。只有项目所有者能用（1.4）；已归档项目只读，写接口回
409 project_archived，状态与记录照样可读。
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..api import deps
from ..services import auth, ownership
from ..services.flywheel import service, store
from ..services.flywheel.assistant import Assistant
from ..services.flywheel.caller import Caller
from .hub_packages import identity

router = APIRouter(prefix="/api/v1/projects/{project_id}/flywheel", tags=["flywheel"])
RUN_ID = Path(pattern=r"^FW-\d{3,6}$")
REFRESH_LIMIT = 8192


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    optimize: bool | None = None
    publish: bool | None = None
    max_rounds: int | None = None
    delegation_days: int | None = None


class EvaluateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 用户确认删除评测图范围内的旧数据（评测服务不会自己替换的那种），带上所确认提示的确认码
    replace_graph: bool = False
    graph_confirmation_code: str = Field(default="", max_length=64, pattern=r"^[0-9a-f]*$")


class RollbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["workspace", "hub"] = "workspace"


def _caller(project_id: str, request: Request, loop, write: bool) -> Caller:
    token, subject, username = identity(request)
    (deps.require_writable if write else deps.require_owner)(project_id, request)
    claims = request.state.user
    meta = deps.workspaces.get_project(project_id)
    can_write = ownership.owns(meta, username) and not meta.get("archived")
    refresh = request.headers.get("x-refresh-token", "")
    return Caller(deps.workspaces, project_id, subject, username, token, can_write,
                  Assistant(loop), float(claims.get("exp") or 0), hub_token=token,
                  refresh=refresh if len(refresh) <= REFRESH_LIMIT else "")


async def _run(project_id: str, request: Request, action, *args, write: bool = True):
    """工作区锁与 git 会阻塞：都放到工作线程里跑，助手会话留在事件循环上。"""
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(lambda: action(_caller(project_id, request, loop, write), *args))


@router.get("")
async def state(project_id: str, request: Request, reconcile: bool = False):
    """FW-01 飞轮状态（顺带推进）。"""
    if not auth.ENABLED:
        await asyncio.to_thread(deps.require_owner, project_id, request)
        return service.unavailable("数据飞轮需要统一账号登录：评测结果和 Hub 发布都按账号隔离")
    return await _run(project_id, request, service.sync, reconcile, write=False)


@router.put("/settings")
async def settings(project_id: str, body: SettingsBody, request: Request):
    """FW-02 开关与上限。"""
    return await _run(project_id, request, service.update_settings, body.model_dump())


@router.get("/runs")
async def runs(project_id: str, request: Request, offset: int = Query(0, ge=0),
               limit: int = Query(20, ge=1, le=store.PAGE_LIMIT),
               group: Literal[tuple(store.RUN_FILTERS)] = Query("all", alias="filter")):
    """FW-03 运行记录分批读取：从最新一条开始，前端滚动到底部时再取下一批；只读，不推进飞轮。"""
    return await _run(project_id, request, service.runs_page, offset, limit, group, write=False)


@router.get("/audit")
async def audit(project_id: str, request: Request, offset: int = Query(0, ge=0),
                limit: int = Query(20, ge=1, le=store.PAGE_LIMIT)):
    """FW-04 设置变更分批读取，同上。"""
    return await _run(project_id, request, service.audit_page, offset, limit, write=False)


@router.get("/runs/{run_id}")
async def run_detail(project_id: str, request: Request, run_id: str = RUN_ID):
    """FW-05 一次运行的详情。"""
    return await _run(project_id, request, service.detail, run_id, write=False)


@router.get("/runs/{run_id}/changes")
async def run_changes(project_id: str, request: Request, run_id: str = RUN_ID):
    """FW-06 这次运行改了哪些知识包文档。"""
    return await _run(project_id, request, service.changes, run_id, write=False)


@router.post("/runs/{run_id}/optimize")
async def confirm_optimize(project_id: str, request: Request, run_id: str = RUN_ID):
    """FW-07 确认评测优化。"""
    return await _run(project_id, request, service.confirm_optimize, run_id)


@router.post("/runs/{run_id}/publish")
async def confirm_publish(project_id: str, request: Request, run_id: str = RUN_ID):
    """FW-08 确认发布。"""
    return await _run(project_id, request, service.confirm_publish, run_id)


@router.post("/runs/{run_id}/evaluate")
async def confirm_evaluate(project_id: str, request: Request, body: EvaluateBody | None = None,
                           run_id: str = RUN_ID):
    """FW-09 开关关闭时发布后的评测：用浏览器转交的刷新令牌在评测服务发起，评测跑完不需要页面一直开着。"""
    confirmation = body.graph_confirmation_code if body and body.replace_graph else ""
    return await _run(project_id, request, service.confirm_evaluate, run_id, confirmation)


@router.post("/runs/{run_id}/skip-evaluation")
async def skip_evaluation(project_id: str, request: Request, run_id: str = RUN_ID):
    """FW-10 跳过发布后的评测。"""
    return await _run(project_id, request, service.skip_evaluation, run_id)


@router.post("/runs/{run_id}/recheck")
async def recheck(project_id: str, request: Request, run_id: str = RUN_ID):
    """FW-11 重新检查。"""
    return await _run(project_id, request, service.recheck, run_id)


@router.post("/runs/{run_id}/ignore")
async def ignore(project_id: str, request: Request, run_id: str = RUN_ID):
    """FW-12 忽略这次运行。"""
    return await _run(project_id, request, service.ignore, run_id)


@router.post("/runs/{run_id}/stop")
async def stop(project_id: str, request: Request, run_id: str = RUN_ID):
    """FW-13 中止运行。"""
    return await _run(project_id, request, service.stop, run_id)


@router.post("/runs/{run_id}/rollback")
async def rollback(project_id: str, body: RollbackBody, request: Request, run_id: str = RUN_ID):
    """FW-14 回退到执行前内容（工作区或 Hub）。"""
    return await _run(project_id, request, service.rollback, run_id, body.scope)
