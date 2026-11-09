"""插件草稿（契约 F17 PD-01～PD-05；ADR-0087）：工作台插件开发模式里，草稿的列表、详情、文件和一键安装。

都要项目所有者；写操作要项目可写。读的都是主线上的草稿；安装走和「安装插件」上传同一份检查、同一套私有包规则。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

from fastapi import APIRouter, Request

from ..api import deps
from ..api.errors import ApiError
from ..services import audit, conversation_store, plugin_mode, project_events
from ..services.packs.assembler import AssemblyError
from ..services.packs.repository import RepositoryError
from ..services.packs.service import PackError
from ..services.packs.uploads import UploadError
from ..services.plugin_mode import drafts

router = APIRouter(prefix="/api/v1/projects/{project_id}/plugin-drafts", tags=["plugin-drafts"])


@contextmanager
def _errors():
    try:
        yield
    except (drafts.DraftError, PackError, UploadError, RepositoryError) as exc:
        raise ApiError(exc.code, str(exc), exc.status, exc.details) from exc
    except AssemblyError as exc:
        raise ApiError("assembly_failed", "装配失败，项目保持原版本", 409, {"error_id": None, "reason": str(exc)}) from exc


def _owner(request: Request) -> str:
    return deps.actor(request)


def _ensure_plugin_idle(project_id: str) -> None:
    """插件对话正在跑：它还会改草稿，网页上的修改和安装等它结束（409 session_busy）。"""
    from ..services.agent_sessions import manager

    live = manager.live_status(project_id)
    if live and live["busy"] and plugin_mode.is_plugin(project_id, live["conversation_id"]):
        raise ApiError("session_busy", "插件对话正在运行，请等它结束或先打断", 409,
                       {"running_conversation_id": live["conversation_id"]})


def _audit(request: Request, action: str, detail: dict) -> None:
    audit.log(deps.actor(request), request.method, request.url.path, {action: detail.pop("name", ""), **detail})


@router.get("")
async def list_drafts(project_id: str, request: Request):
    """PD-01：主线上的草稿，最近改的在前。"""
    deps.require_owner(project_id, request)
    with _errors():
        items = await asyncio.to_thread(drafts.list_views, deps.workspaces, project_id, _owner(request))
    return {"items": items}


@router.get("/{name}")
async def get_draft(project_id: str, name: str, request: Request):
    """PD-02：一份草稿的详情（文件、全量预检、和启用版本的差异）。"""
    deps.require_owner(project_id, request)
    with _errors():
        return await asyncio.to_thread(drafts.view, deps.workspaces, project_id, name, _owner(request))


@router.get("/{name}/files")
async def read_file(project_id: str, name: str, request: Request, path: str = ""):
    """PD-03：读草稿里的一个文件（文本最多 256 KiB）。"""
    deps.require_owner(project_id, request)
    with _errors():
        return await asyncio.to_thread(drafts.read_file, deps.workspaces, project_id, name, path)


@router.put("/{name}/files")
async def write_file(project_id: str, name: str, request: Request):
    """PD-04：网页改草稿里的一个文本文件，作为一次提交合入主线（可以撤回）。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    _ensure_plugin_idle(project_id)
    with _errors():
        return await asyncio.to_thread(drafts.write_file, deps.workspaces, project_id, name,
                                       str(body.get("path") or ""), body.get("content"), deps.actor(request))


@router.post("/{name}/install")
async def install_draft(project_id: str, name: str, request: Request):
    """PD-05：从主线取草稿，私有上传、静态检查、安装，启用到本项目（启用了别的版本就升级）。"""
    deps.require_writable(project_id, request)
    _ensure_plugin_idle(project_id)
    actor = deps.actor(request)
    with _errors():
        outcome = await asyncio.to_thread(drafts.install, deps.workspaces, project_id, name, actor)
    version = outcome["version"]
    if outcome["upload_id"]:  # 草稿和装过的版本一样时直接启用那个版本，没有上传
        _audit(request, "pack.upload", {"name": outcome["upload_id"], "filename": f"{name}-{version}.zip",
                                        "scope": "private", "source": "plugin_draft"})
        _audit(request, "pack.private_install", {"name": name, "version": version})
    _audit(request, f"pack.{outcome['action']}",
           {"name": name, "version": version, "project_id": project_id})
    from ..services.agent_sessions import manager

    conversation_id = await asyncio.to_thread(conversation_store.plugin_conversation, project_id, name)
    if conversation_id:
        manager.append_record(project_id, conversation_id, {"type": "notice", "payload": {
            "kind": "plugin_installed", "text": f"「{outcome['label']}」{version} 已装好，启用到本项目",
            "name": name, "version": version, "align": "center"}})
    # 「切回建模试用」进的是最近一条建模对话：那里也留一行
    chats = [item for item in await asyncio.to_thread(conversation_store.list_conversations, project_id)
             if item["kind"] == "chat"]
    if chats:
        manager.append_record(project_id, chats[0]["id"], {"type": "notice", "payload": {
            "kind": "plugin_enabled", "text": f"插件「{outcome['label']}」{version} 已启用",
            "name": name, "version": version, "align": "center"}})
    project_events.plugin_drafts_changed(project_id, [name])
    with _errors():
        draft = await asyncio.to_thread(drafts.view, deps.workspaces, project_id, name, actor)
    return {"draft": draft, "version": version, "upload_id": outcome["upload_id"], "enabled": outcome["enabled"]}
