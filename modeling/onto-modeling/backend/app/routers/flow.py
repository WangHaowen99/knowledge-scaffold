"""流程（契约 3.12 FL-01～FL-05；详设 6.6、6.7、6.9、6.10；方案 D13、D14、D16）。

节点状态由内核算（执行记录、变更集、产物指纹），下一步与 `state_helper suggest`、宿主注入同一份。
跳过与恢复只对可选节点；开始节点等价于用节点声明的入口命令调 S-20。
按段的节点（建模）带 `slice_id` 时：跳过、恢复的是这一段的可选步骤（缺省「规则」），开始的是这一段的下一步。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from ..api import deps
from ..api.errors import ApiError
from ..services.flow import model, node_kinds, overlay, view
from ..services.flow import state as node_state

router = APIRouter(prefix="/api/v1/projects/{project_id}/flow", tags=["flow"])


@router.get("")
async def get_flow(project_id: str, request: Request, conversation_id: str = ""):
    """FL-01：流程。带 conversation_id 时建议按这条对话的打断状态算。"""
    root = deps.require_owner(project_id, request)
    return await asyncio.to_thread(view.build, root, conversation_id=conversation_id or None)


@router.get("/suggestions")
async def get_suggestions(project_id: str, request: Request, conversation_id: str = ""):
    """FL-02：统一下一步（与 FL-01 的 suggestions 同一份）。"""
    root = deps.require_owner(project_id, request)
    flow = await asyncio.to_thread(view.build, root, conversation_id=conversation_id or None)
    return flow["suggestions"]


def _node(root, node_id: str) -> dict:
    flow = model.load_for(root)
    node = next((item for item in flow.get("nodes") or [] if item.get("id") == node_id), None)
    if node is None:
        raise ApiError("node_not_found", "节点不存在或没有出现在本项目的流程里", 404)
    return node


def _node_view(root, node_id: str) -> dict:
    flow = view.build(root)
    return {"node": next(item for item in flow["nodes"] if item["id"] == node_id), "flow_hash": flow["flow_hash"]}


def _after_change(project_id: str) -> None:
    from ..services import project_events

    project_events.schedule_flow_refresh(project_id, delay=0)


def _slice_step(node: dict, body: dict, *, optional: bool) -> tuple[str, str] | None:
    """按段的节点带了 slice_id：(分段, 步骤)；步骤缺省「规则」（可选步骤）。"""
    if not node.get("per_slice") or not body.get("slice_id"):
        return None
    step_id = str(body.get("step") or "rule")
    step = next((item for item in node.get("steps") or [] if item.get("id") == step_id), None)
    if step is None or (optional and not step.get("optional")):
        raise ApiError("node_not_skippable", "这一步不能跳过", 409)
    return str(body["slice_id"]), step_id


@router.post("/nodes/{node_id}/skip")
async def skip_node(project_id: str, node_id: str, request: Request):
    """FL-03：跳过可选节点（主线与必经节点不能跳过）；建模节点带 slice_id 时跳过这一段的规则。"""
    from ..services.flow import slices

    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    node = _node(root, node_id)
    pair = _slice_step(node, body, optional=True)
    if pair is not None:
        slices.skip(root, *pair, reason=str(body.get("reason") or "")[:200])
        _after_change(project_id)
        return await asyncio.to_thread(_node_view, root, node_id)
    if not node.get("optional") or node.get("section", "required") == "required":
        raise ApiError("node_not_skippable", "必经节点不能跳过", 409)
    node_state.skip(root, node_id, reason=str(body.get("reason") or "")[:200], slice_id=body.get("slice_id") or None)
    _after_change(project_id)
    return await asyncio.to_thread(_node_view, root, node_id)


@router.post("/nodes/{node_id}/restore")
async def restore_node(project_id: str, node_id: str, request: Request):
    """FL-04：恢复跳过的节点。"""
    from ..services.flow import slices

    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    node = _node(root, node_id)
    pair = _slice_step(node, body, optional=True)
    restored = slices.restore(root, *pair) if pair is not None else node_state.restore(root, node_id)
    if not restored:
        raise ApiError("node_not_skipped", "节点没有被跳过", 409)
    _after_change(project_id)
    return await asyncio.to_thread(_node_view, root, node_id)


@router.post("/nodes/{node_id}/start")
async def start_node(project_id: str, node_id: str, request: Request):
    """FL-05：开始节点（含补做、刷新）。agent 节点用入口命令发到对话，等价于 S-20；脚本、人工、外部节点
    由内核执行（详设 6.5，见 services/flow/node_kinds）。"""
    from . import sessions

    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    node = _node(root, node_id)
    if node_id in overlay.broken_nodes(model.load_for(root)):
        raise ApiError("pack_unavailable", "这个节点所属的插件版本已被运维熔断", 409, {"reason": "breaker"})
    page = next((str(action["page"]) for action in node.get("actions") or [] if action.get("page")), "")
    if page and not (node.get("entry") or {}).get("command") and not node.get("decision"):
        # 在页面里做的节点（发布）：没有对话入口，也不是出决策卡的人工节点
        raise ApiError("invalid_argument", "这个节点在页面里操作", 400, {"fields": {"node_id": "no_entry"}, "page": page})
    kind = node_kinds.kind_of(node)
    if kind != "agent":
        return await _start_kernel_node(project_id, root, node, kind, body, request)
    command = str((node.get("entry") or {}).get("command") or "").strip() or next(
        (str(action["command"]).strip() for action in node.get("actions") or []
         if str(action.get("command") or "").startswith("/")), "")
    if not command.startswith("/"):
        raise ApiError("invalid_argument", "这个节点没有对话入口（在面板里操作）", 400, {"fields": {"node_id": "no_entry"}})
    origin = body.get("origin") if isinstance(body.get("origin"), dict) else {}
    if not origin.get("page"):
        origin = {"page": "flow", "label": node.get("title") or node_id}
    slice_id = str(body.get("slice_id") or "")
    if node.get("per_slice"):
        command, slice_id = await asyncio.to_thread(_slice_command, root, node, slice_id, body.get("step"), command)
    payload = {"command": command, "args": body.get("args") or {}, "origin": origin,
               "conversation_id": body.get("conversation_id") or "-"}
    if slice_id:
        payload["args"] = {**payload["args"], "slice": slice_id}
    return await sessions.dispatch_command(project_id, root, request, payload)


async def _start_kernel_node(project_id: str, root, node: dict, kind: str, body: dict, request: Request) -> dict:
    """脚本节点在沙箱里跑完再返回；人工节点出一张决策卡；外部节点要的提供方本期都不可用。"""
    from ..services.agent_sessions import manager
    from . import sessions

    try:
        if kind == "external":
            missing = node_kinds.missing_providers([node])
            raise node_kinds.NodeRunError("pack_unavailable", "需要的数据接入插件不可用", 409,
                                          {"reason": "not_installed", "providers": missing})
        if kind == "script":
            run = await asyncio.to_thread(node_kinds.run_script, deps.workspaces, project_id, node,
                                          actor=deps.actor(request))
            _after_change(project_id)
            return {**await asyncio.to_thread(_node_view, root, node["id"]), "run": run}
        conversation_id = sessions._resolve(project_id, str(body.get("conversation_id") or "-"))
        question = node_kinds.ask(project_id, root, node, conversation_id,
                                  lambda cid, record: manager.append_record(project_id, cid, record))
        _after_change(project_id)  # 决策卡也进待处理（inbox.updated）
        return {**await asyncio.to_thread(_node_view, root, node["id"]), "question": question}
    except node_kinds.NodeRunError as exc:
        raise ApiError(exc.code, str(exc), exc.status, exc.details) from exc


async def answer_human_node(project_id: str, question_id: str, body: dict, actor: str) -> dict | None:
    """S-11 的退路：会话里没有这个问题时，看是不是人工节点的决策卡（routers/sessions 调）。"""
    from ..services.agent_sessions import manager

    root = deps.workspaces.project_root(project_id)
    if question_id in node_kinds.open_questions(root) and deps.workspaces.get_project(project_id).get("archived"):
        # 回答会在主线写记录文件：已归档（只读）的项目不行（审查第 11 条；IN-03 本来就要求可写）
        raise ApiError("project_archived", "项目已归档，只能查看", 409)
    try:
        found = await asyncio.to_thread(node_kinds.answer, deps.workspaces, project_id, question_id, body,
                                        actor=actor)
    except node_kinds.NodeRunError as exc:
        raise ApiError(exc.code, str(exc), exc.status, exc.details) from exc
    if found is None:
        return None
    question, record = found
    manager.append_record(project_id, question["conversation_id"], record)
    _after_change(project_id)
    return question


def _slice_command(root, node: dict, slice_id: str, step_id, fallback: str) -> tuple[str, str]:
    """建模节点开始：给了步骤用它的命令；给了分段取这一段还没做完的第一步；都没给取主线下一步（过期的先刷新）。
    返回 (命令, 分段)；全量模式不带分段。"""
    from ..services.flow import blueprint, slices

    steps = [item for item in node.get("steps") or [] if item.get("command")]
    if step_id:
        found = next((item for item in steps if item["id"] == step_id), None)
        if found is None:
            raise ApiError("invalid_argument", "这一步不存在", 400, {"fields": {"step": "unknown"}})
        return found["command"], slice_id
    result = slices.compute(root, model.load_for(root), doc=blueprint.load_doc(root), persist=False) or {}
    if not slice_id:
        following = result.get("slice_next") or {}
        if not following:
            return fallback, ""
        return following["command"], following["slice_id"] if following.get("sliced") else ""
    summary = next((item for item in result.get("slices") or [] if item["id"] == slice_id), None)
    if summary is None:
        raise ApiError("invalid_argument", "分段不存在或已排除", 400, {"fields": {"slice_id": "unknown"}})
    found = next((item for item in steps if not item.get("optional")
                  and summary["stages"].get(item["id"]) not in ("confirmed", "auto")), None)
    return (found["command"] if found else fallback), (slice_id if result.get("sliced") else "")
