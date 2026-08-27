from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import solver_coordination
from solver_coordination.candidate_service import create_candidate_set
from solver_coordination.candidate_store import CandidateSetStore
from solver_coordination.operation_validation import (
    CandidateSetBatchValidator,
    OperationValidationRunService,
    ResolvedValidationContext,
    ValidationContextResolver,
)
from solver_coordination.errors import (
    CandidateDigestMismatchError,
    VerificationDivergenceError,
    VerificationRuntimeFailure,
)
from solver_coordination.validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
)
from workspace.workspaces import create_workspace


VALIDATOR_REVISION = "ontology_operation_batch.v1"
FACT_SNAPSHOT_DIGEST = "sha256:solver-coordination.v1:" + "f" * 64


def _stored_candidate_set(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    workspace_id = "workspace_demo"
    owner_id = "operator_demo"
    create_workspace(
        "Synthetic Workspace",
        workspace_id=workspace_id,
        set_as_default=True,
    )
    candidate_set = create_candidate_set(
        plan_payload={
            "items": [
                {
                    "item_order": 1,
                    "item_id": "item_synthetic_001",
                    "quantity": "1.0",
                    "duration": "2.0",
                }
            ],
            "option_domain": ["OPTION_0", "OPTION_1"],
            "max_transitions": 1,
        },
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="revision_demo_1",
        source_run_id="solve_run_demo",
    )
    candidate_set_ref, artifact = CandidateSetStore(workspace_id).put(
        candidate_set,
        owner_id=owner_id,
    )
    assert artifact["candidate_set_ref"] == candidate_set_ref
    return workspace_id, owner_id, candidate_set, artifact


def _context(
    *,
    tmp_path: Path,
    workspace_id: str = "workspace_demo",
    owner_id: str = "operator_demo",
    parent_solve_run_id: str = "solve_run_demo",
    validation_run_id: str = "validation_run_demo",
) -> ResolvedValidationContext:
    return ResolvedValidationContext(
        workspace_id=workspace_id,
        owner_id=owner_id,
        parent_solve_run_id=parent_solve_run_id,
        validation_run_id=validation_run_id,
        knowledge_package_id="knowledge_package_demo",
        knowledge_package_revision="revision_demo_1",
        problem_family="binary_ordered_route.v1",
        validation_operation="validate_demo",
        validator_revision=VALIDATOR_REVISION,
        rules_revision="rules_demo_1",
        fact_snapshot_ref="fact_snapshot_synthetic_001",
        fact_snapshot_digest=FACT_SNAPSHOT_DIGEST,
        fact_snapshot_path=tmp_path / "fact_snapshot_synthetic_001.json",
    )


class _RecordingResolver:
    def __init__(self, context: ResolvedValidationContext) -> None:
        self.context = context
        self.calls: list[dict[str, object]] = []

    def resolve(self, **kwargs) -> ResolvedValidationContext:
        self.calls.append(kwargs)
        return self.context


class _RecordingBatchValidator:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ResolvedValidationContext]] = []

    def validate(self, *, candidate_set, context):
        self.calls.append((candidate_set, context))
        return tuple(
            CandidateValidationResult(
                candidate_id=candidate.candidate_id,
                candidate_digest=candidate.candidate_digest,
                status=CandidateValidationStatus.FEASIBLE,
                knowledge_package_id=context.knowledge_package_id,
                knowledge_package_revision=context.knowledge_package_revision,
                fact_snapshot_digest=context.fact_snapshot_digest,
                rules_revision=context.rules_revision,
                evidence_ref="validation_evidence_synthetic_001",
            )
            for candidate in candidate_set.candidates
        )


def test_resolved_validation_context_is_immutable_and_exported(tmp_path):
    context = _context(tmp_path=tmp_path)

    with pytest.raises(FrozenInstanceError):
        context.rules_revision = "rules_demo_2"

    assert solver_coordination.ResolvedValidationContext is ResolvedValidationContext
    assert solver_coordination.ValidationContextResolver is ValidationContextResolver
    assert solver_coordination.CandidateSetBatchValidator is CandidateSetBatchValidator
    assert solver_coordination.OperationValidationRunService is OperationValidationRunService


def test_run_resolves_once_validates_the_complete_batch_once_and_aggregates(
    tmp_path,
    monkeypatch,
):
    workspace_id, owner_id, candidate_set, artifact = _stored_candidate_set(
        tmp_path,
        monkeypatch,
    )
    context = _context(tmp_path=tmp_path)
    resolver = _RecordingResolver(context)
    validator = _RecordingBatchValidator()
    service = OperationValidationRunService(
        context_resolver=resolver,
        batch_validator=validator,
    )

    result = service.run(
        workspace_id=workspace_id,
        owner_id=owner_id,
        candidate_set_ref=artifact["candidate_set_ref"],
        source_run_id=artifact["source_run_id"],
        parent_solve_run_id=artifact["source_run_id"],
        candidate_set_digest=artifact["candidate_set_digest"],
        validation_run_id="validation_run_demo",
        validator_revision=VALIDATOR_REVISION,
    )

    assert service.validator_revision == VALIDATOR_REVISION
    assert len(resolver.calls) == 1
    assert resolver.calls[0] == {
        "candidate_set": candidate_set,
        "workspace_id": workspace_id,
        "owner_id": owner_id,
        "parent_solve_run_id": candidate_set.source_run_id,
        "validation_run_id": "validation_run_demo",
    }
    assert validator.calls == [(candidate_set, context)]
    assert result.result_count == candidate_set.candidate_count
    assert result.completed_count == candidate_set.candidate_count
    assert result.feasible_candidate_ids == tuple(
        candidate.candidate_id for candidate in candidate_set.candidates
    )


@pytest.mark.parametrize(
    "request_override",
    [
        {"source_run_id": "solve_run_other"},
        {"parent_solve_run_id": "solve_run_other"},
        {
            "candidate_set_digest": (
                "sha256:solver-coordination.v1:" + "8" * 64
            )
        },
        {
            "candidate_digest": (
                "sha256:solver-coordination.v1:" + "9" * 64
            )
        },
    ],
    ids=[
        "source-run",
        "parent-solve-run",
        "candidate-set-digest",
        "candidate-digest",
    ],
)
def test_run_rejects_candidate_identity_mismatches_before_resolution(
    tmp_path,
    monkeypatch,
    request_override,
):
    workspace_id, owner_id, candidate_set, artifact = _stored_candidate_set(
        tmp_path,
        monkeypatch,
    )
    resolver = _RecordingResolver(_context(tmp_path=tmp_path))
    validator = _RecordingBatchValidator()
    service = OperationValidationRunService(
        context_resolver=resolver,
        batch_validator=validator,
    )
    request = {
        "workspace_id": workspace_id,
        "owner_id": owner_id,
        "candidate_set_ref": artifact["candidate_set_ref"],
        "source_run_id": candidate_set.source_run_id,
        "parent_solve_run_id": candidate_set.source_run_id,
        "candidate_set_digest": candidate_set.candidate_set_digest,
        "validation_run_id": "validation_run_demo",
        "validator_revision": VALIDATOR_REVISION,
    }
    request.update(request_override)

    with pytest.raises(CandidateDigestMismatchError):
        service.run(**request)

    assert resolver.calls == []
    assert validator.calls == []


def test_batch_runtime_failure_propagates_without_synthetic_candidate_errors(
    tmp_path,
    monkeypatch,
):
    workspace_id, owner_id, candidate_set, artifact = _stored_candidate_set(
        tmp_path,
        monkeypatch,
    )
    resolver = _RecordingResolver(_context(tmp_path=tmp_path))

    class _UnavailableBatchValidator:
        def __init__(self) -> None:
            self.calls = 0

        def validate(self, *, candidate_set, context):
            self.calls += 1
            raise OSError("synthetic validation runtime unavailable")

    validator = _UnavailableBatchValidator()
    service = OperationValidationRunService(
        context_resolver=resolver,
        batch_validator=validator,
    )

    with pytest.raises(VerificationRuntimeFailure):
        service.run(
            workspace_id=workspace_id,
            owner_id=owner_id,
            candidate_set_ref=artifact["candidate_set_ref"],
            source_run_id=candidate_set.source_run_id,
            candidate_set_digest=candidate_set.candidate_set_digest,
            validation_run_id="validation_run_demo",
            validator_revision=VALIDATOR_REVISION,
        )

    assert len(resolver.calls) == 1
    assert validator.calls == 1


@pytest.mark.parametrize(
    "failure",
    [
        OSError("synthetic resolver unavailable"),
        CandidateDigestMismatchError("synthetic resolver contract failure"),
    ],
    ids=["infrastructure-error", "misclassified-api-error"],
)
def test_resolver_call_failures_are_normalized_as_retryable_runtime_failures(
    tmp_path,
    monkeypatch,
    failure,
):
    workspace_id, owner_id, candidate_set, artifact = _stored_candidate_set(
        tmp_path,
        monkeypatch,
    )

    class _FailingResolver:
        def resolve(self, **kwargs):
            raise failure

    validator = _RecordingBatchValidator()
    service = OperationValidationRunService(
        context_resolver=_FailingResolver(),
        batch_validator=validator,
    )

    with pytest.raises(VerificationRuntimeFailure) as captured:
        service.run(
            workspace_id=workspace_id,
            owner_id=owner_id,
            candidate_set_ref=artifact["candidate_set_ref"],
            source_run_id=candidate_set.source_run_id,
            candidate_set_digest=candidate_set.candidate_set_digest,
            validation_run_id="validation_run_demo",
            validator_revision=VALIDATOR_REVISION,
        )

    assert captured.value.__cause__ is failure
    assert validator.calls == []


def test_validator_api_error_is_normalized_as_retryable_runtime_failure(
    tmp_path,
    monkeypatch,
):
    workspace_id, owner_id, candidate_set, artifact = _stored_candidate_set(
        tmp_path,
        monkeypatch,
    )
    resolver = _RecordingResolver(_context(tmp_path=tmp_path))
    failure = CandidateDigestMismatchError("synthetic adapter failure")

    class _FailingValidator:
        def validate(self, **kwargs):
            raise failure

    service = OperationValidationRunService(
        context_resolver=resolver,
        batch_validator=_FailingValidator(),
    )

    with pytest.raises(VerificationRuntimeFailure) as captured:
        service.run(
            workspace_id=workspace_id,
            owner_id=owner_id,
            candidate_set_ref=artifact["candidate_set_ref"],
            source_run_id=candidate_set.source_run_id,
            candidate_set_digest=candidate_set.candidate_set_digest,
            validation_run_id="validation_run_demo",
            validator_revision=VALIDATOR_REVISION,
        )

    assert captured.value.__cause__ is failure


@pytest.mark.parametrize("mutation", ["list", "missing", "reversed"])
def test_returned_batch_shape_and_order_divergence_is_not_retryable(
    tmp_path,
    monkeypatch,
    mutation,
):
    workspace_id, owner_id, candidate_set, artifact = _stored_candidate_set(
        tmp_path,
        monkeypatch,
    )
    context = _context(tmp_path=tmp_path)
    resolver = _RecordingResolver(context)

    class _DivergentValidator(_RecordingBatchValidator):
        def validate(self, *, candidate_set, context):
            results = super().validate(
                candidate_set=candidate_set,
                context=context,
            )
            if mutation == "list":
                return list(results)
            if mutation == "missing":
                return results[:-1]
            return tuple(reversed(results))

    service = OperationValidationRunService(
        context_resolver=resolver,
        batch_validator=_DivergentValidator(),
    )

    with pytest.raises(VerificationDivergenceError):
        service.run(
            workspace_id=workspace_id,
            owner_id=owner_id,
            candidate_set_ref=artifact["candidate_set_ref"],
            source_run_id=candidate_set.source_run_id,
            candidate_set_digest=candidate_set.candidate_set_digest,
            validation_run_id="validation_run_demo",
            validator_revision=VALIDATOR_REVISION,
        )


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("workspace_id", "workspace_other"),
        ("owner_id", "operator_other"),
        ("parent_solve_run_id", "solve_run_other"),
        ("validation_run_id", "validation_run_other"),
        ("knowledge_package_id", "knowledge_package_other"),
        ("knowledge_package_revision", "revision_other_1"),
        ("problem_family", "problem_family_other.v1"),
    ],
)
def test_run_rejects_resolved_context_identity_mismatch_before_validation(
    tmp_path,
    monkeypatch,
    field_name,
    field_value,
):
    workspace_id, owner_id, candidate_set, artifact = _stored_candidate_set(
        tmp_path,
        monkeypatch,
    )
    mismatched_context = replace(
        _context(tmp_path=tmp_path),
        **{field_name: field_value},
    )
    resolver = _RecordingResolver(mismatched_context)
    validator = _RecordingBatchValidator()
    service = OperationValidationRunService(
        context_resolver=resolver,
        batch_validator=validator,
    )

    with pytest.raises(VerificationDivergenceError):
        service.run(
            workspace_id=workspace_id,
            owner_id=owner_id,
            candidate_set_ref=artifact["candidate_set_ref"],
            source_run_id=candidate_set.source_run_id,
            candidate_set_digest=candidate_set.candidate_set_digest,
            validation_run_id="validation_run_demo",
            validator_revision=VALIDATOR_REVISION,
        )

    assert len(resolver.calls) == 1
    assert validator.calls == []


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        (
            "fact_snapshot_digest",
            "sha256:solver-coordination.v1:" + "e" * 64,
        ),
        ("rules_revision", "rules_other_1"),
    ],
)
def test_run_rejects_result_identity_that_differs_from_resolved_context(
    tmp_path,
    monkeypatch,
    field_name,
    field_value,
):
    workspace_id, owner_id, candidate_set, artifact = _stored_candidate_set(
        tmp_path,
        monkeypatch,
    )
    context = _context(tmp_path=tmp_path)
    resolver = _RecordingResolver(context)

    class _MismatchedResultValidator(_RecordingBatchValidator):
        def validate(self, *, candidate_set, context):
            results = super().validate(
                candidate_set=candidate_set,
                context=context,
            )
            return tuple(
                replace(result, **{field_name: field_value})
                for result in results
            )

    validator = _MismatchedResultValidator()
    service = OperationValidationRunService(
        context_resolver=resolver,
        batch_validator=validator,
    )

    with pytest.raises(VerificationDivergenceError):
        service.run(
            workspace_id=workspace_id,
            owner_id=owner_id,
            candidate_set_ref=artifact["candidate_set_ref"],
            source_run_id=candidate_set.source_run_id,
            candidate_set_digest=candidate_set.candidate_set_digest,
            validation_run_id="validation_run_demo",
            validator_revision=VALIDATOR_REVISION,
        )

    assert len(resolver.calls) == 1
    assert len(validator.calls) == 1
