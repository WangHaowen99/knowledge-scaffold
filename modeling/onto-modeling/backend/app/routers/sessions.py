"""会话、对话、执行记录、命令（API 契约 3.11）：S-01～S-20。

一个项目可以有多条对话，同一时刻只有一个在线引擎。对话流与历史按契约 1.11 的信封输出：
`{id, type, ts, project_id, conversation_id, data}`；载荷按第 4.2 节逐类给（会话宿主
`session_host/timeline.py` 投影）。事件 id 是历史行号 + 1，从 1 开始。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..api import deps
from ..api.compat import CURRENT_CONVERSATION
from ..api.errors import ApiError
from ..governance.http import reject_new_session
from ..services import conversation_store, project_events, streams, uploads, workspace_labels
from ..services.agent_sessions import SessionError, manager
from ..services.autobuild import store as autobuild_store
from ..services.session_host import command_tasks, exec_records, timeline, turns
from ..services.session_host import views as session_views

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["session"])

HEARTBEAT_SECONDS = streams.HEARTBEAT_SECONDS
TEXT_MAX = 32000
REFERENCES_MAX = 20
HISTORY_DEFAULT, HISTORY_MAX = 200, 500
_STATE = session_views.STATE
# 发消息的幂等键（client_message_id）→ 第一次的响应；只留最近一批
_SENT: "OrderedDict[tuple[str, str, str], dict]" = OrderedDict()
_SENT_MAX = 512


def v1_event(event: dict, project_id: str, conversation_id: str) -> dict | None:
    """会话层记录 → 对话流信封；只给内部用的记录返回 None（session_host/timeline）。"""
    return timeline.project(event, project_id, conversation_id)


def _resolve(project_id: str, cid: str) -> str:
    """路径里的对话 ID；`-` 表示引擎当前绑定的对话（兼容期旧接口没带对话时用）。会话层的错误由
    全局错误处理翻成契约码（api/errors.py）。"""
    return manager.resolve_conversation(project_id, None if cid == CURRENT_CONVERSATION else cid)


def _pool() -> dict:
    return session_views.pool()


def _conversation(project_id: str, view: dict, live: dict | None) -> dict:
    return session_views.conversation_view(view, live)


def _conversations(project_id: str) -> list[dict]:
    return session_views.conversations(project_id)


# ---- S-01～S-04 对话 ----


@router.get("/conversations")
def list_conversations(project_id: str, request: Request):
    deps.require_owner(project_id, request)
    live = manager.live_status(project_id)
    return {
        "items": _conversations(project_id),
        "live": {"conversation_id": live["conversation_id"], "state": _STATE.get(live["state"], live["state"]),
                 "busy": live["busy"]} if live else None,
        "pool": _pool(),
    }


def _ensure_not_busy(project_id: str, conversation_id: str | None = None) -> None:
    """另一条对话在跑（或运行中不能新建、删除）：409 session_busy。conversation_id 给定时只拦别的对话。"""
    live = manager.live_status(project_id)
    if live and live["busy"] and live["conversation_id"] != conversation_id:
        raise ApiError("session_busy", "有对话正在运行，请等它结束或先打断", 409,
                       {"running_conversation_id": live["conversation_id"]})


@router.post("/conversations")
async def create_conversation(project_id: str, request: Request):
    deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    _ensure_not_busy(project_id)
    view = conversation_store.create(project_id)
    if body.get("title"):
        view = conversation_store.rename(project_id, view["id"], str(body["title"]))
    project_events.conversation_changed(project_id, view["id"])
    return JSONResponse(status_code=201, content=_conversation(project_id, view, manager.live_status(project_id)))


@router.patch("/conversations/{cid}")
async def rename_conversation(project_id: str, cid: str, request: Request):
    deps.require_owner(project_id, request)
    body = await deps.json_body(request)
    conversation_id = _resolve(project_id, cid)
    view = conversation_store.rename(project_id, conversation_id, str(body.get("title") or ""))
    project_events.conversation_changed(project_id, conversation_id)
    return _conversation(project_id, view, manager.live_status(project_id))


@router.delete("/conversations/{cid}")
async def delete_conversation(project_id: str, cid: str, request: Request):
    """对话记录与模型上下文一并删除，累计用量照常计入项目。"""
    deps.require_owner(project_id, request)
    conversation_id = _resolve(project_id, cid)
    autobuild_store.ensure_no_run(project_id, conversation_id=conversation_id)
    snapshot = session_views.conversation(project_id, conversation_id)
    await manager.delete_conversation(project_id, conversation_id)
    project_events.conversation_changed(project_id, conversation_id, deleted=True, snapshot=snapshot)
    return {"items": _conversations(project_id)}


# ---- S-05 状态、S-06 历史 ----


def _context(history: list[dict]) -> dict | None:
    for event in reversed(history):
        if event.get("type") == "context_usage":
            return timeline.context_usage(event)
    return None


def _exec_view(project_id: str, record: dict | None) -> dict | None:
    if record is None:
        return None
    root = deps.workspaces.project_root(project_id)
    return exec_records.view(record, root=root, flow=turns._flow(root))


def _open_changeset(project_id: str, conversation_id: str) -> dict | None:
    """S-05：这条对话里没确认的助手变更集（方案 C12）。"""
    from ..kernel.changesets import service as changesets

    record = changesets.current(deps.workspaces, project_id)
    if record is None or record.get("conversation_id") not in (None, conversation_id):
        return None
    return changesets.view(record)


@router.get("/conversations/{cid}")
def conversation_state(project_id: str, cid: str, request: Request):
    deps.require_owner(project_id, request)
    conversation_id = _resolve(project_id, cid)
    live = manager.get(project_id)
    session = live if live and live.conversation_id == conversation_id else None
    history = manager.get_history(project_id, conversation_id)
    pending = None
    questions: list[str] = []
    if session is not None:
        for request_id, permission in session.pending_permissions.items():
            if permission.future.done():
                continue
            if permission.questions:
                questions += [item["question_id"] for item in permission.questions
                              if item["question_id"] not in permission.answers]
            elif pending is None:
                expires = next((event.get("expires_at") for event in reversed(history)
                                if event.get("type") == "permission_request" and event.get("request_id") == request_id), None)
                pending = {"request_id": request_id, "title": permission.title or "助手请求执行一个操作",
                           "expires_at": session_views.iso(expires)}
    state = _STATE.get(session.state, session.state) if session else "idle"
    if session is not None and manager.queue_position(project_id) is not None:
        state = "queued"
    elif pending or questions:
        state = "waiting_user"
    config_pending = None
    if session is not None and getattr(session, "reload_pending", False):
        from ..services.session_host import fingerprint

        config_pending = {"changes": fingerprint.describe(list(session.pending_changes) or ["model"]),
                          "effective": "next_message"}
    active = exec_records.open_record(project_id, conversation_id)
    view = session_views.conversation(project_id, conversation_id) or {}
    return {
        "conversation_id": conversation_id,
        "state": state,
        "live": session is not None,
        "turn_started_at": session_views.iso(session.turn_started_at) if session else None,
        "queue_position": manager.queue_position(project_id) if session else None,
        "last_seq": len(history),
        "history_count": len(history),
        "context": _context(history),
        "pending_permission": pending,
        "open_questions": questions,
        "active_exec": _exec_view(project_id, active),
        "open_changeset": _open_changeset(project_id, conversation_id),
        "config_pending": config_pending,
        "autobuild_run_id": view.get("run_id"),
        "pool": _pool(),
    }


@router.get("/conversations/{cid}/messages")
def conversation_messages(project_id: str, cid: str, request: Request, before_seq: int | None = None, limit: int = 0):
    """S-06：持久事件按 seq 升序分页（取 before_seq 之前的），不含瞬时事件；卡片与问题给最新状态。"""
    deps.require_owner(project_id, request)
    conversation_id = _resolve(project_id, cid)
    limit = max(1, min(int(limit or HISTORY_DEFAULT), HISTORY_MAX))
    history = list(manager.get_history(project_id, conversation_id))
    if before_seq is not None:
        history = history[: max(0, int(before_seq) - 1)]
    envelopes = []
    start = len(history)
    # 从后往前取够 limit 条对外可见的持久事件
    while start > 0 and len(envelopes) < limit:
        start -= 1
        envelope = v1_event(history[start], project_id, conversation_id)
        if envelope is not None and "id" in envelope:
            envelopes.append(envelope)
    envelopes.reverse()
    return {
        "items": timeline.collapse(envelopes),
        "has_more": any(
            (item := v1_event(event, project_id, conversation_id)) is not None and "id" in item
            for event in history[:start]
        ),
        "first_seq": int(envelopes[0]["id"]) if envelopes else None,
        "last_seq": len(history) if history else None,
    }


# ---- S-07 发消息 ----


def _attachments(project_id: str, request: Request, upload_ids: list) -> list[dict]:
    """对话附件：暂存上传落到资料区（各自一个版本），消息里注明，助手据此读取。"""
    if len(upload_ids) > uploads.LIMITS["attachments_per_message"]:
        raise ApiError("invalid_argument", "一条消息最多 10 个附件", 400, {"fields": {"attachments": "max"}})
    saved = []
    for upload_id in upload_ids:
        meta = uploads.get(str(upload_id), deps.actor(request), "attachment")
        result = deps.workspaces.save_resource(
            project_id, uploads.resource_subdir(meta["filename"]), meta["filename"], uploads.read(meta["upload_id"]),
            author=deps.username(request),
        )
        uploads.consume(meta["upload_id"])
        saved.append({"path": result["path"], "ref": {
            "upload_id": meta["upload_id"], "filename": meta["filename"], "size": int(meta.get("size") or 0),
            "kind": _attachment_kind(meta["filename"]),
        }})
    return saved


def _attachment_kind(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in (".doc", ".docx"):
        return "doc"
    if suffix in (".xls", ".xlsx", ".csv"):
        return "xls"
    if suffix == ".pdf":
        return "pdf"
    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        return "image"
    return "file"


def _reference_text(references: list) -> str:
    labels = []
    for item in references[:REFERENCES_MAX]:
        if isinstance(item, dict):
            labels.append(str(item.get("id") or item.get("anchor") or item.get("key") or item.get("title") or ""))
    labels = [label for label in labels if label]
    return f"\n\n（引用的条目：{'、'.join(labels)}）" if labels else ""


async def _send(project_id: str, root: Path, conversation_id: str, request: Request, text: str, meta: dict) -> dict:
    """发给对话并给出 S-07 的响应形状；会话层的错误转契约错误码。自主构建没结束时一律 409（契约 2.10）。"""
    autobuild_store.ensure_no_run(project_id)
    reject_new_session(project_id)
    _ensure_not_busy(project_id, conversation_id)
    live = manager.get(project_id)
    was_running = bool(live and live.conversation_id == conversation_id and live.state == "running")
    session = await manager.send(project_id, root, text, conversation_id, username=deps.username(request), meta=meta)
    record = next((event for event in reversed(session.events) if event.get("type") == "user_message"), None)
    seq = timeline.external_id(record) if record else None
    queue_position = manager.queue_position(project_id)
    delivery = "queued" if queue_position is not None else ("deferred" if was_running else "immediate")
    return {
        "message_id": timeline.message_id(record) if record else None,
        "seq": seq,
        "conversation_id": session.conversation_id,
        "state": "queued" if queue_position is not None else _STATE.get(session.state, session.state),
        "delivery": delivery,
        "queue_position": queue_position,
    }


@router.post("/conversations/{cid}/messages")
async def send_message(project_id: str, cid: str, request: Request):
    """运行中也能发：消息在这一步结束后送达（deferred）；会话池满时排队（queued）。"""
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    text = str(body.get("text") or "")
    if len(text) > TEXT_MAX:
        raise ApiError("invalid_argument", f"消息不超过 {TEXT_MAX} 个字符", 400, {"fields": {"text": "length"}})
    attachments = body.get("attachments") or []
    references = [item for item in (body.get("references") or [])[:REFERENCES_MAX] if isinstance(item, dict)]
    if not text.strip() and not attachments:
        raise ApiError("message_empty", "消息正文和附件都为空", 400)
    conversation_id = _resolve(project_id, cid)
    key = str(body.get("client_message_id") or "")
    if key and (project_id, conversation_id, key) in _SENT:
        return JSONResponse(status_code=202, content=_SENT[(project_id, conversation_id, key)])
    saved = _attachments(project_id, request, attachments)
    prompt = text
    if saved:
        prompt = f"（我通过界面上传了资料：{'、'.join(item['path'] for item in saved)}）\n{prompt}"
    prompt += _reference_text(references)
    meta = {"display_text": text.strip() or "（附件）", "attachments": [item["ref"] for item in saved],
            "references": references, "origin": None}
    run = autobuild_store.active(project_id)
    if run is not None and run.get("conversation_id") == conversation_id:
        # 运行对话里发的是补充说明（契约 3.22）：不打断运行，下一步开始前带给助手
        from ..services.autobuild.runner import runner

        record = runner.note(project_id, run, prompt, meta)
        result = {"message_id": timeline.message_id(record), "seq": timeline.external_id(record),
                  "conversation_id": conversation_id, "state": "running", "delivery": "deferred",
                  "queue_position": None}
    else:
        result = await _send(project_id, root, conversation_id, request, prompt, meta)
    if key:
        _SENT[(project_id, conversation_id, key)] = result
        while len(_SENT) > _SENT_MAX:
            _SENT.popitem(last=False)
    return JSONResponse(status_code=202, content=result)


# ---- S-08～S-12 打断、重启、确认、回答、取消排队 ----


@router.post("/conversations/{cid}/interrupt")
async def interrupt(project_id: str, cid: str, request: Request):
    deps.require_owner(project_id, request)
    conversation_id = _resolve(project_id, cid)
    autobuild_store.ensure_no_run(project_id, conversation_id=conversation_id)  # 运行中用 AB-06 暂停
    live = manager.get(project_id)
    running = bool(live and live.conversation_id == conversation_id and live.state in ("running", "connecting"))
    manager.interrupted_execs.pop((project_id, conversation_id), None)
    reconciled = await manager.interrupt(project_id, conversation_id)
    record = manager.interrupted_execs.pop((project_id, conversation_id), None)
    return {"interrupted": running or bool(reconciled), "exec": _exec_view(project_id, record)}


@router.post("/conversations/{cid}/restart")
async def restart(project_id: str, cid: str, request: Request):
    """丢弃模型上下文（对话记录保留），下次发消息按最新配置重连。"""
    deps.require_owner(project_id, request)
    conversation_id = _resolve(project_id, cid)
    autobuild_store.ensure_no_run(project_id, conversation_id=conversation_id)
    live = manager.live_status(project_id)
    if live and live["busy"] and live["conversation_id"] == conversation_id:
        raise ApiError("session_busy", "对话正在运行，请先打断", 409, {"running_conversation_id": conversation_id})
    await manager.close(project_id, conversation_id)
    return {"state": "closed"}


@router.post("/conversations/{cid}/permissions/{request_id}")
async def respond_permission(project_id: str, cid: str, request_id: str, request: Request):
    deps.require_owner(project_id, request)
    body = await deps.json_body(request)
    behavior = body.get("behavior")
    if behavior not in ("allow", "deny"):
        raise ApiError("invalid_argument", "behavior 必须是 allow 或 deny", 400, {"fields": {"behavior": "enum"}})
    await manager.respond_permission(project_id, request_id, behavior, str(body.get("message") or ""))
    audit_detail = {"permission.reply": request_id, "behavior": behavior}
    from ..services import audit

    audit.log(deps.actor(request), "POST", request.url.path, audit_detail)
    return {"ok": True}


@router.post("/conversations/{cid}/questions/{question_id}/answer")
async def answer_question(project_id: str, cid: str, question_id: str, request: Request):
    """回答决策卡（AskUserQuestion 拆成的问题）：选项 ID 为「问题序号.选项序号」，其他选项是「序号.custom」。"""
    deps.require_owner(project_id, request)
    _resolve(project_id, cid)
    body = await deps.json_body(request)
    answer = {key: body.get(key) for key in ("option_id", "option_ids", "custom_text") if body.get(key)}
    try:
        question = manager.answer_question(project_id, question_id, answer)
    except SessionError as exc:
        if exc.status == 410:
            # 不是助手提的问题：人工节点（详设 6.5）的决策卡由流程运行时回答
            from .flow import answer_human_node

            question = await answer_human_node(project_id, question_id, answer, deps.actor(request))
            if question is not None:
                return {"question": question}
            raise ApiError("question_gone", "问题已回答、撤回或失效", 410, {"state": "gone"}) from exc
        raise ApiError("answer_invalid", str(exc), 400) from exc
    return {"question": question}


@router.post("/conversations/{cid}/queue/cancel")
async def cancel_queue(project_id: str, cid: str, request: Request):
    deps.require_owner(project_id, request)
    conversation_id = _resolve(project_id, cid)
    autobuild_store.ensure_no_run(project_id, conversation_id=conversation_id)
    if manager.queue_position(project_id) is None:
        raise ApiError("not_queued", "当前不在排队", 409)
    await manager.interrupt(project_id, conversation_id)
    return {"state": "idle"}


# ---- S-13 对话事件流 ----


def _replay_from(after: int | None, last_event_id: str, header: str) -> int:
    """要补发 id 大于几的事件：?after= 优先，其次 Last-Event-ID；都没有从头（0）。"""
    if after is not None:
        return max(0, int(after))
    raw = last_event_id or header
    return int(raw) if str(raw).isdigit() else 0


@router.get("/conversations/{cid}/events")
async def stream(project_id: str, cid: str, request: Request, after: int | None = None, last_event_id: str = ""):
    """SSE 对话流：先补发 id 大于 after 的持久事件，再持续推送；断线重连带 ?after= 或 Last-Event-ID。"""
    deps.require_owner(project_id, request)
    conversation_id = _resolve(project_id, cid)
    sent = _replay_from(after, last_event_id, request.headers.get("last-event-id", ""))
    expires_at = streams.token_expiry(request)

    def frames():
        return conversation_frames(project_id, conversation_id, sent, expires_at=expires_at)

    return streams.event_stream(deps.actor(request), frames)


async def conversation_frames(project_id: str, conversation_id: str, after: int, *, expires_at: float | None = None,
                              heartbeat: float | None = None):
    """对话流的帧序列（单测直接迭代它）。"""
    queue: asyncio.Queue = asyncio.Queue()
    wait = HEARTBEAT_SECONDS if heartbeat is None else heartbeat
    sent = after  # 已发出的最大对外 id；历史重放与实时推送按它去重
    # 先登记再重放：重放长历史时事件循环会让出，期间新事件先进队列，重放结束后按水位去重续发
    manager.watch(project_id, queue, conversation_id)
    try:
        yield f"retry: {streams.RETRY_MS}\n\n"
        history = list(manager.get_history(project_id, conversation_id))
        for event in history[sent:]:
            envelope = v1_event(event, project_id, conversation_id)
            if envelope and "id" in envelope:  # 瞬时事件不回放
                yield streams.frame(envelope)
        sent = max(sent, len(history))
        while True:
            if expires_at is not None and time.time() >= expires_at:
                yield streams.frame(streams.control("auth.expired", {}, project_id=project_id,
                                                    conversation_id=conversation_id))
                return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=wait)
            except asyncio.TimeoutError:
                yield streams.frame(streams.control("ping", {}, project_id=project_id, conversation_id=conversation_id))
                continue
            seq = timeline.external_id(event)
            if seq is not None:
                if seq <= sent:
                    continue
                sent = seq
            envelope = v1_event(event, project_id, conversation_id)
            if envelope:
                yield streams.frame(envelope)
    finally:
        manager.unwatch(project_id, queue, conversation_id)


# ---- S-14 重试 ----


@router.post("/conversations/{cid}/retry")
async def retry(project_id: str, cid: str, request: Request):
    """重新发起最近一轮出错的请求（出错卡的「重试」）。"""
    root = deps.require_writable(project_id, request)
    conversation_id = _resolve(project_id, cid)
    session = manager.get(project_id)
    if session is None or session.conversation_id != conversation_id or session.state != "error" or not session.last_prompt:
        raise ApiError("nothing_to_retry", "没有可重试的出错轮次", 409)
    session.retries += 1
    prompt = session.last_prompt
    result = await _send(project_id, root, conversation_id, request, prompt,
                         {"display_text": f"（重试）{prompt}"[:TEXT_MAX], "origin": None})
    return JSONResponse(status_code=202, content=result)


# ---- S-15～S-17 执行记录 ----


@router.get("/execs")
def list_execs(project_id: str, request: Request, state: str = "", conversation_id: str = "", cursor: str = "",
               limit: int = 0):
    root = deps.require_owner(project_id, request)
    states = [item for item in (part.strip() for part in state.split(",")) if item]
    for item in request.query_params.getlist("state")[1:]:
        states += [part.strip() for part in item.split(",") if part.strip()]
    invalid = [item for item in states if item not in exec_records.STATES]
    if invalid:
        raise ApiError("invalid_argument", "state 取值不合法", 400, {"fields": {"state": "enum"}})
    records = exec_records.query(project_id, states or None, conversation_id or None)
    flow = turns._flow(root)
    page = deps.page(records, cursor, limit, binding={"state": sorted(states), "conversation_id": conversation_id})
    return {"items": [exec_records.view(record, root=root, flow=flow) for record in page["items"]],
            "next_cursor": page["next_cursor"]}


def _exec_or_404(project_id: str, exec_id: str) -> dict:
    record = exec_records.get(project_id, exec_id)
    if record is None:
        raise ApiError("exec_not_found", "执行记录不存在", 404)
    return record


@router.post("/execs/{exec_id}/resume")
async def resume_exec(project_id: str, exec_id: str, request: Request):
    """续跑：往对话发一条续跑指令，接着原来那份执行记录（输入变了或节点升级了不能续跑）。"""
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    record = _exec_or_404(project_id, exec_id)
    flow = turns._flow(root)
    reason = exec_records.not_resumable_reason(record, root, flow)
    if reason is None and record.get("state") != "interrupted":
        reason = "already_finished"
    if reason is not None:
        raise ApiError("exec_not_resumable", "这一步不能续跑", 409, {"reason": reason})
    conversation_id = _resolve(project_id, str(body.get("conversation_id") or record.get("conversation_id") or "-"))
    live = manager.live_status(project_id)
    if live and live["busy"]:
        raise ApiError("session_busy", "有对话正在运行，请等它结束或先打断", 409,
                       {"running_conversation_id": live["conversation_id"]})
    text = turns.resume_text(record)
    label = record.get("command_label") or "命令"
    result = await _send(project_id, root, conversation_id, request, text,
                         {"display_text": f"从打断处继续「{label}」", "origin": None, "resume_exec": exec_id})
    updated = exec_records.get(project_id, exec_id) or record
    _update_resume_card(project_id, conversation_id, updated, root, flow)
    return JSONResponse(status_code=202, content={"exec": exec_records.view(updated, root=root, flow=flow),
                                                  "message_id": result["message_id"],
                                                  "conversation_id": result["conversation_id"]})


@router.post("/execs/{exec_id}/abandon")
async def abandon_exec(project_id: str, exec_id: str, request: Request):
    """放弃：已经保存的版本不回退；续跑卡变为已处理。"""
    root = deps.require_writable(project_id, request)
    autobuild_store.ensure_no_run(project_id)
    record = _exec_or_404(project_id, exec_id)
    if record.get("state") in ("done", "abandoned"):
        raise ApiError("exec_not_resumable", "这一步已经结束", 409, {"reason": "already_finished"})
    session = manager.get(project_id)
    if session is not None and session.exec_id == exec_id and record.get("state") == "running":
        raise ApiError("session_busy", "这一步正在跑，请先打断", 409,
                       {"running_conversation_id": session.conversation_id})
    if session is not None and session.exec_id == exec_id:
        session.exec_id = None
    updated = exec_records.abandon(project_id, exec_id) or record
    _clear_command_scope(root, record)
    flow = turns._flow(root)
    turns.publish_exec(project_id, updated, root, flow)
    conversation_id = updated.get("conversation_id") or ""
    _update_resume_card(project_id, conversation_id, updated, root, flow)
    if conversation_id and conversation_store.exists(project_id, conversation_id):
        # 对话里的系统提示行（EC-13）：已放弃，已经保存的版本不回退
        saved = updated.get("saved") or {}
        manager.append_record(project_id, conversation_id, {"type": "notice", "payload": {
            "kind": "info", "text": f"已放弃「{updated.get('command_label') or '命令'}」，已经保存的版本不回退",
            "version_label": saved.get("version_label"), "changeset_id": None, "align": "center"}})
    return {"exec": exec_records.view(updated, root=root, flow=flow)}


def _clear_command_scope(root: Path, record: dict) -> None:
    """放弃一步：副本里这条命令留下的锁与写入范围一起清掉，守卫与节点状态不再按它算（审查第 9 条）。"""
    from ..kernel.changesets import service as changesets
    from ..kernel.changesets import worktree

    copy = changesets.existing_copy(root)
    if copy is not None and record.get("command_name"):
        worktree.clear_run_state(copy, [record["command_name"]])


def _update_resume_card(project_id: str, conversation_id: str, record: dict, root: Path, flow: dict) -> None:
    """续跑卡（EC-41）：续跑或放弃之后整卡替换成已处理。只在这条对话里放过续跑卡时发。"""
    card_id = f"resume-{record['id']}"
    history = manager.get_history(project_id, conversation_id) if conversation_id else []
    if not any(event.get("type") == "card" and (event.get("payload") or {}).get("card_id") == card_id
               for event in history):
        return
    view = exec_records.view(record, root=root, flow=flow)
    card = exec_records.resume_card(record, view)
    card["status"] = {"key": "running" if record.get("state") == "running" else "skipped",
                      "label": "已续跑" if record.get("state") == "running" else "已放弃"}
    manager.append_record(project_id, conversation_id, {"type": "card_updated", "payload": card})


# ---- S-18～S-20 命令 ----

_SECTION_GROUPS = (("required", "建模"), ("pre_publish", "发布前检查"), ("optional", "可选能力"),
                   ("on_demand", "有需要时"))


def command_catalog(root: Path) -> dict:
    """S-18：当前装配里的命令（只列已启用包带来的），按流程节点所在分组，业务名优先取节点里按钮的叫法。"""
    flow = turns._flow(root)
    nodes = {node["id"]: node for node in flow.get("nodes") or []}
    labels: dict[str, str] = {}
    for node in nodes.values():
        for action in node.get("actions") or []:
            command = str(action.get("command") or "").strip()
            if command.startswith("/") and " " not in command:
                labels.setdefault(command, action.get("label") or "")
    for items in (flow.get("next_steps") or {}).values():
        for item in items or []:
            command = str(item.get("command") or "").strip()
            if command.startswith("/") and " " not in command:
                labels.setdefault(command, item.get("reply") or "")
    commands_dir = Path(root) / ".claude" / "commands"
    try:
        names = sorted(path.stem for path in commands_dir.glob("*.md"))
    except OSError:
        names = []
    # 节点包只在节点上写入口命令（entry.command），没有 commands 映射
    entries = {str((node.get("entry") or {}).get("command") or "").strip(): node for node in nodes.values()}
    groups: dict[str, list] = {key: [] for key, _ in _SECTION_GROUPS}
    groups["other"] = []
    for name in names:
        command = f"/{name}"
        short = name[len("ontology-"):] if name.startswith("ontology-") else name
        spec = (flow.get("commands") or {}).get(short) or {}
        node = nodes.get(spec.get("node") or "") or entries.get(command)
        label = labels.get(command) or (node or {}).get("title") or short
        item = {
            "id": short, "label": label,
            "description": (node or {}).get("title") or "",
            "command": command, "pack": (node or {}).get("pack") or "core", "node_id": (node or {}).get("id"),
            "args_schema": None, "available": True, "unavailable_reason": None,
        }
        groups[(node or {}).get("section") if node and (node.get("section") in groups) else "other"].append(item)
    result = [{"id": key, "label": label, "items": groups[key]} for key, label in _SECTION_GROUPS if groups[key]]
    if groups["other"]:
        result.append({"id": "other", "label": "其他", "items": groups["other"]})
    return {"groups": result, "total": sum(len(group["items"]) for group in result)}


@router.get("/commands/catalog")
def get_command_catalog(project_id: str, request: Request):
    root = deps.require_owner(project_id, request)
    return command_catalog(root)


@router.get("/commands/tasks")
def list_command_tasks(project_id: str, request: Request, origin_page: str = "", state: str = ""):
    deps.require_owner(project_id, request)
    states = [item.strip() for item in state.split(",") if item.strip()] or None
    if states and any(item not in command_tasks.STATES for item in states):
        raise ApiError("invalid_argument", "state 取值不合法", 400, {"fields": {"state": "enum"}})
    return {"items": command_tasks.query(project_id, origin_page=origin_page, states=states)}


_action_resolvers: list = []


def register_action_resolver(resolver) -> None:
    """包页面按钮（详设 5.9.2 actions）→ 命令文本：扩展项注册表（B4c）登记；返回 None 表示不认识。"""
    if resolver not in _action_resolvers:
        _action_resolvers.append(resolver)


def _render_command(command: str, args: dict, text: str) -> str:
    """命令文本：`/命令 键=值 …`，非字符串的值写成 JSON；附带的说明另起一段。"""
    parts = [command.strip()]
    parts += [f"{key}={value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}"
              for key, value in (args or {}).items() if value not in (None, "", [], {})]
    message = " ".join(part for part in parts if part)
    return f"{message}\n\n{text.strip()}" if text and text.strip() else message


@router.post("/commands")
async def send_command(project_id: str, request: Request):
    """S-20：页面只发命令、不跑助手；返回可追踪的任务对象，进度经项目流 command.progress 推送。"""
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    return await dispatch_command(project_id, root, request, body)


async def dispatch_command(project_id: str, root: Path, request: Request, body: dict) -> JSONResponse:
    """S-20 的本体：FL-05（开始节点）也走这里。"""
    origin = body.get("origin")
    if not isinstance(origin, dict) or not origin.get("page"):
        raise ApiError("invalid_argument", "缺少 origin", 400, {"fields": {"origin": "required"}})
    action, command = body.get("action"), str(body.get("command") or "").strip()
    if bool(action) == bool(command):
        raise ApiError("invalid_argument", "action 与 command 二选一", 400, {"fields": {"command": "one_of"}})
    args = body.get("args") or {}
    if not isinstance(args, dict):
        raise ApiError("invalid_argument", "args 必须是对象", 400, {"fields": {"args": "object"}})
    if action:
        if not isinstance(action, dict):
            raise ApiError("invalid_argument", "action 必须是对象", 400, {"fields": {"action": "object"}})
        command = next((found for found in (resolver(project_id, root, action) for resolver in _action_resolvers)
                        if found), "")
        if not command:
            raise ApiError("pack_not_enabled", "这个按钮所属的能力没有启用", 409,
                           {"pack": action.get("pack"), "effective": None})
    if not command.startswith("/"):
        raise ApiError("invalid_argument", "命令须以 / 开头", 400, {"fields": {"command": "format"}})
    if origin.get("pack"):
        # 包自带界面经窄接口发来的：按包再校验一次（ADR-0055，审查第 1 条）
        from ..services.packs import ui as pack_ui

        try:
            pack_ui.check_command(deps.workspaces, project_id, str(origin["pack"]), command, args,
                                  str(body.get("text") or ""))
        except pack_ui.PackUiError as exc:
            raise ApiError(exc.code, str(exc), exc.status, exc.details) from exc
    catalog = command_catalog(root)
    known = {item["command"]: item for group in catalog["groups"] for item in group["items"]}
    name = command.split()[0]
    if name not in known:
        from ..services.packs import service as packs_service

        pending = packs_service.assembly_view(deps.workspaces, project_id).get("state") == "pending"
        raise ApiError("pack_not_enabled", "这条命令所属的能力没有启用", 409,
                       {"command": name, "effective": "next_message" if pending else None})
    conversation_id = _resolve(project_id, str(body.get("conversation_id") or "-"))
    _ensure_not_busy(project_id, conversation_id)
    text = _render_command(command, args, str(body.get("text") or ""))
    label = known[name]["label"]
    task = command_tasks.create(project_id, origin=origin, title=f"助手正在执行「{label}」",
                                conversation_id=conversation_id, command=name)
    # 对话里显示的那条不带参数、不出路径（F4b-paths）：页面可另给 display_text，没给时用说明文字
    shown = str(body.get("display_text") or body.get("text") or "").strip()
    display = f"{command}\n\n{workspace_labels.humanize(shown)}" if shown else command
    meta = {"display_text": display, "origin": {"kind": "page", "page": str(origin.get("page")),
                                             "label": str(origin.get("label") or ""), "task_id": task["task_id"]},
            "task_id": task["task_id"], "command": name}
    try:
        result = await _send(project_id, root, conversation_id, request, text, meta)
    except ApiError:
        command_tasks.update(project_id, task["task_id"], state="failed",
                             result={"summary": "没能发到对话里", "refs": []})
        raise
    fields = {"message_id": result["message_id"]}
    if result["delivery"] != "immediate":
        fields["state"] = "queued"
    task = command_tasks.update(project_id, task["task_id"], **fields) or task
    return JSONResponse(status_code=202, content={"task": command_tasks.view(task),
                                                  "conversation_id": result["conversation_id"],
                                                  "message_id": result["message_id"]})
