"""Native-async MCP chat projection over the canonical run platform."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
import time
import uuid
from typing import Any, Mapping

from agent.core.shared.modes import resolve_agent_mode
from agent.runtime.contracts import (
    RunScope,
    RunSnapshot,
    RunStatus,
    RuntimeInvocationContext,
    TERMINAL_RUN_STATUSES,
)
from agent.runtime.errors import EventReplayUnavailableError
from agent.runtime.event_bus import (
    EventSubscriberLimitExceeded,
    EventSubscriberReservationError,
)
from api.final_output import coerce_final_output_to_text
from api.protocols.mcp.progress import MCPChatProgressProjector
from api.protocols.mcp.public_trace import MCPPublicTraceCollector
from api.protocols.mcp.schemas import error_result
from config.system_config import (
    SystemConfigValidationError,
    resolve_effective_llm_profile_config,
)
from integration.mcp.config import MCPRuntimeConfig
from integration.mcp.ontology_binding import (
    MCPOntologyBindingError,
    bind_chat_arguments,
    validate_runtime_ontology_availability,
)
from integration.mcp.scope import MCPRequestScope
from llm.llm_request_mode import normalize_llm_request_mode
from ontology.ontologies import get_ontology_registry
from utils.identifiers import validate_session_id
from utils.logging import get_logger


logger = get_logger("api.protocols.mcp.workflow_bridge")
_MCP_CLEANUP_TIMEOUT_SECONDS = 5.0
MCPChatProgressSink = Callable[[dict[str, Any]], Awaitable[None]]
_SOLVER_RESULT_FIELDS = frozenset(
    {
        "run_id",
        "job_id",
        "attempt_id",
        "event_seq",
        "status",
        "authoritative",
        "feasibility",
        "optimality",
        "proof_kind",
        "summary",
        "result_artifact_ref",
        "verification_evidence_ref",
        "candidate_results_artifact_ref",
        "candidate_results_artifact_digest",
        "knowledge_revision_id",
        "fact_snapshot_digest",
        "problem_digest",
    }
)


class _MCPOntologySelectionRequired(ValueError):
    pass


class _AuthoritativeSolverResultCollector:
    """Project only the public, authoritative portion of solver events."""

    def __init__(self) -> None:
        self._result: dict[str, Any] | None = None

    def consume(self, event_type: str, payload: Mapping[str, Any] | None) -> None:
        values = payload if isinstance(payload, Mapping) else {}
        is_authoritative_event = event_type == "solver.result.authoritative"
        is_native_tool_projection = (
            event_type == "tool.progress" and values.get("authoritative") is True
        )
        if not (is_authoritative_event or is_native_tool_projection):
            return
        if values.get("authoritative") is not True:
            return
        self._result = {
            str(key): value
            for key, value in values.items()
            if str(key) in _SOLVER_RESULT_FIELDS
        }

    def snapshot(self) -> dict[str, Any] | None:
        return dict(self._result) if self._result is not None else None


async def execute_mcp_chat_workflow_async(
    *,
    runtime: Any,
    execution_store: Any,
    scope: MCPRequestScope,
    payload: Mapping[str, Any] | None,
    concurrency_limit: int,
    queue_wait_seconds: float,
    execution_timeout_seconds: float,
    trace_config: MCPRuntimeConfig | None = None,
    progress_sink: MCPChatProgressSink | None = None,
) -> tuple[dict[str, Any], int]:
    """Admit one MCP request and await its canonical Run on the ASGI loop."""

    manager = getattr(runtime, "manager", None)
    short_io = getattr(runtime, "short_io", None)
    event_hub = getattr(runtime, "event_hub", None)
    if manager is None or short_io is None or event_hub is None:
        return _workflow_error(
            "MCP_CHAT_RUNTIME_UNAVAILABLE",
            "MCP chat runtime is unavailable",
            status_code=503,
            retriable=True,
        )

    data = dict(payload or {})
    include_trace = data.get("include_trace") is True
    trace_collector = _create_trace_collector(
        trace_config,
        include_trace=include_trace,
    )
    progress_projector = _create_progress_projector(
        trace_config,
        enabled=progress_sink is not None,
    )
    solver_collector = _AuthoritativeSolverResultCollector()
    query = str(data.get("request") or data.get("query") or "").strip()
    if not query:
        return _workflow_error(
            "INVALID_TOOL_ARGUMENTS",
            "query must not be empty",
            status_code=400,
            trace=_trace_snapshot(trace_collector),
        )
    raw_session_id = str(data.get("session_id") or "").strip() or (
        "mcp_{}".format(uuid.uuid4().hex)
    )
    try:
        session_id = validate_session_id(raw_session_id)
    except ValueError as exc:
        return _workflow_error(
            "INVALID_SESSION_ID",
            str(exc),
            status_code=400,
            trace=_trace_snapshot(trace_collector),
        )

    context_values = data.get("context")
    if context_values is not None and not isinstance(context_values, dict):
        return _workflow_error(
            "INVALID_TOOL_ARGUMENTS",
            "context must be an object or null",
            status_code=400,
            trace=_trace_snapshot(trace_collector),
        )
    context_values = dict(context_values or {})
    requested_operation_key = str(
        data.get("requested_operation_key")
        or context_values.get("requested_operation_key")
        or ""
    ).strip()
    if requested_operation_key:
        context_values["requested_operation_key"] = requested_operation_key
    raw_arguments = data.get("arguments")
    if raw_arguments is not None and not isinstance(raw_arguments, dict):
        return _workflow_error(
            "INVALID_TOOL_ARGUMENTS",
            "arguments must be an object or null",
            status_code=400,
            trace=_trace_snapshot(trace_collector),
        )
    run_id = "agentic-{}".format(uuid.uuid4().hex)
    await _emit_mcp_progress(
        progress_sink,
        progress_projector.emit_run_started(run_id=run_id)
        if progress_projector is not None
        else None,
    )
    from api.routes import get_orchestrator

    ontology_started_at = time.monotonic()
    try:
        orchestrator = get_orchestrator()
    except RuntimeError:
        orchestrator = None
    try:
        arguments = bind_chat_arguments(
            arguments=raw_arguments,
            token_ontology_id=scope.ontology_id,
        )
        selected_ontology_id = str(
            (arguments or {}).get("selected_ontology_id") or ""
        ).strip()
        if selected_ontology_id:
            context_values = bind_chat_arguments(
                arguments=context_values,
                token_ontology_id=selected_ontology_id,
            ) or {}
        if orchestrator is not None:
            context_values = await short_io.run(
                _resolve_mcp_ontology_context,
                orchestrator=orchestrator,
                scope=scope,
                session_id=session_id,
                context=context_values,
            )
            selected_ontology_id = str(
                context_values.get("selected_ontology_id") or ""
            ).strip()
            if selected_ontology_id:
                merged_arguments = dict(arguments or {})
                for key in (
                    "selected_ontology_id",
                    "selected_ontology_version",
                    "selected_version",
                ):
                    value = context_values.get(key)
                    if value not in (None, ""):
                        merged_arguments[key] = value
                arguments = merged_arguments
        if scope.ontology_id and selected_ontology_id:
            requested_ontology_version = str(
                context_values.get("selected_ontology_version")
                or context_values.get("selected_version")
                or ""
            ).strip()
            selected_ontology_version = str(
                await short_io.run(
                    validate_runtime_ontology_availability,
                    workspace_id=scope.workspace_id,
                    ontology_id=selected_ontology_id,
                    ontology_version=requested_ontology_version or None,
                )
                or ""
            ).strip()
            if selected_ontology_version:
                context_values["selected_ontology_version"] = (
                    selected_ontology_version
                )
                context_values["selected_version"] = selected_ontology_version
                merged_arguments = dict(arguments or {})
                merged_arguments["selected_ontology_version"] = (
                    selected_ontology_version
                )
                merged_arguments["selected_version"] = selected_ontology_version
                arguments = merged_arguments
        await _emit_mcp_progress(
            progress_sink,
            progress_projector.emit_ontology_ready(
                run_id=run_id,
                elapsed_ms=max(
                    0,
                    int((time.monotonic() - ontology_started_at) * 1000),
                ),
            )
            if progress_projector is not None
            else None,
        )
    except _MCPOntologySelectionRequired as exc:
        await _emit_mcp_terminal_progress(
            progress_sink,
            progress_projector,
            run_id=run_id,
            status="failed",
        )
        return _workflow_error(
            "MCP_ONTOLOGY_SELECTION_REQUIRED",
            str(exc),
            status_code=400,
            trace=_trace_snapshot(trace_collector),
        )
    except MCPOntologyBindingError as exc:
        await _emit_mcp_terminal_progress(
            progress_sink,
            progress_projector,
            run_id=run_id,
            status="failed",
        )
        return _workflow_error(
            exc.code,
            exc.detail,
            status_code=exc.status_code,
            trace=_trace_snapshot(trace_collector),
            error_type=exc.error_type,
        )
    model_profile_id = str(
        data.get("model_profile_id")
        or context_values.get("model_profile_id")
        or ""
    ).strip()
    try:
        preflight_snapshot = None
        preflight_builder = getattr(
            orchestrator,
            "build_run_preflight_snapshot",
            None,
        )
        if callable(preflight_builder):
            preflight_snapshot = await short_io.run(
                preflight_builder,
                user_request=query,
                user_context=context_values,
                arguments=arguments,
                session_id=session_id,
                model_profile_id=model_profile_id or None,
                user_id=scope.username,
                workspace_id=scope.workspace_id,
            )
        else:
            deterministic_preflight = getattr(
                orchestrator,
                "is_deterministic_request_potentially_applicable",
                None,
            )
            deterministic_candidate = bool(
                callable(deterministic_preflight)
                and await short_io.run(
                    deterministic_preflight,
                    user_request=query,
                    user_context=context_values,
                    arguments=arguments,
                    session_id=session_id,
                    user_id=scope.username,
                    workspace_id=scope.workspace_id,
                )
            )
            if not deterministic_candidate:
                await short_io.run(
                    resolve_effective_llm_profile_config,
                    model_profile_id or None,
                    allow_default_fallback=not bool(model_profile_id),
                    workspace_id=scope.workspace_id,
                )
    except SystemConfigValidationError as exc:
        await _emit_mcp_terminal_progress(
            progress_sink,
            progress_projector,
            run_id=run_id,
            status="failed",
        )
        return _workflow_error(
            "INVALID_MODEL_PROFILE",
            str(exc),
            status_code=400,
            trace=_trace_snapshot(trace_collector),
        )

    run_scope = RunScope(
        username=scope.username,
        workspace_id=scope.workspace_id,
        scopes=("agent:run", "agent:read", "agent:cancel"),
    )
    reservation_id: str | None = None
    turn_id: int | None = None
    snapshot: RunSnapshot | None = None
    run_started = False
    admission_complete = False
    admission_cancel_reason = "mcp_chat_admission_failed"
    try:
        async with manager.session_guard(run_scope, session_id):
            await manager.assert_session_available(run_scope, session_id)
            reservation_id = await event_hub.reserve_subscriber(run_id)
            turn, created = await _allocate_mcp_turn(
                short_io=short_io,
                execution_store=execution_store,
                session_id=session_id,
                user_request=query,
                run_id=run_id,
            )
            if not created:
                raise RuntimeError("MCP chat admission was not unique")
            turn_id = int(turn["turn_id"])
            context = RuntimeInvocationContext(
                run_id=run_id,
                session_id=session_id,
                turn_id=turn_id,
                workspace_id=scope.workspace_id,
                username=scope.username,
                query=query,
                agent_mode=resolve_agent_mode(context_values),
                llm_request_mode=normalize_llm_request_mode(
                    context_values.get("llm_request_mode")
                ),
                model_profile_id=model_profile_id or None,
                preflight_snapshot=preflight_snapshot,
                requested_operation_key=requested_operation_key or None,
                arguments=arguments,
                include_trace=include_trace,
                supports_user_interaction=False,
                traceparent=scope.traceparent,
                concurrency_limit=max(1, int(concurrency_limit)),
                admission_timeout_seconds=max(0.01, float(queue_wait_seconds)),
                timeout_seconds=max(1.0, float(execution_timeout_seconds)),
            )
            snapshot, manager_created = await manager.create_run(
                context,
                run_scope,
                session_guarded=True,
            )
            if not manager_created:
                raise RuntimeError("MCP chat task registration was not unique")
            run_started = True
        admission_complete = True
    except EventSubscriberLimitExceeded as exc:
        await _emit_mcp_terminal_progress(
            progress_sink,
            progress_projector,
            run_id=run_id,
            status="failed",
        )
        return _workflow_error(
            "RUNTIME_CAPACITY_EXCEEDED",
            str(exc),
            status_code=429,
            retriable=True,
            trace=_trace_snapshot(trace_collector),
        )
    except asyncio.CancelledError:
        admission_cancel_reason = "mcp_chat_request_cancelled"
        raise
    except Exception as exc:
        error_code = str(getattr(exc, "error_code", ""))
        status_code = (
            409
            if error_code in {"SESSION_BUSY", "WORKSPACE_UNAVAILABLE"}
            else 500
        )
        await _emit_mcp_terminal_progress(
            progress_sink,
            progress_projector,
            run_id=run_id,
            status="failed",
        )
        return _workflow_error(
            error_code or "MCP_CHAT_WORKFLOW_FAILED",
            str(exc) or "MCP chat admission failed",
            status_code=status_code,
            retriable=status_code >= 500,
            trace=_trace_snapshot(trace_collector),
        )
    finally:
        if not admission_complete:
            if run_started:
                await _cancel_mcp_run(
                    manager=manager,
                    run_id=run_id,
                    scope=run_scope,
                    reason=admission_cancel_reason,
                )
            elif turn_id is not None:
                await _mark_mcp_turn_start_failed(
                    short_io=short_io,
                    execution_store=execution_store,
                    session_id=session_id,
                    turn_id=turn_id,
                    run_id=run_id,
                )
            if reservation_id is not None:
                await _release_mcp_reservation(
                    event_hub=event_hub,
                    reservation_id=reservation_id,
                    run_id=run_id,
                )

    if snapshot is None or reservation_id is None:
        raise RuntimeError("MCP chat admission completed without runtime state")

    total_timeout = max(0.01, float(queue_wait_seconds)) + max(
        0.01, float(execution_timeout_seconds)
    )
    try:
        snapshot = await _wait_for_terminal(
            runtime,
            snapshot,
            run_scope,
            reservation_id=reservation_id,
            timeout_seconds=total_timeout,
            trace_collector=trace_collector,
            solver_collector=solver_collector,
            progress_projector=progress_projector,
            progress_sink=progress_sink,
        )
    except TimeoutError:
        await _cancel_mcp_run(
            manager=manager,
            run_id=snapshot.run_id,
            scope=run_scope,
            reason="mcp_chat_execution_timeout",
        )
        await _emit_mcp_terminal_progress(
            progress_sink,
            progress_projector,
            run_id=snapshot.run_id,
            status="failed",
        )
        return _workflow_error(
            "MCP_CHAT_EXECUTION_TIMEOUT",
            "MCP chat execution timed out and was cancelled",
            status_code=504,
            retriable=True,
            run_id=snapshot.run_id,
            session_id=session_id,
            trace=_trace_snapshot(trace_collector),
        )
    except asyncio.CancelledError:
        await _cancel_mcp_run(
            manager=manager,
            run_id=snapshot.run_id,
            scope=run_scope,
            reason="mcp_chat_request_cancelled",
        )
        raise
    except Exception as exc:
        current = await manager.get_run(snapshot.run_id, run_scope)
        if current is not None and current.status in TERMINAL_RUN_STATUSES:
            await _emit_mcp_terminal_progress(
                progress_sink,
                progress_projector,
                run_id=current.run_id,
                status=current.status,
            )
            return _project_terminal(
                current,
                scope=scope,
                trace_collector=trace_collector,
                solver_collector=solver_collector,
            )
        await _cancel_mcp_run(
            manager=manager,
            run_id=snapshot.run_id,
            scope=run_scope,
            reason="mcp_chat_workflow_failed",
        )
        logger.warning(
            "MCP chat wait failed run_id=%s error_type=%s",
            snapshot.run_id,
            type(exc).__name__,
        )
        await _emit_mcp_terminal_progress(
            progress_sink,
            progress_projector,
            run_id=snapshot.run_id,
            status="failed",
        )
        return _workflow_error(
            "MCP_CHAT_WORKFLOW_FAILED",
            "MCP chat workflow failed",
            status_code=500,
            retriable=True,
            run_id=snapshot.run_id,
            session_id=session_id,
            trace=_trace_snapshot(trace_collector),
        )
    finally:
        await _release_mcp_reservation(
            event_hub=event_hub,
            reservation_id=reservation_id,
            run_id=snapshot.run_id,
        )
    await _emit_mcp_progress(
        progress_sink,
        progress_projector.emit_terminal(
            run_id=snapshot.run_id,
            status=snapshot.status,
        )
        if progress_projector is not None
        else None,
    )
    return _project_terminal(
        snapshot,
        scope=scope,
        trace_collector=trace_collector,
        solver_collector=solver_collector,
    )


async def _cancel_mcp_run(
    *,
    manager: Any,
    run_id: str,
    scope: RunScope,
    reason: str,
) -> None:
    await _run_mcp_cleanup(
        manager.cancel_run(run_id, scope, reason=reason),
        action="cancel_run",
        run_id=run_id,
    )


async def _release_mcp_reservation(
    *,
    event_hub: Any,
    reservation_id: str,
    run_id: str,
) -> None:
    await _run_mcp_cleanup(
        event_hub.release_subscriber_reservation(reservation_id),
        action="release_reservation",
        run_id=run_id,
    )


async def _mark_mcp_turn_start_failed(
    *,
    short_io: Any,
    execution_store: Any,
    session_id: str,
    turn_id: int,
    run_id: str,
) -> None:
    await _run_mcp_cleanup(
        short_io.run(
            execution_store.mark_turn_task_start_failed,
            session_id,
            turn_id,
        ),
        action="mark_turn_task_start_failed",
        run_id=run_id,
    )


def _log_mcp_cleanup_completion(
    task: asyncio.Task[Any],
    *,
    action: str,
    run_id: str,
) -> None:
    if task.cancelled():
        logger.warning("MCP cleanup was cancelled action=%s run_id=%s", action, run_id)
        return
    try:
        task.result()
    except Exception as exc:
        logger.warning(
            "MCP cleanup failed action=%s run_id=%s error_type=%s",
            action,
            run_id,
            type(exc).__name__,
        )


async def _run_mcp_cleanup(
    operation: Any,
    *,
    action: str,
    run_id: str,
) -> Any:
    cleanup = asyncio.create_task(operation)
    try:
        return await asyncio.wait_for(
            asyncio.shield(cleanup),
            timeout=_MCP_CLEANUP_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        cleanup.add_done_callback(
            lambda task: _log_mcp_cleanup_completion(
                task,
                action=action,
                run_id=run_id,
            )
        )
        raise
    except TimeoutError:
        cleanup.add_done_callback(
            lambda task: _log_mcp_cleanup_completion(
                task,
                action=action,
                run_id=run_id,
            )
        )
        logger.warning("MCP cleanup timed out action=%s run_id=%s", action, run_id)
    except Exception as exc:
        logger.warning(
            "MCP cleanup failed action=%s run_id=%s error_type=%s",
            action,
            run_id,
            type(exc).__name__,
        )
    return None


async def _allocate_mcp_turn(
    *,
    short_io: Any,
    execution_store: Any,
    session_id: str,
    user_request: str,
    run_id: str,
) -> tuple[dict[str, Any], bool]:
    abandoned = asyncio.Event()
    create_turn = partial(
        execution_store.create_turn_for_run,
        session_id=session_id,
        user_request=user_request,
        run_id=run_id,
        protocol_metadata={
            "protocol": "mcp",
            "external_run_id": "mcp_{}".format(uuid.uuid4().hex),
        },
    )

    async def allocate() -> tuple[dict[str, Any], bool]:
        turn, created = await short_io.run(create_turn)
        if abandoned.is_set() and created:
            await _mark_mcp_turn_start_failed(
                short_io=short_io,
                execution_store=execution_store,
                session_id=session_id,
                turn_id=int(turn["turn_id"]),
                run_id=str(turn["run_id"]),
            )
        return turn, created

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
                    await _mark_mcp_turn_start_failed(
                        short_io=short_io,
                        execution_store=execution_store,
                        session_id=session_id,
                        turn_id=int(turn["turn_id"]),
                        run_id=str(turn["run_id"]),
                    )
        else:
            async def settle_allocation() -> None:
                await allocation

            await _run_mcp_cleanup(
                settle_allocation(),
                action="allocate_turn",
                run_id=run_id,
            )
        raise


async def _wait_for_terminal(
    runtime: Any,
    initial: RunSnapshot,
    scope: RunScope,
    *,
    reservation_id: str,
    timeout_seconds: float,
    trace_collector: MCPPublicTraceCollector | None,
    solver_collector: _AuthoritativeSolverResultCollector,
    progress_projector: MCPChatProgressProjector | None,
    progress_sink: MCPChatProgressSink | None,
) -> RunSnapshot:
    manager = runtime.manager
    current = await manager.get_run(initial.run_id, scope)
    if current is not None:
        is_untraced_terminal = (
            current.status in TERMINAL_RUN_STATUSES
            and trace_collector is None
            and current.last_seq == 0
        )
        if is_untraced_terminal:
            return current

    async def consume() -> RunSnapshot:
        cursor = 0
        pending_reservation_id: str | None = reservation_id
        while True:
            stream = runtime.event_hub.stream(
                initial.run_id,
                after_seq=cursor,
                reservation_id=pending_reservation_id,
            )
            pending_reservation_id = None
            try:
                async for event in stream:
                    cursor = max(cursor, int(event.seq))
                    if trace_collector is not None:
                        trace_collector.consume(
                            {"type": event.event_type, "payload": event.payload}
                        )
                    solver_collector.consume(event.event_type, event.payload)
                    if progress_projector is not None:
                        for progress_event in progress_projector.consume(
                            event_type=event.event_type,
                            payload=event.payload,
                            run_id=initial.run_id,
                        ):
                            await _emit_mcp_progress(
                                progress_sink,
                                progress_event,
                            )
            except EventSubscriberReservationError:
                continue
            except EventReplayUnavailableError:
                current_snapshot = await manager.get_run(initial.run_id, scope)
                if current_snapshot is None:
                    raise RuntimeError("canonical run disappeared")
                if current_snapshot.status in TERMINAL_RUN_STATUSES:
                    return current_snapshot
                cursor = current_snapshot.last_seq
                continue
            except EventSubscriberLimitExceeded:
                await asyncio.sleep(0.01)
                continue
            finally:
                await stream.aclose()

            current_snapshot = await manager.get_run(initial.run_id, scope)
            if current_snapshot is None:
                raise RuntimeError("canonical run disappeared")
            if current_snapshot.status in TERMINAL_RUN_STATUSES:
                if cursor >= current_snapshot.last_seq:
                    return current_snapshot
                continue
            await asyncio.sleep(0)

    try:
        return await asyncio.wait_for(consume(), timeout=max(0.01, timeout_seconds))
    except asyncio.TimeoutError as exc:
        raise TimeoutError("canonical run did not reach a terminal state") from exc


def _project_terminal(
    snapshot: RunSnapshot,
    *,
    scope: MCPRequestScope,
    trace_collector: MCPPublicTraceCollector | None,
    solver_collector: _AuthoritativeSolverResultCollector,
) -> tuple[dict[str, Any], int]:
    status = snapshot.status
    content = (
        snapshot.result.content
        if snapshot.result is not None and snapshot.result.content is not None
        else coerce_final_output_to_text(
            snapshot.result.structured_content if snapshot.result is not None else None
        )
    )
    result = {
        "content": content,
        "session_id": snapshot.session_id,
        "run_id": snapshot.run_id,
        "status": "success" if status is RunStatus.COMPLETED else status.value,
        "resumable": False,
    }
    authoritative_solver_result = solver_collector.snapshot()
    if authoritative_solver_result is not None:
        result["authoritative_solver_result"] = authoritative_solver_result
    if snapshot.result is not None and snapshot.result.structured_content is not None:
        result["final_output"] = snapshot.result.structured_content
    if status is RunStatus.COMPLETED:
        response = {
            "ok": True,
            "result": result,
            "scope": {"username": scope.username, "workspace_id": scope.workspace_id},
        }
        _attach_trace(response, trace_collector)
        return response, 200

    error = snapshot.error
    error_code = error.error_code if error is not None else "MCP_CHAT_WORKFLOW_FAILED"
    if status is RunStatus.AWAITING_INPUT:
        error_code = "INTERACTION_NOT_SUPPORTED"
    if error_code == "RUN_ADMISSION_TIMEOUT":
        public_code, status_code = "MCP_CHAT_ADMISSION_TIMEOUT", 504
    elif error_code == "MODEL_TIMEOUT":
        public_code, status_code = "MCP_CHAT_EXECUTION_TIMEOUT", 504
    elif error_code == "INTERACTION_NOT_SUPPORTED":
        public_code, status_code = "INTERACTION_NOT_SUPPORTED", 409
    else:
        public_code, status_code = error_code, 500
    error_details = error.details if error is not None else {}
    return _workflow_error(
        public_code,
        error.message if error is not None else "MCP chat workflow failed",
        status_code=status_code,
        retriable=bool(error.retriable) if error is not None else False,
        run_id=snapshot.run_id,
        session_id=snapshot.session_id,
        result=result,
        trace=_trace_snapshot(trace_collector),
        phase=str(error_details.get("phase") or "").strip(),
    )


def _workflow_error(
    code: str,
    detail: str,
    *,
    status_code: int,
    retriable: bool = False,
    run_id: str = "",
    session_id: str = "",
    result: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
    error_type: str = "workflow_error",
    phase: str = "",
) -> tuple[dict[str, Any], int]:
    body = error_result(
        error_type=error_type,
        error_code=code,
        detail=detail,
        retriable=retriable,
        phase=phase,
    )
    if result is not None:
        body["result"] = result
    elif run_id or session_id:
        body["result"] = {
            "run_id": run_id,
            "session_id": session_id,
            "status": "error",
            "resumable": False,
        }
    if trace is not None:
        body["trace"] = trace
    return body, status_code


def _resolve_mcp_ontology_context(
    *,
    orchestrator: Any,
    scope: MCPRequestScope,
    session_id: str,
    context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    resolved = dict(context or {})
    locked = _locked_session_ontology_context(
        orchestrator,
        username=scope.username,
        workspace_id=scope.workspace_id,
        session_id=session_id,
    )
    if locked:
        requested_id = str(resolved.get("selected_ontology_id") or "").strip()
        locked_id = str(locked.get("selected_ontology_id") or "").strip()
        if requested_id and requested_id != locked_id:
            raise MCPOntologyBindingError(
                "MCP_ONTOLOGY_SCOPE_MISMATCH",
                "MCP chat session ontology does not match the requested ontology.",
                status_code=403,
                error_type="auth_error",
            )
        resolved.update(locked)
        return resolved

    if str(resolved.get("selected_ontology_id") or "").strip():
        return resolved

    registry = get_ontology_registry(workspace_id=scope.workspace_id)
    enabled = sorted(
        {
            str(item or "").strip()
            for item in registry.list_enabled_ontologies()
            if str(item or "").strip()
        }
    )
    if len(enabled) == 1:
        resolved["selected_ontology_id"] = enabled[0]
    elif len(enabled) > 1:
        raise _MCPOntologySelectionRequired(
            "This workspace has multiple enabled ontology packages; "
            "selected_ontology_id is required for a new MCP chat session."
        )
    return resolved


def _locked_session_ontology_context(
    orchestrator: Any,
    *,
    username: str,
    workspace_id: str,
    session_id: str,
) -> dict[str, Any]:
    get_stores = getattr(orchestrator, "_get_stores", None)
    if not callable(get_stores):
        return {}
    try:
        chat_store, _execution_store = get_stores(username, workspace_id)
        metadata_reader = getattr(chat_store, "get_session_metadata", None)
        if callable(metadata_reader):
            metadata = dict(metadata_reader(session_id) or {})
        else:
            # Compatibility for third-party orchestrator store adapters.
            session = chat_store.get_session(session_id)
            metadata = dict(getattr(session, "metadata", {}) or {})
    except (AttributeError, LookupError, OSError, TypeError, ValueError):
        return {}
    selected_id = str(metadata.get("selected_ontology_id") or "").strip()
    if not selected_id:
        return {}
    result = {"selected_ontology_id": selected_id}
    selected_version = str(
        metadata.get("selected_ontology_version")
        or metadata.get("selected_version")
        or ""
    ).strip()
    if selected_version:
        result["selected_ontology_version"] = selected_version
        result["selected_version"] = selected_version
    return result


def _create_progress_projector(
    config: MCPRuntimeConfig | None,
    *,
    enabled: bool,
) -> MCPChatProgressProjector | None:
    if not enabled:
        return None
    return MCPChatProgressProjector(
        max_events=int(getattr(config, "chat_progress_max_events", 100)),
    )


async def _emit_mcp_progress(
    sink: MCPChatProgressSink | None,
    event: dict[str, Any] | None,
) -> None:
    if sink is None or event is None:
        return
    try:
        await sink(event)
    except Exception:
        logger.warning(
            "MCP chat progress delivery failed event=%s",
            str(event.get("event") or "unknown"),
            exc_info=True,
        )


async def _emit_mcp_terminal_progress(
    sink: MCPChatProgressSink | None,
    projector: MCPChatProgressProjector | None,
    *,
    run_id: str,
    status: Any,
) -> None:
    await _emit_mcp_progress(
        sink,
        projector.emit_terminal(run_id=run_id, status=status)
        if projector is not None
        else None,
    )


def _create_trace_collector(
    config: MCPRuntimeConfig | None,
    *,
    include_trace: bool,
) -> MCPPublicTraceCollector | None:
    if config is None or not include_trace:
        return None
    return MCPPublicTraceCollector(
        max_events=config.chat_trace_max_events,
        max_bytes=config.chat_trace_max_bytes,
        max_text_chars=config.chat_trace_max_text_chars,
    )


def _trace_snapshot(
    collector: MCPPublicTraceCollector | None,
) -> dict[str, Any] | None:
    return collector.snapshot() if collector is not None else None


def _attach_trace(
    response: dict[str, Any],
    collector: MCPPublicTraceCollector | None,
) -> None:
    trace = _trace_snapshot(collector)
    if trace is not None:
        response["trace"] = trace
