"""流程（契约 3.12 FL-01～FL-05；详设 6.6、6.7、6.9、6.10；方案 D13、D14、D16）。

节点状态由内核算（执行记录、变更集、产物指纹），下一步与 `state_helper suggest`、宿主注入同一份。
跳过与恢复只对可选节点；开始节点等价于用节点声明的入口命令调 S-20。
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


@router.post("/nodes/{node_id}/skip")
async def skip_node(project_id: str, node_id: str, request: Request):
    """FL-03：跳过可选节点（主线与必经节点不能跳过）。"""
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    node = _node(root, node_id)
    if not node.get("optional") or node.get("section", "required") == "required":
        raise ApiError("node_not_skippable", "主线或必经节点不能跳过", 409)
    node_state.skip(root, node_id, reason=str(body.get("reason") or "")[:200], slice_id=body.get("slice_id") or None)
    _after_change(project_id)
    return await asyncio.to_thread(_node_view, root, node_id)


@router.post("/nodes/{node_id}/restore")
async def restore_node(project_id: str, node_id: str, request: Request):
    """FL-04：恢复跳过的节点。"""
    root = deps.require_writable(project_id, request)
    _node(root, node_id)
    if not node_state.restore(root, node_id):
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
    payload = {"command": command, "args": body.get("args") or {}, "origin": origin,
               "conversation_id": body.get("conversation_id") or "-"}
    if body.get("slice_id"):
        payload["args"] = {**payload["args"], "slice": body["slice_id"]}
    return await sessions.dispatch_command(project_id, root, request, payload)


async def _start_kernel_node(project_id: str, root, node: dict, kind: str, body: dict, request: Request) -> dict:
    """脚本节点在沙箱里跑完再返回；人工节点出一张决策卡；外部节点要的提供方本期都不可用。"""
    from ..services.agent_sessions import manager
    from . import sessions

    try:
        if kind == "external":
            missing = node_kinds.missing_providers([node])
            raise node_kinds.NodeRunError("pack_unavailable", "需要的证据源提供方不可用", 409,
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
