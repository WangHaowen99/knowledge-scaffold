"""项目流 P-15 与用户流 A-12（契约 1.11、4.3、4.4）。对话流 S-13 在 sessions.py。

两条流的 id 是 `<epoch>.<n>`，重连带 `Last-Event-ID` 头或 `?last_event_id=`；补发不了时先发
`stream.reset`。令牌到期发 `auth.expired` 后关流；每个用户同时最多 8 条流（429 too_many_streams）。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from ..api import deps
from ..services import project_events, streams

router = APIRouter(tags=["events"])
project_events.install()


@router.get("/api/v1/projects/{project_id}/events")
async def project_stream(project_id: str, request: Request, last_event_id: str = ""):
    """P-15：项目流。首次打开（不带 id）不回放，前端先拉快照。"""
    deps.require_owner(project_id, request)
    # 记下当前流程作基线，之后的 flow.updated 只带变了的节点
    asyncio.get_running_loop().run_in_executor(None, project_events.ensure_baseline, project_id)
    return streams.channel_stream(
        request, streams.project_channel(project_id), streams.last_event_id(request, last_event_id),
        user=deps.actor(request), resync=streams.PROJECT_RESYNC,
    )


@router.get("/api/v1/me/events")
async def user_stream(request: Request, last_event_id: str = ""):
    """A-12：用户流（通知、项目卡片、维护状态）。本地模式的频道键是空串。"""
    username = deps.username(request)
    return streams.channel_stream(
        request, streams.user_channel(username), streams.last_event_id(request, last_event_id),
        user=deps.actor(request), resync=streams.USER_RESYNC,
    )
