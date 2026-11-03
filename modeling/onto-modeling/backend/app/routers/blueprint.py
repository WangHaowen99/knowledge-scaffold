"""建模蓝图（契约 3.12 BP-01～BP-03；详设第 9 章建议 2、6.8、8.5）。

蓝图文件与校验在 core 的 `blueprint.py`，放在哪、怎么写进去在 `services/flow/blueprint.py`。
资料分析那一轮结束就合入（F3），确认蓝图只在主线上标已确认；升级前留下的待确认变更集先合入，冲突时 409；
`next=L2` 往对话发「开始第一段」（同 S-20），`next=L4` 返回自主构建的开始前预览（B5b 登记）。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request

from ..api import deps
from ..api.errors import ApiError
from ..services.flow import blueprint as service

router = APIRouter(prefix="/api/v1/projects/{project_id}/blueprint", tags=["blueprint"])


async def announce(project_id: str) -> None:
    """蓝图改了：对话里的蓝图卡更新到最新；流程重算（资料分析的「蓝图待确认」、分段）。"""
    from ..services import project_events
    from ..services.agent_sessions import manager

    from .changesets import _suggestions_after_close

    conversations = set()
    for conversation_id, item in await asyncio.to_thread(service.card_updates, deps.workspaces, project_id):
        try:
            manager.append_record(project_id, conversation_id, item)
            conversations.add(conversation_id)
        except Exception:  # noqa: BLE001 - 对话已删除：卡片不更新
            continue
    # 蓝图卡所在对话的建议条跟着换（确认之后不再是「确认蓝图 / 调整分段」）
    suggestions = await asyncio.to_thread(_suggestions_after_close, project_id) if conversations else None
    for conversation_id in sorted(conversations) if suggestions is not None else ():
        try:
            manager.append_record(project_id, conversation_id, {"type": "suggestions", "payload": suggestions})
        except Exception:  # noqa: BLE001 - 同上
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
    """BP-03：确认蓝图；L2 开始第一段，L4 返回自主构建开始前预览。"""
    from . import changesets, sessions

    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    actor = deps.actor(request)
    prepared = await asyncio.to_thread(service.prepare_confirm, deps.workspaces, project_id, body)
    if prepared["settle"]:
        # 升级前留下的待确认变更集（资料分析那一轮）：先合入成一个版本，检查不拦；冲突时 409 指向待处理
        settled = await changesets.settle_and_announce(project_id, actor=actor)
        if settled and settled["state"] == "conflict":
            raise ApiError("merge_conflict", "建模蓝图所在的这一轮和你的修改有冲突，先在待处理里选好保留哪边", 409,
                           {"entities": (settled["record"].get("conflict") or {}).get("entities") or [],
                            "inbox_item_id": f"changeset_conflict:{settled['record']['id']}"})
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
