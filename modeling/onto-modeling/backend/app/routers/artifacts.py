"""插件产物与通用视图（契约 3.17 AR-01～AR-10；详设 5.9.4、5.9.5；方案 C13）。

`GET .../artifacts/{type}/summary` 先于 `GET .../artifacts/{type}/{key}` 登记：键恰好叫 summary 的
产物要用 AR-02 列表找。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..api import deps
from ..api.errors import ApiError
from ..kernel.artifacts import service
from ..services import audit
from ..services.workspace.errors import WorkspaceError

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["artifacts"])


@contextmanager
def _errors():
    try:
        yield
    except service.ArtifactError as exc:
        raise ApiError(exc.code, str(exc), exc.status, exc.details) from exc
    except WorkspaceError as exc:
        raise ApiError(exc.code or "invalid_argument", str(exc), exc.status, getattr(exc, "details", None)) from exc


def _filters(request: Request) -> dict:
    out = {}
    for name, value in request.query_params.multi_items():
        if name.startswith("filter[") and name.endswith("]"):
            out[name[len("filter["):-1]] = value
    return out


def _bool(value: str | None) -> bool | None:
    if value in (None, ""):
        return None
    if value.lower() in ("true", "1"):
        return True
    if value.lower() in ("false", "0"):
        return False
    raise ApiError("invalid_argument", "stale 只能是 true 或 false", 400, {"fields": {"stale": "bool"}})


def _audit(request: Request, action: str, project_id: str, art_type: str, key: str) -> None:
    audit.log(deps.actor(request), request.method, request.url.path,
              {"action": action, "project_id": project_id, "type": art_type, "key": key})


@router.get("/artifact-types")
async def artifact_types(project_id: str, request: Request):
    """AR-01：已启用包登记的产物类型。"""
    deps.require_owner(project_id, request)
    return await asyncio.to_thread(service.list_types, deps.workspaces, project_id)


@router.get("/artifacts/{art_type}")
async def list_artifacts(project_id: str, art_type: str, request: Request, q: str = "", stale: str = "",
                         sort: str = "", order: str = "", cursor: str = "", limit: int = 0):
    """AR-02：产物列表（只能按视图声明的 filters 筛选）。"""
    deps.require_owner(project_id, request)
    with _errors():
        return await asyncio.to_thread(
            service.list_rows, deps.workspaces, project_id, art_type, filters=_filters(request), q=q,
            stale=_bool(stale), sort=sort, order=order, cursor=cursor, limit=limit, page=deps.page, iso=deps.iso,
        )


@router.get("/artifacts/{art_type}/summary")
async def artifact_summary(project_id: str, art_type: str, request: Request, group_by: str = "", trend: str = ""):
    """AR-09：产物汇总（按状态计数、过期数、可选的趋势）。"""
    deps.require_owner(project_id, request)
    with _errors():
        return await asyncio.to_thread(service.summary, deps.workspaces, project_id, art_type,
                                       group_by=group_by, trend=bool(_bool(trend)))


@router.get("/artifacts/{art_type}/{key}")
async def get_artifact(project_id: str, art_type: str, key: str, request: Request, rows_limit: int = 0,
                       changeset: str = ""):
    """AR-03：产物详情。带 changeset（产物卡上的 changeset_id）时，这个变更集还没确认就读助手副本里这一轮的版本。"""
    deps.require_owner(project_id, request)
    with _errors():
        if changeset:
            return await asyncio.to_thread(service.detail_in_changeset, deps.workspaces, project_id, art_type, key,
                                           changeset, rows_limit=rows_limit, iso=deps.iso)
        return await asyncio.to_thread(service.detail, deps.workspaces, project_id, art_type, key,
                                       rows_limit=rows_limit, iso=deps.iso)


@router.get("/artifacts/{art_type}/{key}/history")
async def artifact_history(project_id: str, art_type: str, key: str, request: Request, cursor: str = "", limit: int = 0):
    """AR-04：产物历史。"""
    deps.require_owner(project_id, request)
    with _errors():
        return await asyncio.to_thread(service.history, deps.workspaces, project_id, art_type, key,
                                       cursor=cursor, limit=limit, page=deps.page, iso=deps.iso)


@router.put("/artifacts/{art_type}/{key}")
async def save_artifact(project_id: str, art_type: str, key: str, request: Request):
    """AR-05：保存（写入路径，先校验字段定义，再过包的门禁）。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request, limit=8 * 1024 * 1024)
    with _errors():
        result = await asyncio.to_thread(service.save, deps.workspaces, project_id, art_type, key, body,
                                         actor=deps.actor(request), iso=deps.iso)
    _audit(request, "artifact.save", project_id, art_type, key)
    return result


@router.post("/artifacts/{art_type}")
async def create_artifact(project_id: str, art_type: str, request: Request):
    """AR-06：新建（路径按登记的 path 模板，只能落在包自己的目录下）。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request, limit=8 * 1024 * 1024)
    with _errors():
        result = await asyncio.to_thread(service.create, deps.workspaces, project_id, art_type, body,
                                         actor=deps.actor(request), iso=deps.iso)
    _audit(request, "artifact.create", project_id, art_type, result["artifact"]["key"])
    return JSONResponse(status_code=201, content=result)


@router.delete("/artifacts/{art_type}/{key}")
async def delete_artifact(project_id: str, art_type: str, key: str, request: Request):
    """AR-07：删除。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    with _errors():
        result = await asyncio.to_thread(service.delete, deps.workspaces, project_id, art_type, key, body,
                                         actor=deps.actor(request))
    _audit(request, "artifact.delete", project_id, art_type, key)
    return result


@router.post("/artifacts/{art_type}/{key}/validate")
async def validate_artifact(project_id: str, art_type: str, key: str, request: Request):
    """AR-08：校验（确定性检查，不调模型、不落盘）。"""
    deps.require_owner(project_id, request)
    body = await deps.json_body(request, limit=8 * 1024 * 1024)
    with _errors():
        return await asyncio.to_thread(service.validate, deps.workspaces, project_id, art_type, key, body)


@router.post("/artifacts/{art_type}/{key}/drawio")
async def artifact_drawio(project_id: str, art_type: str, key: str, request: Request):
    """AR-10：交给 drawio 高级编辑器的 XML（原文，或把未保存的模型合进原文；不落盘）。"""
    deps.require_owner(project_id, request)
    body = await deps.json_body(request, limit=8 * 1024 * 1024)
    with _errors():
        return await asyncio.to_thread(service.drawio_source, deps.workspaces, project_id, art_type, key, body)
