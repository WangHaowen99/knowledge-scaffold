"""会话与对话（API 契约 3.11）：S-01～S-13。

一个项目可以有多条对话，同一时刻只有一个在线引擎。对话流与历史按契约 1.11 的信封输出：
`{id, type, ts, project_id, conversation_id, data}`，事件名改为点号小写（第 6.2 节对照表）；
载荷沿用会话层现有字段，会话宿主改造（P2）时再按第 4 章逐类收敛。
执行记录、续跑、页面命令（S-14～S-20）属于以后阶段。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..api import deps
from ..api.compat import CURRENT_CONVERSATION
from ..api.errors import ApiError
from ..services import conversation_store, uploads
from ..services.agent_sessions import manager

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["session"])

HEARTBEAT_SECONDS = 15
TEXT_MAX = 32000
REFERENCES_MAX = 20
HISTORY_DEFAULT, HISTORY_MAX = 200, 500
# 旧事件名 → v1 事件名（契约第 6.2 节）；None 表示并进别的事件、不再单独发出
EVENT_TYPES = {
    "user_message": "message.user",
    "stream_delta": "message.delta",
    "assistant_message": "message.assistant",
    "tool_results": "tool.activity",
    "result": "turn.completed",
    "subagent_started": "subagent.updated",
    "subagent_finished": "subagent.updated",
    "permission_request": "permission.requested",
    "permission_resolved": "permission.resolved",
    "session_state": "session.state",
    "session_started": "session.started",
    "session_error": "session.error",
    "session_interrupted": "session.interrupted",
    "session_config_pending": "session.config_pending",
    "session_reconnected": "session.reconnected",
    "queue_position": "session.queued",
    "context_usage": "context.usage",
    "context_compacted": "context.compacted",
    "workspace_changes": "card",
    "model_usage": None,
}
# 瞬时事件：不带 id 行，不回放（契约 1.11）
TRANSIENT_TYPES = {"message.delta", "context.usage", "ping"}
_STATE = {"connecting": "connecting", "running": "running", "idle": "idle", "closed": "closed", "error": "error"}
# 发消息的幂等键（client_message_id）→ 第一次的响应；只留最近一批
_SENT: "OrderedDict[tuple[str, str, str], dict]" = OrderedDict()
_SENT_MAX = 512


def v1_event(event: dict, project_id: str, conversation_id: str) -> dict | None:
    """会话层事件 → 对话流信封；并进别的事件的返回 None。"""
    old = str(event.get("type") or "")
    kind = EVENT_TYPES.get(old, old.replace("_", "."))
    if kind is None:
        return None
    data = {key: value for key, value in event.items() if key not in ("type", "seq", "ts")}
    if old in ("subagent_started", "subagent_finished"):
        data["phase"] = "started" if old == "subagent_started" else "finished"
    if old == "workspace_changes":
        data["card_type"] = "round_changes"
    if old == "session_state" and "state" in data:
        data["state"] = _STATE.get(data["state"], data["state"])
    envelope = {
        "type": kind,
        "ts": deps.iso(event.get("ts") or time.time()),
        "project_id": project_id,
        "conversation_id": conversation_id,
        "data": data,
    }
    if event.get("seq") is not None and kind not in TRANSIENT_TYPES:
        envelope = {"id": str(event["seq"]), **envelope}
    return envelope


def _resolve(project_id: str, cid: str) -> str:
    """路径里的对话 ID；`-` 表示引擎当前绑定的对话（兼容期旧接口没带对话时用）。"""
    return manager.resolve_conversation(project_id, None if cid == CURRENT_CONVERSATION else cid)


def _pool() -> dict:
    status = manager.pool_status()
    return {"max": status["max"], "active": status["active"], "queued": len(status["waiting"]),
            "idle_ttl_s": status["idle_ttl_seconds"]}


def _conversation(project_id: str, view: dict, live: dict | None) -> dict:
    is_live = bool(live and live["conversation_id"] == view["id"])
    return {
        "id": view["id"], "title": view["title"], "title_manual": view["title_manual"],
        "created_at": deps.iso(view["created_at"]), "updated_at": deps.iso(view["updated_at"]),
        "live": is_live, "state": _STATE.get(live["state"], live["state"]) if is_live else "idle",
        "kind": "chat", "run_id": None,
    }


def _conversations(project_id: str) -> list[dict]:
    live = manager.live_status(project_id)
    return [_conversation(project_id, view, live) for view in conversation_store.list_conversations(project_id)]


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


def _ensure_not_busy(project_id: str) -> None:
    live = manager.live_status(project_id)
    if live and live["busy"]:
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
    return JSONResponse(status_code=201, content=_conversation(project_id, view, manager.live_status(project_id)))


@router.patch("/conversations/{cid}")
async def rename_conversation(project_id: str, cid: str, request: Request):
    deps.require_owner(project_id, request)
    body = await deps.json_body(request)
    view = conversation_store.rename(project_id, _resolve(project_id, cid), str(body.get("title") or ""))
    return _conversation(project_id, view, manager.live_status(project_id))


@router.delete("/conversations/{cid}")
async def delete_conversation(project_id: str, cid: str, request: Request):
    """对话记录与模型上下文一并删除，累计用量照常计入项目。"""
    deps.require_owner(project_id, request)
    await manager.delete_conversation(project_id, _resolve(project_id, cid))
    return {"items": _conversations(project_id)}


# ---- S-05 状态、S-06 历史 ----


def _context(history: list[dict]) -> dict | None:
    for event in reversed(history):
        if event.get("type") == "context_usage":
            used = int(event.get("used") or 0)
            window = int(event.get("window") or 0)
            compact_at = int(event.get("compact_at") or 0)
            return {
                "used": used, "window": window, "pct": round(used * 100 / window, 1) if window else 0,
                "compact_at_pct": round(compact_at * 100 / window) if window and compact_at else 0,
                "breakdown": None, "last_call": None,
            }
    return None


@router.get("/conversations/{cid}")
def conversation_state(project_id: str, cid: str, request: Request):
    deps.require_owner(project_id, request)
    conversation_id = _resolve(project_id, cid)
    live = manager.get(project_id)
    session = live if live and live.conversation_id == conversation_id else None
    history = manager.get_history(project_id, conversation_id)
    pending = None
    if session is not None:
        for request_id, permission in session.pending_permissions.items():
            if not permission.future.done():
                pending = {"request_id": request_id, "title": permission.title or permission.tool_name or "确认",
                           "expires_at": None}
                break
    state = _STATE.get(session.state, session.state) if session else "idle"
    if session is not None and manager.queue_position(project_id) is not None:
        state = "queued"
    elif pending:
        state = "waiting_user"
    config_pending = None
    if session is not None and getattr(session, "reload_pending", False):
        config_pending = {"changes": [{"kind": "model", "summary": "模型配置已修改"}], "effective": "next_message"}
    return {
        "conversation_id": conversation_id,
        "state": state,
        "live": session is not None,
        "turn_started_at": None,
        "queue_position": manager.queue_position(project_id) if session else None,
        "last_seq": history[-1].get("seq", len(history) - 1) if history else -1,
        "history_count": len(history),
        "context": _context(history),
        "pending_permission": pending,
        "open_questions": [],
        "active_exec": None,
        "open_changeset": None,
        "config_pending": config_pending,
        "autobuild_run_id": None,
        "pool": _pool(),
    }


@router.get("/conversations/{cid}/messages")
def conversation_messages(project_id: str, cid: str, request: Request, before_seq: int | None = None, limit: int = 0):
    """S-06：持久事件按 seq 升序分页（取 before_seq 之前的），不含瞬时事件。"""
    deps.require_owner(project_id, request)
    conversation_id = _resolve(project_id, cid)
    limit = max(1, min(int(limit or HISTORY_DEFAULT), HISTORY_MAX))
    history = list(manager.get_history(project_id, conversation_id))
    if before_seq is not None:
        history = [event for event in history if event.get("seq", 0) < before_seq]
    window = history[-limit:]
    items = [item for item in (v1_event(event, project_id, conversation_id) for event in window) if item]
    return {
        "items": items,
        "has_more": len(history) > len(window),
        "first_seq": window[0].get("seq") if window else None,
        "last_seq": window[-1].get("seq") if window else None,
    }


# ---- S-07 发消息 ----


def _attachments(project_id: str, request: Request, upload_ids: list) -> list[str]:
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
        saved.append(result["path"])
    return saved


def _reference_text(references: list) -> str:
    labels = []
    for item in references[:REFERENCES_MAX]:
        if isinstance(item, dict):
            labels.append(str(item.get("id") or item.get("anchor") or item.get("key") or item.get("title") or ""))
    labels = [label for label in labels if label]
    return f"\n\n（引用的条目：{'、'.join(labels)}）" if labels else ""


@router.post("/conversations/{cid}/messages")
async def send_message(project_id: str, cid: str, request: Request):
    """运行中也能发：消息在这一步结束后送达（deferred）；会话池满时排队（queued）。"""
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    text = str(body.get("text") or "")
    if len(text) > TEXT_MAX:
        raise ApiError("invalid_argument", f"消息不超过 {TEXT_MAX} 个字符", 400, {"fields": {"text": "length"}})
    attachments = body.get("attachments") or []
    references = body.get("references") or []
    if not text.strip() and not attachments:
        raise ApiError("message_empty", "消息正文和附件都为空", 400)
    conversation_id = _resolve(project_id, cid)
    key = str(body.get("client_message_id") or "")
    if key and (project_id, conversation_id, key) in _SENT:
        return JSONResponse(status_code=202, content=_SENT[(project_id, conversation_id, key)])
    saved = _attachments(project_id, request, attachments)
    if saved:
        text = f"（我通过界面上传了资料：{'、'.join(saved)}）\n{text}"
    text += _reference_text(references)
    live = manager.get(project_id)
    was_running = bool(live and live.conversation_id == conversation_id and live.state == "running")
    session = await manager.send(project_id, root, text, conversation_id, username=deps.username(request))
    seq = next(
        (event.get("seq") for event in reversed(session.events) if event.get("type") == "user_message"), None,
    )
    queue_position = manager.queue_position(project_id)
    delivery = "queued" if queue_position is not None else ("deferred" if was_running else "immediate")
    result = {
        "message_id": f"m-{seq}" if seq is not None else None,
        "seq": seq,
        "conversation_id": session.conversation_id,
        "state": "queued" if queue_position is not None else _STATE.get(session.state, session.state),
        "delivery": delivery,
        "queue_position": queue_position,
    }
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
    live = manager.get(project_id)
    running = bool(live and live.conversation_id == conversation_id and live.state in ("running", "connecting"))
    reconciled = await manager.interrupt(project_id, conversation_id)
    return {"interrupted": running or bool(reconciled), "exec": None}


@router.post("/conversations/{cid}/restart")
async def restart(project_id: str, cid: str, request: Request):
    """丢弃模型上下文（对话记录保留），下次发消息按最新配置重连。"""
    deps.require_owner(project_id, request)
    conversation_id = _resolve(project_id, cid)
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


def _question_request(project_id: str, question_id: str):
    session = manager.get(project_id)
    pending = session.pending_permissions.get(question_id) if session else None
    if pending is None or pending.future.done():
        raise ApiError("question_gone", "问题已回答、撤回或失效", 410, {"state": "gone"})
    return pending


@router.post("/conversations/{cid}/questions/{question_id}/answer")
async def answer_question(project_id: str, cid: str, question_id: str, request: Request):
    """回答助手的提问（AskUserQuestion）：问题 ID 即确认请求 ID，选项 ID 为「问题序号.选项序号」。"""
    deps.require_owner(project_id, request)
    _resolve(project_id, cid)
    body = await deps.json_body(request)
    pending = _question_request(project_id, question_id)
    questions = (getattr(pending, "tool_input", None) or {}).get("questions") or []
    chosen = [body["option_id"]] if body.get("option_id") else list(body.get("option_ids") or [])
    custom = str(body.get("custom_text") or "").strip()
    if not chosen and not custom:
        raise ApiError("answer_invalid", "没有选择任何选项", 400)
    answers: dict[str, list[str]] = {}
    for option_id in chosen:
        q_index, _, o_index = str(option_id).partition(".")
        try:
            question = questions[int(q_index)]
            label = question["options"][int(o_index)]["label"]
        except (ValueError, IndexError, KeyError, TypeError):
            raise ApiError("answer_invalid", "选项不存在", 400) from None
        answers.setdefault(question.get("question", ""), []).append(label)
    if custom:
        target = questions[0].get("question", "") if questions else ""
        answers.setdefault(target, []).append(custom)
    flat = {question: "、".join(labels) for question, labels in answers.items()}
    await manager.respond_permission(project_id, question_id, "allow", "", flat)
    return {"question": {"id": question_id, "state": "answered", "answers": flat}}


@router.post("/conversations/{cid}/queue/cancel")
async def cancel_queue(project_id: str, cid: str, request: Request):
    deps.require_owner(project_id, request)
    conversation_id = _resolve(project_id, cid)
    if manager.queue_position(project_id) is None:
        raise ApiError("not_queued", "当前不在排队", 409)
    await manager.interrupt(project_id, conversation_id)
    return {"state": "idle"}


# ---- S-13 对话事件流 ----


def _frame(envelope: dict) -> str:
    head = f"id: {envelope['id']}\n" if "id" in envelope else ""
    return f"{head}data: {json.dumps(envelope, ensure_ascii=False)}\n\n"


@router.get("/conversations/{cid}/events")
async def stream(project_id: str, cid: str, request: Request, after: int | None = None, last_event_id: str = ""):
    """SSE 对话流：先补发 after 之后的持久事件，再持续推送；断线重连带 Last-Event-ID 或 ?after=。"""
    deps.require_owner(project_id, request)
    conversation_id = _resolve(project_id, cid)
    resume_from = last_event_id or request.headers.get("last-event-id", "")
    if after is None:
        after = int(resume_from) + 1 if resume_from.isdigit() else 0

    async def generate():
        queue: asyncio.Queue = asyncio.Queue()
        sent = after  # 已发送到的 seq 水位，避免"历史重放 + 实时推送"重复发送
        # 先登记再重放：重放长历史时事件循环会让出，期间新事件先进队列，重放结束后按 seq 水位去重续发
        manager.watch(project_id, queue, conversation_id)
        try:
            yield "retry: 3000\n\n"
            history = list(manager.get_history(project_id, conversation_id))
            for event in history[sent:]:
                envelope = v1_event(event, project_id, conversation_id)
                if envelope:
                    yield _frame(envelope)
            sent = max(sent, len(history))
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield _frame({"type": "ping", "ts": deps.iso(time.time()), "project_id": project_id,
                                  "conversation_id": conversation_id, "data": {}})
                    continue
                seq = event.get("seq")
                if seq is not None:
                    if seq < sent:
                        continue
                    sent = seq + 1
                envelope = v1_event(event, project_id, conversation_id)
                if envelope:
                    yield _frame(envelope)
        finally:
            manager.unwatch(project_id, queue, conversation_id)

    return StreamingResponse(
        generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
