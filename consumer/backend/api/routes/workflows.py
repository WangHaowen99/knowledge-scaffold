"""Canonical FastAPI endpoints for asynchronous workflow Runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agent.core.shared.modes import resolve_agent_mode
from agent.runtime.contracts import (
    RunScope,
    RunSnapshot,
    RunStatus,
    RuntimeInvocationContext,
    TERMINAL_RUN_STATUSES,
    USER_WORKSPACE_RUN_CONCURRENCY_LIMIT,
)
from agent.runtime.errors import (
    EventReplayUnavailableError,
    RunNotFoundError,
    SessionBusyError,
    WorkspaceUnavailableError,
)
from agent.runtime.event_bus import (
    EventSubscriberLimitExceeded,
    EventSubscriberReservationError,
)
from agent.runtime.offload import OffloadPoolClosed, OffloadPoolSaturated
from agent.runtime.runtime import RuntimeState
from api.auth.auth import AuthUser, require_authenticated_user
from api.dependencies import require_workspace_id
from api.final_output import coerce_final_output_to_text
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
from utils.logging import get_logger


workflows_router = APIRouter(prefix="/agentic")
logger = get_logger("api.routes.workflows")
_SSE_HEARTBEAT_SECONDS = max(
    1.0,
    float(os.getenv("AGENTIC_SSE_HEARTBEAT_SECONDS", "15")),
)
_SYNC_CLEANUP_TIMEOUT_SECONDS = 10.0
_SYNC_TRACE_MAX_TOOL_CALLS = 2048
_SYNC_TRACE_MAX_TOOL_FAILURES = 512


def _get_orchestrator():
    from api.routes import get_orchestrator

    return get_orchestrator()


def _get_runtime():
    from api.routes import get_agent_execution_runtime

    return get_agent_execution_runtime()


def _get_stores():
    from api.routes import get_request_stores

    return get_request_stores()


async def _resolve_stores(runtime: Any):
    return await runtime.short_io.run(_get_stores)


async def _reserve_session_admission(
    runtime: Any,
    execution_store: Any,
    session_id: str,
    *,
    timeout_seconds: float = 5.0,
) -> str | None:
    """Acquire the prune-visible lease without blocking the event loop."""

    reserve = getattr(execution_store, "reserve_session_admission", None)
    if not callable(reserve):
        return ""
    deadline = asyncio.get_running_loop().time() + max(0.05, timeout_seconds)
    while True:
        token = await runtime.short_io.run(reserve, session_id)
        if token:
            return str(token)
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(0.01)


async def _release_session_admission(
    runtime: Any,
    execution_store: Any,
    session_id: str,
    reservation_id: str | None,
) -> None:
    if not reservation_id:
        return
    release = getattr(execution_store, "release_session_admission", None)
    if not callable(release):
        return
    try:
        await runtime.short_io.run(release, session_id, reservation_id)
    except Exception:
        logger.warning(
            "Could not release session admission reservation session=%s",
            session_id,
            exc_info=True,
        )


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
            "requested_operation_key": str(
                data.get("requested_operation_key") or ""
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


async def _deterministic_preflight(
    runtime: Any,
    *,
    user_request: str,
    user_context: dict[str, Any],
    arguments: dict[str, Any] | None,
    session_id: str,
    username: str,
    workspace_id: str,
) -> bool:
    preflight = getattr(
        _get_orchestrator(),
        "is_deterministic_request_potentially_applicable",
        None,
    )
    if not callable(preflight):
        return False
    return bool(
        await runtime.short_io.run(
            preflight,
            user_request=user_request,
            user_context=user_context,
            arguments=arguments,
            session_id=session_id,
            user_id=username,
            workspace_id=workspace_id,
        )
    )


async def _build_protocol_preflight_snapshot(
    runtime: Any,
    *,
    user_request: str,
    user_context: dict[str, Any],
    arguments: dict[str, Any] | None,
    session_id: str,
    model_profile_id: str,
    username: str,
    workspace_id: str,
):
    """Use the optimized preflight contract with a legacy fallback."""

    builder = getattr(_get_orchestrator(), "build_run_preflight_snapshot", None)
    if callable(builder):
        return await runtime.short_io.run(
            builder,
            user_request=user_request,
            user_context=user_context,
            arguments=arguments,
            session_id=session_id,
            model_profile_id=model_profile_id or None,
            user_id=username,
            workspace_id=workspace_id,
        )
    deterministic_candidate = await _deterministic_preflight(
        runtime,
        user_request=user_request,
        user_context=user_context,
        arguments=arguments,
        session_id=session_id,
        username=username,
        workspace_id=workspace_id,
    )
    if not deterministic_candidate:
        await _validate_model_profile(
            runtime,
            model_profile_id=model_profile_id,
            workspace_id=workspace_id,
        )
    return None


async def _check_rate_limit(runtime: Any) -> bool:
    limiter = getattr(_get_orchestrator(), "run_rate_limiter", None)
    try_acquire = getattr(limiter, "try_acquire", None)
    if not callable(try_acquire):
        return True
    return bool(await runtime.short_io.run(try_acquire))


def _run_tps_limit(orchestrator: Any) -> int:
    agent_config = getattr(orchestrator, "agent_config", None)
    limits = getattr(agent_config, "limits", None)
    try:
        return max(1, int(getattr(limits, "run_tps", 50)))
    except (TypeError, ValueError):
        return 50


def _sync_error(
    detail: str,
    status_code: int,
    *,
    session_id: str = "",
    run_id: str = "",
    error_code: str = "",
    limit: int | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "status": "error",
        "error": detail,
        "final_answer": "",
    }
    if session_id:
        body["session_id"] = session_id
    if run_id:
        body["run_id"] = run_id
    if error_code:
        body["error_code"] = error_code
    if limit is not None:
        body["limit"] = limit
    return JSONResponse(body, status_code=status_code)


def _sync_status(status: RunStatus) -> str:
    return {
        RunStatus.COMPLETED: "success",
        RunStatus.FAILED: "error",
        RunStatus.CANCELLED: "cancelled",
        RunStatus.AWAITING_INPUT: "awaiting_input",
    }.get(status, status.value)


def _log_sync_cleanup_completion(
    task: asyncio.Task[Any],
    *,
    action: str,
    run_id: str,
) -> None:
    if task.cancelled():
        logger.warning(
            "sync cleanup task cancelled action=%s run_id=%s",
            action,
            run_id,
        )
        return
    error = task.exception()
    if error is not None:
        logger.warning(
            "sync cleanup task failed action=%s run_id=%s error_type=%s",
            action,
            run_id,
            type(error).__name__,
        )


async def _run_sync_cleanup(
    operation: Any,
    *,
    action: str,
    run_id: str,
) -> Any:
    cleanup = asyncio.create_task(operation)
    try:
        return await asyncio.wait_for(
            asyncio.shield(cleanup),
            timeout=_SYNC_CLEANUP_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        cleanup.add_done_callback(
            lambda task: _log_sync_cleanup_completion(
                task,
                action=action,
                run_id=run_id,
            )
        )
        raise
    except TimeoutError:
        cleanup.add_done_callback(
            lambda task: _log_sync_cleanup_completion(
                task,
                action=action,
                run_id=run_id,
            )
        )
        logger.warning(
            "sync cleanup timed out action=%s run_id=%s",
            action,
            run_id,
        )
    except Exception as exc:
        logger.warning(
            "sync cleanup failed action=%s run_id=%s error_type=%s",
            action,
            run_id,
            type(exc).__name__,
        )
    return None


async def _cancel_sync_run(
    *,
    manager: Any,
    run_id: str,
    scope: RunScope,
    reason: str,
) -> RunSnapshot | None:
    return await _run_sync_cleanup(
        manager.cancel_run(run_id, scope, reason=reason),
        action="cancel_run",
        run_id=run_id,
    )


async def _release_sync_reservation(
    *,
    event_hub: Any,
    reservation_id: str,
    run_id: str,
) -> None:
    await _run_sync_cleanup(
        event_hub.release_subscriber_reservation(reservation_id),
        action="release_reservation",
        run_id=run_id,
    )


async def _mark_sync_turn_start_failed(
    *,
    runtime: Any,
    execution_store: Any,
    session_id: str,
    turn_id: int,
    run_id: str,
) -> None:
    await _run_sync_cleanup(
        runtime.short_io.run(
            execution_store.mark_turn_task_start_failed,
            session_id,
            turn_id,
        ),
        action="mark_turn_task_start_failed",
        run_id=run_id,
    )


async def _allocate_sync_turn(
    *,
    runtime: Any,
    execution_store: Any,
    session_id: str,
    user_request: str,
    run_id: str,
    admission_reservation_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    abandoned = asyncio.Event()

    async def allocate() -> tuple[dict[str, Any], bool]:
        try:
            create_options = {
                "session_id": session_id,
                "user_request": user_request,
                "run_id": run_id,
                "protocol_metadata": {"protocol": "agentic_sync"},
            }
            if admission_reservation_id:
                create_options["admission_reservation_id"] = (
                    admission_reservation_id
                )
            turn, created = await runtime.short_io.run(
                execution_store.create_turn_for_run,
                **create_options,
            )
            if abandoned.is_set() and created:
                await _mark_sync_turn_start_failed(
                    runtime=runtime,
                    execution_store=execution_store,
                    session_id=session_id,
                    turn_id=int(turn["turn_id"]),
                    run_id=str(turn["run_id"]),
                )
            return turn, created
        finally:
            await _release_session_admission(
                runtime,
                execution_store,
                session_id,
                admission_reservation_id,
            )

    allocation = asyncio.create_task(allocate())
    try:
        return await asyncio.shield(allocation)
    except asyncio.CancelledError:
        abandoned.set()
        if allocation.done() and not allocation.cancelled():
            error = allocation.exception()
            if error is None:
                turn, created = allocation.result()
                if created:
                    await _mark_sync_turn_start_failed(
                        runtime=runtime,
                        execution_store=execution_store,
                        session_id=session_id,
                        turn_id=int(turn["turn_id"]),
                        run_id=str(turn["run_id"]),
                    )
        else:
            allocation.add_done_callback(
                lambda task: _log_sync_cleanup_completion(
                    task,
                    action="allocate_turn",
                    run_id=run_id,
                )
            )
        raise


def _collect_sync_trace_event(
    event: Any,
    *,
    tool_calls: list[str],
    tool_failures: list[dict[str, str]],
) -> None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.event_type == "tool.started":
        tool_name = str(
            payload.get("name") or payload.get("tool_name") or ""
        ).strip()[:256]
        if tool_name and len(tool_calls) < _SYNC_TRACE_MAX_TOOL_CALLS:
            tool_calls.append(tool_name)
        return
    if event.event_type != "tool.finished" or str(
        payload.get("status") or ""
    ).strip().lower() not in {"error", "failed"}:
        return
    tool_name = str(
        payload.get("name") or payload.get("tool_name") or ""
    ).strip()[:256]
    raw_error = payload.get("error")
    error = (
        str(raw_error.get("message") or "")
        if isinstance(raw_error, dict)
        else str(raw_error or "")
    ).strip()[:1024]
    if (
        tool_name
        and error
        and len(tool_failures) < _SYNC_TRACE_MAX_TOOL_FAILURES
    ):
        tool_failures.append({"tool_name": tool_name, "error": error})


async def _wait_for_sync_terminal(
    *,
    manager: Any,
    event_hub: Any,
    run_id: str,
    scope: RunScope,
    reservation_id: str,
    include_trace: bool,
) -> tuple[RunSnapshot, dict[str, Any] | None]:
    cursor = 0
    pending_reservation_id: str | None = reservation_id
    trace_complete = include_trace
    tool_calls: list[str] = []
    tool_failures: list[dict[str, str]] = []

    while True:
        stream = event_hub.stream(
            run_id,
            after_seq=cursor,
            reservation_id=pending_reservation_id,
        )
        pending_reservation_id = None
        try:
            async for event in stream:
                cursor = max(cursor, int(event.seq))
                if include_trace and trace_complete:
                    _collect_sync_trace_event(
                        event,
                        tool_calls=tool_calls,
                        tool_failures=tool_failures,
                    )
        except EventSubscriberReservationError:
            # A very fast terminal Run releases outstanding reservations before
            # this request activates its stream. Re-open without the consumed
            # reservation; terminal replay does not consume subscriber capacity.
            continue
        except EventReplayUnavailableError as exc:
            snapshot = await manager.get_run(run_id, scope)
            if snapshot is None:
                raise RuntimeError("canonical run disappeared") from exc
            trace_complete = False
            if snapshot.status in TERMINAL_RUN_STATUSES:
                return snapshot, None
            cursor = snapshot.last_seq
            continue
        finally:
            await stream.aclose()

        snapshot = await manager.get_run(run_id, scope)
        if snapshot is None:
            raise RuntimeError("canonical run disappeared")
        if snapshot.status in TERMINAL_RUN_STATUSES:
            if not include_trace or not trace_complete:
                return snapshot, None
            trace: dict[str, Any] = {"tool_calls": tool_calls}
            if tool_failures:
                trace["tool_failures"] = tool_failures
            return snapshot, trace
        # A slow-consumer detach closes the stream without completing the Run.
        # Reattach after the last observed sequence instead of projecting an
        # active snapshot as a terminal response.
        await asyncio.sleep(0)


def _sync_terminal_response(
    snapshot: RunSnapshot,
    *,
    execution_trace: dict[str, Any] | None,
) -> JSONResponse:
    runtime_result = snapshot.result
    final_output = (
        runtime_result.content
        if runtime_result is not None and runtime_result.content is not None
        else (
            runtime_result.structured_content
            if runtime_result is not None
            else None
        )
    )
    body: dict[str, Any] = {
        "status": _sync_status(snapshot.status),
        "final_answer": coerce_final_output_to_text(final_output),
        "session_id": snapshot.session_id,
        "run_id": snapshot.run_id,
    }
    if snapshot.error is not None and snapshot.error.message:
        body["error"] = snapshot.error.message
        body["error_code"] = snapshot.error.error_code
        body["retriable"] = snapshot.error.retriable
        phase = str(snapshot.error.details.get("phase") or "").strip()
        if phase:
            body["phase"] = phase
    if snapshot.pending_interaction is not None:
        body["pending_user_interaction"] = dict(snapshot.pending_interaction)
    if execution_trace is not None:
        body["execution_trace"] = execution_trace
    return JSONResponse(body)


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
    _chat_store, execution_store = await _resolve_stores(runtime)
    fingerprint = _request_fingerprint(
        username=current_user.username,
        workspace_id=workspace_id,
        session_id=session_id,
        data=data,
    )

    try:
        session_admission_id = await _reserve_session_admission(
            runtime,
            execution_store,
            session_id,
        )
    except (OffloadPoolClosed, OffloadPoolSaturated, TimeoutError):
        return _error(
            "RUNTIME_CAPACITY_EXCEEDED",
            "runtime capacity is exhausted",
            429,
            retriable=True,
        )
    if session_admission_id is None:
        return _error(
            "SESSION_BUSY",
            "session admission or retention work is already in progress",
            409,
            retriable=True,
        )

    async def release_session_admission() -> None:
        nonlocal session_admission_id
        reservation = session_admission_id
        session_admission_id = None
        await _release_session_admission(
            runtime,
            execution_store,
            session_id,
            reservation,
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
            await release_session_admission()
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
            await release_session_admission()
            return JSONResponse(
                await _existing_admission_payload(
                    repeated,
                    manager=manager,
                    scope=scope,
                ),
                status_code=200,
            )
    except DurableIdempotencyConflictError as exc:
        await release_session_admission()
        return _error("IDEMPOTENCY_CONFLICT", str(exc), 409)
    except InteractionCheckpointError as exc:
        await release_session_admission()
        return _interaction_error(exc)
    except (OffloadPoolClosed, OffloadPoolSaturated, TimeoutError):
        await release_session_admission()
        return _error(
            "RUNTIME_CAPACITY_EXCEEDED",
            "runtime capacity is exhausted",
            429,
            retriable=True,
        )
    except ExecutionStoreCorruptionError as exc:
        await release_session_admission()
        return _error("EXECUTION_STORE_CORRUPT", str(exc), 503)
    except BaseException:
        await release_session_admission()
        raise

    if runtime.state is not RuntimeState.ACCEPTING:
        await release_session_admission()
        return _error(
            "RUNTIME_DRAINING",
            "runtime is not accepting new runs",
            503,
            retriable=True,
        )
    try:
        if not await _check_rate_limit(runtime):
            await release_session_admission()
            return _error(
                "RUN_RATE_LIMITED",
                "too many run requests",
                429,
                retriable=True,
            )
        model_profile_id = str(data.get("model_profile_id") or "").strip()
        preflight_snapshot = await _build_protocol_preflight_snapshot(
            runtime,
            user_request=user_request,
            user_context={
                **data,
                "interaction_response": interaction_response,
            },
            arguments=(dict(arguments) if arguments is not None else None),
            session_id=session_id,
            model_profile_id=model_profile_id,
            username=current_user.username,
            workspace_id=workspace_id,
        )
    except SystemConfigValidationError as exc:
        await release_session_admission()
        return _error("INVALID_ARGUMENT", str(exc), 422)
    except (OffloadPoolClosed, OffloadPoolSaturated, TimeoutError):
        await release_session_admission()
        return _error(
            "RUNTIME_CAPACITY_EXCEEDED",
            "runtime capacity is exhausted",
            429,
            retriable=True,
        )
    except BaseException:
        await release_session_admission()
        raise

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
                await release_session_admission()
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
                await release_session_admission()
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
                create_options = {
                    "session_id": session_id,
                    "user_request": user_request,
                    "idempotency_key_hash": idempotency_key_hash,
                    "request_fingerprint": fingerprint,
                }
                if session_admission_id:
                    create_options["admission_reservation_id"] = (
                        session_admission_id
                    )
                turn, created = await runtime.short_io.run(
                    execution_store.create_turn_for_run,
                    **create_options,
                )
            else:
                interaction_options = {
                    "session_id": session_id,
                    "interaction_id": str(
                        interaction_response.get("interaction_id") or ""
                    ).strip(),
                    "user_request": user_request,
                    "interaction_response": dict(interaction_response),
                    "idempotency_key_hash": idempotency_key_hash,
                    "request_fingerprint": fingerprint,
                }
                if session_admission_id:
                    interaction_options["admission_reservation_id"] = (
                        session_admission_id
                    )
                turn, created = await runtime.short_io.run(
                    execution_store.consume_interaction_and_create_turn,
                    **interaction_options,
                )
            if not created:
                await release_session_admission()
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
                preflight_snapshot=preflight_snapshot,
                requested_operation_key=(
                    str(data.get("requested_operation_key") or "").strip()
                    or None
                ),
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
                concurrency_limit=USER_WORKSPACE_RUN_CONCURRENCY_LIMIT,
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
                await release_session_admission()
                return _error(
                    "TASK_START_FAILED",
                    "run task was not uniquely registered",
                    500,
                )
            await release_session_admission()
            return JSONResponse(
                _snapshot_payload(snapshot, created=True),
                status_code=202,
            )
    except DurableIdempotencyConflictError as exc:
        await release_session_admission()
        return _error("IDEMPOTENCY_CONFLICT", str(exc), 409)
    except InteractionCheckpointError as exc:
        await release_session_admission()
        return _interaction_error(exc)
    except SessionBusyError as exc:
        await release_session_admission()
        return _error(
            "SESSION_BUSY",
            str(exc),
            409,
            run_id=exc.run_id,
        )
    except WorkspaceUnavailableError as exc:
        return _error(exc.error_code, str(exc), 409)
    except (OffloadPoolClosed, OffloadPoolSaturated, TimeoutError):
        await release_session_admission()
        return _error(
            "RUNTIME_CAPACITY_EXCEEDED",
            "runtime capacity is exhausted",
            429,
            retriable=True,
        )
    except ExecutionStoreCorruptionError as exc:
        await release_session_admission()
        return _error("EXECUTION_STORE_CORRUPT", str(exc), 503)
    except BaseException:
        await release_session_admission()
        raise


@workflows_router.post("/runs/sync")
async def create_sync_run(
    request: Request,
    current_user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    """Run the canonical async workflow and project its terminal legacy JSON."""

    data = await read_json_object(request)
    user_request = str(data.get("request") or "").strip()
    if not user_request:
        return _sync_error("Request is required", 400)

    raw_context = data.get("context")
    if raw_context is not None and not isinstance(raw_context, dict):
        return _sync_error("context must be an object or null", 400)
    context_values = dict(raw_context or {})
    arguments = data.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        return _sync_error("arguments must be an object or null", 400)
    if "include_trace" in data and not isinstance(data.get("include_trace"), bool):
        return _sync_error("include_trace must be a boolean", 400)
    include_trace = data.get("include_trace") is True

    raw_session_id = data.get("session_id")
    generated_session_id = "sync_{}".format(uuid.uuid4().hex[:8])
    try:
        session_id = validate_session_id(
            raw_session_id
            if raw_session_id not in (None, "")
            else generated_session_id
        )
    except ValueError as exc:
        return _sync_error(str(exc), 400)
    try:
        trace_context = parse_traceparent(request.headers.get("traceparent"))
    except TraceParentValidationError as exc:
        return _sync_error(
            str(exc),
            400,
            session_id=session_id,
            error_code="INVALID_TRACEPARENT",
        )

    runtime = _get_runtime()
    manager = getattr(runtime, "manager", None)
    event_hub = getattr(runtime, "event_hub", None) or getattr(
        manager,
        "event_bus",
        None,
    )
    if manager is None or runtime.short_io is None or event_hub is None:
        return _sync_error("runtime is unavailable", 503, session_id=session_id)
    if runtime.state is not RuntimeState.ACCEPTING:
        return _sync_error(
            "runtime is not accepting new runs",
            503,
            session_id=session_id,
        )

    _chat_store, execution_store = await _resolve_stores(runtime)
    model_profile_id = str(
        data.get("model_profile_id")
        or context_values.get("model_profile_id")
        or ""
    ).strip()
    try:
        session_admission_id = await _reserve_session_admission(
            runtime,
            execution_store,
            session_id,
        )
    except (OffloadPoolClosed, OffloadPoolSaturated, TimeoutError):
        return _sync_error(
            "runtime capacity is exhausted",
            429,
            session_id=session_id,
        )
    if session_admission_id is None:
        return _sync_error(
            "session admission or retention work is already in progress",
            409,
            session_id=session_id,
            error_code="SESSION_BUSY",
        )

    async def release_session_admission() -> None:
        nonlocal session_admission_id
        reservation = session_admission_id
        session_admission_id = None
        await _release_session_admission(
            runtime,
            execution_store,
            session_id,
            reservation,
        )

    try:
        if not await _check_rate_limit(runtime):
            await release_session_admission()
            return _sync_error(
                "请求过于频繁，请稍后重试",
                429,
                session_id=session_id,
                limit=_run_tps_limit(_get_orchestrator()),
            )
        preflight_snapshot = await _build_protocol_preflight_snapshot(
            runtime,
            user_request=user_request,
            user_context={
                **context_values,
                "interaction_response": data.get("interaction_response"),
            },
            arguments=(dict(arguments) if arguments is not None else None),
            session_id=session_id,
            model_profile_id=model_profile_id,
            username=current_user.username,
            workspace_id=workspace_id,
        )
    except SystemConfigValidationError as exc:
        await release_session_admission()
        return _sync_error(str(exc), 400, session_id=session_id)
    except (OffloadPoolClosed, OffloadPoolSaturated, TimeoutError):
        await release_session_admission()
        return _sync_error(
            "runtime capacity is exhausted",
            429,
            session_id=session_id,
        )
    except BaseException:
        await release_session_admission()
        raise

    scope = RunScope(
        username=current_user.username,
        workspace_id=workspace_id,
        scopes=("agent:run", "agent:read", "agent:cancel"),
    )
    run_id = "agentic-{}".format(uuid.uuid4().hex)
    turn_id = 0
    turn_allocated = False
    reservation_id: str | None = None
    run_started = False
    admission_complete = False
    admission_cancel_reason = "sync_admission_failed"
    try:
        async with manager.session_guard(scope, session_id):
            await manager.assert_session_available(scope, session_id)
            try:
                reservation_id = await event_hub.reserve_subscriber(run_id)
                allocation_admission_id = session_admission_id
                session_admission_id = None
                turn, created = await _allocate_sync_turn(
                    runtime=runtime,
                    execution_store=execution_store,
                    session_id=session_id,
                    user_request=user_request,
                    run_id=run_id,
                    admission_reservation_id=allocation_admission_id,
                )
                turn_id = int(turn["turn_id"])
                if not created or str(turn["run_id"]) != run_id:
                    raise RuntimeError("sync run admission was not unique")
                turn_allocated = True
                agent_mode = data.get("agent_mode")
                if agent_mode in (None, ""):
                    agent_mode = context_values.get("agent_mode")
                llm_request_mode = data.get("llm_request_mode")
                if llm_request_mode in (None, ""):
                    llm_request_mode = context_values.get("llm_request_mode")
                invocation = RuntimeInvocationContext(
                    run_id=run_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    workspace_id=workspace_id,
                    username=current_user.username,
                    query=user_request,
                    agent_mode=resolve_agent_mode({"agent_mode": agent_mode}),
                    llm_request_mode=normalize_llm_request_mode(llm_request_mode),
                    model_profile_id=model_profile_id or None,
                    preflight_snapshot=preflight_snapshot,
                    requested_operation_key=(
                        str(
                            data.get("requested_operation_key")
                            or context_values.get("requested_operation_key")
                            or ""
                        ).strip()
                        or None
                    ),
                    arguments=dict(arguments) if arguments is not None else None,
                    include_trace=include_trace,
                    supports_user_interaction=False,
                    traceparent=(
                        trace_context.value if trace_context is not None else ""
                    ),
                    concurrency_limit=USER_WORKSPACE_RUN_CONCURRENCY_LIMIT,
                    admission_timeout_seconds=300.0,
                )
                _snapshot, manager_created = await manager.create_run(
                    invocation,
                    scope,
                    session_guarded=True,
                )
                if not manager_created:
                    raise RuntimeError("sync run task was not uniquely registered")
                run_started = True
                await release_session_admission()
            except BaseException:
                try:
                    if turn_allocated:
                        await _mark_sync_turn_start_failed(
                            runtime=runtime,
                            execution_store=execution_store,
                            session_id=session_id,
                            turn_id=turn_id,
                            run_id=run_id,
                        )
                finally:
                    await release_session_admission()
                    if reservation_id is not None:
                        await _release_sync_reservation(
                            event_hub=event_hub,
                            reservation_id=reservation_id,
                            run_id=run_id,
                        )
                        reservation_id = None
                raise
        admission_complete = True
    except EventSubscriberLimitExceeded:
        return _sync_error(
            "runtime capacity is exhausted",
            429,
            session_id=session_id,
            run_id=run_id if turn_allocated else "",
        )
    except SessionBusyError as exc:
        return _sync_error(
            str(exc),
            409,
            session_id=session_id,
            run_id=exc.run_id,
        )
    except WorkspaceUnavailableError as exc:
        return _sync_error(
            str(exc),
            409,
            session_id=session_id,
            run_id=run_id if turn_allocated else "",
        )
    except InteractionCheckpointError as exc:
        return _sync_error(
            str(exc),
            409,
            session_id=session_id,
            run_id=getattr(exc, "existing_run_id", None) or "",
        )
    except (OffloadPoolClosed, OffloadPoolSaturated, TimeoutError):
        return _sync_error(
            "runtime capacity is exhausted",
            429,
            session_id=session_id,
            run_id=run_id if turn_allocated else "",
        )
    except ExecutionStoreCorruptionError:
        return _sync_error(
            "execution store is unavailable",
            503,
            session_id=session_id,
            run_id=run_id if turn_allocated else "",
        )
    except asyncio.CancelledError:
        admission_cancel_reason = "sync_request_cancelled"
        raise
    except Exception as exc:
        logger.error(
            "sync run admission failed run_id=%s error_type=%s",
            run_id,
            type(exc).__name__,
        )
        return _sync_error(
            "运行失败",
            500,
            session_id=session_id,
            run_id=run_id if turn_allocated else "",
        )
    finally:
        if not admission_complete:
            try:
                if run_started:
                    await _cancel_sync_run(
                        manager=manager,
                        run_id=run_id,
                        scope=scope,
                        reason=admission_cancel_reason,
                    )
            finally:
                await release_session_admission()
                if reservation_id is not None:
                    await _release_sync_reservation(
                        event_hub=event_hub,
                        reservation_id=reservation_id,
                        run_id=run_id,
                    )
                    reservation_id = None

    try:
        terminal, execution_trace = await _wait_for_sync_terminal(
            manager=manager,
            event_hub=event_hub,
            run_id=run_id,
            scope=scope,
            reservation_id=reservation_id,
            include_trace=include_trace,
        )
        return _sync_terminal_response(
            terminal,
            execution_trace=execution_trace,
        )
    except asyncio.CancelledError:
        await _cancel_sync_run(
            manager=manager,
            run_id=run_id,
            scope=scope,
            reason="sync_request_cancelled",
        )
        raise
    except Exception as exc:
        current = await manager.get_run(run_id, scope)
        if current is not None and current.status in TERMINAL_RUN_STATUSES:
            return _sync_terminal_response(current, execution_trace=None)
        await _cancel_sync_run(
            manager=manager,
            run_id=run_id,
            scope=scope,
            reason="sync_wait_failed",
        )
        logger.warning(
            "sync run wait failed run_id=%s error_type=%s",
            run_id,
            type(exc).__name__,
        )
        return _sync_error(
            "运行失败",
            500,
            session_id=session_id,
            run_id=run_id,
        )
    finally:
        await release_session_admission()
        if reservation_id is not None:
            await _release_sync_reservation(
                event_hub=event_hub,
                reservation_id=reservation_id,
                run_id=run_id,
            )


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
    _chat_store, execution_store = await _resolve_stores(runtime)
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
        _chat_store, execution_store = await _resolve_stores(runtime)
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
        active_stream = event_stream
        try:
            yield "retry: 2000\n\n"
            next_event = asyncio.create_task(anext(active_stream))
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
                except EventSubscriberReservationError:
                    # A terminal event can release the lazy reservation after
                    # the response is returned but before first iteration.
                    # Re-open from the last delivered cursor; terminal replay
                    # does not consume subscriber capacity.
                    await active_stream.aclose()
                    active_stream = manager.event_bus.stream(
                        run_id,
                        after_seq=last_seq,
                    )
                    next_event = asyncio.create_task(anext(active_stream))
                    continue
                last_seq = int(event.seq)
                yield _encode_event(event)
                next_event = asyncio.create_task(anext(active_stream))
        finally:
            if next_event is not None and not next_event.done():
                next_event.cancel()
                await asyncio.gather(next_event, return_exceptions=True)
            await active_stream.aclose()
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
    _chat_store, execution_store = await _resolve_stores(runtime)
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
