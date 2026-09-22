import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..services.agent_sessions import manager
from .projects import ensure_access

router = APIRouter(prefix="/api/projects/{project_id}/session", tags=["session"])


class MessageBody(BaseModel):
    text: str


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
def session_state(project_id: str, request: Request):
    ensure_access(project_id, request)
    session = manager.get(project_id)
    history = manager.get_history(project_id)
    return {
        "active": session is not None,
        "session_id": session.id if session else None,
        "state": session.state if session else "idle",
        "events": len(history),
        "queue_position": manager.queue_position(project_id),
        "pool": manager.pool_status(),
    }


@router.post("/messages")
async def send_message(project_id: str, body: MessageBody, request: Request):
    cwd = ensure_access(project_id, request, write=True)
    session = await manager.send(project_id, cwd, body.text)
    return {"session_id": session.id, "state": session.state}


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
async def close_session(project_id: str, request: Request):
    """终止并丢弃当前会话（历史保留）；下次发消息按最新配置新建会话。"""
    ensure_access(project_id, request, write=True)
    await manager.close(project_id)
    return {"ok": True}


@router.post("/interrupt")
async def interrupt(project_id: str, request: Request):
    ensure_access(project_id, request, write=True)
    reconciled = await manager.interrupt(project_id)
    return {"ok": True, "reconciled": bool(reconciled)}


@router.get("/stream")
async def stream(project_id: str, request: Request, after: int = 0):
    """SSE 事件流：先补发 after 之后的历史，再持续推送新事件。"""
    ensure_access(project_id, request)

    async def generate():
        queue: asyncio.Queue = asyncio.Queue()
        sent = after  # 已发送到的 seq 水位，避免"文件历史 + 会话补挂"重复发送
        # 先订阅再重放：重放长历史时事件循环会让出，期间新事件先进队列，
        # 重放结束后按 seq 水位去重续发，不会再丢失重放窗口内的事件
        session = manager.get(project_id)
        if session:
            session.subscribers.append(queue)
        history = list(manager.get_history(project_id))
        for event in history[sent:]:
            yield _sse(event)
        sent = max(sent, len(history))
        try:
            while True:
                current = manager.get(project_id)
                if current and queue not in current.subscribers:
                    # send() 之后才建立的会话：补挂订阅并补发缺口
                    for event in current.events[sent:]:
                        yield _sse(event)
                    sent = max(sent, len(current.events))
                    current.subscribers.append(queue)
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    seq = event.get("seq")
                    if seq is not None:
                        if seq < sent:
                            continue
                        sent = seq + 1
                    yield _sse(event)
                except asyncio.TimeoutError:
                    # 可见心跳：前端据此判断连接是否僵死（SSE 注释行不触发 onmessage）
                    yield _sse({"type": "ping"})
        finally:
            current = manager.get(project_id)
            if current and queue in current.subscribers:
                current.subscribers.remove(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
