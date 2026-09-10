from __future__ import annotations

import json
import hashlib
from types import SimpleNamespace

import yaml

from agent.tools.solver import build_solver_tool_specs
from integration import solver_coordination_runtime as runtime_module
from integration.solver_coordination_runtime import build_solver_coordination_service
from solver_coordination.capability_catalog import (
    load_solver_capability_catalog,
    public_solver_catalog,
)
from solver_coordination.canonical import canonical_json_bytes
from solver_coordination.contracts import SolverResult


_CONNECTOR_CAPABILITIES = {
    "protocol_versions": ["ortools.solver.v1"],
    "connector": {
        "name": "ortools",
        "version": "1.0.0",
        "build": "sha256:" + "a" * 64,
    },
    "execution": {
        "async_jobs": True,
        "cancellation": True,
        "prefer_wait": True,
    },
    "problem_families": [
        {
            "name": "binary_ordered_route",
            "versions": ["1.0"],
            "features": ["lexicographic_objective"],
            "result_assurances": ["feasible_solution"],
            "limits": {
                "max_payload_bytes": 1048576,
                "max_solve_time_ms": 60000,
                "max_workers": 1,
            },
        }
    ],
}


def _capability_fingerprint(payload):
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


class _FixtureOnlyBackend:
    """Connector-neutral backend double for platform orchestration tests."""

    def __init__(self):
        self.problem = None

    def capabilities(self, *, binding=None):
        return _CONNECTOR_CAPABILITIES

    def assert_binding_capabilities(self, binding):
        assert binding["capability_fingerprint"] == _capability_fingerprint(
            _CONNECTOR_CAPABILITIES
        )

    def submit(self, *, binding, problem, idempotency_key, deadline):
        del binding, idempotency_key, deadline
        self.problem = problem
        return {"job_id": "job_SYNTHETIC0001", "status": "accepted"}

    def poll(self, *, binding, job_id):
        del binding
        return {"job_id": job_id, "status": "completed"}

    def cancel(self, *, binding, job_id):
        del binding
        return {"job_id": job_id, "status": "cancelled"}

    def result(self, *, binding, job_id):
        del binding, job_id
        return SolverResult(
            problem_id=self.problem.problem_id,
            status="optimal",
            assignment={"choice": "OPTION_0"},
            objective_values={},
            diagnostics=(),
            engine={
                "name": "synthetic-fake-engine",
                "version": "1.0.0",
            },
        )


class _FailIfCalledFactResolver:
    def resolve(self, **kwargs):
        raise AssertionError("runtime replay must use the frozen fact snapshot")


def _write_package(root):
    for directory in ("operations", "decision-models", "schemas"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    manifest = {
        "kind": "ontology-package",
        "schema_version": "0.2",
        "id": "synthetic-solver-package",
        "name": "Synthetic solver package",
        "description": "Synthetic end-to-end solver fixture",
        "version": "1.0.0",
        "language": "en",
        "enabled": True,
        "capabilities": {
            "decision_models": [
                {
                    "id": "bounded_choice",
                    "entry": "decision-models/bounded-choice.yaml",
                    "input_schema": "schemas/solver-input.schema.json",
                    "result_schema": "schemas/solver-result.schema.json",
                    "engine_requirements": {
                        "problem_family": "synthetic_choice.v1",
                        "capabilities": ["test_only_first_domain"],
                    },
                }
            ]
        },
    }
    (root / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    (root / "decision-models" / "bounded-choice.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "decision-model",
                "id": "bounded_choice",
                "required_facts": [],
                "decision_space": {
                    "choices": ["OPTION_0", "OPTION_1"],
                },
                "parameters": {
                    "compatibility_mode": "test_only_first_domain.v1"
                },
                "constraints": [],
                "objectives": [],
                "limits": {"max_candidates": 2},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / "schemas" / "solver-input.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"request_key": {"type": "string", "minLength": 1}},
                "required": ["request_key"],
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    (root / "schemas" / "solver-result.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "choice": {"type": "string", "enum": ["OPTION_0", "OPTION_1"]}
                },
                "required": ["choice"],
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    (root / "operations" / "solve-choice.md").write_text(
        """---
resource: operation
key: solve_choice
title: Solve synthetic choice
execution:
  mode: solver_workflow
  decision_model: bounded_choice
  validator_revision: bounded-independent.v1
---
Solve one bounded synthetic choice.
""",
        encoding="utf-8",
    )
    return manifest


def test_runtime_solver_path_is_authoritative_and_exactly_once(tmp_path, monkeypatch):
    package_root = tmp_path / "package"
    manifest = _write_package(package_root)
    ontology = SimpleNamespace(
        version="1.0.0",
        ontology_dir=str(package_root),
    )
    monkeypatch.setattr(
        runtime_module,
        "get_ontology_manifest",
        lambda package_id, **kwargs: (
            ontology if package_id == "synthetic-solver-package" else None,
            None,
        ),
    )
    service = build_solver_coordination_service(
        runtime_root=tmp_path / "runtime",
        test_only_compatibility=True,
    )
    service.job_orchestrator.backend = _FixtureOnlyBackend()
    binding_configuration = {
        "endpoint": "https://solver.example.test",
        "connector_type": "ortools",
        "service_api_version": "ortools.solver.v1",
        "service_credential_ref": "secret://solver/synthetic",
        "capability_fingerprint": _capability_fingerprint(
            _CONNECTOR_CAPABILITIES
        ),
        "allowed_families": {"binary_ordered_route": ["1.0"]},
        "neutral_limits": {
            "max_payload_bytes": 1048576,
            "max_solve_time_ms": 60000,
            "max_workers": 1,
        },
        "execution_profile_ref": None,
        "enabled": True,
    }
    service.binding_resolver.configuration = binding_configuration
    records = load_solver_capability_catalog(package_root, manifest)
    (spec,) = build_solver_tool_specs(public_solver_catalog(records))
    events = []
    arguments = dict(
        workspace_id="workspace-synthetic",
        owner_id="owner-synthetic",
        run_id="run-synthetic",
        turn_id=1,
        tool_call_id="tool-call-synthetic",
        card_name=spec.binding.card_name,
        package_id=spec.binding.package_id,
        package_revision=spec.binding.package_revision,
        operation_key=spec.binding.operation_key,
        contract_digest=spec.binding.contract_digest,
        schema_digest=spec.binding.schema_digest,
        arguments={"request_key": "REQUEST_0"},
        progress_callback=lambda kind, payload: events.append((kind, payload)),
    )

    first = service.start_or_resume(**arguments)
    service.fact_resolver = _FailIfCalledFactResolver()
    replay = service.start_or_resume(**arguments)

    assert first == replay
    assert first["status"] == "completed"
    assert first["authoritative"] is True
    assert first["feasibility"] == "feasible"
    assert first["optimality"] == "not_proven"
    assert first["summary"]["assignment"] == {"choice": "OPTION_0"}
    assert first["result_artifact_ref"].startswith("artifact://solver_result/")
    assert first["verification_evidence_ref"].startswith(
        "artifact://verification_evidence/"
    )
    assert [kind for kind, _payload in events][-2:] == [
        "solver.verification.completed",
        "solver.result.authoritative",
    ]
    artifacts = service.store.list_artifacts(
        first["job_id"],
        workspace_id="workspace-synthetic",
        owner_id="owner-synthetic",
    )
    assert {artifact["artifact_type"] for artifact in artifacts} == {
        "normalized_input",
        "fact_snapshot",
        "problem_ir",
        "solver_result",
        "verification_evidence",
    }
    assert len(
        service.store.list_attempts(
            first["job_id"],
            workspace_id="workspace-synthetic",
            owner_id="owner-synthetic",
        )
    ) == 1
