"""Application adapter from neutral operation requests to the script tool runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent.tools.execute_script import handle_execute_script
from solver_coordination.operation_runtime import OperationInvocation


@dataclass(frozen=True)
class ControlledScriptOperationRuntime:
    """Execute only the operation and script identity frozen by the platform."""

    execute_script: Callable[..., str] = handle_execute_script

    def execute(self, invocation: OperationInvocation) -> str:
        if not isinstance(invocation, OperationInvocation):
            raise TypeError("invocation must be an OperationInvocation")
        return self.execute_script(
            ontology_id=invocation.ontology_id,
            ontology_version=invocation.ontology_version,
            operation_key=invocation.operation_key,
            arguments=dict(invocation.arguments),
            base_path=invocation.base_path,
            workspace_id=invocation.workspace_id,
            run_id=invocation.run_id,
            caller="deterministic_service",
            timeout_seconds=invocation.timeout_seconds,
            data_access=invocation.data_access,
            expected_script_path=invocation.expected_script_path,
            expected_script_digest=invocation.expected_script_digest,
        )
