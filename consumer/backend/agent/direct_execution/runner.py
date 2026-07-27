"""Script execution with SSE event emission for the direct execution branch."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.tools.execute_script import BACKEND_DIR, _build_script_env_overrides, handle_execute_script_path
from utils.logging import get_logger
from ontology.ontology_catalog import get_ontology_manifest, resolve_ontology_script_path
from utils.streaming import build_agent_event
from integration.observability.traces import DirectExecutionRuntimeTrace
from agent.direct_execution.arguments import build_direct_cli_args
from agent.direct_execution.models import DirectExecutionResult, RouteDecision, RuntimeContext
from agent.tools.events import build_tool_finished_payload

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

    def fail_preflight(message: str, error_code: str) -> Tuple[List[Dict[str, Any]], DirectExecutionResult]:
        result = DirectExecutionResult(
            status="error",
            output=message,
            elapsed_ms=0,
            error=error_code,
        )
        events.append(_build_event(
            run_id,
            session_id,
            turn_id,
            "tool.finished",
            build_tool_finished_payload(
                tool_call_id="direct",
                tool_name="execute_script",
                status="error",
                raw_output=message,
                error=message,
                elapsed_ms=0,
            ),
        ))
        tracer.mark_failed()
        return events, result

    with tracer.direct_execution_scope(ontology_id=ontology_id, operation_key=operation_key):
        # --- Resolve script path ---
        raw_script_path = str(script_entry.get("_script_path") or "").strip() if script_entry else ""
        if not raw_script_path:
            return fail_preflight(
                "Error: No script path found for operation '{}'.".format(operation_key),
                "script_path_missing",
            )

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
            return fail_preflight(system_error, "runtime_context_incomplete")

        # --- Resolve project base path ---
        base_path = Path(project_root).resolve() if project_root else None
        if base_path is None:
            base_path = BACKEND_DIR.parent.resolve()

        manifest_kwargs = {"workspace_id": runtime_context.workspace_id}
        if str(runtime_context.selected_ontology_version or "").strip():
            manifest_kwargs["version"] = runtime_context.selected_ontology_version
        onto_manifest, onto_error = get_ontology_manifest(ontology_id, **manifest_kwargs)
        if onto_error or onto_manifest is None:
            return fail_preflight(
                onto_error or "Error: Ontology '{}' not found.".format(ontology_id),
                "ontology_not_found",
            )
        ontology_dir = Path(onto_manifest.ontology_dir).resolve()
        resolved = resolve_ontology_script_path(ontology_dir, raw_script_path)
        if resolved is None:
            return fail_preflight(
                "Error: Registered script for operation '{}' is outside ontology scripts directory.".format(
                    operation_key
                ),
                "script_path_outside_ontology",
            )
        try:
            script_rel = str(resolved.relative_to(base_path)).replace("\\", "/")
        except ValueError:
            return fail_preflight(
                "Error: Registered script for operation '{}' is outside project root.".format(
                    operation_key
                ),
                "script_path_outside_project",
            )

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

        ontology_pythonpath = [ontology_dir]

        # --- Build env overrides from the ontology-scoped business data source ---
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
                    workspace_id=runtime_context.workspace_id,
                    ontology_id=ontology_id,
                    ontology_dir=ontology_dir,
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
                events.append(_build_event(
                    run_id,
                    session_id,
                    turn_id,
                    "tool.finished",
                    build_tool_finished_payload(
                        tool_call_id="direct",
                        tool_name="execute_script",
                        status=status,
                        raw_output=prefixed_output,
                        error=script_output if is_error else None,
                        elapsed_ms=elapsed_ms,
                    ),
                ))
                script_trace.record_result(
                    output=script_output,
                    is_error=is_error,
                    elapsed_ms=elapsed_ms,
                )
                return events, result

            except Exception:
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                error_msg = "Error: Direct script execution failed."
                error_code = "direct_script_execution_failed"
                logger.error(
                    "Direct execution script error: run_id=%s operation=%s",
                    run_id,
                    operation_key,
                    exc_info=True,
                )
                result = DirectExecutionResult(
                    status="error",
                    output=error_msg,
                    elapsed_ms=elapsed_ms,
                    error=error_code,
                )
                events.append(_build_event(
                    run_id,
                    session_id,
                    turn_id,
                    "tool.finished",
                    build_tool_finished_payload(
                        tool_call_id="direct",
                        tool_name="execute_script",
                        status="error",
                        raw_output=error_msg,
                        error=error_msg,
                        elapsed_ms=elapsed_ms,
                    ),
                ))
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
