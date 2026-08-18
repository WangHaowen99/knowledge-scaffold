"""Backend-only bridge from MCP chat to the canonical workflow runtime."""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from agent.core.shared.modes import resolve_agent_mode
from api.final_output import coerce_final_output_to_text
from config.system_config import SystemConfigValidationError, resolve_effective_llm_profile_config
from utils.identifiers import validate_session_id
from llm.llm_request_mode import normalize_llm_request_mode

from api.protocols.mcp.schemas import error_result
from api.protocols.mcp.public_trace import MCPPublicTraceCollector
from integration.mcp.config import MCPRuntimeConfig
from integration.mcp.scope import MCPRequestScope


def _execute_mcp_chat_via_async_manager(
    *,
    orchestrator: Any,
    scope: MCPRequestScope,
    data: Mapping[str, Any],
    trace_config: MCPRuntimeConfig | None,
    timeout_seconds: float,
    concurrency_limit: int,
    queue_wait_seconds: float,
) -> tuple[dict[str, Any], int]:
    """Execute MCP chat through the canonical async run platform.

    MCP remains a synchronous unary protocol surface, but it must not start a
    second Agent loop when the process-scoped Runtime is unavailable or stale.
    """
    from agent.runtime.bootstrap import (
        async_manager_matches,
        call_async_run,
        get_async_run_manager,
    )
    from agent.runtime.bridge import status_to_legacy, wait_for_run
    from agent.runtime.contracts import RunScope, RuntimeInvocationContext

    if not async_manager_matches(orchestrator):
        return _bridge_error(
            "MCP_CHAT_RUNTIME_UNAVAILABLE",
            "MCP chat 异步运行服务尚未绑定当前运行实例",
            status_code=503,
            retriable=True,
        )
    try:
        manager = get_async_run_manager()
    except RuntimeError:
        return _bridge_error(
            "MCP_CHAT_RUNTIME_UNAVAILABLE",
            "MCP chat 异步运行服务尚未就绪",
            status_code=503,
            retriable=True,
        )

    include_trace = data.get("include_trace") is True
    collector = _create_trace_collector(trace_config, include_trace=include_trace)
    query = str(data.get("request") or data.get("query") or "").strip()
    if not query:
        return _bridge_error(
            "INVALID_TOOL_ARGUMENTS",
            "query 不能为空",
            status_code=400,
            trace=_trace_snapshot(collector),
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
            trace=_trace_snapshot(collector),
        )

    context_values = dict(data.get("context") or {})
    raw_arguments = data.get("arguments")
    if raw_arguments is not None and not isinstance(raw_arguments, dict):
        return _bridge_error(
            "INVALID_TOOL_ARGUMENTS",
            "arguments 必须是 object 或 null",
            status_code=400,
            trace=_trace_snapshot(collector),
        )
    arguments = raw_arguments
    model_profile_id = str(
        data.get("model_profile_id") or context_values.get("model_profile_id") or ""
    ).strip() or None
    try:
        resolve_effective_llm_profile_config(
            model_profile_id,
            allow_default_fallback=not bool(model_profile_id),
            workspace_id=scope.workspace_id,
        )
    except SystemConfigValidationError as exc:
        return _bridge_error(
            "INVALID_MODEL_PROFILE",
            str(exc),
            status_code=400,
            trace=_trace_snapshot(collector),
        )
    run_id = "mcp-{}".format(uuid.uuid4().hex)
    limiter = getattr(orchestrator, "run_rate_limiter", None)
    try_acquire_rate = getattr(limiter, "try_acquire", None)
    if callable(try_acquire_rate) and not try_acquire_rate():
        return _bridge_error(
            "MCP_CHAT_RATE_LIMITED",
            "请求过于频繁，请稍后重试",
            status_code=429,
            retriable=True,
            trace=_trace_snapshot(collector),
        )
    context = RuntimeInvocationContext(
        run_id=run_id,
        session_id=session_id,
        turn_id=1,
        workspace_id=scope.workspace_id,
        username=scope.username,
        query=query,
        agent_mode=resolve_agent_mode(context_values),
        llm_request_mode=normalize_llm_request_mode(
            context_values.get("llm_request_mode")
        ),
        model_profile_id=model_profile_id,
        arguments=dict(arguments) if arguments is not None else None,
        include_trace=include_trace,
        supports_user_interaction=False,
        traceparent=scope.traceparent,
        concurrency_limit=max(1, int(concurrency_limit)),
        admission_timeout_seconds=max(0.01, float(queue_wait_seconds)),
        timeout_seconds=max(1.0, float(timeout_seconds)),
    )
    try:
        snapshot, _created = call_async_run(
            manager.create_run(
                context,
                RunScope(
                    username=scope.username,
                    workspace_id=scope.workspace_id,
                    scopes=("agent:run", "agent:read", "agent:cancel"),
                ),
            )
        )
        snapshot = wait_for_run(
            manager,
            call_async_run,
            snapshot.run_id,
            RunScope(
                username=scope.username,
                workspace_id=scope.workspace_id,
                scopes=("agent:read", "agent:cancel"),
            ),
            timeout_seconds=(
                max(0.01, float(queue_wait_seconds))
                + max(0.01, float(timeout_seconds))
            ),
            on_event=(
                lambda event: collector.consume({
                    "type": event.event_type,
                    "payload": event.payload,
                })
                if collector is not None
                else None
            ),
        )
    except TimeoutError:
        try:
            call_async_run(
                manager.cancel_run(
                    run_id,
                    RunScope(
                        username=scope.username,
                        workspace_id=scope.workspace_id,
                        scopes=("agent:cancel",),
                    ),
                    reason="mcp_chat_execution_timeout",
                )
            )
        except Exception:
            pass
        return _bridge_error(
            "MCP_CHAT_EXECUTION_TIMEOUT",
            "MCP chat 执行超时，运行已取消",
            status_code=504,
            retriable=True,
            run_id=run_id,
            session_id=session_id,
            trace=_trace_snapshot(collector),
        )
    except Exception:
        return _bridge_error(
            "MCP_CHAT_WORKFLOW_FAILED",
            "MCP chat workflow 未成功完成",
            status_code=500,
            retriable=True,
            run_id=run_id,
            session_id=session_id,
            trace=_trace_snapshot(collector),
        )

    status = snapshot.status.value
    legacy_status = status_to_legacy(status)
    result = {
        "content": (
            snapshot.result.content
            if snapshot.result is not None and snapshot.result.content is not None
            else coerce_final_output_to_text(
                snapshot.result.structured_content
                if snapshot.result is not None
                else None
            )
        ),
        "session_id": snapshot.session_id,
        "run_id": snapshot.run_id,
        "status": legacy_status,
        "resumable": status == "awaiting_input",
    }
    if status == "completed":
        response = {
            "ok": True,
            "result": result,
            "scope": {"username": scope.username, "workspace_id": scope.workspace_id},
        }
        _attach_trace(response, collector)
        return response, 200
    if status == "awaiting_input":
        result["pending_user_interaction"] = snapshot.pending_interaction
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
        _attach_trace(response, collector)
        return response, 200
    error = snapshot.error
    if error is not None and error.error_code == "RUN_ADMISSION_TIMEOUT":
        return _bridge_error(
            "MCP_CHAT_ADMISSION_TIMEOUT",
            "MCP chat 等待执行配额超时",
            status_code=504,
            retriable=True,
            run_id=run_id,
            session_id=session_id,
            result=result,
            trace=_trace_snapshot(collector),
        )
    if error is not None and error.error_code == "MODEL_TIMEOUT":
        return _bridge_error(
            "MCP_CHAT_EXECUTION_TIMEOUT",
            "MCP chat 执行超时，运行已取消",
            status_code=504,
            retriable=True,
            run_id=run_id,
            session_id=session_id,
            result=result,
            trace=_trace_snapshot(collector),
        )
    return _bridge_error(
        "MCP_CHAT_WORKFLOW_FAILED",
        error.message if error is not None else "MCP chat workflow 未成功完成",
        status_code=500,
        retriable=bool(error.retriable) if error is not None else False,
        run_id=run_id,
        session_id=session_id,
        result=result,
        trace=_trace_snapshot(collector),
    )


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
    data = dict(payload or {})
    return _execute_mcp_chat_via_async_manager(
        orchestrator=orchestrator,
        scope=scope,
        data=data,
        trace_config=trace_config,
        timeout_seconds=execution_timeout_seconds,
        concurrency_limit=concurrency_limit,
        queue_wait_seconds=queue_wait_seconds,
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
