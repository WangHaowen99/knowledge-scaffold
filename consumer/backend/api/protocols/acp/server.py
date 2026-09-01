"""FastAPI ACP adapter backed exclusively by canonical Runs and durable Turns."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agent.core.shared.modes import resolve_agent_mode
from agent.runtime.contracts import (
    RunScope,
    RunSnapshot,
    RunStatus as CanonicalRunStatus,
    RuntimeInvocationContext,
    TERMINAL_RUN_STATUSES,
    USER_WORKSPACE_RUN_CONCURRENCY_LIMIT,
)
from agent.runtime.errors import RunNotFoundError, SessionBusyError
from api.auth.auth import (
    AuthUser,
    authenticate_user,
    current_user_scope,
)
from api.final_output import coerce_final_output_to_text
from api.protocols.acp.config import ACPConfig, get_config
from api.protocols.acp.models import (
    MessagePartType,
    MessageRole,
    RunResponse,
    RunStatus,
)
from api.request_parsing import read_optional_json_object
from llm.llm_request_mode import normalize_llm_request_mode
from persistence.execution_store import InteractionCheckpointError
from utils.identifiers import validate_session_id
from workspace.workspaces import resolve_workspace_id, workspace_scope


acp_router = APIRouter(prefix="/acp")
_PROTOCOL = "acp"


def _runtime(request: Request):
    container = getattr(request.app.state, "runtime_container", None)
    if container is not None:
        return container
    from api.routes import get_agent_execution_runtime

    return get_agent_execution_runtime()


async def _short_io(request: Request, function, *args, **kwargs):
    return await _runtime(request).short_io.run(function, *args, **kwargs)


async def _acp_principal(request: Request):
    if get_config().acp.authentication.type == "none":
        user = AuthUser(username="anonymous", role="anonymous")
    else:
        user = await authenticate_user(request)
    with current_user_scope(user):
        yield user


async def _acp_workspace_id(
    request: Request,
    _principal: AuthUser = Depends(_acp_principal),
):
    workspace_id = await _short_io(request, resolve_workspace_id, request)
    with workspace_scope(workspace_id):
        yield workspace_id


async def _stores(request: Request, username: str, workspace_id: str):
    from api.routes import get_user_stores

    return await _short_io(request, get_user_stores, username, workspace_id)


def _scope(user: AuthUser, workspace_id: str) -> RunScope:
    return RunScope(
        username=user.username,
        workspace_id=workspace_id,
        scopes=("agent:run", "agent:read", "agent:cancel"),
    )


def _extract_text_from_messages(messages: Iterable[Any]) -> str:
    text_parts: list[str] = []
    for message in messages:
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        if role not in (MessageRole.USER, MessageRole.USER.value):
            continue
        parts = message.get("parts", []) if isinstance(message, dict) else getattr(message, "parts", [])
        for part in parts or []:
            part_type = part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
            if part_type not in (MessagePartType.TEXT, MessagePartType.TEXT.value):
                continue
            content = part.get("content") if isinstance(part, dict) else getattr(part, "content", None)
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, dict):
                text_parts.append(json.dumps(content, ensure_ascii=False))
    return "\n".join(text_parts).strip()


def _agent_message(text: str) -> dict[str, Any]:
    return {
        "role": "agent",
        "parts": [
            {"type": "text", "content": str(text), "content_type": "text/plain"}
        ],
        "metadata": {},
    }


def _build_await_response(awaiting_data: dict[str, Any] | None):
    if not isinstance(awaiting_data, dict):
        return None
    interaction = awaiting_data.get("interaction")
    if not isinstance(interaction, dict):
        interaction = awaiting_data
    await_id = str(
        awaiting_data.get("tool_call_id")
        or interaction.get("tool_call_id")
        or interaction.get("id")
        or ""
    ).strip()
    question = str(
        awaiting_data.get("question")
        or awaiting_data.get("prompt")
        or awaiting_data.get("text")
        or interaction.get("prompt")
        or ""
    ).strip()
    if not await_id and not question:
        return None
    return {
        "await_id": await_id,
        "message": _agent_message(question),
        "options": awaiting_data.get("options") or interaction.get("options") or None,
        "timeout": None,
    }


def _interaction_response_values(
    payload: dict[str, Any],
    pending: dict[str, Any],
    user_response: str,
) -> tuple[str, Any, str | None]:
    response = payload.get("response")
    response = response if isinstance(response, dict) else {}
    metadata = response.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    interaction_id = str(
        payload.get("await_id")
        or payload.get("interaction_id")
        or pending.get("id")
        or ""
    ).strip()
    option_id = str(
        payload.get("option_id")
        or payload.get("selected_option_id")
        or response.get("option_id")
        or response.get("selected_option_id")
        or metadata.get("option_id")
        or metadata.get("selected_option_id")
        or ""
    ).strip() or None
    options = (
        pending.get("options")
        if isinstance(pending.get("options"), list)
        else []
    )
    selected = None
    if option_id:
        for option in options:
            if not isinstance(option, dict):
                continue
            candidate_id = str(option.get("id") or option.get("option_id") or "")
            if candidate_id == option_id:
                selected = option
                break
    if selected is None and not option_id and user_response:
        for option in options:
            if not isinstance(option, dict):
                continue
            response_values = {
                str(option.get("id") or option.get("option_id") or ""),
                str(option.get("value") or ""),
                str(option.get("label") or ""),
            }
            if user_response in response_values:
                selected = option
                break
    if selected is not None:
        option_id = (
            str(selected.get("id") or selected.get("option_id") or "").strip()
            or None
        )
        value = selected.get("value")
        if value is None:
            value = user_response or option_id
    else:
        value = user_response
    return interaction_id, value, option_id


def _protocol_metadata(
    external_run_id: str,
    context: dict[str, Any],
    *,
    parent_run_id: str | None = None,
    interaction_id: str | None = None,
    lang: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "protocol": _PROTOCOL,
        "external_run_id": external_run_id,
        "context": dict(context),
    }
    if parent_run_id:
        metadata["parent_run_id"] = parent_run_id
    if interaction_id:
        metadata["interaction_id"] = interaction_id
    if lang:
        metadata["lang"] = lang
    return metadata


async def _find_turn(request: Request, execution_store: Any, external_run_id: str):
    session_id, turn = await _short_io(
        request,
        execution_store.find_turn_by_protocol_id,
        protocol=_PROTOCOL,
        external_run_id=external_run_id,
    )
    if session_id is None or turn is None:
        return None
    return session_id, turn


async def _admit_run(
    request: Request,
    *,
    user: AuthUser,
    workspace_id: str,
    execution_store: Any,
    external_run_id: str,
    session_id: str,
    query: str,
    context_values: dict[str, Any],
    lang: str | None,
    interaction_response: dict[str, Any] | None = None,
    parent_run_id: str | None = None,
) -> RunSnapshot:
    runtime = _runtime(request)
    manager = runtime.manager
    scope = _scope(user, workspace_id)
    raw_arguments = context_values.get("arguments")
    if raw_arguments is not None and not isinstance(raw_arguments, dict):
        raise ValueError("arguments must be an object or null")
    async with manager.session_guard(scope, session_id):
        await manager.assert_session_available(scope, session_id)
        metadata = _protocol_metadata(
            external_run_id,
            context_values,
            parent_run_id=parent_run_id,
            interaction_id=(
                str(interaction_response.get("interaction_id") or "")
                if interaction_response is not None
                else None
            ),
            lang=lang,
        )
        if interaction_response is None:
            turn, created = await runtime.short_io.run(
                execution_store.create_turn_for_run,
                session_id=session_id,
                user_request=query,
                protocol_metadata=metadata,
            )
        else:
            turn, created = await runtime.short_io.run(
                execution_store.consume_interaction_and_create_turn,
                session_id=session_id,
                interaction_id=str(interaction_response.get("interaction_id") or ""),
                user_request=query,
                interaction_response=interaction_response,
                protocol_metadata=metadata,
            )
        if not created:
            existing = await manager.get_run(str(turn["run_id"]), scope)
            if existing is None:
                raise RuntimeError("canonical run state is unavailable")
            return existing
        runtime_context = RuntimeInvocationContext(
            run_id=str(turn["run_id"]),
            session_id=session_id,
            turn_id=int(turn["turn_id"]),
            workspace_id=workspace_id,
            username=user.username,
            query=query,
            agent_mode=resolve_agent_mode(context_values),
            llm_request_mode=normalize_llm_request_mode(
                context_values.get("llm_request_mode")
            ),
            model_profile_id=(
                str(context_values.get("model_profile_id") or "").strip() or None
            ),
            requested_operation_key=(
                str(context_values.get("requested_operation_key") or "").strip()
                or None
            ),
            arguments=dict(raw_arguments) if raw_arguments is not None else None,
            include_trace=False,
            supports_user_interaction=True,
            traceparent=str(request.headers.get("traceparent") or "").strip(),
            interaction_response=(
                dict(interaction_response) if interaction_response is not None else None
            ),
            concurrency_limit=USER_WORKSPACE_RUN_CONCURRENCY_LIMIT,
            timeout_seconds=float(max(1, get_config().acp.run.timeout)),
        )
        try:
            snapshot, manager_created = await manager.create_run(
                runtime_context,
                scope,
                session_guarded=True,
            )
        except Exception:
            await runtime.short_io.run(
                execution_store.mark_turn_task_start_failed,
                session_id,
                int(turn["turn_id"]),
            )
            raise
        if not manager_created:
            await runtime.short_io.run(
                execution_store.mark_turn_task_start_failed,
                session_id,
                int(turn["turn_id"]),
            )
            raise RuntimeError("canonical run task was not uniquely registered")
        return snapshot


def _result_content(snapshot: RunSnapshot) -> str:
    if snapshot.result is None:
        return ""
    return snapshot.result.content or coerce_final_output_to_text(
        snapshot.result.structured_content
    )


def _response_from_snapshot(
    snapshot: RunSnapshot,
    *,
    external_run_id: str,
) -> RunResponse:
    status = {
        CanonicalRunStatus.QUEUED: RunStatus.PENDING,
        CanonicalRunStatus.RUNNING: RunStatus.RUNNING,
        CanonicalRunStatus.AWAITING_INPUT: RunStatus.AWAITING,
        CanonicalRunStatus.COMPLETED: RunStatus.COMPLETED,
        CanonicalRunStatus.FAILED: RunStatus.FAILED,
        CanonicalRunStatus.CANCELLED: RunStatus.CANCELLED,
    }[snapshot.status]
    content = _result_content(snapshot)
    return RunResponse(
        run_id=external_run_id,
        status=status,
        messages=[_agent_message(content)] if content else [],
        created_at=datetime.fromisoformat(snapshot.created_at.replace("Z", "+00:00")),
        updated_at=datetime.fromisoformat(snapshot.updated_at.replace("Z", "+00:00")),
        awaiting=_build_await_response(snapshot.pending_interaction),
        error=snapshot.error.message if snapshot.error is not None else None,
        metadata={
            "canonical_run_id": snapshot.run_id,
            "session_id": snapshot.session_id,
        },
    )


def _response_from_turn(turn: dict[str, Any], *, external_run_id: str) -> RunResponse:
    status_value = str(turn.get("status") or "failed")
    status = {
        "queued": RunStatus.PENDING,
        "running": RunStatus.RUNNING,
        "awaiting_input": RunStatus.AWAITING,
        "success": RunStatus.COMPLETED,
        "failed": RunStatus.FAILED,
        "cancelled": RunStatus.CANCELLED,
    }.get(status_value, RunStatus.FAILED)
    final_output = turn.get("final_output")
    content = coerce_final_output_to_text(final_output)
    pending = turn.get("pending_interaction")
    started = str(turn.get("start_time") or datetime.now(timezone.utc).isoformat())
    updated = str(turn.get("end_time") or started)
    return RunResponse(
        run_id=external_run_id,
        status=status,
        messages=[_agent_message(content)] if content else [],
        created_at=datetime.fromisoformat(started.replace("Z", "+00:00")),
        updated_at=datetime.fromisoformat(updated.replace("Z", "+00:00")),
        awaiting=_build_await_response(pending if isinstance(pending, dict) else None),
        error=(
            str(final_output.get("error") or "Execution failed")
            if status is RunStatus.FAILED and isinstance(final_output, dict)
            else None
        ),
        metadata={
            "canonical_run_id": str(turn.get("run_id") or ""),
            "session_id": str(turn.get("session_id") or ""),
            "durable": True,
        },
    )


async def _current_response(
    request: Request,
    *,
    user: AuthUser,
    workspace_id: str,
    execution_store: Any,
    external_run_id: str,
) -> tuple[RunResponse | None, RunSnapshot | None, dict[str, Any] | None]:
    mapping = await _find_turn(request, execution_store, external_run_id)
    if mapping is None:
        return None, None, None
    _session_id, turn = mapping
    snapshot = await _runtime(request).manager.get_run(
        str(turn["run_id"]),
        _scope(user, workspace_id),
    )
    if snapshot is not None:
        return _response_from_snapshot(snapshot, external_run_id=external_run_id), snapshot, turn
    return _response_from_turn(turn, external_run_id=external_run_id), None, turn


async def _wait_for_terminal(
    request: Request,
    snapshot: RunSnapshot,
    scope: RunScope,
) -> RunSnapshot:
    manager = _runtime(request).manager
    current = await manager.get_run(snapshot.run_id, scope)
    if current is not None and current.status in TERMINAL_RUN_STATUSES:
        return current

    async def consume():
        async for _event in _runtime(request).event_hub.stream(
            snapshot.run_id,
            after_seq=0,
        ):
            pass
        result = await manager.get_run(snapshot.run_id, scope)
        if result is None:
            raise RuntimeError("canonical run disappeared")
        return result

    return await asyncio.wait_for(
        consume(),
        timeout=float(max(1, get_config().acp.run.timeout)),
    )


def _map_public_event(event: Any, external_run_id: str):
    payload = dict(event.payload or {})
    base = {"run_id": external_run_id, "canonical_run_id": event.run_id}
    if event.event_type in {"token.delta", "reasoning.delta"}:
        text = str(payload.get("text") or "")
        if text:
            yield "message_delta", {**base, "type": "message_delta", "text": text}
    elif event.event_type in {"tool.started", "tool.progress"}:
        text = str(payload.get("message") or payload.get("name") or "")
        if text:
            yield "thinking", {**base, "type": "thinking", "text": text}
    elif event.event_type == "tool.finished":
        yield "result", {**base, "type": "result", "data": payload, "done": True}
    elif event.event_type == "turn.started":
        yield "run.started", {**base, "type": "run.started"}
    elif event.event_type == "turn.finished":
        status = str(event.status or payload.get("status") or "")
        if status == "completed":
            content = coerce_final_output_to_text(
                payload.get("structured_content")
                if payload.get("structured_content") is not None
                else payload.get("content")
            )
            if content:
                yield "answer", {**base, "type": "answer", "text": content}
            yield "run.completed", {**base, "type": "run.completed"}
        elif status == "awaiting_input":
            yield "run.awaiting", {
                **base,
                "type": "run.awaiting",
                "interaction": payload.get("interaction")
                or payload.get("pending_interaction"),
            }
        elif status == "cancelled":
            yield "run.cancelled", {**base, "type": "run.cancelled"}
        else:
            yield "run.failed", {
                **base,
                "type": "run.failed",
                "error": payload.get("error") or "Run failed",
            }


async def _stream_response(
    request: Request,
    *,
    external_run_id: str,
    canonical_run_id: str,
    after_seq: int,
):
    runtime = _runtime(request)
    try:
        await runtime.manager.repository.list_after(canonical_run_id, after_seq)
    except Exception:
        return JSONResponse(
            {"detail": "Run event stream is unavailable"},
            status_code=410,
        )

    async def generate() -> AsyncIterator[str]:
        iterator = runtime.event_hub.stream(
            canonical_run_id,
            after_seq=after_seq,
        ).__aiter__()
        pending: asyncio.Task | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(iterator))
                try:
                    event = await asyncio.wait_for(asyncio.shield(pending), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                except StopAsyncIteration:
                    return
                pending = None
                for event_type, data in _map_public_event(event, external_run_id):
                    yield "event: {}\ndata: {}\n\n".format(
                        event_type,
                        json.dumps(data, ensure_ascii=False),
                    )
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await iterator.aclose()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _cursor(request: Request) -> int:
    raw = request.headers.get("Last-Event-ID") or request.query_params.get("after_seq") or 0
    value = int(raw)
    if value < 0:
        raise ValueError("after_seq must be non-negative")
    return value


@acp_router.get("/agent")
async def get_agent_detail(
    request: Request,
    _user: AuthUser = Depends(_acp_principal),
    workspace_id: str = Depends(_acp_workspace_id),
):
    config: ACPConfig = get_config()
    request_root = f"{str(request.base_url).rstrip('/')}/"
    base = urljoin(request_root, "acp")
    return {
        "name": config.agent.name,
        "version": config.agent.version,
        "description": config.agent.description,
        "capabilities": {
            "supported_content_types": config.acp.capabilities.supported_content_types,
            "supported_modes": config.acp.capabilities.supported_modes,
            "features": config.acp.capabilities.features,
        },
        "authentication": {
            "type": config.acp.authentication.type,
            "scheme": "bearer" if config.acp.authentication.type != "none" else None,
        },
        "endpoints": {
            "agent": urljoin(f"{base.rstrip('/')}/", "agent"),
            "runs": urljoin(f"{base.rstrip('/')}/", "runs"),
        },
        "metadata": {
            "workspace_id": workspace_id,
            "protocol_version": config.acp.protocol_version,
        },
    }


@acp_router.post("/runs", status_code=202)
async def create_run(
    request: Request,
    user: AuthUser = Depends(_acp_principal),
    workspace_id: str = Depends(_acp_workspace_id),
):
    payload = await read_optional_json_object(request)
    query = _extract_text_from_messages(payload.get("messages", []))
    if not query:
        return JSONResponse({"detail": "No user message provided"}, status_code=400)
    context_values = payload.get("context")
    if context_values is not None and not isinstance(context_values, dict):
        return JSONResponse({"detail": "context must be an object or null"}, status_code=400)
    context_values = dict(context_values or {})
    raw_arguments = payload.get("arguments")
    if raw_arguments is not None and not isinstance(raw_arguments, dict):
        return JSONResponse(
            {"detail": "arguments must be an object or null"},
            status_code=400,
        )
    if raw_arguments is not None:
        context_values["arguments"] = dict(raw_arguments)
    requested_operation_key = str(
        payload.get("requested_operation_key") or ""
    ).strip()
    if requested_operation_key:
        context_values["requested_operation_key"] = requested_operation_key
    external_run_id = "run_{}".format(uuid.uuid4().hex)
    try:
        session_id = validate_session_id(
            context_values.get("session_id") or "session_{}".format(uuid.uuid4().hex)
        )
        _chat_store, execution_store = await _stores(
            request,
            user.username,
            workspace_id,
        )
        snapshot = await _admit_run(
            request,
            user=user,
            workspace_id=workspace_id,
            execution_store=execution_store,
            external_run_id=external_run_id,
            session_id=session_id,
            query=query,
            context_values=context_values,
            lang=str(payload.get("lang") or "").strip() or None,
        )
    except (ValueError, SessionBusyError) as exc:
        status_code = 409 if isinstance(exc, SessionBusyError) else 400
        return JSONResponse({"detail": str(exc)}, status_code=status_code)
    return JSONResponse(
        _response_from_snapshot(snapshot, external_run_id=external_run_id).model_dump(mode="json"),
        status_code=202,
    )


@acp_router.get("/runs/{run_id}")
async def get_run_status(
    run_id: str,
    request: Request,
    user: AuthUser = Depends(_acp_principal),
    workspace_id: str = Depends(_acp_workspace_id),
):
    _chat_store, execution_store = await _stores(
        request,
        user.username,
        workspace_id,
    )
    response, _snapshot, _turn = await _current_response(
        request,
        user=user,
        workspace_id=workspace_id,
        execution_store=execution_store,
        external_run_id=run_id,
    )
    if response is None:
        return JSONResponse({"detail": "Run not found"}, status_code=404)
    return response.model_dump(mode="json")


@acp_router.post("/runs/{run_id}/execute")
async def execute_run(
    run_id: str,
    request: Request,
    user: AuthUser = Depends(_acp_principal),
    workspace_id: str = Depends(_acp_workspace_id),
):
    _chat_store, execution_store = await _stores(
        request,
        user.username,
        workspace_id,
    )
    response, snapshot, _turn = await _current_response(
        request,
        user=user,
        workspace_id=workspace_id,
        execution_store=execution_store,
        external_run_id=run_id,
    )
    if response is None:
        return JSONResponse({"detail": "Run not found"}, status_code=404)
    if snapshot is None or snapshot.status in TERMINAL_RUN_STATUSES:
        return response.model_dump(mode="json")
    try:
        terminal = await _wait_for_terminal(request, snapshot, _scope(user, workspace_id))
    except asyncio.TimeoutError:
        return JSONResponse({"detail": "Run execution timed out"}, status_code=504)
    return _response_from_snapshot(terminal, external_run_id=run_id).model_dump(mode="json")


@acp_router.post("/runs/{run_id}/stream")
async def stream_run(
    run_id: str,
    request: Request,
    user: AuthUser = Depends(_acp_principal),
    workspace_id: str = Depends(_acp_workspace_id),
):
    _chat_store, execution_store = await _stores(
        request,
        user.username,
        workspace_id,
    )
    response, snapshot, turn = await _current_response(
        request,
        user=user,
        workspace_id=workspace_id,
        execution_store=execution_store,
        external_run_id=run_id,
    )
    if response is None or turn is None:
        return JSONResponse({"detail": "Run not found"}, status_code=404)
    try:
        after_seq = _cursor(request)
    except (TypeError, ValueError):
        return JSONResponse({"detail": "after_seq must be non-negative"}, status_code=400)
    canonical_run_id = snapshot.run_id if snapshot is not None else str(turn.get("run_id") or "")
    return await _stream_response(
        request,
        external_run_id=run_id,
        canonical_run_id=canonical_run_id,
        after_seq=after_seq,
    )


async def _create_from_payload(
    request: Request,
    payload: dict[str, Any],
    user: AuthUser,
    workspace_id: str,
) -> tuple[str, RunSnapshot] | JSONResponse:
    query = _extract_text_from_messages(payload.get("messages", []))
    if not query:
        return JSONResponse({"detail": "No user message provided"}, status_code=400)
    context_values = payload.get("context")
    if context_values is not None and not isinstance(context_values, dict):
        return JSONResponse({"detail": "context must be an object or null"}, status_code=400)
    context_values = dict(context_values or {})
    raw_arguments = payload.get("arguments")
    if raw_arguments is not None and not isinstance(raw_arguments, dict):
        return JSONResponse(
            {"detail": "arguments must be an object or null"},
            status_code=400,
        )
    if raw_arguments is not None:
        context_values["arguments"] = dict(raw_arguments)
    requested_operation_key = str(
        payload.get("requested_operation_key") or ""
    ).strip()
    if requested_operation_key:
        context_values["requested_operation_key"] = requested_operation_key
    external_run_id = "run_{}".format(uuid.uuid4().hex)
    session_id = validate_session_id(
        context_values.get("session_id") or "session_{}".format(uuid.uuid4().hex)
    )
    _chat_store, execution_store = await _stores(
        request,
        user.username,
        workspace_id,
    )
    snapshot = await _admit_run(
        request,
        user=user,
        workspace_id=workspace_id,
        execution_store=execution_store,
        external_run_id=external_run_id,
        session_id=session_id,
        query=query,
        context_values=context_values,
        lang=str(payload.get("lang") or "").strip() or None,
    )
    return external_run_id, snapshot


@acp_router.post("/runs/execute_stream")
async def execute_stream(
    request: Request,
    user: AuthUser = Depends(_acp_principal),
    workspace_id: str = Depends(_acp_workspace_id),
):
    payload = await read_optional_json_object(request)
    try:
        created = await _create_from_payload(request, payload, user, workspace_id)
    except (ValueError, SessionBusyError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=409)
    if isinstance(created, JSONResponse):
        return created
    external_run_id, snapshot = created
    return await _stream_response(
        request,
        external_run_id=external_run_id,
        canonical_run_id=snapshot.run_id,
        after_seq=0,
    )


async def _answer_interaction(
    run_id: str,
    request: Request,
    payload: dict[str, Any],
    user: AuthUser,
    workspace_id: str,
) -> tuple[RunSnapshot, Any] | JSONResponse:
    _chat_store, execution_store = await _stores(
        request,
        user.username,
        workspace_id,
    )
    response, snapshot, turn = await _current_response(
        request,
        user=user,
        workspace_id=workspace_id,
        execution_store=execution_store,
        external_run_id=run_id,
    )
    if response is None or turn is None:
        return JSONResponse({"detail": "Run not found"}, status_code=404)
    if response.status is not RunStatus.AWAITING:
        return JSONResponse({"detail": "Run is not in awaiting state"}, status_code=409)
    pending = (
        snapshot.pending_interaction
        if snapshot is not None
        else turn.get("pending_interaction")
    )
    if not isinstance(pending, dict):
        return JSONResponse({"detail": "Run interaction checkpoint is unavailable"}, status_code=409)
    response_message = payload.get("response")
    user_text = _extract_text_from_messages([response_message]) if response_message else ""
    user_text = user_text or str(payload.get("response_text") or "").strip()
    interaction_id, value, option_id = _interaction_response_values(
        payload,
        pending,
        user_text,
    )
    interaction_response: dict[str, Any] = {
        "interaction_id": interaction_id,
        "type": str(pending.get("type") or "input"),
        "value": value,
    }
    if option_id:
        interaction_response["option_id"] = option_id
    query = user_text or str(value or option_id or "interaction response")
    protocol_metadata = turn.get("protocol_metadata")
    protocol_metadata = protocol_metadata if isinstance(protocol_metadata, dict) else {}
    context_values = protocol_metadata.get("context")
    context_values = dict(context_values) if isinstance(context_values, dict) else {}
    try:
        next_snapshot = await _admit_run(
            request,
            user=user,
            workspace_id=workspace_id,
            execution_store=execution_store,
            external_run_id=run_id,
            session_id=str(turn.get("session_id") or ""),
            query=query,
            context_values=context_values,
            lang=str(protocol_metadata.get("lang") or "").strip() or None,
            interaction_response=interaction_response,
            parent_run_id=str(turn.get("run_id") or ""),
        )
    except InteractionCheckpointError as exc:
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=409)
    return next_snapshot, execution_store


@acp_router.post("/runs/{run_id}/await")
async def respond_to_await(
    run_id: str,
    request: Request,
    user: AuthUser = Depends(_acp_principal),
    workspace_id: str = Depends(_acp_workspace_id),
):
    payload = await read_optional_json_object(request)
    answered = await _answer_interaction(run_id, request, payload, user, workspace_id)
    if isinstance(answered, JSONResponse):
        return answered
    snapshot, _execution_store = answered
    try:
        terminal = await _wait_for_terminal(request, snapshot, _scope(user, workspace_id))
    except asyncio.TimeoutError:
        return JSONResponse({"detail": "Run interaction timed out"}, status_code=504)
    return _response_from_snapshot(terminal, external_run_id=run_id).model_dump(mode="json")


@acp_router.post("/runs/{run_id}/interact_stream")
async def interact_stream(
    run_id: str,
    request: Request,
    user: AuthUser = Depends(_acp_principal),
    workspace_id: str = Depends(_acp_workspace_id),
):
    payload = await read_optional_json_object(request)
    answered = await _answer_interaction(run_id, request, payload, user, workspace_id)
    if isinstance(answered, JSONResponse):
        return answered
    snapshot, _execution_store = answered
    return await _stream_response(
        request,
        external_run_id=run_id,
        canonical_run_id=snapshot.run_id,
        after_seq=0,
    )


@acp_router.delete("/runs/{run_id}")
async def cancel_run(
    run_id: str,
    request: Request,
    user: AuthUser = Depends(_acp_principal),
    workspace_id: str = Depends(_acp_workspace_id),
):
    _chat_store, execution_store = await _stores(
        request,
        user.username,
        workspace_id,
    )
    response, snapshot, _turn = await _current_response(
        request,
        user=user,
        workspace_id=workspace_id,
        execution_store=execution_store,
        external_run_id=run_id,
    )
    if response is None:
        return JSONResponse({"detail": "Run not found"}, status_code=404)
    if snapshot is None or snapshot.status in TERMINAL_RUN_STATUSES:
        return response.model_dump(mode="json")
    try:
        cancelled = await _runtime(request).manager.cancel_run(
            snapshot.run_id,
            _scope(user, workspace_id),
            reason="acp_cancel",
        )
    except RunNotFoundError:
        return JSONResponse({"detail": "Run not found"}, status_code=404)
    return _response_from_snapshot(cancelled, external_run_id=run_id).model_dump(mode="json")


@acp_router.get("/runs")
async def list_runs(
    request: Request,
    user: AuthUser = Depends(_acp_principal),
    workspace_id: str = Depends(_acp_workspace_id),
):
    _chat_store, execution_store = await _stores(
        request,
        user.username,
        workspace_id,
    )
    turns = await _short_io(
        request,
        execution_store.list_turns_by_protocol,
        protocol=_PROTOCOL,
    )
    runs = []
    scope = _scope(user, workspace_id)
    for turn in turns:
        metadata = turn.get("protocol_metadata")
        if not isinstance(metadata, dict):
            continue
        external_run_id = str(metadata.get("external_run_id") or "")
        snapshot = await _runtime(request).manager.get_run(str(turn.get("run_id") or ""), scope)
        response = (
            _response_from_snapshot(snapshot, external_run_id=external_run_id)
            if snapshot is not None
            else _response_from_turn(turn, external_run_id=external_run_id)
        )
        runs.append(response.model_dump(mode="json"))
    return {"runs": runs}
