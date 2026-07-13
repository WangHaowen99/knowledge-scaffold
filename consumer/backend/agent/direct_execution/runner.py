"""Script execution with SSE event emission for the direct execution branch."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.tools.execute_script import BACKEND_DIR, _build_script_env_overrides, handle_execute_script_path
from utils.logging import get_logger
from ontology.ontology_catalog import get_ontology_manifest
from utils.streaming import build_agent_event
from integration.observability.traces import DirectExecutionRuntimeTrace
from agent.direct_execution.arguments import build_direct_cli_args
from agent.direct_execution.models import DirectExecutionResult, RouteDecision, RuntimeContext

logger = get_logger("direct_execution.runner")


def run_direct_script(
    decision: RouteDecision,
    runtime_context: RuntimeContext,
    *,
    project_root: Optional[str] = None,
    timeout_seconds: int = 300,
    observer: Any = None,
) -> Tuple[List[Dict[str, Any]], DirectExecutionResult]:
    """Execute a matched script operation and return SSE events + result.

    Args:
        decision: Route decision from OperationDirectRouter.
        runtime_context: Runtime context with run/session/turn IDs.
        project_root: Base path for script resolution.
        timeout_seconds: Script execution timeout.
        observer: Optional observability observer for trace/span emission.

    Returns:
        Tuple of (events, result) where *events* is a list of agent-stream.v2
        dicts (tool.started → tool.finished) and *result* is the execution
        outcome.
    """
    run_id = runtime_context.run_id
    session_id = runtime_context.session_id
    turn_id = runtime_context.turn_id
    ontology_id = str(decision.ontology_id or "")
    operation_key = str(decision.operation_key or "")
    script_entry = decision.script_entry
    events: List[Dict[str, Any]] = []

    # Initialize tracer for observability
    tracer = DirectExecutionRuntimeTrace(
        observer=observer,
        run_id=run_id,
        session_id=session_id,
        turn_id=turn_id,
    )

    with tracer.direct_execution_scope(ontology_id=ontology_id, operation_key=operation_key):
        # --- Resolve script path ---
        raw_script_path = str(script_entry.get("_script_path") or "").strip() if script_entry else ""
        if not raw_script_path:
            result = DirectExecutionResult(
                status="error",
                output="Error: No script path found for operation '{}'.".format(operation_key),
                elapsed_ms=0,
                error="script_path_missing",
            )
            events.append(_build_event(run_id, session_id, turn_id, "tool.finished", {
                "tool_call_id": "direct",
                "tool_name": "execute_script",
                "status": "error",
                "output_preview": result.output,
                "output_truncated": False,
                "error": result.output,
                "elapsed_ms": 0,
            }))
            tracer.mark_failed()
            return events, result

        # --- Build CLI args with runtime injection ---
        script_args = list(script_entry.get("arguments") or []) if script_entry else []
        script_parameters = list(script_entry.get("parameters") or []) if script_entry else []
        cli_args, _missing_business, system_error = build_direct_cli_args(
            script_args=script_args,
            script_parameters=script_parameters,
            business_arguments=decision.business_arguments,
            runtime_context=runtime_context,
        )

        if system_error:
            result = DirectExecutionResult(
                status="error",
                output=system_error,
                elapsed_ms=0,
                error="runtime_context_incomplete",
            )
            events.append(_build_event(run_id, session_id, turn_id, "tool.finished", {
                "tool_call_id": "direct",
                "tool_name": "execute_script",
                "status": "error",
                "output_preview": system_error,
                "output_truncated": False,
                "error": system_error,
                "elapsed_ms": 0,
            }))
            tracer.mark_failed()
            return events, result

        # --- Resolve project base path ---
        base_path = Path(project_root).resolve() if project_root else None
        if base_path is None:
            base_path = BACKEND_DIR.parent.resolve()

        # Resolve the script path relative to project root
        if script_entry.get("_resolved_script_path"):
            resolved = Path(str(script_entry["_resolved_script_path"]))
            try:
                script_rel = str(resolved.relative_to(base_path)).replace("\\", "/")
            except ValueError:
                script_rel = raw_script_path
        else:
            script_rel = raw_script_path

        script_name = Path(script_rel).name if script_rel else operation_key

        events.append(_build_event(run_id, session_id, turn_id, "tool.started", {
            "tool_call_id": "direct",
            "tool_name": "execute_script",
            "input": {
                "ontology_id": ontology_id,
                "operation_key": operation_key,
                "arguments": decision.business_arguments,
            },
            "message": "直接执行流程：{}".format(operation_key),
        }))

        # --- Resolve ontology directory for script imports ---
        ontology_pythonpath: List[Path] = []
        if ontology_id:
            onto_manifest, onto_error = get_ontology_manifest(ontology_id)
            if onto_manifest and not onto_error:
                ontology_pythonpath.append(Path(onto_manifest.ontology_dir).resolve())

        # --- Build env overrides for business data connection ---
        env_overrides = _build_script_env_overrides(
            workspace_id=runtime_context.workspace_id,
            ontology_id=ontology_id,
        )

        started_at = time.monotonic()
        with tracer.script_scope(
            script_name=script_name,
            script_path=script_rel,
            ontology_id=ontology_id,
            operation_key=operation_key,
            arguments=decision.business_arguments,
        ) as script_trace:
            try:
                script_output = handle_execute_script_path(
                    script_path=script_rel,
                    args=cli_args,
                    base_path=base_path,
                    timeout_seconds=timeout_seconds,
                    tool_call_id="direct",
                    caller="direct",
                    run_id=run_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    extra_pythonpath_dirs=ontology_pythonpath or None,
                    env_overrides=env_overrides or None,
                )
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                is_error = script_output.startswith("Error:")
                status = "error" if is_error else "completed"
                result = DirectExecutionResult(
                    status="error" if is_error else "success",
                    output=script_output,
                    elapsed_ms=elapsed_ms,
                    error=script_output if is_error else None,
                )
                prefixed_output = "[Direct Execution] {}".format(script_output)
                events.append(_build_event(run_id, session_id, turn_id, "tool.finished", {
                    "tool_call_id": "direct",
                    "tool_name": "execute_script",
                    "status": status,
                    "output_preview": prefixed_output[:4096],
                    "output_truncated": len(prefixed_output) > 4096,
                    "error": script_output if is_error else None,
                    "elapsed_ms": elapsed_ms,
                }))
                script_trace.record_result(
                    output=script_output,
                    is_error=is_error,
                    elapsed_ms=elapsed_ms,
                )
                return events, result

            except Exception as exc:
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                error_msg = "Direct script execution failed: {}".format(str(exc))
                logger.exception(
                    "Direct execution script error: run_id=%s operation=%s",
                    run_id,
                    operation_key,
                )
                result = DirectExecutionResult(
                    status="error",
                    output=error_msg,
                    elapsed_ms=elapsed_ms,
                    error=str(exc),
                )
                events.append(_build_event(run_id, session_id, turn_id, "tool.finished", {
                    "tool_call_id": "direct",
                    "tool_name": "execute_script",
                    "status": "error",
                    "output_preview": error_msg[:4096],
                    "output_truncated": False,
                    "error": error_msg,
                    "elapsed_ms": elapsed_ms,
                }))
                script_trace.record_result(
                    output=error_msg,
                    is_error=True,
                    elapsed_ms=elapsed_ms,
                )
                return events, result


def _build_event(
    run_id: str,
    session_id: str,
    turn_id: int,
    event_type: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    return build_agent_event(
        run_id=run_id,
        session_id=session_id,
        turn_id=turn_id,
        event_type=event_type,
        source={"node": "direct_execution", "component": "service"},
        payload=payload,
    )
