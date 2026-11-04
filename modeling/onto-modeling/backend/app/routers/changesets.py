"""变更集与写入范围（契约 3.10 CS-01～CS-07；详设 8.2、4.3；方案 C7～C12；F3）。

F3 起助手每一轮结束就合入成一个版本，变更集是内部机制：用户只在两处碰到它——「撤回这一轮」（CS-05）与合入冲突
（CS-07）。CS-04 只留给遗留的待确认变更集（升级前留下的、自动合入没成的），界面不再调。
撤回、解决冲突在线程里做（git 与检查），对话里的卡片更新与提示行回到事件循环再追加（会话的订阅队列不是线程安全的）。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from ..api import deps
from ..api.errors import ApiError
from ..kernel.changesets import service

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["changesets"])


def _not_in_run(project_id: str, changeset_id: str) -> None:
    """自主构建这一段的变更集由运行器自动合入：运行没结束时不能手动确认、撤回（409 autobuild_running）。"""
    from ..services.autobuild import store as autobuild_store

    record = service.get(deps.workspaces, project_id, changeset_id)
    run = autobuild_store.active(project_id)
    if run is not None and record.get("source") == "autobuild" and record.get("run_id") == run["run_id"]:
        raise ApiError("autobuild_running", "自主构建运行中", 409, {"run_id": run["run_id"]})

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
    """合入、撤回之后：这个变更集的卡片更新、对话里一条提示行（EC-13）。"""
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
    # 建议条（EC-53）跟着换，和 FL-02 同一份
    conversations = {conversation_id for conversation_id, _item in records}
    if record.get("conversation_id"):
        conversations.add(record["conversation_id"])
    for conversation_id in sorted(conversations):
        suggestions = await asyncio.to_thread(_suggestions_after_close, project_id, conversation_id)
        if suggestions is None:
            continue
        try:
            manager.append_record(project_id, conversation_id, {"type": "suggestions", "payload": suggestions})
        except Exception:  # noqa: BLE001 - 同上
            continue


def _suggestions_after_close(project_id: str, conversation_id: str | None = None) -> dict | None:
    """按那条对话算（在跑、刚被打断、等你回答都要算进去），和 FL-02 同一份。"""
    from ..services.flow import view

    try:
        return dict(view.build(deps.workspaces.project_root(project_id),
                               conversation_id=conversation_id)["suggestions"])
    except Exception:  # noqa: BLE001 - 算不出建议不影响合入结果，下一轮结束时照常给
        return None


async def merge_and_announce(project_id: str, changeset_id: str, *, actor: str, expected_head: str | None = None,
                             choices: dict | None = None) -> dict:
    """CS-04 / CS-07 共用：合入并更新对话里的卡片。"""
    result = await asyncio.to_thread(service.merge, deps.workspaces, project_id, changeset_id, actor=actor,
                                     expected_head=expected_head, choices=choices)
    record = result.pop("record")
    await after_close(project_id, record, "merged", f"这一轮的修改已生效 · 版本 {result['version_label']}")
    return result


async def settle_and_announce(project_id: str, *, actor: str) -> dict | None:
    """遗留的待确认变更集（升级前留下的）合入成一个版本，并更新对话里的卡片（BP-03 用）。"""
    result = await asyncio.to_thread(service.settle, deps.workspaces, project_id, actor=actor)
    if result and result["state"] == "merged":
        await after_close(project_id, result["record"], "merged", f"这一轮的修改已生效 · 版本 {result['version_label']}")
    return result


async def discard_and_announce(project_id: str, changeset_id: str, *, actor: str) -> dict:
    """撤回这一轮：合入过的回到上一个版本（另存一个版本），冲突中还没合入的撤回副本。"""
    result = await asyncio.to_thread(service.revert_round, deps.workspaces, project_id, changeset_id, actor=actor)
    record = result.pop("record")
    text = f"已撤回这一轮 · 版本 {result['version_label']}" if result["state"] == "reverted" else "已撤回这一轮"
    await after_close(project_id, record, "discarded", text)
    return result


@router.post("/changesets/{changeset_id}/merge")
async def merge_changeset(project_id: str, changeset_id: str, request: Request):
    """CS-04 合入遗留的待确认变更集（F3 起界面不再调；一轮结束自动合入）。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    expected = body.get("expected_head")
    _not_in_run(project_id, changeset_id)
    if expected is not None and not isinstance(expected, str):
        raise ApiError("invalid_argument", "expected_head 要是提交号", 400, {"fields": {"expected_head": "type"}})
    return await merge_and_announce(project_id, changeset_id, actor=deps.actor(request), expected_head=expected or None)


@router.post("/changesets/{changeset_id}/discard")
async def discard_changeset(project_id: str, changeset_id: str, request: Request):
    """CS-05 撤回这一轮（F3：合入过的写反向提交，回到上一个版本）。"""
    deps.require_writable(project_id, request)
    _not_in_run(project_id, changeset_id)
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
