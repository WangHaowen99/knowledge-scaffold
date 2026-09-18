import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..services.agent_sessions import manager
from .projects import workspaces

router = APIRouter(prefix="/api/projects/{project_id}/session", tags=["session"])


class MessageBody(BaseModel):
    text: str


class PermissionBody(BaseModel):
    behavior: str  # allow | deny
    message: str = ""


@router.get("")
def session_state(project_id: str):
    workspaces.project_root(project_id)
    session = manager.get(project_id)
    if not session:
        return {"active": False}
    return {
        "active": True,
        "session_id": session.id,
        "state": session.state,
        "events": len(session.events),
    }


@router.post("/messages")
async def send_message(project_id: str, body: MessageBody):
    cwd = workspaces.project_root(project_id)
    session = await manager.send(project_id, cwd, body.text)
    return {"session_id": session.id, "state": session.state}


@router.post("/permissions/{request_id}")
async def respond_permission(project_id: str, request_id: str, body: PermissionBody):
    await manager.respond_permission(project_id, request_id, body.behavior, body.message)
    return {"ok": True}


@router.post("/interrupt")
async def interrupt(project_id: str):
    await manager.interrupt(project_id)
    return {"ok": True}


@router.get("/stream")
async def stream(project_id: str, after: int = 0):
    """SSE 事件流：先补发 after 之后的历史，再持续推送新事件。"""
    workspaces.project_root(project_id)

    async def generate():
        queue: asyncio.Queue = asyncio.Queue()
        session = manager.get(project_id)
        if session:
            for event in session.events[after:]:
                yield _sse(event)
            session.subscribers.append(queue)
        try:
            while True:
                current = manager.get(project_id)
                if current and queue not in current.subscribers:
                    # send() 之后才建立的会话：补挂订阅并补发历史
                    for event in current.events[after:]:
                        yield _sse(event)
                    current.subscribers.append(queue)
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield _sse(event)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
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
