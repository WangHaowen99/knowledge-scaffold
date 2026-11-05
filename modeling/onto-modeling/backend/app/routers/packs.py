"""插件：目录、按项目启用、装配状态、上传与审核（契约 3.16 PL-01～PL-03、PL-05～PL-18）。

依据详设 4.6、5.2、5.6、5.9、8.1，方案 D1～D4、D17。切换从下一条消息开始生效，正在进行的一轮按
旧版本跑完；只有纯数据包（H0）立即生效。左栏页面跟锁文件走：启用马上出现、停用马上消失（产物保留）。
PL-04、PL-19（提供方安装）随证据源只留接口，在 routers/evidence.py。PL-20、PL-21 是上传包自带界面的
iframe 地址与静态资源（ADR-0055）。
"""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from ..api import deps
from ..api.errors import ApiError
from ..services import audit
from ..services.packs import catalog, dev_skill, registry, service
from ..services.packs import ui as pack_ui
from ..services.packs.assembler import AssemblyError
from ..services.packs.repository import RepositoryError
from ..services.packs.uploads import MAX_UPLOAD_BYTES, SCOPE_PRIVATE, UploadError, UploadStore
from ..services.uploads import LIMITS

router = APIRouter(tags=["packs"])
LEGACY_UPLOAD_PATH = "/api/settings/assets/upload"


@contextmanager
def _errors():
    try:
        yield
    except (service.PackError, UploadError, RepositoryError) as exc:
        raise ApiError(exc.code, str(exc), exc.status, exc.details) from exc
    except AssemblyError as exc:
        raise ApiError("assembly_failed", "装配失败，项目保持原版本", 409, {"error_id": None, "reason": str(exc)}) from exc


def _uploads() -> UploadStore:
    return UploadStore(deps.workspaces.packs.repository, deps.workspaces.assets_dir)


def _iso_pack(item: dict | None) -> dict | None:
    """ProjectPack 的时间转 RFC 3339（契约 1.9）。"""
    if item is not None and "enabled_at" in item:
        item = {**item, "enabled_at": deps.iso(item.get("enabled_at"))}
    return item


def _iso_result(result: dict) -> dict:
    return {**result, "pack": _iso_pack(result.get("pack"))}


def _audit(request: Request, action: str, detail: dict) -> None:
    audit.log(deps.actor(request), request.method, request.url.path, {action: detail.pop("name", ""), **detail})


# ---- 目录（登录即可） ----


@router.get("/api/v1/packs/dev-skills/{category}")
async def download_dev_skill(category: str):
    """PL-27：按分类下载插件开发 skill（ADR-0081）。现打包，zip 最坏约 6 MiB；exporter 和不认识的分类回 404。"""
    with _errors():
        filename, data = await asyncio.to_thread(dev_skill.build, deps.workspaces, category)
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/api/v1/packs")
async def list_packs(request: Request, category: str = "", q: str = "", project_id: str = "",
                     installed_only: bool = False, updatable_only: bool = False):
    """PL-01：插件目录。带 project_id 时按本项目视角给状态（要是项目所有者）。"""
    if project_id:
        deps.require_owner(project_id, request)
    with _errors():
        return await asyncio.to_thread(
            catalog.catalog, deps.workspaces, category=category, q=q, project_id=project_id,
            viewer=deps.actor(request), installed_only=installed_only, updatable_only=updatable_only,
        )


@router.get("/api/v1/packs/{name}")
async def get_pack(name: str, request: Request, project_id: str = "", version: str = ""):
    """PL-02：插件详情。"""
    if project_id:
        deps.require_owner(project_id, request)
    with _errors():
        service.require_visible(deps.workspaces, name, deps.actor(request), project_id)
        detail = await asyncio.to_thread(catalog.detail, deps.workspaces, name, project_id=project_id, version=version)
    detail["released_at"] = deps.iso(detail.get("released_at"))
    detail["enabled_at"] = deps.iso(detail.get("enabled_at"))
    detail["changelog"] = [{**item, "date": deps.iso(item.get("date"))} for item in detail["changelog"]]
    return detail


@router.get("/api/v1/packs/{name}/versions/{version}/compatibility")
async def compatibility(name: str, version: str, request: Request, project_id: str = ""):
    """PL-03：兼容检查（试装配，不改锁文件）。"""
    if not project_id:
        raise ApiError("invalid_argument", "project_id 必填", 400, {"fields": {"project_id": "required"}})
    deps.require_owner(project_id, request)
    with _errors():
        service.require_visible(deps.workspaces, name, deps.actor(request), project_id)
        return await asyncio.to_thread(service.compatibility, deps.workspaces, name, version, project_id)


# ---- 本项目（所有者） ----


@router.get("/api/v1/projects/{project_id}/packs")
async def project_packs(project_id: str, request: Request):
    """PL-05：本项目的能力。"""
    deps.require_owner(project_id, request)
    with _errors():
        body = await asyncio.to_thread(service.project_packs, deps.workspaces, project_id)
    body.pop("status", None)
    body["items"] = [_iso_pack(item) for item in body["items"]]
    return body


@router.post("/api/v1/projects/{project_id}/packs")
async def enable_pack(project_id: str, request: Request):
    """PL-06：启用到本项目。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    name = str(body.get("name") or "")
    if not name:
        raise ApiError("invalid_argument", "name 必填", 400, {"fields": {"name": "required"}})
    with _errors():
        service.require_visible(deps.workspaces, name, deps.actor(request), project_id)
        result = await asyncio.to_thread(service.enable, deps.workspaces, project_id, name,
                                         body.get("version") or None, deps.actor(request))
    _audit(request, "pack.enable", {"name": name, "version": (result["pack"] or {}).get("locked_version"),
                                    "project_id": project_id})
    return JSONResponse(status_code=201, content=_iso_result(result))


@router.post("/api/v1/projects/{project_id}/packs/{name}/upgrade")
async def upgrade_pack(project_id: str, name: str, request: Request):
    """PL-07：升级。effective=next_autobuild 在自主构建（P3）之前按下一条消息生效处理。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    to_version = str(body.get("to_version") or "")
    if not to_version:
        raise ApiError("invalid_argument", "to_version 必填", 400, {"fields": {"to_version": "required"}})
    with _errors():
        result = await asyncio.to_thread(service.change_version, deps.workspaces, project_id, name, to_version,
                                         kind="upgrade", actor=deps.actor(request))
    _audit(request, "pack.upgrade", {"name": name, "version": to_version, "project_id": project_id})
    return _iso_result(result)


@router.post("/api/v1/projects/{project_id}/packs/{name}/rollback")
async def rollback_pack(project_id: str, name: str, request: Request):
    """PL-08：回滚到包仓库里保留的旧版本。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    to_version = str(body.get("to_version") or "")
    if not to_version:
        raise ApiError("invalid_argument", "to_version 必填", 400, {"fields": {"to_version": "required"}})
    with _errors():
        result = await asyncio.to_thread(service.change_version, deps.workspaces, project_id, name, to_version,
                                         kind="rollback", actor=deps.actor(request))
    _audit(request, "pack.rollback", {"name": name, "version": to_version, "project_id": project_id})
    return _iso_result(result)


@router.post("/api/v1/projects/{project_id}/packs/{name}/disable")
async def disable_pack(project_id: str, name: str, request: Request):
    """PL-09：停用（产物保留）。"""
    deps.require_writable(project_id, request)
    with _errors():
        result = await asyncio.to_thread(service.disable, deps.workspaces, project_id, name, deps.actor(request))
    _audit(request, "pack.disable", {"name": name, "project_id": project_id})
    return _iso_result(result)


@router.post("/api/v1/projects/{project_id}/packs/{name}/cancel-pending")
async def cancel_pending(project_id: str, name: str, request: Request):
    """PL-10：撤销待生效的切换。"""
    deps.require_writable(project_id, request)
    with _errors():
        result = await asyncio.to_thread(service.cancel_pending, deps.workspaces, project_id, name, deps.actor(request))
    return _iso_result(result)


@router.get("/api/v1/projects/{project_id}/extensions")
async def extensions(project_id: str, request: Request):
    """PL-12：扩展项注册表（左栏、子页、页面声明、界面入口、产物类型、视图、门禁、配置项）。"""
    deps.require_owner(project_id, request)
    with _errors():
        return await asyncio.to_thread(registry.extensions, deps.workspaces, project_id)


@router.get("/api/v1/projects/{project_id}/assembly")
async def assembly(project_id: str, request: Request):
    """PL-11：装配状态。"""
    deps.require_owner(project_id, request)
    with _errors():
        view = await asyncio.to_thread(service.assembly_view, deps.workspaces, project_id)
    if view["last_error"]:
        view["last_error"] = {**view["last_error"], "at": deps.iso(view["last_error"].get("at"))}
    return view


# ---- 上传包自带界面（ADR-0055） ----


@router.post("/api/v1/projects/{project_id}/packs/{name}/ui-sessions")
async def open_pack_ui(project_id: str, name: str, request: Request):
    """PL-20：给本项目启用中的上传包的编辑器发 iframe 地址（短期票据在路径里，不是登录态）。"""
    deps.require_owner(project_id, request)
    body = await deps.json_body(request)
    editor = body.get("editor") if isinstance(body.get("editor"), str) else ""
    try:
        result = await asyncio.to_thread(pack_ui.open_session, deps.workspaces, project_id, name, editor,
                                         user=deps.actor(request))
    except pack_ui.PackUiError as exc:
        raise ApiError(exc.code, str(exc), exc.status, exc.details) from exc
    return JSONResponse(status_code=201, content={**result, "expires_at": deps.iso(result["expires_at"])})


@router.get(pack_ui.URL_PREFIX + "/{ticket}/{path:path}", include_in_schema=False)
async def pack_ui_file(ticket: str, path: str):
    """PL-21：上传包 ui/ 下的静态资源；免登录（票据即授权），带隔离响应头。"""
    try:
        found, media = await asyncio.to_thread(pack_ui.resolve, deps.workspaces, ticket, path)
    except pack_ui.PackUiError as exc:
        raise ApiError(exc.code, str(exc), exc.status, None) from exc
    return FileResponse(found, media_type=media, headers=pack_ui.ISOLATION_HEADERS)


# ---- 上传与审核（管理员） ----


def _upload_view(record: dict, *, full: bool = False) -> dict:
    view = {
        "id": record["id"], "filename": record["filename"], "size": record["size"],
        "uploaded_at": deps.iso(record["uploaded_at"]), "uploaded_by": record["uploaded_by"],
        "tenant_label": record.get("tenant_label"), "scope": record.get("scope"), "state": record["state"],
        "summary": record["summary"],
        "manifest_ok": record["manifest_ok"], "checksums_ok": record["checksums_ok"],
        "name": record.get("name"), "version": record.get("version"),
    }
    if full:
        view["manifest"] = record.get("manifest")
        view["checks"] = record.get("checks") or []
        view["timeline"] = [{**item, "at": deps.iso(item["at"])} for item in record.get("timeline") or []]
    return view


def _check_and_maybe_publish(store: UploadStore, upload_id: str, legacy: bool, actor: str) -> None:
    record = store.run_checks(upload_id)
    if legacy and record["state"] == "passed":
        # 旧资产包上传：当作 core 新版本，检查通过直接发布到 stable（方案 2.3 第 4 步）
        store.publish(upload_id, channel="stable", rollout=None, by=actor)


async def _receive(file: UploadFile, limit: int) -> Path:
    """上传的 zip 落到临时文件，超过上限直接拒。"""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        size = 0
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > limit:
                Path(tmp.name).unlink(missing_ok=True)
                raise ApiError("payload_too_large", f"插件包超过 {limit // (1 << 20)} MiB", 413, {"limit_bytes": limit})
            tmp.write(chunk)
        return Path(tmp.name)


@router.post("/api/v1/admin/packs/uploads")
async def upload_pack(request: Request, background: BackgroundTasks, file: UploadFile = File(...),
                      tenant_label: str = Form("")):
    """PL-13：上传插件包，静态检查在后台跑（响应 state=checking）。"""
    deps.require_role(request, "admin")
    legacy = getattr(request.state, "legacy_path", "") == LEGACY_UPLOAD_PATH
    archive = await _receive(file, MAX_UPLOAD_BYTES)
    store = _uploads()
    try:
        with _errors():
            record = await asyncio.to_thread(store.create, archive, file.filename or "pack.zip",
                                             by=deps.actor(request), tenant_label=tenant_label, legacy=legacy)
    finally:
        archive.unlink(missing_ok=True)
    background.add_task(_check_and_maybe_publish, store, record["id"], legacy, deps.actor(request))
    _audit(request, "pack.upload", {"name": record["id"], "filename": record["filename"], "legacy": legacy})
    return JSONResponse(status_code=202, content=_upload_view(record))


@router.get("/api/v1/admin/packs/uploads")
async def list_uploads(request: Request, state: str = "", cursor: str = "", limit: int = 0):
    """PL-14：上传列表。"""
    deps.require_role(request, "admin")
    records = await asyncio.to_thread(_uploads().list, state)
    page = deps.page([_upload_view(record) for record in records], cursor, limit, {"state": state})
    return {"items": page["items"], "next_cursor": page["next_cursor"]}


@router.get("/api/v1/admin/packs/uploads/{upload_id}")
async def get_upload(upload_id: str, request: Request):
    """PL-15：上传详情与静态检查。"""
    deps.require_role(request, "admin")
    with _errors():
        return _upload_view(_uploads().get(upload_id), full=True)


@router.get("/api/v1/admin/packs/uploads/{upload_id}/report")
async def upload_report(upload_id: str, request: Request):
    """PL-16：下载检查报告（Markdown 附件）。"""
    deps.require_role(request, "admin")
    with _errors():
        text = _uploads().report(upload_id)
    return Response(content=text, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{upload_id}-report.md"'})


@router.post("/api/v1/admin/packs/uploads/{upload_id}/publish")
async def publish_upload(upload_id: str, request: Request):
    """PL-17：发布到通道。包进入只读的包仓库目录。"""
    deps.require_role(request, "admin")
    body = await deps.json_body(request)
    channel = str(body.get("channel") or "")
    rollout = body.get("rollout") if isinstance(body.get("rollout"), dict) else {}
    with _errors():
        record = await asyncio.to_thread(_uploads().publish, upload_id, channel=channel, rollout=rollout,
                                         by=deps.actor(request), auto_breaker=bool(body.get("auto_breaker")))
    _audit(request, "pack.publish", {"name": record["name"], "version": record["version"], "channel": channel})
    from ..services.packs import extensions

    await asyncio.to_thread(extensions.sync_config, deps.workspaces)  # 新版本声明的配置项进配置注册表
    label = deps.workspaces.packs.repository.manifest(record["name"], record["version"]).label
    return {"pack": {"name": record["name"], "version": record["version"], "label": label},
            "channel": channel, "rollout": rollout}


@router.post("/api/v1/admin/packs/uploads/{upload_id}/reject")
async def reject_upload(upload_id: str, request: Request):
    """PL-18：退回，通知上传者（自己退回自己的不通知）。"""
    from ..services import notifications

    deps.require_role(request, "admin")
    body = await deps.json_body(request)
    reason = str(body.get("reason") or "").strip()
    if not reason:
        raise ApiError("invalid_argument", "reason 必填", 400, {"fields": {"reason": "required"}})
    with _errors():
        record = await asyncio.to_thread(_uploads().reject, upload_id, reason=reason, by=deps.actor(request))
    uploader = str(record.get("uploaded_by") or "")
    if uploader and uploader != deps.actor(request):
        notifications.notify(uploader, "upload_rejected", "插件包被退回",
                             f"{record.get('name') or ''} {record.get('version') or ''}：{reason}".strip(),
                             action={"label": "查看", "page": "plugin-upload", "params": {"upload": upload_id}})
    return _upload_view(record)


# ---- 自己的插件包（登录即可，ADR-0079、ADR-0080）：只看得到自己的上传，装好后只有自己能启用 ----


def _own_upload(store: UploadStore, upload_id: str, request: Request) -> dict:
    record = store.get(upload_id)
    if record.get("scope") != SCOPE_PRIVATE or record.get("uploaded_by") != deps.actor(request):
        raise UploadError("not_found", "上传不存在", 404)
    return record


@router.post("/api/v1/me/pack-uploads")
async def upload_own_pack(request: Request, background: BackgroundTasks, file: UploadFile = File(...)):
    """PL-22：上传自己用的插件包，静态检查在后台跑。"""
    archive = await _receive(file, LIMITS["pack_private_upload_bytes"])
    store = _uploads()
    try:
        with _errors():
            record = await asyncio.to_thread(store.create, archive, file.filename or "pack.zip",
                                             by=deps.actor(request), private=True)
    finally:
        archive.unlink(missing_ok=True)
    background.add_task(_check_and_maybe_publish, store, record["id"], False, deps.actor(request))
    _audit(request, "pack.upload", {"name": record["id"], "filename": record["filename"], "scope": SCOPE_PRIVATE})
    return JSONResponse(status_code=202, content=_upload_view(record))


@router.get("/api/v1/me/pack-uploads")
async def list_own_uploads(request: Request, state: str = "", cursor: str = "", limit: int = 0):
    """PL-23：自己的上传。"""
    records = await asyncio.to_thread(_uploads().list, state, uploaded_by=deps.actor(request))
    page = deps.page([_upload_view(record) for record in records], cursor, limit, {"state": state})
    return {"items": page["items"], "next_cursor": page["next_cursor"]}


@router.get("/api/v1/me/pack-uploads/{upload_id}")
async def get_own_upload(upload_id: str, request: Request):
    """PL-24：自己的上传详情与静态检查。"""
    with _errors():
        return _upload_view(_own_upload(_uploads(), upload_id, request), full=True)


@router.get("/api/v1/me/pack-uploads/{upload_id}/report")
async def own_upload_report(upload_id: str, request: Request):
    """PL-25：下载自己的检查报告。"""
    store = _uploads()
    with _errors():
        _own_upload(store, upload_id, request)
        text = store.report(upload_id)
    return Response(content=text, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{upload_id}-report.md"'})


@router.post("/api/v1/me/pack-uploads/{upload_id}/install")
async def install_own_upload(upload_id: str, request: Request):
    """PL-26：安装到自己名下，之后在插件中心启用到项目。"""
    store = _uploads()
    with _errors():
        _own_upload(store, upload_id, request)
        record = await asyncio.to_thread(store.install_private, upload_id, by=deps.actor(request))
    _audit(request, "pack.private_install", {"name": record["name"], "version": record["version"]})
    return _upload_view(record)
