"""数据源（API 契约 3.19a，ADR-0103）：DS-01 连接器、DS-02 接入、DS-03 修改连接、DS-04 测试连接；
多源连接器的信任 PL-28、PL-29（契约 3.16）。

凭据只经这里转交给连接器网关，不存、不进审计详情、不回给前端。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..api import deps
from ..services import audit, datasources
from ..services.packs import catalog

router = APIRouter(tags=["datasources"])


def _material(project_id: str, rel: str) -> dict:
    from . import resources

    root = deps.workspaces.project_root(project_id)
    return resources.material_view(project_id, root, rel)


def _audit(request: Request, action: str, **detail) -> None:
    audit.log(deps.actor(request), request.method, request.url.path, {"action": action, **detail})


@router.get("/api/v1/source-connectors")
async def list_connectors(request: Request):
    """DS-01：网关里已加载的数据源连接器。"""
    deps.actor(request)
    return await asyncio.to_thread(datasources.connectors)


@router.post("/api/v1/projects/{project_id}/sources")
async def create_source(project_id: str, request: Request):
    """DS-02：接入数据源。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    rel = await asyncio.to_thread(
        datasources.create, project_id, name=body.get("name"), connector=body.get("connector"),
        config=body.get("config"), secrets=body.get("secrets"), author=deps.username(request))
    _audit(request, "datasource.create", path=rel, connector=str(body.get("connector") or ""))
    return JSONResponse(status_code=201, content={"material": _material(project_id, rel)})


@router.patch("/api/v1/projects/{project_id}/sources")
async def update_source(project_id: str, request: Request):
    """DS-03：修改连接。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    rel = await asyncio.to_thread(
        datasources.update, project_id, path=body.get("path"), config=body.get("config"),
        secrets=body.get("secrets"), author=deps.username(request))
    _audit(request, "datasource.update", path=rel, secrets=sorted((body.get("secrets") or {}).keys())
           if isinstance(body.get("secrets"), dict) else [])
    return {"material": _material(project_id, rel)}


@router.post("/api/v1/projects/{project_id}/sources/test")
async def test_source(project_id: str, request: Request):
    """DS-04：测试连接（同步，最长 40 秒）。"""
    deps.require_owner(project_id, request)
    body = await deps.json_body(request)
    return await asyncio.to_thread(
        datasources.test, project_id, connector=body.get("connector"), config=body.get("config"),
        secrets=body.get("secrets"), path=body.get("path"))


@router.post("/api/v1/admin/packs/{name}/versions/{version}/trust")
async def trust_pack(name: str, version: str, request: Request):
    """PL-28：信任上传的多源连接器。"""
    deps.require_role(request, "admin")
    await asyncio.to_thread(datasources.set_trust, deps.workspaces, name, version, on=True, by=deps.actor(request))
    audit.log(deps.actor(request), request.method, request.url.path, {"pack.trust": name, "version": version, "on": True})
    return catalog.card(deps.workspaces, name, None)


@router.delete("/api/v1/admin/packs/{name}/versions/{version}/trust")
async def untrust_pack(name: str, version: str, request: Request):
    """PL-29：撤销信任。"""
    deps.require_role(request, "admin")
    await asyncio.to_thread(datasources.set_trust, deps.workspaces, name, version, on=False, by=deps.actor(request))
    audit.log(deps.actor(request), request.method, request.url.path, {"pack.trust": name, "version": version, "on": False})
    return catalog.card(deps.workspaces, name, None)
