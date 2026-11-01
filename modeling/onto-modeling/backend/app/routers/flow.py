"""流程（契约 3.12 FL-01、FL-02；详设 6.7 第 6 项、方案 D14）。

P1 只读：流程图、节点、分组、主线下一步。跳过 / 恢复 / 开始节点（FL-03～FL-05）、节点状态的
完整计算、`flow.updated` 事件随项目事件流与流程运行时状态计算在 P2 做。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from ..api import deps
from ..services.flow import view

router = APIRouter(prefix="/api/v1/projects/{project_id}/flow", tags=["flow"])


@router.get("")
async def get_flow(project_id: str, request: Request, conversation_id: str = ""):
    """FL-01：流程。conversation_id 预留给按对话打断状态算建议（P2）。"""
    root = deps.require_owner(project_id, request)
    return await asyncio.to_thread(view.build, root)


@router.get("/suggestions")
async def get_suggestions(project_id: str, request: Request, conversation_id: str = ""):
    """FL-02：统一下一步（与 FL-01 的 suggestions 同一份）。"""
    root = deps.require_owner(project_id, request)
    flow = await asyncio.to_thread(view.build, root)
    return flow["suggestions"]
