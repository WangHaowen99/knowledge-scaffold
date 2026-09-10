from __future__ import annotations

import json
from pathlib import Path

import pytest
from solver_coordination.contracts import Candidate, CandidateSet, Option, OrderedPlan, PlanItem
from solver_coordination.errors import VerificationDivergenceError, VerificationRuntimeFailure
from solver_coordination.operation_validation import ResolvedValidationContext
from solver_coordination.validation import CandidateValidationStatus
from solver_coordination.validation_runner import OntologyValidationOperationRunner


def _candidate_set() -> CandidateSet:
    plan = OrderedPlan(
        items=(PlanItem(item_order=1, item_id="item_synthetic_001", quantity="1.0", duration="2.0"),),
        option_domain=(Option.OPTION_0, Option.OPTION_1),
        max_transitions=1,
    )
    return CandidateSet.create(
        source_run_id="solve_run_demo",
        problem_family="binary_ordered_route.v1",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="revision_demo_1",
        plan=plan,
        candidates=(
            Candidate(candidate_id="ALL_0", assignments=(Option.OPTION_0,), transition_count=0),
            Candidate(candidate_id="ALL_1", assignments=(Option.OPTION_1,), transition_count=0),
        ),
    )


def _context(tmp_path: Path) -> ResolvedValidationContext:
    return ResolvedValidationContext(
        workspace_id="workspace_demo",
        owner_id="operator_demo",
        parent_solve_run_id="solve_run_demo",
        validation_run_id="validation_run_demo",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="revision_demo_1",
        problem_family="binary_ordered_route.v1",
        validation_operation="validate_demo",
        validator_revision="ontology_operation_batch.v1",
        rules_revision="rules_demo_1",
        fact_snapshot_ref="fact_snapshot_demo",
        fact_snapshot_digest="sha256:solver-coordination.v1:" + "a" * 64,
        fact_snapshot_path=tmp_path / "fact_snapshot.json",
        validation_script_path=tmp_path / "validate_demo.py",
        ontology_dir=tmp_path,
        fact_snapshot_payload={"synthetic_fact": "value"},
        validation_script_digest="sha256:solver-coordination.script.v1:" + "b" * 64,
    )


class _EvidenceStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def put(self, payload, *, owner_id, source_run_id):
        self.calls.append({"payload": payload, "owner_id": owner_id, "source_run_id": source_run_id})
        return "validation_evidence_demo"


def test_runner_invokes_one_frozen_batch_and_trusts_platform_identity(tmp_path):
    calls: list[dict[str, object]] = []
    evidence = _EvidenceStore()

    def execute_operation(**kwargs):
        calls.append(kwargs)
        return json.dumps(
            {
                "schema_version": "solver_coordination.validation_operation.v1",
                "results": [
                    {"candidate_id": "ALL_0", "status": "feasible"},
                    {"candidate_id": "ALL_1", "status": "infeasible"},
                ],
            }
        )

    results = OntologyValidationOperationRunner(
        execute_operation=execute_operation,
        evidence_store_factory=lambda _workspace_id: evidence,
    ).validate(candidate_set=_candidate_set(), context=_context(tmp_path))

    assert len(calls) == 1
    assert calls[0]["data_access"] == "frozen_snapshot"
    assert calls[0]["request_payload"]["fact_snapshot"]["payload"] == {"synthetic_fact": "value"}
    assert results[0].status is CandidateValidationStatus.FEASIBLE
    assert results[0].candidate_digest == _candidate_set().candidates[0].candidate_digest
    assert results[0].evidence_ref == "validation_evidence_demo"
    assert len(evidence.calls) == 1


def test_runner_rejects_reordered_or_incomplete_output(tmp_path):
    def execute_operation(**_kwargs):
        return json.dumps(
            {
                "schema_version": "solver_coordination.validation_operation.v1",
                "results": [{"candidate_id": "ALL_1", "status": "feasible"}],
            }
        )

    runner = OntologyValidationOperationRunner(
        execute_operation=execute_operation,
        evidence_store_factory=lambda _workspace_id: _EvidenceStore(),
    )

    with pytest.raises(VerificationDivergenceError, match="candidate"):
        runner.validate(candidate_set=_candidate_set(), context=_context(tmp_path))


def test_bound_operation_rechecks_resolved_script_path_and_digest(tmp_path, monkeypatch):
    captured = []

    class _Runtime:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def execute(self, invocation):
            captured.append(invocation)
            return json.dumps(
                {
                    "schema_version": "solver_coordination.validation_operation.v1",
                    "results": [
                        {"candidate_id": "ALL_0", "status": "feasible"},
                        {"candidate_id": "ALL_1", "status": "infeasible"},
                    ],
                }
            )

    context = _context(tmp_path)
    OntologyValidationOperationRunner(
        operation_runtime=_Runtime(),
        evidence_store_factory=lambda _workspace_id: _EvidenceStore(),
    ).validate(candidate_set=_candidate_set(), context=context)

    assert len(captured) == 1
    invocation = captured[0]
    assert invocation.expected_script_path == context.validation_script_path.resolve()
    assert invocation.expected_script_digest == context.validation_script_digest
    assert invocation.data_access == "frozen_snapshot"
