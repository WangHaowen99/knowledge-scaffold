"""知识包 Hub（ADR-0098）：浏览本人可见的 Hub 包与版本、下载、从 Hub 导入成新项目，以及项目的 Hub 版本与共享资料。

Hub 按登录用户授权，这里的接口都要统一账号登录（鉴权关闭的本地模式没有 Hub 身份，一律 401）。
`GET /api/hub/package-projects` 是评测服务调用的外部路径，不进 /api/v1、不改名。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ..api import deps
from ..api.errors import ApiError
from ..services import auth, hub_members, hub_packages, knowledge_hub, ownership
from ..services.workspace import WorkspaceError

router = APIRouter(prefix="/api/v1/hub", tags=["hub"])
project_router = APIRouter(prefix="/api/v1/projects/{project_id}/hub", tags=["hub"])
external_router = APIRouter(prefix="/api/hub", tags=["hub"])

IDENTIFIER = r"^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,127}$"


def identity(request: Request) -> tuple[str, str, str]:
    """(登录令牌, Hub 主体, 用户名)。主体 = [签发方, sub] 的 JSON，与 Hub 的授权口径一致。"""
    token = knowledge_hub.token_of(request)
    claims = auth.verify_access_token(token) if token and auth.ENABLED else None
    if not claims or not claims.get("sub"):
        raise WorkspaceError("知识包 Hub 需要统一账号登录", 401, "unauthenticated")
    request.state.user = claims
    subject = json.dumps([claims.get("iss", auth.ISSUER), claims["sub"]], ensure_ascii=False)
    return token, subject, claims["sub"]


def _file_response(folder: Path, path: Path, filename: str, media_type: str | None = None) -> FileResponse:
    return FileResponse(path, filename=filename, media_type=media_type, background=BackgroundTask(shutil.rmtree, folder))


# ---- 本人的 Hub 包 ----


@router.get("/packages")
def packages(request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    token, _, _ = identity(request)
    return hub_packages.call("GET", f"/api/packages?offset={offset}&limit={limit}", token)


@router.get("/packages/{package_id}/releases")
def package_releases(package_id: str, request: Request, offset: int = Query(0, ge=0),
                     limit: int = Query(20, ge=1, le=100)):
    token, _, _ = identity(request)
    return hub_packages.call("GET", hub_packages.reference_path(package_id) + f"?offset={offset}&limit={limit}", token)


def download(package_id: str, release_id: str, token: str) -> FileResponse:
    folder = Path(tempfile.mkdtemp(prefix="onto-hub-package-download-"))
    path = folder / "package.zip"
    try:
        receipt = hub_packages.download(package_id, release_id, token, path)
    except Exception:
        shutil.rmtree(folder)
        raise
    name = knowledge_hub.package_filename(receipt["ontology_id"], receipt["name"], receipt["version"])
    return _file_response(folder, path, name, "application/zip")


@router.get("/packages/{package_id}/releases/{release_id}/download")
def package_download(package_id: str, release_id: str, request: Request):
    token, _, _ = identity(request)
    return download(package_id, release_id, token)


@router.post("/imports", status_code=201)
async def import_package(request: Request):
    """从 Hub 导入一个版本成为新项目；同一 request_id 重试得到同一个项目。"""
    token, subject, username = identity(request)
    body = await deps.json_body(request)
    fields = {"request_id": IDENTIFIER, "ontology_id": IDENTIFIER, "release_id": IDENTIFIER,
              "package_digest": r"^[a-f0-9]{64}$", "name": r"^.{1,128}$"}
    import re

    bad = {key: "invalid" for key, pattern in fields.items()
           if not isinstance(body.get(key), str) or not re.fullmatch(pattern, body[key])}
    extra = sorted(set(body) - set(fields))
    if bad or extra:
        raise ApiError("invalid_argument", "导入参数不合法", 400, {"fields": {**bad, **{key: "unknown" for key in extra}}})
    meta = hub_packages.import_package(deps.workspaces, {key: body[key] for key in fields}, token, subject, username)
    return ownership.meta_view(meta)


@external_router.get("/package-projects")
def package_projects(request: Request, ontology_id: list[str] = Query(...)):
    """评测服务展示用：当前用户可见、且关联到这些 Hub 包的建模项目名称。"""
    _, subject, username = identity(request)
    is_admin = ownership.is_admin(request.state.user)
    return hub_packages.linked_projects(deps.workspaces, subject, username, is_admin, ontology_id)


# ---- 项目的 Hub 版本与共享资料 ----


def _linked(project_id: str) -> dict:
    linked = hub_packages.association(project_id)
    if not linked:
        raise ApiError("not_found", "项目还没有发布到 Hub", 404)
    return linked


@project_router.get("/releases")
def project_releases(project_id: str, request: Request, offset: int = Query(0, ge=0),
                     limit: int = Query(20, ge=1, le=100)):
    """项目关联的 Hub 包的全部版本（含别人发布的），按 Hub 提交时间从新到旧。"""
    token, subject, _ = identity(request)
    deps.require_owner(project_id, request)
    return hub_packages.releases(project_id, subject, token, offset, limit)


@project_router.get("/releases/{release_id}/download")
def project_release_download(project_id: str, release_id: str, request: Request):
    token, _, _ = identity(request)
    deps.require_owner(project_id, request)
    return download(_linked(project_id)["ontology_id"], release_id, token)


@project_router.get("/resources/status")
def resource_status(project_id: str, request: Request):
    deps.require_owner(project_id, request)
    return knowledge_hub.resource_status(project_id)


@project_router.put("/resources")
async def share_resources(project_id: str, request: Request):
    """把选中的资料同步成 Hub 包的共享资料（只同步当前选中的集合）；要明确确认可向全部授权用户共享。"""
    token, _, _ = identity(request)
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    paths = body.get("paths")
    if not isinstance(paths, list) or len(paths) > 10000 or any(not isinstance(item, str) for item in paths):
        raise ApiError("invalid_argument", "paths 要是资料路径列表", 400, {"fields": {"paths": "invalid"}})
    if body.get("confirm_shared") is not True:
        raise ApiError("invalid_argument", "要确认所选资料可向全部授权用户共享", 422,
                       {"fields": {"confirm_shared": "required"}})
    return knowledge_hub.sync_resources(deps.workspaces, project_id, token, paths)


@project_router.get("/resources")
def resources(project_id: str, request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    token, _, _ = identity(request)
    deps.require_owner(project_id, request)
    package = _linked(project_id)["ontology_id"]
    return knowledge_hub.call("GET", f"/api/packages/{package}/resources/current?offset={offset}&limit={limit}", token)


@project_router.get("/resources/{file_id}/download")
def resource_download(project_id: str, file_id: str, request: Request):
    token, _, _ = identity(request)
    deps.require_owner(project_id, request)
    knowledge_hub.identifier(file_id)
    package = _linked(project_id)["ontology_id"]
    folder = Path(tempfile.mkdtemp(prefix="onto-hub-resource-"))
    dest = folder / "resource"
    try:
        knowledge_hub.call("GET", f"/api/packages/{package}/resources/current/{file_id}", token, download=dest)
    except Exception:
        shutil.rmtree(folder)
        raise
    return _file_response(folder, dest, file_id)


# ---- 知识包共享（develop !590 的成员共享，本分支在知识包这一层管理，ADR-0098） ----

MAX_SHARED_USERS = 200


def _sharing_view(project_id: str, sharing: dict) -> dict:
    meta = deps.workspaces.get_project(project_id)
    return {"owner": meta.get("owner") or "", "users": hub_members.shared_users(project_id), "hub_sync": sharing}


@project_router.get("/sharing")
def get_sharing(project_id: str, request: Request):
    """共享用户与 Hub 授权同步状态（pending 队列、各用户最近一次结果）。"""
    deps.require_owner(project_id, request)
    return _sharing_view(project_id, hub_members.overview(project_id))


@project_router.put("/sharing")
async def put_sharing(project_id: str, request: Request):
    """改共享用户名单：已发布时按差异排授权 / 撤权并立即同步；同步失败不回滚名单，可以按用户重试。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    users = body.get("users")
    if not isinstance(users, list) or len(users) > MAX_SHARED_USERS:
        raise ApiError("invalid_argument", "users 要是用户名列表", 400, {"fields": {"users": "invalid"}})
    names = []
    for item in users:
        if not isinstance(item, str) or not ownership.OWNER_NAME_RE.fullmatch(item.strip()):
            raise ApiError("invalid_argument", "用户名不合法", 400, {"fields": {"users": "invalid"}})
        names.append(item.strip())
    owner = str(deps.workspaces.get_project(project_id).get("owner") or "")
    token = knowledge_hub.token_of(request)
    with knowledge_hub.lock(project_id):
        hub_members.writable(project_id)
        hub_members.set_shared(project_id, names, owner)
        sharing = hub_members.sync(deps.workspaces, project_id, token)
    return _sharing_view(project_id, sharing)


@project_router.post("/sharing/retry")
def retry_sharing(project_id: str, request: Request, username: str | None = None):
    """按用户重试授权 / 撤权（develop 的 members/hub-retry）；不给 username 时重试全部。"""
    deps.require_writable(project_id, request)
    with knowledge_hub.lock(project_id):
        sharing = hub_members.sync(deps.workspaces, project_id, knowledge_hub.token_of(request), username=username)
    return _sharing_view(project_id, sharing)


@project_router.get("/delete-preview")
def delete_preview(project_id: str, request: Request):
    """删除项目前看它的 Hub 包：没发布过、知识包名称与本体标识、删除进度（develop 的 hub-delete-preview）。"""
    deps.require_owner(project_id, request)
    with knowledge_hub.lock(project_id):
        return hub_members.delete_preview(deps.workspaces, project_id, knowledge_hub.token_of(request))
