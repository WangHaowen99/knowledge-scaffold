from __future__ import annotations

import pytest
from solver_coordination import Option, OrderedPlan, PlanItem, SolverContractError
from solver_coordination.problem_families.binary_ordered_route import (
    enumerate_ordered_routes,
)
from solver_coordination.validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    aggregate_validation_results,
)
from solver_coordination.validation_service import ValidationContext, validate_candidate_set


def _candidate_set(item_count: int = 3):
    plan = OrderedPlan(
        items=tuple(
            PlanItem(
                item_order=index,
                item_id="ITEM_SYNTHETIC_{}".format(index),
                quantity=str(1000 + index),
                duration="1",
            )
            for index in range(1, item_count + 1)
        ),
        option_domain=(Option.OPTION_0, Option.OPTION_1),
        max_transitions=1,
    )
    return enumerate_ordered_routes(
        plan,
        source_run_id="run_synthetic_validation",
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="package-revision-synthetic",
    )


def _result(candidate_id: str, status: CandidateValidationStatus, candidate_digest: str = ""):
    if not candidate_digest:
        candidate_digest = next(
            candidate.candidate_digest
            for candidate in _candidate_set().candidates
            if candidate.candidate_id == candidate_id
        )
    common = {
        "candidate_id": candidate_id,
        "candidate_digest": candidate_digest,
        "status": status,
        "knowledge_package_id": "knowledge_package_demo",
        "knowledge_package_revision": "package-revision-synthetic",
        "fact_snapshot_digest": "sha256:solver-coordination.v1:" + "1" * 64,
        "rules_revision": "rules-revision-synthetic",
    }
    if status is CandidateValidationStatus.VALIDATION_ERROR:
        return CandidateValidationResult(
            **common,
            error_code="SCRIPT_EXECUTION_FAILED",
            error_stage="candidate_validation",
            evidence_ref="evidence_ref_synthetic_validation_error",
        )
    return CandidateValidationResult(
        **common,
        evidence_ref="evidence_ref_synthetic_{}".format(candidate_id.lower()),
    )


def _results(candidate_set, status: CandidateValidationStatus):
    return tuple(
        _result(candidate.candidate_id, status, candidate.candidate_digest) for candidate in candidate_set.candidates
    )


def test_all_feasible_results_publish_the_complete_candidate_set():
    candidate_set = _candidate_set()

    batch = aggregate_validation_results(
        validation_run_id="validation_run_synthetic_001",
        candidate_set=candidate_set,
        results=_results(candidate_set, CandidateValidationStatus.FEASIBLE),
    )

    assert batch.result_count == candidate_set.candidate_count
    assert batch.completed_count == candidate_set.candidate_count
    assert batch.error_count == 0
    assert batch.batch_status == "validation_completed"
    assert batch.selection_status == "published"
    assert batch.feasible_candidate_ids == tuple(candidate.candidate_id for candidate in candidate_set.candidates)


def test_all_infeasible_results_are_successfully_published_with_empty_selection():
    candidate_set = _candidate_set()

    batch = aggregate_validation_results(
        validation_run_id="validation_run_synthetic_002",
        candidate_set=candidate_set,
        results=_results(candidate_set, CandidateValidationStatus.INFEASIBLE),
    )

    assert batch.batch_status == "validation_completed"
    assert batch.selection_status == "published"
    assert batch.feasible_candidate_ids == ()
    assert batch.completed_count == candidate_set.candidate_count


def test_mixed_feasible_and_infeasible_results_publish_only_feasible_candidates():
    candidate_set = _candidate_set()
    results = list(_results(candidate_set, CandidateValidationStatus.FEASIBLE))
    results[1] = _result(
        candidate_set.candidates[1].candidate_id,
        CandidateValidationStatus.INFEASIBLE,
        candidate_set.candidates[1].candidate_digest,
    )

    batch = aggregate_validation_results(
        validation_run_id="validation_run_synthetic_mixed",
        candidate_set=candidate_set,
        results=results,
    )

    assert batch.batch_status == "validation_completed"
    assert batch.selection_status == "published"
    assert batch.feasible_candidate_ids == tuple(
        result.candidate_id for result in results if result.status is CandidateValidationStatus.FEASIBLE
    )


def test_one_validation_error_preserves_all_results_but_makes_batch_not_publishable():
    candidate_set = _candidate_set()
    results = list(_results(candidate_set, CandidateValidationStatus.FEASIBLE))
    failed_id = candidate_set.candidates[2].candidate_id
    results[2] = _result(
        failed_id,
        CandidateValidationStatus.VALIDATION_ERROR,
        candidate_set.candidates[2].candidate_digest,
    )

    batch = aggregate_validation_results(
        validation_run_id="validation_run_synthetic_003",
        candidate_set=candidate_set,
        results=results,
    )

    assert batch.result_count == candidate_set.candidate_count
    assert batch.error_count == 1
    assert batch.completed_count == candidate_set.candidate_count - 1
    assert batch.batch_status == "validation_failed"
    assert batch.selection_status == "not_publishable"
    assert failed_id not in batch.feasible_candidate_ids
    assert batch.to_dict()["all_candidate_results"][2]["status"] == "validation_error"


def test_aggregate_rejects_candidate_digest_substitution():
    candidate_set = _candidate_set()
    results = list(_results(candidate_set, CandidateValidationStatus.FEASIBLE))
    replaced = results[0]
    results[0] = CandidateValidationResult(
        candidate_id=replaced.candidate_id,
        candidate_digest=candidate_set.candidates[1].candidate_digest,
        status=replaced.status,
        knowledge_package_id=replaced.knowledge_package_id,
        knowledge_package_revision=replaced.knowledge_package_revision,
        fact_snapshot_digest=replaced.fact_snapshot_digest,
        rules_revision=replaced.rules_revision,
        evidence_ref=replaced.evidence_ref,
    )

    with pytest.raises(SolverContractError, match="candidate_digest"):
        aggregate_validation_results(
            validation_run_id="validation_run_synthetic_digest_mismatch",
            candidate_set=candidate_set,
            results=results,
        )


def test_validator_digest_mismatch_is_isolated_as_candidate_validation_error():
    candidate_set = _candidate_set()

    class WrongDigestValidator:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def validate(self, candidate_set, candidate, *, context):
            return CandidateValidationResult(
                candidate_id=candidate.candidate_id,
                candidate_digest="sha256:solver-coordination.v1:" + "f" * 64,
                status=CandidateValidationStatus.FEASIBLE,
                knowledge_package_id=candidate_set.knowledge_package_id,
                knowledge_package_revision=candidate_set.knowledge_package_revision,
                fact_snapshot_digest=context.fact_snapshot_digest,
                rules_revision=context.rules_revision,
                evidence_ref="evidence_synthetic_digest_mismatch",
            )

    result = validate_candidate_set(
        validation_run_id="validation_run_synthetic_digest_mismatch",
        candidate_set=candidate_set,
        validator=WrongDigestValidator(),
        context=ValidationContext(
            fact_snapshot_digest="sha256:solver-coordination.v1:" + "a" * 64,
            rules_revision="rules_revision_synthetic",
            validator_revision="validator_revision_synthetic",
        ),
    )

    assert result.error_count == candidate_set.candidate_count
    assert result.batch_status == "validation_failed"
    assert all(item.error_code == "verification_runtime_failed" for item in result.all_candidate_results)


def test_validation_result_rejects_unversioned_identity_digests():
    candidate_set = _candidate_set()
    candidate = candidate_set.candidates[0]
    with pytest.raises(SolverContractError, match="candidate_digest"):
        CandidateValidationResult(
            candidate_id=candidate.candidate_id,
            candidate_digest="foo",
            status=CandidateValidationStatus.FEASIBLE,
            knowledge_package_id=candidate_set.knowledge_package_id,
            knowledge_package_revision=candidate_set.knowledge_package_revision,
            fact_snapshot_digest="sha256:solver-coordination.v1:" + "a" * 64,
            rules_revision="rules_revision_synthetic",
            evidence_ref="evidence_synthetic_001",
        )

    with pytest.raises(SolverContractError, match="fact_snapshot_digest"):
        CandidateValidationResult(
            candidate_id=candidate.candidate_id,
            candidate_digest=candidate.candidate_digest,
            status=CandidateValidationStatus.FEASIBLE,
            knowledge_package_id=candidate_set.knowledge_package_id,
            knowledge_package_revision=candidate_set.knowledge_package_revision,
            fact_snapshot_digest="bar",
            rules_revision="rules_revision_synthetic",
            evidence_ref="evidence_synthetic_001",
        )


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "reordered"])
def test_aggregate_rejects_incomplete_or_misaligned_result_sets(mutation):
    candidate_set = _candidate_set()
    results = list(_results(candidate_set, CandidateValidationStatus.FEASIBLE))
    if mutation == "missing":
        results.pop()
    elif mutation == "duplicate":
        results[-1] = results[0]
    elif mutation == "unknown":
        results[-1] = _result(
            "UNKNOWN",
            CandidateValidationStatus.FEASIBLE,
            "sha256:solver-coordination.v1:" + "f" * 64,
        )
    else:
        results[0], results[1] = results[1], results[0]

    with pytest.raises(SolverContractError, match="exact candidate order"):
        aggregate_validation_results(
            validation_run_id="validation_run_synthetic_004",
            candidate_set=candidate_set,
            results=results,
        )


def test_validation_error_requires_diagnostic_fields():
    with pytest.raises(SolverContractError, match="error_code"):
        CandidateValidationResult(
            candidate_id="ALL_0",
            candidate_digest="sha256:solver-coordination.v1:" + "a" * 64,
            status=CandidateValidationStatus.VALIDATION_ERROR,
            knowledge_package_id="knowledge_package_demo",
            knowledge_package_revision="package-revision-synthetic",
            fact_snapshot_digest="sha256:solver-coordination.v1:" + "1" * 64,
            rules_revision="rules-revision-synthetic",
            evidence_ref="evidence_ref_synthetic_validation_error",
        )


def test_completed_result_rejects_error_fields():
    with pytest.raises(SolverContractError, match="only allowed"):
        CandidateValidationResult(
            candidate_id="ALL_0",
            candidate_digest="sha256:solver-coordination.v1:" + "a" * 64,
            status=CandidateValidationStatus.FEASIBLE,
            knowledge_package_id="knowledge_package_demo",
            knowledge_package_revision="package-revision-synthetic",
            fact_snapshot_digest="sha256:solver-coordination.v1:" + "1" * 64,
            rules_revision="rules-revision-synthetic",
            evidence_ref="evidence_ref_synthetic_all_0",
            error_code="SHOULD_NOT_EXIST",
        )
