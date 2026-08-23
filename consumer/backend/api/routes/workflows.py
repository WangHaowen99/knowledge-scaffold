"""Canonical FastAPI endpoints for asynchronous workflow Runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agent.core.shared.modes import resolve_agent_mode
from agent.runtime.contracts import RunScope, RunSnapshot, RuntimeInvocationContext
from agent.runtime.errors import (
    EventReplayUnavailableError,
    RunNotFoundError,
    SessionBusyError,
)
from agent.runtime.event_bus import EventSubscriberLimitExceeded
from agent.runtime.offload import OffloadPoolClosed, OffloadPoolSaturated
from agent.runtime.runtime import RuntimeState
from api.auth.auth import AuthUser, require_authenticated_user
from api.dependencies import require_workspace_id
from api.request_parsing import read_json_object, read_optional_json_object
from config.system_config import (
    SystemConfigValidationError,
    get_public_llm_profiles,
    resolve_effective_llm_profile_config,
)
from integration.observability.trace_context import (
    TraceParentValidationError,
    parse_traceparent,
)
from llm.llm_request_mode import normalize_llm_request_mode
from persistence.execution_store import (
    ExecutionStoreCorruptionError,
    IdempotencyConflictError as DurableIdempotencyConflictError,
    InteractionCheckpointError,
)
from utils.identifiers import validate_session_id


workflows_router = APIRouter(prefix="/agentic")
_SSE_HEARTBEAT_SECONDS = max(
    1.0,
    float(os.getenv("AGENTIC_SSE_HEARTBEAT_SECONDS", "15")),
)


def _get_orchestrator():
    from api.routes import get_orchestrator

    return get_orchestrator()


def _get_runtime():
    from api.routes import get_agent_execution_runtime

    return get_agent_execution_runtime()


def _get_stores():
    from api.routes import get_request_stores

    return get_request_stores()


def _run_scope(username: str, workspace_id: str, action: str) -> RunScope:
    required = {
        "run": "agent:run",
        "read": "agent:read",
        "cancel": "agent:cancel",
    }.get(action)
    return RunScope(
        username=username,
        workspace_id=workspace_id,
        scopes=(required,) if required else (),
    )


def _run_links(run_id: str) -> dict[str, str]:
    base = "/agentic/runs/{}".format(run_id)
    return {
        "self": base,
        "events": "{}/events".format(base),
        "cancel": "{}/cancel".format(base),
    }


def _error(
    error_code: str,
    detail: str,
    status_code: int,
    *,
    retriable: bool = False,
    run_id: str | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {
            "type": "async_run_error",
            "error_code": error_code,
            "detail": detail,
            "retriable": retriable,
        }
    }
    if run_id:
        body["run_id"] = run_id
    return JSONResponse(body, status_code=status_code)


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _encode_event(event: Any) -> str:
    return "id: {}\nevent: {}\ndata: {}\n\n".format(
        event.seq,
        event.event_type,
        json.dumps(
            _json_safe(event.to_dict()),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ),
    )


def _heartbeat(run_id: str, last_seq: int) -> str:
    payload = json.dumps(
        {
            "run_id": run_id,
            "last_seq": last_seq,
            "ts": int(time.time() * 1000),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return ": heartbeat {}\n\n".format(payload)


def _request_fingerprint(
    *,
    username: str,
    workspace_id: str,
    session_id: str,
    data: dict[str, Any],
) -> str:
    canonical = json.dumps(
        {
            "username": username,
            "workspace_id": workspace_id,
            "session_id": session_id,
            "request": str(data.get("request") or "").strip(),
            "interaction_response": data.get("interaction_response"),
            "agent_mode": resolve_agent_mode(
                {"agent_mode": data.get("agent_mode")}
            ),
            "llm_request_mode": normalize_llm_request_mode(
                data.get("llm_request_mode")
            ),
            "model_profile_id": str(
                data.get("model_profile_id") or ""
            ).strip(),
            "arguments": data.get("arguments"),
            "include_trace": data.get("include_trace") is True,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _idempotency_hash(request: Request) -> str:
    raw = str(request.headers.get("Idempotency-Key") or "").strip()
    if not raw:
        raise ValueError("Idempotency-Key is required")
    if len(raw) > 512:
        raise ValueError("Idempotency-Key is too long")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _status_from_turn(turn: dict[str, Any]) -> str:
    return {
        "success": "completed",
        "queued": "queued",
        "running": "running",
        "awaiting_input": "awaiting_input",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(str(turn.get("status") or ""), "failed")


def _durable_payload(
    turn: dict[str, Any],
    *,
    created: bool | None = None,
) -> dict[str, Any]:
    run_id = str(turn.get("run_id") or "")
    payload: dict[str, Any] = {
        "run_id": run_id,
        "session_id": str(turn.get("session_id") or ""),
        "turn_id": int(turn.get("turn_id") or 0),
        "status": _status_from_turn(turn),
        "links": _run_links(run_id),
    }
    if created is not None:
        payload["created"] = created
    final_output = turn.get("final_output")
    if final_output is not None:
        payload["result"] = final_output
    pending = turn.get("pending_interaction")
    if isinstance(pending, dict):
        payload["pending_interaction"] = dict(pending)
    error_code = str(turn.get("error_code") or "").strip()
    if error_code:
        error_detail = "Execution failed"
        if isinstance(final_output, dict):
            error_detail = str(final_output.get("error") or error_detail)
        payload["error"] = {
            "error_code": error_code,
            "message": error_detail,
            "retriable": error_code in {
                "SERVICE_RESTARTED",
                "TASK_START_FAILED",
            },
        }
    return payload


def _snapshot_payload(
    snapshot: RunSnapshot,
    *,
    created: bool | None = None,
) -> dict[str, Any]:
    payload = snapshot.to_view().to_dict()
    payload["links"] = _run_links(snapshot.run_id)
    if created is not None:
        payload["created"] = created
    return payload


async def _existing_admission_payload(
    turn: dict[str, Any],
    *,
    manager: Any,
    scope: RunScope,
) -> dict[str, Any]:
    snapshot = await manager.get_run(str(turn.get("run_id") or ""), scope)
    if snapshot is not None:
        return _snapshot_payload(snapshot, created=False)
    return _durable_payload(turn, created=False)


async def _lookup_exact_retry(
    *,
    runtime: Any,
    execution_store: Any,
    session_id: str,
    idempotency_key_hash: str,
    request_fingerprint: str,
) -> dict[str, Any] | None:
    return await runtime.short_io.run(
        execution_store.lookup_idempotent_turn,
        session_id=session_id,
        idempotency_key_hash=idempotency_key_hash,
        request_fingerprint=request_fingerprint,
    )


async def _lookup_repeated_interaction(
    *,
    runtime: Any,
    execution_store: Any,
    session_id: str,
    interaction_response: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if interaction_response is None:
        return None
    return await runtime.short_io.run(
        execution_store.lookup_consumed_interaction_turn,
        session_id=session_id,
        interaction_id=str(
            interaction_response.get("interaction_id") or ""
        ).strip(),
        interaction_response=interaction_response,
    )


def _interaction_error(exc: InteractionCheckpointError) -> JSONResponse:
    code = str(getattr(exc, "code", "INTERACTION_CONFLICT"))
    return _error(
        code,
        str(exc),
        404 if code == "INTERACTION_NOT_FOUND" else 409,
        run_id=getattr(exc, "existing_run_id", None),
    )


async def _validate_model_profile(
    runtime: Any,
    *,
    model_profile_id: str,
    workspace_id: str,
) -> None:
    await runtime.short_io.run(
        resolve_effective_llm_profile_config,
        model_profile_id or None,
        allow_default_fallback=not bool(model_profile_id),
        workspace_id=workspace_id,
    )


async def _check_rate_limit(runtime: Any) -> bool:
    limiter = getattr(_get_orchestrator(), "run_rate_limiter", None)
    try_acquire = getattr(limiter, "try_acquire", None)
    if not callable(try_acquire):
        return True
    return bool(await runtime.short_io.run(try_acquire))


@workflows_router.get("/model-profiles")
async def list_model_profiles(
    workspace_id: str = Depends(require_workspace_id),
):
    runtime = _get_runtime()
    return await runtime.short_io.run(
        get_public_llm_profiles,
        workspace_id=workspace_id,
    )


@workflows_router.post("/runs")
async def create_run(
    request: Request,
    current_user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    data = await read_json_object(request)
    user_request = str(data.get("request") or "").strip()
    if not user_request:
        return _error("INVALID_ARGUMENT", "request is required", 400)
    raw_session_id = data.get("session_id")
    if raw_session_id in (None, ""):
        return _error("INVALID_ARGUMENT", "session_id is required", 400)
    try:
        session_id = validate_session_id(raw_session_id)
        idempotency_key_hash = _idempotency_hash(request)
        trace_context = parse_traceparent(request.headers.get("traceparent"))
    except TraceParentValidationError as exc:
        return _error("INVALID_TRACEPARENT", str(exc), 400)
    except ValueError as exc:
        return _error("INVALID_ARGUMENT", str(exc), 400)

    arguments = data.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        return _error("INVALID_ARGUMENT", "arguments must be an object or null", 400)
    if "include_trace" in data and not isinstance(data.get("include_trace"), bool):
        return _error("INVALID_ARGUMENT", "include_trace must be a boolean", 400)
    interaction_response = data.get("interaction_response")
    if interaction_response is not None and not isinstance(
        interaction_response,
        dict,
    ):
        return _error(
            "INVALID_ARGUMENT",
            "interaction_response must be an object or null",
            400,
        )
    if isinstance(interaction_response, dict) and str(
        interaction_response.get("action") or ""
    ).strip().lower() == "cancel":
        return _error(
            "INTERACTION_CONFLICT",
            "cancel the interaction through the session interaction endpoint",
            409,
        )

    runtime = _get_runtime()
    manager = runtime.manager
    if manager is None or runtime.short_io is None:
        return _error("RUNTIME_DRAINING", "runtime is unavailable", 503, retriable=True)
    scope = _run_scope(current_user.username, workspace_id, "run")
    _chat_store, execution_store = _get_stores()
    fingerprint = _request_fingerprint(
        username=current_user.username,
        workspace_id=workspace_id,
        session_id=session_id,
        data=data,
    )

    try:
        existing = await _lookup_exact_retry(
            runtime=runtime,
            execution_store=execution_store,
            session_id=session_id,
            idempotency_key_hash=idempotency_key_hash,
            request_fingerprint=fingerprint,
        )
        if existing is not None:
            return JSONResponse(
                await _existing_admission_payload(
                    existing,
                    manager=manager,
                    scope=scope,
                ),
                status_code=200,
            )
        repeated = await _lookup_repeated_interaction(
            runtime=runtime,
            execution_store=execution_store,
            session_id=session_id,
            interaction_response=interaction_response,
        )
        if repeated is not None:
            return JSONResponse(
                await _existing_admission_payload(
                    repeated,
                    manager=manager,
                    scope=scope,
                ),
                status_code=200,
            )
    except DurableIdempotencyConflictError as exc:
        return _error("IDEMPOTENCY_CONFLICT", str(exc), 409)
    except InteractionCheckpointError as exc:
        return _interaction_error(exc)
    except (OffloadPoolClosed, OffloadPoolSaturated, TimeoutError):
        return _error(
            "RUNTIME_CAPACITY_EXCEEDED",
            "runtime capacity is exhausted",
            429,
            retriable=True,
        )
    except ExecutionStoreCorruptionError as exc:
        return _error("EXECUTION_STORE_CORRUPT", str(exc), 503)

    if runtime.state is not RuntimeState.ACCEPTING:
        return _error(
            "RUNTIME_DRAINING",
            "runtime is not accepting new runs",
            503,
            retriable=True,
        )
    try:
        if not await _check_rate_limit(runtime):
            return _error(
                "RUN_RATE_LIMITED",
                "too many run requests",
                429,
                retriable=True,
            )
        model_profile_id = str(data.get("model_profile_id") or "").strip()
        await _validate_model_profile(
            runtime,
            model_profile_id=model_profile_id,
            workspace_id=workspace_id,
        )
    except SystemConfigValidationError as exc:
        return _error("INVALID_ARGUMENT", str(exc), 422)
    except (OffloadPoolClosed, OffloadPoolSaturated, TimeoutError):
        return _error(
            "RUNTIME_CAPACITY_EXCEEDED",
            "runtime capacity is exhausted",
            429,
            retriable=True,
        )

    try:
        async with manager.session_guard(scope, session_id):
            existing = await _lookup_exact_retry(
                runtime=runtime,
                execution_store=execution_store,
                session_id=session_id,
                idempotency_key_hash=idempotency_key_hash,
                request_fingerprint=fingerprint,
            )
            if existing is not None:
                return JSONResponse(
                    await _existing_admission_payload(
                        existing,
                        manager=manager,
                        scope=scope,
                    ),
                    status_code=200,
                )
            repeated = await _lookup_repeated_interaction(
                runtime=runtime,
                execution_store=execution_store,
                session_id=session_id,
                interaction_response=interaction_response,
            )
            if repeated is not None:
                return JSONResponse(
                    await _existing_admission_payload(
                        repeated,
                        manager=manager,
                        scope=scope,
                    ),
                    status_code=200,
                )
            await manager.assert_session_available(scope, session_id)
            if interaction_response is None:
                turn, created = await runtime.short_io.run(
                    execution_store.create_turn_for_run,
                    session_id=session_id,
                    user_request=user_request,
                    idempotency_key_hash=idempotency_key_hash,
                    request_fingerprint=fingerprint,
                )
            else:
                turn, created = await runtime.short_io.run(
                    execution_store.consume_interaction_and_create_turn,
                    session_id=session_id,
                    interaction_id=str(
                        interaction_response.get("interaction_id") or ""
                    ).strip(),
                    user_request=user_request,
                    interaction_response=dict(interaction_response),
                    idempotency_key_hash=idempotency_key_hash,
                    request_fingerprint=fingerprint,
                )
            if not created:
                return JSONResponse(
                    await _existing_admission_payload(
                        turn,
                        manager=manager,
                        scope=scope,
                    ),
                    status_code=200,
                )

            context = RuntimeInvocationContext(
                run_id=str(turn["run_id"]),
                session_id=session_id,
                turn_id=int(turn["turn_id"]),
                workspace_id=workspace_id,
                username=current_user.username,
                query=user_request,
                agent_mode=resolve_agent_mode(
                    {"agent_mode": data.get("agent_mode")}
                ),
                llm_request_mode=normalize_llm_request_mode(
                    data.get("llm_request_mode")
                ),
                model_profile_id=model_profile_id or None,
                arguments=dict(arguments) if arguments is not None else None,
                include_trace=data.get("include_trace") is True,
                supports_user_interaction=True,
                traceparent=(
                    trace_context.value if trace_context is not None else ""
                ),
                interaction_response=(
                    dict(interaction_response)
                    if interaction_response is not None
                    else None
                ),
                concurrency_limit=100 if current_user.role == "admin" else 1,
            )
            try:
                snapshot, manager_created = await manager.create_run(
                    context,
                    scope,
                    session_guarded=True,
                )
            except BaseException:
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
                return _error(
                    "TASK_START_FAILED",
                    "run task was not uniquely registered",
                    500,
                )
            return JSONResponse(
                _snapshot_payload(snapshot, created=True),
                status_code=202,
            )
    except DurableIdempotencyConflictError as exc:
        return _error("IDEMPOTENCY_CONFLICT", str(exc), 409)
    except InteractionCheckpointError as exc:
        return _interaction_error(exc)
    except SessionBusyError as exc:
        return _error(
            "SESSION_BUSY",
            str(exc),
            409,
            run_id=exc.run_id,
        )
    except (OffloadPoolClosed, OffloadPoolSaturated, TimeoutError):
        return _error(
            "RUNTIME_CAPACITY_EXCEEDED",
            "runtime capacity is exhausted",
            429,
            retriable=True,
        )
    except ExecutionStoreCorruptionError as exc:
        return _error("EXECUTION_STORE_CORRUPT", str(exc), 503)


@workflows_router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    current_user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    runtime = _get_runtime()
    manager = runtime.manager
    scope = _run_scope(current_user.username, workspace_id, "read")
    if manager is not None:
        snapshot = await manager.get_run(run_id, scope)
        if snapshot is not None:
            return _snapshot_payload(snapshot)
    _chat_store, execution_store = _get_stores()
    try:
        _session_id, turn = await runtime.short_io.run(
            execution_store.find_turn_by_run_id,
            run_id,
        )
    except ExecutionStoreCorruptionError as exc:
        return _error("EXECUTION_STORE_CORRUPT", str(exc), 503)
    if turn is None:
        return _error("RUN_NOT_FOUND", "Run not found", 404)
    return _durable_payload(turn)


def _event_cursor(request: Request) -> int:
    header = request.headers.get("Last-Event-ID")
    query = request.query_params.get("after_seq")
    if header not in (None, "") and query not in (None, ""):
        if int(header) != int(query):
            raise ValueError("Last-Event-ID and after_seq conflict")
    cursor = int(header or query or 0)
    if cursor < 0:
        raise ValueError("after_seq must be a non-negative integer")
    return cursor


@workflows_router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    current_user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    runtime = _get_runtime()
    manager = runtime.manager
    scope = _run_scope(current_user.username, workspace_id, "read")
    snapshot = (
        await manager.get_run(run_id, scope)
        if manager is not None
        else None
    )
    if snapshot is None:
        _chat_store, execution_store = _get_stores()
        _session_id, turn = await runtime.short_io.run(
            execution_store.find_turn_by_run_id,
            run_id,
        )
        if turn is None:
            return _error("RUN_NOT_FOUND", "Run not found", 404)
        return JSONResponse(
            {
                "error": {
                    "type": "async_run_error",
                    "error_code": "EVENT_REPLAY_UNAVAILABLE",
                    "detail": "Run event replay is unavailable after service restart",
                    "retriable": False,
                },
                "run_id": run_id,
                "status": _status_from_turn(turn),
            },
            status_code=410,
        )
    try:
        cursor = _event_cursor(request)
    except (TypeError, ValueError):
        return _error(
            "INVALID_ARGUMENT",
            "after_seq must be a non-negative integer",
            400,
        )
    try:
        await manager.repository.list_after(run_id, cursor)
        reservation_id = await manager.event_bus.reserve_subscriber(run_id)
    except EventReplayUnavailableError as exc:
        return _error(exc.error_code, str(exc), 410, run_id=run_id)
    except EventSubscriberLimitExceeded as exc:
        return _error(
            "RUNTIME_CAPACITY_EXCEEDED",
            str(exc),
            429,
            retriable=True,
        )

    event_stream = manager.event_bus.stream(
        run_id,
        after_seq=cursor,
        reservation_id=reservation_id,
    )

    async def generate():
        last_seq = cursor
        next_event: asyncio.Task[Any] | None = None
        try:
            yield "retry: 2000\n\n"
            next_event = asyncio.create_task(anext(event_stream))
            while True:
                done, _pending = await asyncio.wait(
                    {next_event},
                    timeout=_SSE_HEARTBEAT_SECONDS,
                )
                if not done:
                    yield _heartbeat(run_id, last_seq)
                    continue
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    return
                last_seq = int(event.seq)
                yield _encode_event(event)
                next_event = asyncio.create_task(anext(event_stream))
        finally:
            if next_event is not None and not next_event.done():
                next_event.cancel()
                await asyncio.gather(next_event, return_exceptions=True)
            await event_stream.aclose()
            await manager.event_bus.release_subscriber_reservation(
                reservation_id
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@workflows_router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    request: Request,
    current_user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    data = await read_optional_json_object(request)
    reason = str(data.get("reason") or "").strip() or "user_requested"
    runtime = _get_runtime()
    manager = runtime.manager
    if manager is None:
        return _error("RUN_NOT_FOUND", "Run not found", 404)
    try:
        snapshot = await manager.cancel_run(
            run_id,
            _run_scope(current_user.username, workspace_id, "cancel"),
            reason=reason,
        )
    except RunNotFoundError:
        return _error("RUN_NOT_FOUND", "Run not found", 404)
    return _snapshot_payload(snapshot)


_FEEDBACK_VOTES = {"up", "down", "none"}


@workflows_router.post("/runs/{run_id}/feedback")
async def submit_run_feedback(
    run_id: str,
    request: Request,
    current_user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    data = await read_optional_json_object(request)
    vote = str(data.get("vote") or data.get("feedback") or "").strip().lower()
    if vote not in _FEEDBACK_VOTES:
        return JSONResponse(
            {"error": "Invalid vote", "allowed": sorted(_FEEDBACK_VOTES)},
            status_code=400,
        )
    runtime = _get_runtime()
    manager = runtime.manager
    scope = _run_scope(current_user.username, workspace_id, "read")
    live = await manager.get_run(run_id, scope) if manager is not None else None
    _chat_store, execution_store = _get_stores()
    session_id, turn = await runtime.short_io.run(
        execution_store.find_turn_by_run_id,
        run_id,
    )
    if turn is None or session_id is None:
        if live is None:
            return _error("RUN_NOT_FOUND", "Run not found", 404)
        return _error(
            "RUN_NOT_DURABLE",
            "Run feedback is unavailable until its Turn is durable",
            409,
        )
    orchestrator = _get_orchestrator()
    records = list(turn.get("evolution_recall_records") or [])
    previous_vote = str(turn.get("evolution_feedback_vote") or "").strip() or None
    voted = await runtime.short_io.run(
        orchestrator.vote_on_memory_records,
        records,
        vote=vote,
        previous_vote=previous_vote,
    )
    if records and vote != previous_vote:
        await runtime.short_io.run(
            execution_store.set_turn_evolution_feedback_vote,
            session_id,
            int(turn["turn_id"]),
            vote,
        )
    persisted = await runtime.short_io.run(
        execution_store.set_turn_feedback,
        session_id,
        int(turn["turn_id"]),
        vote=vote,
    )
    return {
        "success": True,
        "vote": vote,
        "fingerprints_voted": voted,
        "feedback_persisted": persisted,
    }


@workflows_router.post("/runs/stream", include_in_schema=False)
async def retired_run_stream():
    """Explain the permanent replacement for the retired single-call stream API."""

    return JSONResponse(
        {
            "error": {
                "type": "async_run_error",
                "error_code": "ENDPOINT_RETIRED",
                "detail": (
                    "POST /agentic/runs/stream has been retired; create a Run "
                    "with POST /agentic/runs and follow links.events"
                ),
                "retriable": False,
            },
            "replacement": {
                "create": {
                    "method": "POST",
                    "path": "/agentic/runs",
                },
                "events": {
                    "method": "GET",
                    "path": "/agentic/runs/{run_id}/events",
                },
            },
        },
        status_code=410,
    )


@workflows_router.api_route(
    "/runs/{unsupported_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def unsupported_run_control_path(unsupported_path: str):
    """Return a hard 404 for every non-canonical Run control path."""

    del unsupported_path
    return _error("RUN_NOT_FOUND", "Run control endpoint not found", 404)
