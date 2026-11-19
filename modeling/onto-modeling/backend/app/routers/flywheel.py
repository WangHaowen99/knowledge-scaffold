"""Knowledge package data flywheel of a project, for the signed-in user.

The state request also advances the flywheel with the caller's login (see services/flywheel), so it
is a GET polled by the open workbench rather than a POST: polling must not flood the audit log.
Automatic steps write their own audit entries.
"""
import asyncio
from typing import Literal

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from . import projects
from .hub_packages import identity
from ..services import auth
from ..services.flywheel import service, store
from ..services.flywheel.assistant import Assistant
from ..services.flywheel.caller import Caller

router = APIRouter(prefix='/api/projects/{project_id}/flywheel', tags=['flywheel'])
RUN_ID = Path(pattern=r'^FW-\d{3,6}$')
REFRESH_LIMIT = 8192


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    optimize: bool | None = None
    publish: bool | None = None
    max_rounds: int | None = None
    delegation_days: int | None = None


class EvaluateBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # 用户确认删除评测图范围内的旧数据（评测服务不会自己替换的那种），带上所确认提示的确认码
    replace_graph: bool = False
    graph_confirmation_code: str = Field(default='', max_length=64, pattern=r'^[0-9a-f]*$')


class RollbackBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: Literal['workspace', 'hub'] = 'workspace'


def _caller(project_id, request, loop, write):
    token, subject, username = identity(request)
    projects.ensure_access(project_id, request, write=write)
    claims = request.state.user
    meta = projects.workspaces.get_project(project_id)
    can_write = projects.workspaces.can_write(meta, username, claims.get('role') == 'admin')
    refresh = request.headers.get('x-refresh-token', '')
    return Caller(projects.workspaces, project_id, subject, username, token, can_write,
                  Assistant(loop), float(claims.get('exp') or 0), hub_token=token,
                  refresh=refresh if len(refresh) <= REFRESH_LIMIT else '')


async def _run(project_id, request, action, *args, write=True):
    """Workspace locks and git block: everything runs in a worker thread, sessions stay on this loop."""
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(lambda: action(_caller(project_id, request, loop, write), *args))


@router.get('')
async def state(project_id: str, request: Request, reconcile: bool = False):
    if not auth.ENABLED:
        await asyncio.to_thread(projects.ensure_access, project_id, request)
        return service.unavailable('数据飞轮需要统一账号登录：评测结果和 Hub 发布都按账号隔离')
    return await _run(project_id, request, service.sync, reconcile, write=False)


@router.put('/settings')
async def settings(project_id: str, body: SettingsBody, request: Request):
    return await _run(project_id, request, service.update_settings, body.model_dump())


@router.get('/runs')
async def runs(project_id: str, request: Request, offset: int = Query(0, ge=0),
               limit: int = Query(20, ge=1, le=store.PAGE_LIMIT),
               group: Literal[tuple(store.RUN_FILTERS)] = Query('all', alias='filter')):
    """运行记录分批读取：从最新一条开始，前端滚动到底部时再取下一批；只读，不推进飞轮。"""
    return await _run(project_id, request, service.runs_page, offset, limit, group, write=False)


@router.get('/audit')
async def audit(project_id: str, request: Request, offset: int = Query(0, ge=0),
                limit: int = Query(20, ge=1, le=store.PAGE_LIMIT)):
    """设置变更分批读取，同上。"""
    return await _run(project_id, request, service.audit_page, offset, limit, write=False)


@router.get('/runs/{run_id}')
async def run_detail(project_id: str, request: Request, run_id: str = RUN_ID):
    return await _run(project_id, request, service.detail, run_id, write=False)


@router.get('/runs/{run_id}/changes')
async def run_changes(project_id: str, request: Request, run_id: str = RUN_ID):
    return await _run(project_id, request, service.changes, run_id, write=False)


@router.post('/runs/{run_id}/optimize')
async def confirm_optimize(project_id: str, request: Request, run_id: str = RUN_ID):
    return await _run(project_id, request, service.confirm_optimize, run_id)


@router.post('/runs/{run_id}/publish')
async def confirm_publish(project_id: str, request: Request, run_id: str = RUN_ID):
    return await _run(project_id, request, service.confirm_publish, run_id)


@router.post('/runs/{run_id}/evaluate')
async def confirm_evaluate(project_id: str, request: Request, body: EvaluateBody | None = None, run_id: str = RUN_ID):
    """开关关闭时发布后的评测：用浏览器转交的刷新令牌在评测服务发起，评测跑完不需要页面一直开着。"""
    confirmation = body.graph_confirmation_code if body and body.replace_graph else ''
    return await _run(project_id, request, service.confirm_evaluate, run_id, confirmation)


@router.post('/runs/{run_id}/skip-evaluation')
async def skip_evaluation(project_id: str, request: Request, run_id: str = RUN_ID):
    return await _run(project_id, request, service.skip_evaluation, run_id)


@router.post('/runs/{run_id}/recheck')
async def recheck(project_id: str, request: Request, run_id: str = RUN_ID):
    return await _run(project_id, request, service.recheck, run_id)


@router.post('/runs/{run_id}/ignore')
async def ignore(project_id: str, request: Request, run_id: str = RUN_ID):
    return await _run(project_id, request, service.ignore, run_id)


@router.post('/runs/{run_id}/stop')
async def stop(project_id: str, request: Request, run_id: str = RUN_ID):
    return await _run(project_id, request, service.stop, run_id)


@router.post('/runs/{run_id}/rollback')
async def rollback(project_id: str, body: RollbackBody, request: Request, run_id: str = RUN_ID):
    return await _run(project_id, request, service.rollback, run_id, body.scope)
