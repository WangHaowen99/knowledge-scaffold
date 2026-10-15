import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..services import conversation_store
from ..services.agent_sessions import VISIBLE_HISTORY_TAIL, manager, visible_history_start
from .projects import ensure_access

router = APIRouter(prefix="/api/projects/{project_id}/session", tags=["session"])

HEARTBEAT_SECONDS = 15


class MessageBody(BaseModel):
    text: str
    conversation_id: str | None = None  # 省略 = 引擎当前绑定的会话，否则最近更新的一条


class ConversationTitleBody(BaseModel):
    title: str


class PermissionBody(BaseModel):
    behavior: str  # allow | deny
    message: str = ""
    answers: dict | None = None  # AskUserQuestion 的选项答案（问题文本 → 选项文本）


@router.get("/usage")
def session_usage(project_id: str, request: Request):
    """汇总该项目全部模型调用的成本与 token 用量（从事件历史统计）。"""
    ensure_access(project_id, request)
    from ..services.agent_sessions import usage_summary

    return usage_summary(project_id)


@router.get("")
def session_state(project_id: str, request: Request, conversation: str | None = None):
    ensure_access(project_id, request)
    conversation_id = manager.resolve_conversation(project_id, conversation)
    live = manager.get(project_id)
    # 引擎绑着别的会话时，这条会话自己没有在线状态
    session = live if live and live.conversation_id == conversation_id else None
    history = manager.get_history(project_id, conversation_id)
    return {
        "conversation_id": conversation_id,
        "live_conversation_id": live.conversation_id if live else None,
        "active": session is not None,
        "session_id": session.id if session else None,
        "state": session.state if session else "idle",
        "events": len(history),
        "history_start": visible_history_start(history, VISIBLE_HISTORY_TAIL),
        "queue_position": manager.queue_position(project_id) if session else None,
        "pool": manager.pool_status(),
    }


@router.get("/conversations")
def list_conversations(project_id: str, request: Request):
    """项目的全部会话（最近更新的在前），以及引擎当前绑定的是哪一条。"""
    ensure_access(project_id, request)
    return {
        "conversations": conversation_store.list_conversations(project_id),
        "live": manager.live_status(project_id),
    }


@router.post("/conversations")
def create_conversation(project_id: str, request: Request):
    """新建一条空会话；发第一条消息时引擎才换绑过来。"""
    ensure_access(project_id, request, write=True)
    return conversation_store.create(project_id)


@router.patch("/conversations/{conversation_id}")
def rename_conversation(
    project_id: str, conversation_id: str, body: ConversationTitleBody, request: Request
):
    ensure_access(project_id, request, write=True)
    return conversation_store.rename(project_id, conversation_id, body.title)


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(project_id: str, conversation_id: str, request: Request):
    ensure_access(project_id, request, write=True)
    remaining = await manager.delete_conversation(project_id, conversation_id)
    return {"conversations": remaining}


@router.post("/messages")
async def send_message(project_id: str, body: MessageBody, request: Request):
    cwd = ensure_access(project_id, request, write=True)
    session = await manager.send(project_id, cwd, body.text, body.conversation_id)
    return {
        "session_id": session.id,
        "state": session.state,
        "conversation_id": session.conversation_id,
    }


@router.post("/permissions/{request_id}")
async def respond_permission(
    project_id: str, request_id: str, body: PermissionBody, request: Request
):
    ensure_access(project_id, request, write=True)
    await manager.respond_permission(
        project_id, request_id, body.behavior, body.message, body.answers
    )
    return {"ok": True}


@router.post("/close")
async def close_session(project_id: str, request: Request, conversation: str | None = None):
    """丢弃会话的模型上下文（历史保留）；下次发消息按最新配置新建会话。"""
    ensure_access(project_id, request, write=True)
    await manager.close(project_id, conversation)
    return {"ok": True}


@router.post("/interrupt")
async def interrupt(project_id: str, request: Request, conversation: str | None = None):
    ensure_access(project_id, request, write=True)
    reconciled = await manager.interrupt(project_id, conversation)
    return {"ok": True, "reconciled": bool(reconciled)}


@router.get("/stream")
async def stream(
    project_id: str, request: Request, after: int = 0, conversation: str | None = None
):
    """SSE 事件流：先补发 after 之后的历史，再持续推送新事件。"""
    ensure_access(project_id, request)
    conversation_id = manager.resolve_conversation(project_id, conversation)

    async def generate():
        queue: asyncio.Queue = asyncio.Queue()
        sent = after  # 已发送到的 seq 水位，避免"历史重放 + 实时推送"重复发送
        # 先登记再重放：重放长历史时事件循环会让出，期间新事件先进队列，
        # 重放结束后按 seq 水位去重续发。登记在会话管理器上：此后为这条会话新建的
        # Session（首条消息、空闲回收后再发、切换会话后再发）建立时即挂上本队列；
        # 引擎绑着别的会话时这条流只回放历史并发心跳
        manager.watch(project_id, queue, conversation_id)
        try:
            history = list(manager.get_history(project_id, conversation_id))
            for event in history[sent:]:
                yield _sse(event)
            sent = max(sent, len(history))
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    # 可见心跳：前端据此判断连接是否僵死（SSE 注释行不触发 onmessage）
                    yield _sse({"type": "ping"})
                    continue
                seq = event.get("seq")
                if seq is not None:
                    if seq < sent:
                        continue
                    sent = seq + 1
                yield _sse(event)
        finally:
            manager.unwatch(project_id, queue, conversation_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
