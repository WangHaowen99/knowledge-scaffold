from __future__ import annotations

from pathlib import Path

from integration.solver_operation_runtime import ControlledScriptOperationRuntime
from solver_coordination.operation_runtime import OperationInvocation
from solver_coordination.runtime import build_operation_validation_service


def test_solver_coordination_has_no_reverse_dependency_on_agent_tools():
    root = Path(__file__).resolve().parents[1] / "solver_coordination"
    offenders = []
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "agent.tools" in source:
            offenders.append(path.relative_to(root).as_posix())

    assert offenders == []


def test_controlled_script_runtime_forwards_only_bound_operation_identity(tmp_path):
    calls = []

    def execute_script(**kwargs):
        calls.append(kwargs)
        return '{"schema_version":"synthetic.operation-result.v1"}'

    script_path = tmp_path / "package" / "scripts" / "verify.py"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("print('synthetic')", encoding="utf-8")
    invocation = OperationInvocation(
        ontology_id="package-synthetic",
        ontology_version="revision-synthetic",
        operation_key="verify-synthetic",
        arguments={"request_json": "{}"},
        base_path=tmp_path,
        workspace_id="workspace-synthetic",
        run_id="run-synthetic",
        timeout_seconds=30,
        data_access="frozen_snapshot",
        expected_script_path=script_path,
        expected_script_digest="sha256:" + "a" * 64,
    )

    output = ControlledScriptOperationRuntime(execute_script=execute_script).execute(
        invocation
    )

    assert output == '{"schema_version":"synthetic.operation-result.v1"}'
    assert calls == [
        {
            "ontology_id": "package-synthetic",
            "ontology_version": "revision-synthetic",
            "operation_key": "verify-synthetic",
            "arguments": {"request_json": "{}"},
            "base_path": tmp_path.resolve(),
                "workspace_id": "workspace-synthetic",
                "run_id": "run-synthetic",
                "caller": "deterministic_service",
                "timeout_seconds": 30,
            "data_access": "frozen_snapshot",
            "expected_script_path": script_path.resolve(),
            "expected_script_digest": "sha256:" + "a" * 64,
        }
    ]


def test_validation_service_factory_uses_injected_neutral_runtime():
    runtime = object()

    service = build_operation_validation_service(operation_runtime=runtime)

    assert service.batch_validator.operation_runtime is runtime
