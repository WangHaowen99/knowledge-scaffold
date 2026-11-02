"""待处理（契约 3.13 IN-01～IN-04；详设 4.7）与变更点清单（契约 3.14 INC-01～INC-05）。

处理事项（IN-03）一律转给等价接口：决策 → S-11（人工节点的卡走 S-11 同一个退路），续跑 / 放弃 → S-16 / S-17，
确认生效 / 撤回 → CS-04 / CS-05，合入冲突 → CS-07，刷新 / 重做 → FL-05，报告 → 以校验命令调 S-20，
外部反馈 → 以反馈演进命令调 S-20，变更清单 → INC-03 / INC-02 / INC-04。忽略只记在主线工作区的 `.onto/run/`（不进 git）。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..api import deps
from ..api.errors import ApiError
from ..services.flow import change_lists, inbox
from ..services.flow import state as node_state

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["inbox"])


def _multi(request: Request, name: str) -> list[str]:
    values = []
    for raw in request.query_params.getlist(name):
        values += [item.strip() for item in raw.split(",") if item.strip()]
    return values


@router.get("/inbox")
async def list_inbox(project_id: str, request: Request, filter: str = "all", pack: str = "", run_id: str = "",
                     cursor: str = "", limit: int = 0):
    """IN-01。"""
    deps.require_owner(project_id, request)
    if filter not in inbox.FILTERS:
        raise ApiError("invalid_argument", f"filter 只能取 {'、'.join(inbox.FILTERS)}", 400, {"fields": {"filter": "enum"}})
    kinds = _multi(request, "kind")
    bad = [kind for kind in kinds if kind not in inbox.KIND_LABELS]
    if bad:
        raise ApiError("invalid_argument", "kind 取值不合法", 400, {"fields": {"kind": "enum"}})
    items, counts, today = await asyncio.to_thread(inbox.listing, deps.workspaces, project_id, filter_=filter,
                                                   kinds=kinds, pack=pack or None, run_id=run_id or None)
    paged = deps.page([inbox.public(item) for item in items], cursor, limit,
                      binding={"filter": filter, "kind": kinds, "pack": pack, "run_id": run_id})
    return {"items": paged["items"], "next_cursor": paged["next_cursor"], "counts": counts, "today": today,
            "autobuild_summary": None}


async def _item_or_404(project_id: str, item_id: str) -> dict:
    item = await asyncio.to_thread(inbox.find, deps.workspaces, project_id, item_id)
    if item is None or item.get("_dismissed"):
        raise ApiError("not_found", "这一项已经不在待处理里了，刷新列表", 404)
    return item


def _detail(project_id: str, item: dict) -> dict | None:
    from ..kernel.changesets import service as changesets

    extra = item.get("_extra") or {}
    kind = item["kind"]
    if kind == "decision":
        return extra.get("question")
    if kind == "resumable":
        return extra.get("exec")
    if kind == "pending_changeset":
        return changesets.detail(deps.workspaces, project_id, extra["changeset"]["id"])
    if kind == "changeset_conflict":
        record = extra["changeset"]
        return {"changeset": changesets.view(record), "entities": (record.get("conflict") or {}).get("entities") or []}
    if kind == "stale":
        node = extra.get("node") or {}
        return {"upstream_diff": {"title": f"上游（{'、'.join(node.get('stale_on') or [])}）在确认之后改过", "lines": []},
                "impact": {"expected_before": "已确认", "now": "过期", "action": f"重新执行「{node.get('title')}」",
                           "eta_ms": 0},
                "siblings": 0}
    if kind == "review":
        return {**(extra.get("review") or {}), "node": {"id": (extra.get("node") or {}).get("id"),
                                                       "title": (extra.get("node") or {}).get("title")},
                "options": [{"id": "accept", "label": "接受"}, {"id": "rejudge", "label": "重做这一步"}]}
    if kind == "change_list":
        return change_lists.view(extra["change_list"])
    if kind == "feedback":
        return extra.get("feedback")
    if kind == "report":
        return {"path": extra.get("path"), "pending": extra.get("pending")}
    if kind == "format_migration":
        return {key: extra.get(key) for key in ("type", "key", "from", "to")}
    return None


@router.get("/inbox/{item_id}")
async def get_inbox_item(project_id: str, item_id: str, request: Request):
    """IN-02。"""
    deps.require_owner(project_id, request)
    item = await _item_or_404(project_id, item_id)
    detail = await asyncio.to_thread(_detail, project_id, item)
    return {**inbox.public(item), "detail": detail}


async def _run_action(project_id: str, item: dict, action: str, request: Request, body: dict) -> dict:
    from . import changesets as changeset_routes
    from . import flow as flow_routes
    from . import sessions as session_routes

    extra = item.get("_extra") or {}
    source_id = item["id"].split(":", 1)[1]
    root = deps.workspaces.project_root(project_id)
    actor = deps.actor(request)
    if action == "dismiss" or action == "keep_stale":
        inbox.dismiss(root, item["id"], by=actor, fingerprint=item.get("_fp"))
        return {}
    if action == "answer" and extra.get("blueprint_question"):
        from ..services.flow import blueprint
        from ..services.workspace.errors import WorkspaceError

        try:
            await asyncio.to_thread(blueprint.answer, deps.workspaces, project_id, extra["blueprint_question"],
                                    body, actor=actor)
        except WorkspaceError as exc:
            if exc.code == "invalid_argument":
                raise ApiError("answer_invalid", str(exc), 400) from exc
            raise
        from .blueprint import announce

        await announce(project_id)
        return {"question": {**extra["question"], "state": "answered",
                             "answer": {key: body.get(key) for key in ("option_id", "custom_text") if body.get(key)}}}
    if action == "answer" and item["kind"] == "decision":
        answer = {key: body.get(key) for key in ("option_id", "custom_text") if body.get(key)}
        if extra.get("human_node"):
            # 人工节点的决策卡：和 S-11 同一个退路（结论写进节点声明的记录文件）
            question = await flow_routes.answer_human_node(project_id, source_id, answer, actor)
            if question is None:
                raise ApiError("question_gone", "问题已回答、撤回或失效", 410, {"state": "gone"})
            return {"question": question}
        from ..services.agent_sessions import SessionError

        try:
            question = session_routes.manager.answer_question(project_id, source_id, answer)
        except SessionError as exc:
            if exc.status == 410:
                raise ApiError("question_gone", "问题已回答、撤回或失效", 410, {"state": "gone"}) from exc
            raise ApiError("answer_invalid", str(exc), 400) from exc
        return {"question": question}
    if action == "resume":
        response = await session_routes.resume_exec(project_id, source_id, request)
        return _json(response)
    if action == "abandon":
        return await session_routes.abandon_exec(project_id, source_id, request)
    if action == "merge":
        return await changeset_routes.merge_and_announce(project_id, source_id, actor=actor)
    if action == "discard":
        return await changeset_routes.discard_and_announce(project_id, source_id, actor=actor)
    if action == "resolve":
        return await changeset_routes.resolve_conflicts(project_id, source_id, request)
    if action in ("refresh", "rejudge"):
        response = await flow_routes.start_node(project_id, source_id, request)
        return {"command_task": _json(response).get("task")}
    if action == "accept" and item["kind"] == "review":
        node_state.accept_review(root, source_id)
        return {"accepted": True}
    if action == "send_to_assistant":
        payload = {"command": "/ontology-validate", "origin": {"page": "inbox", "label": "发给助手修复"},
                   "text": f"处理报告 {extra.get('path')} 里还没处理的 [ ] 条目", "conversation_id": "-"}
        response = await session_routes.dispatch_command(project_id, root, request, payload)
        return {"command_task": _json(response).get("task")}
    if action == "send_to_evolution":
        payload = {"command": "/ontology-evolution", "args": {"feedback_id": source_id},
                   "origin": {"page": "inbox", "label": "交给反馈演进"}, "conversation_id": "-"}
        response = await session_routes.dispatch_command(project_id, root, request, payload)
        # 交出去就算处理了：这批反馈没有「已处理」状态（R-11、R-12 未做），按处理记下（审查第 22 条）
        inbox.dismiss(root, item["id"], by=actor, fingerprint=item.get("_fp"))
        return {"command_task": _json(response).get("task")}
    if action == "migrate":
        raise ApiError("pack_not_enabled", "登记这个产物的包没有提供迁移命令", 409,
                       {"pack": item.get("pack"), "effective": None})
    if item["kind"] == "change_list":
        if action == "accept_points":
            return {"change_list": change_lists.view(_call(change_lists.accept, root, source_id,
                                                           point_ids=list(body.get("point_ids") or [])))}
        if action == "rewrite_point":
            record = change_lists.get(root, source_id) or {}
            points = [dict(point, rewritten_text=body.get("text")) if point["id"] == body.get("point_id") else point
                      for point in record.get("points") or []]
            return {"change_list": change_lists.view(_call(change_lists.save_points, root, source_id,
                                                           record.get("revision") or "", points))}
        if action == "execute":
            response = await execute_change_list(project_id, source_id, request,
                                                 {"base_revision": (change_lists.get(root, source_id) or {}).get("revision")})
            return _json(response)
    raise ApiError("invalid_argument", f"「{item['kind_label']}」现在不支持 {action}", 400, {"fields": {"action": "enum"}})


def _json(response) -> dict:
    if isinstance(response, JSONResponse):
        import json

        return json.loads(response.body)
    return response or {}


def _call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except change_lists.ChangeListError as exc:
        raise ApiError(exc.code, str(exc), exc.status, exc.details) from exc


@router.post("/inbox/{item_id}/actions")
async def act_on_item(project_id: str, item_id: str, request: Request):
    """IN-03。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    action = str(body.get("action") or "")
    item = await _item_or_404(project_id, item_id)
    if action not in item["actions"]:
        raise ApiError("invalid_argument", f"「{item['kind_label']}」不能做 {action}", 400, {"fields": {"action": "enum"}})
    result = await _run_action(project_id, item, action, request, body)
    root = deps.workspaces.project_root(project_id)
    inbox.record_handled(root, item_id, action)
    remaining = await asyncio.to_thread(inbox.collect, deps.workspaces, project_id)
    active = [entry for entry in remaining if not entry.get("_dismissed")]
    await asyncio.to_thread(inbox.refresh, deps.workspaces, project_id)
    return {"item_id": item_id, "resolved": all(entry["id"] != item_id for entry in active), "result": result,
            "next_item_id": next((entry["id"] for entry in active if entry["id"] != item_id), None)}


@router.post("/inbox/batch")
async def batch(project_id: str, request: Request):
    """IN-04。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    action = body.get("action")
    if action not in ("accept_high_confidence", "refresh_stale", "accept_change_points"):
        raise ApiError("invalid_argument", "action 取值不合法", 400, {"fields": {"action": "enum"}})
    root = deps.workspaces.project_root(project_id)
    if action == "accept_change_points":
        list_id = str(body.get("change_list_id") or "")
        record = _call(change_lists.accept, root, list_id, point_ids=list(body.get("point_ids") or []))
        accepted = [point["id"] for point in record["points"] if point.get("state") == "accepted"]
        return {"accepted": len(accepted), "skipped": [], "command_task": None}
    items = await asyncio.to_thread(inbox.collect, deps.workspaces, project_id)
    if action == "accept_high_confidence":
        # 自主构建的高置信自动决定在 P3（决策台账）；现在没有这类事项
        return {"accepted": 0, "skipped": [], "command_task": None}
    stale = [item for item in items if item["kind"] == "stale" and not item.get("_dismissed")
             and (not body.get("item_ids") or item["id"] in body["item_ids"])]
    if not stale:
        return {"accepted": 0, "skipped": [], "command_task": None}
    # 一次只能跑一条命令：刷新最上游的那一个（按流程里的节点顺序，审查第 18 条），其余等它确认生效后再刷新
    from ..services.flow import model as flow_model
    from . import flow as flow_routes

    order = {node["id"]: index for index, node in enumerate(flow_model.load_for(root).get("nodes") or [])}
    stale.sort(key=lambda item: order.get(item["id"].split(":", 1)[1], len(order)))
    first, rest = stale[0], stale[1:]
    response = await flow_routes.start_node(project_id, first["id"].split(":", 1)[1], request)
    return {"accepted": 1, "skipped": [{"id": item["id"], "reason": "一次只跑一条命令，等前一个确认生效后再刷新"}
                                       for item in rest],
            "command_task": _json(response).get("task")}


# ---- INC-01～INC-05 ----


def _list_or_404(project_id: str, list_id: str) -> dict:
    record = change_lists.get(deps.workspaces.project_root(project_id), list_id)
    if record is None:
        raise ApiError("not_found", "变更清单不存在", 404)
    return record


@router.get("/change-lists/{list_id}")
def get_change_list(project_id: str, list_id: str, request: Request):
    """INC-01。"""
    deps.require_owner(project_id, request)
    return change_lists.view(_list_or_404(project_id, list_id))


@router.put("/change-lists/{list_id}")
async def save_change_list(project_id: str, list_id: str, request: Request):
    """INC-02：执行前可以编辑，不受写入范围锁约束。"""
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    if not isinstance(body.get("points"), list):
        raise ApiError("invalid_argument", "要给出完整的 points", 400, {"fields": {"points": "required"}})
    record = _call(change_lists.save_points, root, list_id, str(body.get("base_revision") or ""), body["points"])
    from ..services import streams

    impact = change_lists.impact(record)
    streams.publish_project(project_id, "change_list.updated",
                            {"change_list_id": list_id, "revision": record["revision"], "impact_ms": impact["duration_ms"]})
    return {"change_list": change_lists.view(record), "impact_ms": impact["duration_ms"]}


@router.post("/change-lists/{list_id}/accept")
async def accept_change_points(project_id: str, list_id: str, request: Request):
    """INC-03。"""
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    if not body.get("point_ids") and not body.get("high_confidence"):
        raise ApiError("invalid_argument", "point_ids 与 high_confidence 二选一", 400, {"fields": {"point_ids": "one_of"}})
    record = _call(change_lists.accept, root, list_id, point_ids=list(body.get("point_ids") or []),
                   high_confidence=bool(body.get("high_confidence")))
    return change_lists.view(record)


async def execute_change_list(project_id: str, list_id: str, request: Request, body: dict):
    from ..kernel.changesets import service as changesets
    from . import sessions as session_routes

    root = deps.workspaces.project_root(project_id)
    record = _list_or_404(project_id, list_id)
    if record.get("state") in ("confirmed", "executed"):
        # 已经发出执行、或已经执行完：同一份清单不再执行一次（审查第 12 条）
        raise ApiError("changeset_closed", "清单已执行", 409, {"state": record["state"]})
    if str(body.get("base_revision") or "") != record.get("revision"):
        raise ApiError("revision_conflict", "清单已被修改，请刷新", 409,
                       {"expected": body.get("base_revision"), "actual": record.get("revision")})
    pending = change_lists.ready_to_execute(record)
    if pending:
        raise ApiError("invalid_argument", "还有变更点没确认", 400, {"unconfirmed": pending})
    copy = await asyncio.to_thread(changesets.copy_for_turn, deps.workspaces, project_id, root)
    change_lists.export(root, copy, record)
    payload = {"command": "/ontology-update", "args": {"change_list_id": list_id},
               "origin": {"page": "incremental", "label": "确认清单并执行"},
               "conversation_id": record.get("conversation_id") or "-"}
    response = await session_routes.dispatch_command(project_id, root, request, payload)
    current = changesets.current(deps.workspaces, project_id)
    change_lists.mark(root, list_id, state="confirmed", changeset_id=(current or {}).get("id"))
    data = _json(response)
    return JSONResponse(status_code=202, content={"command_task": data.get("task"),
                                                  "changeset_id": (current or {}).get("id")})


@router.post("/change-lists/{list_id}/execute")
async def execute(project_id: str, list_id: str, request: Request):
    """INC-04：往对话发 `/ontology-update change_list_id=<清单>`。"""
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    return await execute_change_list(project_id, list_id, request, body)


@router.get("/change-lists/{list_id}/impact")
def change_list_impact(project_id: str, list_id: str, request: Request):
    """INC-05。"""
    deps.require_owner(project_id, request)
    return change_lists.impact(_list_or_404(project_id, list_id))
