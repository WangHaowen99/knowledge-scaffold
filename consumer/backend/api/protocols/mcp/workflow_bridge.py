"""Native-async MCP chat projection over the canonical run platform."""

from __future__ import annotations

import asyncio
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
from api.final_output import coerce_final_output_to_text
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
from utils.identifiers import validate_session_id
from utils.logging import get_logger


logger = get_logger("api.protocols.mcp.workflow_bridge")


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
    collector = _create_trace_collector(trace_config, include_trace=include_trace)
    query = str(data.get("request") or data.get("query") or "").strip()
    if not query:
        return _workflow_error(
            "INVALID_TOOL_ARGUMENTS",
            "query must not be empty",
            status_code=400,
            trace=_trace_snapshot(collector),
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
            trace=_trace_snapshot(collector),
        )

    context_values = data.get("context")
    if context_values is not None and not isinstance(context_values, dict):
        return _workflow_error(
            "INVALID_TOOL_ARGUMENTS",
            "context must be an object or null",
            status_code=400,
            trace=_trace_snapshot(collector),
        )
    context_values = dict(context_values or {})
    raw_arguments = data.get("arguments")
    if raw_arguments is not None and not isinstance(raw_arguments, dict):
        return _workflow_error(
            "INVALID_TOOL_ARGUMENTS",
            "arguments must be an object or null",
            status_code=400,
            trace=_trace_snapshot(collector),
        )
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
        if scope.ontology_id and selected_ontology_id:
            validate_runtime_ontology_availability(
                workspace_id=scope.workspace_id,
                ontology_id=selected_ontology_id,
            )
    except MCPOntologyBindingError as exc:
        return _workflow_error(
            exc.code,
            exc.detail,
            status_code=exc.status_code,
            trace=_trace_snapshot(collector),
            error_type=exc.error_type,
        )
    model_profile_id = str(
        data.get("model_profile_id")
        or context_values.get("model_profile_id")
        or ""
    ).strip()
    try:
        await short_io.run(
            resolve_effective_llm_profile_config,
            model_profile_id or None,
            allow_default_fallback=not bool(model_profile_id),
            workspace_id=scope.workspace_id,
        )
    except SystemConfigValidationError as exc:
        return _workflow_error(
            "INVALID_MODEL_PROFILE",
            str(exc),
            status_code=400,
            trace=_trace_snapshot(collector),
        )

    run_scope = RunScope(
        username=scope.username,
        workspace_id=scope.workspace_id,
        scopes=("agent:run", "agent:read", "agent:cancel"),
    )
    try:
        async with manager.session_guard(run_scope, session_id):
            await manager.assert_session_available(run_scope, session_id)
            turn, created = await short_io.run(
                execution_store.create_turn_for_run,
                session_id=session_id,
                user_request=query,
                protocol_metadata={
                    "protocol": "mcp",
                    "external_run_id": "mcp_{}".format(uuid.uuid4().hex),
                },
            )
            if not created:
                return _workflow_error(
                    "MCP_CHAT_WORKFLOW_FAILED",
                    "MCP chat admission was not unique",
                    status_code=500,
                    retriable=True,
                    trace=_trace_snapshot(collector),
                )
            context = RuntimeInvocationContext(
                run_id=str(turn["run_id"]),
                session_id=session_id,
                turn_id=int(turn["turn_id"]),
                workspace_id=scope.workspace_id,
                username=scope.username,
                query=query,
                agent_mode=resolve_agent_mode(context_values),
                llm_request_mode=normalize_llm_request_mode(
                    context_values.get("llm_request_mode")
                ),
                model_profile_id=model_profile_id or None,
                arguments=dict(arguments) if arguments is not None else None,
                include_trace=include_trace,
                supports_user_interaction=False,
                traceparent=scope.traceparent,
                concurrency_limit=max(1, int(concurrency_limit)),
                admission_timeout_seconds=max(0.01, float(queue_wait_seconds)),
                timeout_seconds=max(1.0, float(execution_timeout_seconds)),
            )
            try:
                snapshot, manager_created = await manager.create_run(
                    context,
                    run_scope,
                    session_guarded=True,
                )
            except Exception:
                await short_io.run(
                    execution_store.mark_turn_task_start_failed,
                    session_id,
                    int(turn["turn_id"]),
                )
                raise
            if not manager_created:
                await short_io.run(
                    execution_store.mark_turn_task_start_failed,
                    session_id,
                    int(turn["turn_id"]),
                )
                return _workflow_error(
                    "MCP_CHAT_WORKFLOW_FAILED",
                    "MCP chat task registration was not unique",
                    status_code=500,
                    retriable=True,
                    run_id=str(turn["run_id"]),
                    session_id=session_id,
                    trace=_trace_snapshot(collector),
                )
    except Exception as exc:
        error_code = str(getattr(exc, "error_code", ""))
        status_code = 409 if error_code == "SESSION_BUSY" else 500
        return _workflow_error(
            error_code or "MCP_CHAT_WORKFLOW_FAILED",
            str(exc) or "MCP chat admission failed",
            status_code=status_code,
            retriable=status_code >= 500,
            trace=_trace_snapshot(collector),
        )

    total_timeout = max(0.01, float(queue_wait_seconds)) + max(
        0.01, float(execution_timeout_seconds)
    )
    try:
        snapshot = await _wait_for_terminal(
            runtime,
            snapshot,
            run_scope,
            timeout_seconds=total_timeout,
            collector=collector,
        )
    except TimeoutError:
        try:
            await manager.cancel_run(
                snapshot.run_id,
                run_scope,
                reason="mcp_chat_execution_timeout",
            )
        except Exception:
            logger.exception("failed to cancel timed-out MCP chat run")
        return _workflow_error(
            "MCP_CHAT_EXECUTION_TIMEOUT",
            "MCP chat execution timed out and was cancelled",
            status_code=504,
            retriable=True,
            run_id=snapshot.run_id,
            session_id=session_id,
            trace=_trace_snapshot(collector),
        )
    except Exception:
        return _workflow_error(
            "MCP_CHAT_WORKFLOW_FAILED",
            "MCP chat workflow failed",
            status_code=500,
            retriable=True,
            run_id=snapshot.run_id,
            session_id=session_id,
            trace=_trace_snapshot(collector),
        )
    return _project_terminal(snapshot, scope=scope, collector=collector)


async def _wait_for_terminal(
    runtime: Any,
    initial: RunSnapshot,
    scope: RunScope,
    *,
    timeout_seconds: float,
    collector: MCPPublicTraceCollector | None,
) -> RunSnapshot:
    manager = runtime.manager
    current = await manager.get_run(initial.run_id, scope)
    if (
        current is not None
        and current.status in TERMINAL_RUN_STATUSES
        and collector is None
    ):
        return current

    async def consume() -> RunSnapshot:
        async for event in runtime.event_hub.stream(initial.run_id, after_seq=0):
            if collector is not None:
                collector.consume({"type": event.event_type, "payload": event.payload})
        terminal = await manager.get_run(initial.run_id, scope)
        if terminal is None:
            raise RuntimeError("canonical run disappeared")
        return terminal

    try:
        return await asyncio.wait_for(consume(), timeout=max(0.01, timeout_seconds))
    except asyncio.TimeoutError as exc:
        raise TimeoutError("canonical run did not reach a terminal state") from exc


def _project_terminal(
    snapshot: RunSnapshot,
    *,
    scope: MCPRequestScope,
    collector: MCPPublicTraceCollector | None,
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
    if status is RunStatus.COMPLETED:
        response = {
            "ok": True,
            "result": result,
            "scope": {"username": scope.username, "workspace_id": scope.workspace_id},
        }
        _attach_trace(response, collector)
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
        public_code, status_code = "MCP_CHAT_WORKFLOW_FAILED", 500
    return _workflow_error(
        public_code,
        error.message if error is not None else "MCP chat workflow failed",
        status_code=status_code,
        retriable=bool(error.retriable) if error is not None else False,
        run_id=snapshot.run_id,
        session_id=snapshot.session_id,
        result=result,
        trace=_trace_snapshot(collector),
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
) -> tuple[dict[str, Any], int]:
    body = error_result(
        error_type=error_type,
        error_code=code,
        detail=detail,
        retriable=retriable,
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
