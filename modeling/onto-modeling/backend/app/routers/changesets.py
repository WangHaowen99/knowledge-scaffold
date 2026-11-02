"""变更集与写入范围（契约 3.10 CS-01～CS-07；详设 8.2、4.3；方案 C7～C12）。

确认生效、撤回、解决冲突在线程里做（git 与门禁检查），对话里的卡片更新与提示行回到事件循环再追加
（会话的订阅队列不是线程安全的）。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from ..api import deps
from ..api.errors import ApiError
from ..kernel.changesets import service

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["changesets"])

STATES = ("open", "pending_confirm", "merged", "discarded", "conflict")
SOURCES = ("assistant", "autobuild", "studio", "mcp", "import", "plugin")


def _multi(request: Request, name: str, allowed: tuple[str, ...]) -> list[str]:
    values = []
    for raw in request.query_params.getlist(name):
        values += [item.strip() for item in raw.split(",") if item.strip()]
    bad = [item for item in values if item not in allowed]
    if bad:
        raise ApiError("invalid_argument", f"{name} 只能取 {'、'.join(allowed)}", 400, {"fields": {name: "enum"}})
    return values


@router.get("/changesets")
def list_changesets(project_id: str, request: Request, cursor: str = "", limit: int = 0):
    """CS-01。"""
    deps.require_owner(project_id, request)
    states = _multi(request, "state", STATES)
    sources = _multi(request, "source", SOURCES)
    items = [service.view(record)
             for record in service.records(deps.workspaces, project_id, states=states, sources=sources)]
    paged = deps.page(items, cursor, limit, binding={"state": states, "source": sources})
    return {"items": paged["items"], "next_cursor": paged["next_cursor"]}


@router.get("/changesets/{changeset_id}")
def get_changeset(project_id: str, changeset_id: str, request: Request):
    """CS-02。"""
    deps.require_owner(project_id, request)
    return service.detail(deps.workspaces, project_id, changeset_id)


@router.get("/changesets/{changeset_id}/diff")
def changeset_diff(project_id: str, changeset_id: str, request: Request, target: str = ""):
    """CS-03。"""
    deps.require_owner(project_id, request)
    return service.entity_diff_view(deps.workspaces, project_id, changeset_id, target.strip())


async def after_close(project_id: str, record: dict, kind: str, text: str) -> None:
    """确认生效、撤回之后：这个变更集的卡片更新、对话里一条提示行（EC-13）。"""
    from ..services.agent_sessions import manager

    records = await asyncio.to_thread(service.card_records, deps.workspaces, project_id, record)
    notice = service.notice_record(record, kind, text)
    if notice is not None:
        records.append(notice)
    for conversation_id, item in records:
        try:
            manager.append_record(project_id, conversation_id, item)
        except Exception:  # noqa: BLE001 - 对话已删除：卡片不更新，不影响合入结果
            continue


async def merge_and_announce(project_id: str, changeset_id: str, *, actor: str, expected_head: str | None = None,
                             choices: dict | None = None) -> dict:
    """CS-04 / CS-07 / 待处理里「确认生效」共用：合入并更新对话里的卡片。"""
    result = await asyncio.to_thread(service.merge, deps.workspaces, project_id, changeset_id, actor=actor,
                                     expected_head=expected_head, choices=choices)
    record = result.pop("record")
    await after_close(project_id, record, "merged",
                      f"这一轮的修改已确认生效 · 版本 {result['version_label']}")
    return result


async def discard_and_announce(project_id: str, changeset_id: str, *, actor: str) -> dict:
    result = await asyncio.to_thread(service.discard, deps.workspaces, project_id, changeset_id, actor=actor)
    record = result.pop("record")
    await after_close(project_id, record, "discarded", "这一轮的修改已撤回")
    return result


@router.post("/changesets/{changeset_id}/merge")
async def merge_changeset(project_id: str, changeset_id: str, request: Request):
    """CS-04 确认生效。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    expected = body.get("expected_head")
    if expected is not None and not isinstance(expected, str):
        raise ApiError("invalid_argument", "expected_head 要是提交号", 400, {"fields": {"expected_head": "type"}})
    return await merge_and_announce(project_id, changeset_id, actor=deps.actor(request), expected_head=expected or None)


@router.post("/changesets/{changeset_id}/discard")
async def discard_changeset(project_id: str, changeset_id: str, request: Request):
    """CS-05 撤回这一轮。"""
    deps.require_writable(project_id, request)
    return await discard_and_announce(project_id, changeset_id, actor=deps.actor(request))


@router.post("/changesets/{changeset_id}/conflicts/resolve")
async def resolve_conflicts(project_id: str, changeset_id: str, request: Request):
    """CS-07：选好保留哪一边后自动重试合入。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ApiError("invalid_argument", "要给出 choices", 400, {"fields": {"choices": "required"}})
    record = await asyncio.to_thread(service.get, deps.workspaces, project_id, changeset_id)
    if record["state"] != "conflict":
        raise ApiError("changeset_closed", "这一轮的修改没有待解决的冲突", 409, {"state": record["state"]})
    mapping = {}
    for item in choices:
        if not isinstance(item, dict) or not item.get("target") or item.get("keep") not in ("mainline", "changeset"):
            raise ApiError("invalid_argument", "choices 每项要有 target 与 keep（mainline|changeset）", 400,
                           {"fields": {"choices": "item"}})
        mapping[str(item["target"])] = item["keep"]
    return await merge_and_announce(project_id, changeset_id, actor=deps.actor(request), choices=mapping)


@router.get("/scope")
def get_scope(project_id: str, request: Request, target: str = ""):
    """CS-06 写入范围锁；带 target 时只问它锁没锁。"""
    deps.require_owner(project_id, request)
    if target.strip():
        return service.target_lock(deps.workspaces, project_id, target.strip())
    return service.scope_lock(deps.workspaces, project_id)
