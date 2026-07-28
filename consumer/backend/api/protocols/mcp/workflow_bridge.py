"""Backend-only bridge from MCP chat to the canonical workflow runtime."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Mapping

from agent.core.shared.interaction_policy import set_user_interaction_capability
from config.system_config import SystemConfigValidationError, resolve_effective_llm_profile_config
from persistence.run_queue import QueuedRunAdmission, RunAdmissionQueue
from persistence.run_store import RunStatus
from utils.identifiers import validate_session_id

from api.protocols.mcp.schemas import error_result
from api.protocols.mcp.public_trace import MCPPublicTraceCollector
from integration.mcp.config import MCPRuntimeConfig
from integration.mcp.scope import MCPRequestScope


def execute_mcp_chat_workflow(
    *,
    orchestrator: Any,
    scope: MCPRequestScope,
    payload: Mapping[str, Any] | None,
    concurrency_limit: int,
    queue_wait_seconds: float,
    execution_timeout_seconds: float,
    trace_config: MCPRuntimeConfig | None = None,
) -> tuple[dict[str, Any], int]:
    from api.routes.workflows import AgenticRunHandle, get_run_registry

    data = dict(payload or {})
    trace_collector = _create_trace_collector(
        trace_config,
        include_trace=data.get("include_trace") is True,
    )
    query = str(data.get("request") or data.get("query") or "").strip()
    if not query:
        return _bridge_error(
            "INVALID_TOOL_ARGUMENTS",
            "query 不能为空",
            status_code=400,
            trace=_trace_snapshot(trace_collector),
        )

    raw_session_id = str(data.get("session_id") or "").strip()
    if not raw_session_id:
        raw_session_id = "mcp-{}".format(uuid.uuid4().hex)
    try:
        session_id = validate_session_id(raw_session_id)
    except ValueError as exc:
        return _bridge_error(
            "INVALID_SESSION_ID",
            str(exc),
            status_code=400,
            trace=_trace_snapshot(trace_collector),
        )

    context = set_user_interaction_capability(
        dict(data.get("context") or {}),
        supported=False,
    )
    requested_profile = str(context.get("model_profile_id") or "").strip()
    try:
        resolve_effective_llm_profile_config(
            requested_profile or None,
            allow_default_fallback=not bool(requested_profile),
            workspace_id=scope.workspace_id,
        )
    except SystemConfigValidationError as exc:
        return _bridge_error(
            "INVALID_MODEL_PROFILE",
            str(exc),
            status_code=400,
            trace=_trace_snapshot(trace_collector),
        )

    limiter = getattr(orchestrator, "run_rate_limiter", None)
    try_acquire_rate = getattr(limiter, "try_acquire", None)
    if callable(try_acquire_rate) and not try_acquire_rate():
        return _bridge_error(
            "MCP_CHAT_RATE_LIMITED",
            "请求过于频繁，请稍后重试",
            status_code=429,
            retriable=True,
            trace=_trace_snapshot(trace_collector),
        )

    admission_queue = getattr(orchestrator, "run_admission_queue", None)
    if not isinstance(admission_queue, RunAdmissionQueue):
        admission_queue = RunAdmissionQueue(orchestrator.slot_manager)
        orchestrator.run_admission_queue = admission_queue

    run_id = "mcp-{}".format(uuid.uuid4().hex)
    arguments = data.get("arguments") if isinstance(data.get("arguments"), dict) else None
    metadata = {
        "source": "mcp_chat",
        "session_id": session_id,
        "user_id": scope.username,
        "workspace_id": scope.workspace_id,
        "admission_limit": max(1, int(concurrency_limit)),
    }
    if scope.traceparent:
        trace_id, parent_id, trace_flags = scope.traceparent.split("-")[1:]
        metadata.update({
            "traceparent": scope.traceparent,
            "external_trace_id": trace_id,
            "external_parent_span_id": parent_id,
            "external_trace_flags": trace_flags,
        })
    handle = AgenticRunHandle(
        run_id=run_id,
        user_request=query,
        user_context=context,
        arguments=arguments,
        session_id=session_id,
        user_id=scope.username,
        workspace_id=scope.workspace_id,
        orchestrator=orchestrator,
        slot_manager=admission_queue,
        slot_limit_user_id=scope.username,
        traceparent=scope.traceparent,
        event_observer=trace_collector.consume if trace_collector is not None else None,
    )
    registry = get_run_registry()
    admitted = threading.Event()

    def start_handle() -> None:
        try:
            queued_run = orchestrator.run_store.get_run(run_id)
            if queued_run is not None and queued_run.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
                admission_queue.release(scope.username)
                return
            orchestrator.run_store.create_run(
                run_id=run_id,
                user_request=query,
                metadata=metadata,
            )
            registered = registry.add(handle)
            if registered is not handle:
                admission_queue.release(scope.username)
            admitted.set()
        except Exception:
            orchestrator.run_store.fail_run(run_id, "Failed to start MCP chat workflow")
            admission_queue.release(scope.username)
            admitted.set()
            raise

    if admission_queue.try_acquire(scope.username, max(1, int(concurrency_limit))):
        start_handle()
    else:
        orchestrator.run_store.queue_run(
            run_id=run_id,
            user_request=query,
            metadata={**metadata, "queued_at": int(time.time() * 1000)},
        )
        admission_queue.enqueue(
            QueuedRunAdmission(
                run_id=run_id,
                user_id=scope.username,
                limit=max(1, int(concurrency_limit)),
                on_admit=start_handle,
            )
        )
        if not admitted.wait(timeout=max(0.01, float(queue_wait_seconds))):
            if admission_queue.cancel(run_id):
                orchestrator.run_store.cancel_run(
                    run_id,
                    metadata={"cancel_reason": "mcp_chat_admission_timeout"},
                )
                return _bridge_error(
                    "MCP_CHAT_ADMISSION_TIMEOUT",
                    "MCP chat 等待执行配额超时",
                    status_code=504,
                    retriable=True,
                    run_id=run_id,
                    session_id=session_id,
                    trace=_trace_snapshot(trace_collector),
                )
            admitted.wait(timeout=0.1)

    run = orchestrator.run_store.get_run(run_id)
    if run is not None and run.status == RunStatus.FAILED and not handle.done.is_set():
        return _bridge_error(
            "MCP_CHAT_START_FAILED",
            str(run.error or "MCP chat workflow 启动失败"),
            status_code=500,
            retriable=True,
            run_id=run_id,
            session_id=session_id,
            trace=_trace_snapshot(trace_collector),
        )

    if not handle.done.wait(timeout=max(0.01, float(execution_timeout_seconds))):
        orchestrator.cancel_run(run_id, reason="mcp_chat_execution_timeout")
        return _bridge_error(
            "MCP_CHAT_EXECUTION_TIMEOUT",
            "MCP chat 执行超时，运行已取消",
            status_code=504,
            retriable=True,
            run_id=run_id,
            session_id=session_id,
            trace=_trace_snapshot(trace_collector),
        )

    terminal_payload = handle.terminal_payload()
    status = str(terminal_payload.get("status") or "interrupted").strip()
    final_output = terminal_payload.get("final_output")
    result = {
        "content": final_output,
        "session_id": session_id,
        "run_id": handle.active_run_id or run_id,
        "status": status,
        "resumable": status == "awaiting_input",
    }
    if status == "success":
        response = {
            "ok": True,
            "result": result,
            "scope": {"username": scope.username, "workspace_id": scope.workspace_id},
        }
        _attach_trace(response, trace_collector)
        return response, 200
    if status == "awaiting_input":
        current_run = orchestrator.run_store.get_run(handle.active_run_id or run_id)
        run_result = getattr(current_run, "result", None)
        if isinstance(run_result, dict):
            result["pending_user_interaction"] = run_result.get("pending_user_interaction")
        response = {
            "ok": False,
            "error": {
                "type": "awaiting_input",
                "error_code": "MCP_CHAT_AWAITING_INPUT",
                "detail": "运行正在等待用户交互",
                "retriable": False,
            },
            "result": result,
            "scope": {"username": scope.username, "workspace_id": scope.workspace_id},
        }
        _attach_trace(response, trace_collector)
        return response, 200
    return _bridge_error(
        "MCP_CHAT_WORKFLOW_FAILED",
        str(terminal_payload.get("error") or "MCP chat workflow 未成功完成"),
        status_code=500,
        retriable=status not in {"cancelled"},
        run_id=handle.active_run_id or run_id,
        session_id=session_id,
        result=result,
        trace=_trace_snapshot(trace_collector),
    )


def _bridge_error(
    code: str,
    detail: str,
    *,
    status_code: int,
    retriable: bool = False,
    run_id: str = "",
    session_id: str = "",
    result: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    payload = error_result(
        error_type="workflow_error",
        error_code=code,
        detail=detail,
        retriable=retriable,
    )
    if result is not None:
        payload["result"] = result
    elif run_id or session_id:
        payload["result"] = {
            "run_id": run_id,
            "session_id": session_id,
            "status": "error",
            "resumable": False,
        }
    if trace is not None:
        payload["trace"] = trace
    return payload, status_code


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
