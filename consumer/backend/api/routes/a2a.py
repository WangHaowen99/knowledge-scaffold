"""A2A HTTP projection over the canonical native-async run platform."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agent.runtime.errors import (
    EventReplayUnavailableError,
    RunConflictError,
    RunNotFoundError,
    SessionBusyError,
)
from agent.runtime.event_bus import (
    EventBusClosedError,
    EventSubscriberLimitExceeded,
)
from api.auth.auth import AuthUser, require_authenticated_user
from api.dependencies import require_workspace_id
from api.protocols.a2a.models import A2ATaskCreateRequest, A2ATaskState
from api.protocols.a2a.service import A2ATaskService
from api.request_parsing import read_optional_json_object
from utils.identifiers import validate_session_id


a2a_router = APIRouter()


def _runtime(request: Request):
    container = getattr(request.app.state, "runtime_container", None)
    if container is not None:
        return container
    from api.routes import get_agent_execution_runtime

    return get_agent_execution_runtime()


async def _service(
    request: Request,
    user: AuthUser,
    workspace_id: str,
) -> A2ATaskService:
    from api.routes import get_user_stores

    runtime = _runtime(request)
    _chat_store, execution_store = await runtime.short_io.run(
        get_user_stores,
        user.username,
        workspace_id,
    )
    return A2ATaskService(
        runtime=runtime,
        execution_store=execution_store,
        username=user.username,
        workspace_id=workspace_id,
        traceparent=str(request.headers.get("traceparent") or "").strip(),
    )


def _extract_message(payload: dict[str, Any]) -> str:
    message = str(payload.get("message") or "").strip()
    if message:
        return message
    input_payload = payload.get("input")
    if isinstance(input_payload, dict):
        parts = input_payload.get("parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], dict):
            return str(parts[0].get("text") or "").strip()
    return ""


def _event_cursor(request: Request) -> int:
    raw = request.headers.get("Last-Event-ID") or request.query_params.get(
        "after_seq", 0
    )
    try:
        cursor = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("after_seq must be a non-negative integer") from exc
    if cursor < 0:
        raise ValueError("after_seq must be a non-negative integer")
    return cursor


def _encode_task_update(event: Any, *, external_task_id: str) -> str:
    update = {
        "task_id": external_task_id,
        "canonical_run_id": event.run_id,
        "session_id": event.session_id,
        "event_type": event.event_type,
        "timestamp": event.created_at,
        "source": event.source or {},
        "payload": event.payload,
        "seq": event.seq,
    }
    return "id: {}\nevent: task.update\ndata: {}\n\n".format(
        event.seq,
        json.dumps(update, ensure_ascii=False),
    )


def _subscriber_limit(exc: EventSubscriberLimitExceeded) -> JSONResponse:
    return JSONResponse(
        {
            "error": "A2A task event subscriber capacity is exhausted",
            "error_code": exc.error_code,
            "scope": exc.scope,
            "limit": exc.limit,
            "retriable": True,
        },
        status_code=429,
    )


async def _task_stream(
    request: Request,
    *,
    external_task_id: str,
    canonical_run_id: str,
    after_seq: int,
) -> StreamingResponse | JSONResponse:
    runtime = _runtime(request)
    manager = runtime.manager
    event_hub = runtime.event_hub
    reservation_id: str | None = None
    try:
        await manager.repository.list_after(canonical_run_id, after_seq)
        reservation_id = await event_hub.reserve_subscriber(canonical_run_id)
    except EventReplayUnavailableError:
        return JSONResponse(
            {"error": "Task event stream is no longer available"},
            status_code=410,
        )
    except EventSubscriberLimitExceeded as exc:
        return _subscriber_limit(exc)
    except (KeyError, RunNotFoundError):
        return JSONResponse({"error": "Task not found"}, status_code=404)
    except EventBusClosedError:
        return JSONResponse(
            {"error": "Runtime is draining", "error_code": "RUNTIME_DRAINING"},
            status_code=503,
        )

    subscription = event_hub.stream(
        canonical_run_id,
        after_seq=after_seq,
        reservation_id=reservation_id,
    )

    async def generate():
        iterator = subscription.__aiter__()
        pending: asyncio.Task | None = None
        try:
            yield "retry: 2000\n\n"
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(iterator))
                try:
                    event = await asyncio.wait_for(
                        asyncio.shield(pending),
                        timeout=15.0,
                    )
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                except StopAsyncIteration:
                    return
                pending = None
                yield _encode_task_update(
                    event,
                    external_task_id=external_task_id,
                )
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await iterator.aclose()
            if reservation_id is not None:
                await event_hub.release_subscriber_reservation(reservation_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@a2a_router.get("/.well-known/a2a/agent-card.json")
def get_agent_card(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return {
        "name": "onto-agent",
        "description": "A2A adapter over canonical asynchronous Runs",
        "version": "0.1.0",
        "url": base_url,
        "capabilities": {
            "streaming": True,
            "cancellation": True,
            "supports_user_interaction": False,
        },
        "endpoints": {
            "create_task": "/a2a/tasks",
            "get_task": "/a2a/tasks/{task_id}",
            "cancel_task": "/a2a/tasks/{task_id}",
            "task_events": "/a2a/tasks/{task_id}/events",
            "stream_create": "/a2a/tasks/stream",
        },
        "authentication": {
            "type": "bearer",
            "notes": "Same authentication as the canonical agent endpoints",
        },
    }


def _task_request(payload: dict[str, Any], message: str) -> A2ATaskCreateRequest:
    context = payload.get("context")
    if context is not None and not isinstance(context, dict):
        raise ValueError("context must be an object or null")
    return A2ATaskCreateRequest(
        message=message,
        session_id=payload.get("session_id") or None,
        task_id=str(payload.get("task_id") or "").strip() or None,
        context=dict(context or {}),
    )


@a2a_router.post("/a2a/tasks")
async def create_task(
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    payload = await read_optional_json_object(request)
    message = _extract_message(payload)
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)
    try:
        service = await _service(request, user, workspace_id)
        view, _snapshot = await service.create_task(
            _task_request(payload, message)
        )
    except (RunConflictError, SessionBusyError) as exc:
        return JSONResponse(
            {"error": str(exc), "error_code": exc.error_code},
            status_code=409,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"task": view.to_dict()}, status_code=202)


@a2a_router.get("/a2a/tasks/{task_id}")
async def get_task(
    task_id: str,
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    session_id = str(request.query_params.get("session_id") or "").strip() or None
    try:
        if session_id:
            session_id = validate_session_id(session_id)
        service = await _service(request, user, workspace_id)
        view, _snapshot = await service.get_task(
            task_id,
            session_id=session_id,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(
        {"task": view.to_dict()},
        status_code=404 if view.state is A2ATaskState.NOT_FOUND else 200,
    )


@a2a_router.delete("/a2a/tasks/{task_id}")
async def cancel_task(
    task_id: str,
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    try:
        service = await _service(request, user, workspace_id)
        view = await service.cancel_task(task_id)
    except RunNotFoundError:
        return JSONResponse({"error": "Task not found"}, status_code=404)
    return {"success": True, "task": view.to_dict()}


@a2a_router.get("/a2a/tasks/{task_id}/events")
async def replay_task_events(
    task_id: str,
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    service = await _service(request, user, workspace_id)
    view, snapshot = await service.get_task(task_id)
    if view.state is A2ATaskState.NOT_FOUND:
        return JSONResponse({"error": "Task not found"}, status_code=404)
    canonical_run_id = (
        snapshot.run_id if snapshot is not None else await service.canonical_run_id(task_id)
    )
    if not canonical_run_id:
        return JSONResponse({"error": "Task not found"}, status_code=404)
    try:
        after_seq = _event_cursor(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return await _task_stream(
        request,
        external_task_id=task_id,
        canonical_run_id=canonical_run_id,
        after_seq=after_seq,
    )


@a2a_router.post("/a2a/tasks/stream")
async def create_task_and_stream(
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    payload = await read_optional_json_object(request)
    message = _extract_message(payload)
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)
    if not str(payload.get("session_id") or "").strip():
        return JSONResponse({"error": "session_id is required"}, status_code=400)
    try:
        after_seq = _event_cursor(request)
        service = await _service(request, user, workspace_id)
        view, snapshot = await service.create_task(
            _task_request(payload, message)
        )
    except (RunConflictError, SessionBusyError) as exc:
        return JSONResponse(
            {"error": str(exc), "error_code": exc.error_code},
            status_code=409,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return await _task_stream(
        request,
        external_task_id=view.id,
        canonical_run_id=snapshot.run_id,
        after_seq=after_seq,
    )
