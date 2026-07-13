"""MCP chat adapter using the current AgenticOrchestrator directly."""

from __future__ import annotations

import threading
import uuid
from typing import Any, Mapping

from ..config.system_config import SystemConfigValidationError, resolve_llm_profile_config
from ..orchestrator import AgenticOrchestrator
from ..workspaces import workspace_scope
from .schemas import error_result
from .scope import MCPRequestScope


_ORCHESTRATOR_LOCK = threading.Lock()
_ORCHESTRATOR: AgenticOrchestrator | None = None


def execute_chat_tool(args: Mapping[str, Any] | None, *, scope: MCPRequestScope) -> dict[str, Any]:
    payload = dict(args or {})
    query = str(payload.get("query") or payload.get("request") or "").strip()
    if not query:
        return error_result(
            error_type="tool_input_error",
            error_code="INVALID_TOOL_ARGUMENTS",
            detail="query 不能为空",
            hint="请传入 chat(query)。",
            retriable=True,
        )
    session_id = str(payload.get("session_id") or "").strip() or "mcp-{}".format(uuid.uuid4().hex[:8])
    context = dict(payload.get("context") or {})
    model_profile_id = str(payload.get("model_profile_id") or "").strip()
    if model_profile_id:
        context["model_profile_id"] = model_profile_id
    try:
        requested_model_profile_id = str(context.get("model_profile_id") or "").strip()
        resolve_llm_profile_config(
            requested_model_profile_id or None,
            allow_default_fallback=not bool(requested_model_profile_id),
        )
    except SystemConfigValidationError as exc:
        return error_result(
            error_type="tool_input_error",
            error_code="INVALID_MODEL_PROFILE",
            detail=str(exc),
            retriable=False,
        )

    arguments = payload.get("arguments")
    orchestrator = _get_orchestrator()
    final_output = ""
    run_id = None
    last_status = "success"
    try:
        with workspace_scope(scope.workspace_id):
            for event in orchestrator.run(
                user_request=query,
                user_context=context,
                arguments=arguments if isinstance(arguments, dict) else None,
                session_id=session_id,
                user_id=scope.username,
                workspace_id=scope.workspace_id,
            ):
                event_data = event.get("data", event) if isinstance(event, dict) else event
                if not isinstance(event_data, dict):
                    continue
                if event_data.get("run_id"):
                    run_id = event_data.get("run_id")
                if event_data.get("type") == "turn.finished":
                    turn_payload = event_data.get("payload", {})
                    if isinstance(turn_payload, dict):
                        last_status = str(turn_payload.get("status") or "success")
                        if "final_output" in turn_payload:
                            final_output = turn_payload["final_output"]
    except Exception as exc:
        return error_result(
            error_type="agent_error",
            error_code="MCP_CHAT_AGENT_ERROR",
            detail=str(exc),
            retriable=True,
        )

    return {
        "ok": last_status == "success",
        "result": {
            "content": final_output,
            "session_id": session_id,
            "run_id": run_id,
            "status": last_status,
        },
        "scope": {
            "username": scope.username,
            "workspace_id": scope.workspace_id,
        },
    }


def _get_orchestrator() -> AgenticOrchestrator:
    global _ORCHESTRATOR
    if _ORCHESTRATOR is not None:
        return _ORCHESTRATOR
    with _ORCHESTRATOR_LOCK:
        if _ORCHESTRATOR is not None:
            return _ORCHESTRATOR
        from ..app import build_runtime_services
        from ..services.llm import configure_llm_router

        runtime = build_runtime_services()
        configure_llm_router(runtime["llm_router"])
        _ORCHESTRATOR = AgenticOrchestrator(
            user_store_manager=runtime["user_store_manager"],
            agent_config=runtime["agent_config"],
        )
        return _ORCHESTRATOR
