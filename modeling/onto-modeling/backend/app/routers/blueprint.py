"""建模蓝图（契约 3.12 BP-01～BP-03；详设第 9 章建议 2、6.8、8.5）。

蓝图文件与校验在 core 的 `blueprint.py`，放在哪、怎么写进去在 `services/flow/blueprint.py`。
确认蓝图时蓝图还在没确认的变更集里（资料分析那一轮），先把这一轮确认生效（同 CS-04），再标已确认；
`next=L2` 往对话发「开始第一个切片」（同 S-20），`next=L4` 返回自主构建的开始前预览（B5b 登记）。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request

from ..api import deps
from ..services.flow import blueprint as service

router = APIRouter(prefix="/api/v1/projects/{project_id}/blueprint", tags=["blueprint"])


async def announce(project_id: str) -> None:
    """蓝图改了：对话里的蓝图卡更新到最新；流程重算（资料分析的「蓝图待确认」、切片）。"""
    from ..services import project_events
    from ..services.agent_sessions import manager

    for conversation_id, item in await asyncio.to_thread(service.card_updates, deps.workspaces, project_id):
        try:
            manager.append_record(project_id, conversation_id, item)
        except Exception:  # noqa: BLE001 - 对话已删除：卡片不更新
            continue
    project_events.schedule_flow_refresh(project_id, delay=0)


@router.get("")
async def get_blueprint(project_id: str, request: Request):
    """BP-01。"""
    deps.require_owner(project_id, request)
    return await asyncio.to_thread(service.view, deps.workspaces, project_id)


@router.put("")
async def save_blueprint(project_id: str, request: Request):
    """BP-02：保存为草案（草案号不变）或保存调整（草案号加 1）。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    result = await asyncio.to_thread(service.save, deps.workspaces, project_id, body, actor=deps.actor(request))
    await announce(project_id)
    return result


@router.post("/confirm")
async def confirm_blueprint(project_id: str, request: Request):
    """BP-03：确认蓝图；L2 开始第一个切片，L4 返回自主构建开始前预览。"""
    from . import changesets, sessions

    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    actor = deps.actor(request)
    prepared = await asyncio.to_thread(service.prepare_confirm, deps.workspaces, project_id, body)
    if prepared["changeset_id"]:
        # 蓝图还在资料分析那一轮的修改里：确认蓝图就是确认这一轮（门禁、冲突照 CS-04 处理）
        await changesets.merge_and_announce(project_id, prepared["changeset_id"], actor=actor)
    blueprint = await asyncio.to_thread(service.mark_confirmed, deps.workspaces, project_id, actor=actor)
    await announce(project_id)
    if prepared["next"] == "L4":
        preview = await asyncio.to_thread(service.autobuild_preview, deps.workspaces, project_id, blueprint, actor=actor)
        return {"blueprint": blueprint, "autobuild_preview": preview}
    command, args = await asyncio.to_thread(service.first_command, deps.workspaces, project_id)
    response = await sessions.dispatch_command(project_id, root, request, {
        "command": command, "args": args, "origin": {"page": "wb-blueprint", "label": "建模蓝图"},
        "conversation_id": body.get("conversation_id") or "-",
    })
    return {"blueprint": blueprint, "command_task": json.loads(response.body)["task"]}
