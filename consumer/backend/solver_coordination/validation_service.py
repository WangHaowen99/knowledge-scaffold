"""Pure, dependency-injected execution of one candidate validation batch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .candidate_store import CandidateSetStore
from .contracts import Candidate, CandidateSet, SolverContractError
from .errors import CandidateDigestMismatchError
from .problem_families.binary_ordered_route import assert_candidate_set_shape
from .validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    ValidationBatchResult,
    aggregate_validation_results,
)


@dataclass(frozen=True)
class ValidationContext:
    """Facts and rule identity shared by every candidate in one batch."""

    fact_snapshot_digest: str
    rules_revision: str
    validator_revision: str

    def __post_init__(self) -> None:
        for name in ("fact_snapshot_digest", "rules_revision", "validator_revision"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise SolverContractError("{} is required and must be a string".format(name))
            object.__setattr__(self, name, value.strip())


class CandidateValidator(Protocol):
    """Server-side validator contract; callers cannot select its implementation."""

    def validate(
        self,
        candidate_set: CandidateSet,
        candidate: Candidate,
        *,
        context: ValidationContext,
    ) -> CandidateValidationResult:
        """Return one result for the supplied candidate."""


def _error_result(
    candidate_set: CandidateSet,
    candidate: Candidate,
    context: ValidationContext,
    *,
    error_code: str,
) -> CandidateValidationResult:
    # Deliberately do not copy exception text into an API response or evidence.
    return CandidateValidationResult(
        candidate_id=candidate.candidate_id,
        candidate_digest=candidate.candidate_digest,
        status=CandidateValidationStatus.VALIDATION_ERROR,
        knowledge_package_id=candidate_set.knowledge_package_id,
        knowledge_package_revision=candidate_set.knowledge_package_revision,
        fact_snapshot_digest=context.fact_snapshot_digest,
        rules_revision=context.rules_revision,
        evidence_ref="validation_evidence_synthetic_{}".format(candidate.candidate_id.lower()),
        error_code=error_code,
        error_stage="candidate_validation",
    )


def validate_candidate_set(
    *,
    validation_run_id: str,
    candidate_set: CandidateSet,
    validator: CandidateValidator,
    context: ValidationContext,
) -> ValidationBatchResult:
    """Validate all candidates while isolating failures to individual candidates."""

    if not hasattr(validator, "validate"):
        raise SolverContractError("validator is not configured")
    results: list[CandidateValidationResult] = []
    for candidate in candidate_set.candidates:
        try:
            result = validator.validate(candidate_set, candidate, context=context)
            if not isinstance(result, CandidateValidationResult):
                raise SolverContractError("validator returned an invalid result")
            if result.candidate_id != candidate.candidate_id:
                raise SolverContractError("validator returned a mismatched candidate_id")
            if result.candidate_digest != candidate.candidate_digest:
                raise SolverContractError("validator returned a mismatched candidate_digest")
            if result.knowledge_package_id != candidate_set.knowledge_package_id:
                raise SolverContractError("validator returned a mismatched package id")
            if result.knowledge_package_revision != candidate_set.knowledge_package_revision:
                raise SolverContractError("validator returned a mismatched package revision")
            if result.fact_snapshot_digest != context.fact_snapshot_digest:
                raise SolverContractError("validator returned a mismatched fact snapshot")
            if result.rules_revision != context.rules_revision:
                raise SolverContractError("validator returned a mismatched rules revision")
        except Exception:
            result = _error_result(
                candidate_set,
                candidate,
                context,
                error_code="verification_runtime_failed",
            )
        results.append(result)

    return aggregate_validation_results(
        validation_run_id=validation_run_id,
        candidate_set=candidate_set,
        results=tuple(results),
        expected_candidate_count=None,
    )


class SyntheticValidationService:
    """Small adapter used by contract tests; production injects a real validator."""

    def __init__(self, validator: CandidateValidator, context: ValidationContext) -> None:
        self.validator = validator
        self.context = context

    def validate(
        self,
        *,
        validation_run_id: str,
        candidate_set: CandidateSet,
    ) -> ValidationBatchResult:
        return validate_candidate_set(
            validation_run_id=validation_run_id,
            candidate_set=candidate_set,
            validator=self.validator,
            context=self.context,
        )


class ValidationRunService:
    """Application service binding an immutable candidate artifact to validation."""

    def __init__(self, *, validator: CandidateValidator, context: ValidationContext) -> None:
        self.validator = validator
        self.context = context

    @property
    def validator_revision(self) -> str:
        return self.context.validator_revision

    def run(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        candidate_set_ref: str,
        source_run_id: str,
        candidate_set_digest: str,
        validation_run_id: str,
        validator_revision: str | None = None,
        candidate_digest: str | None = None,
        parent_solve_run_id: str | None = None,
    ) -> ValidationBatchResult:
        candidate_set = CandidateSetStore(workspace_id).get(
            candidate_set_ref,
            owner_id=owner_id,
        )
        expected_source_run_id = parent_solve_run_id or source_run_id
        if candidate_set.source_run_id != expected_source_run_id:
            raise CandidateDigestMismatchError("candidate source does not match")
        if candidate_set.candidate_set_digest != candidate_set_digest:
            raise CandidateDigestMismatchError("candidate set digest does not match")
        if candidate_digest is not None:
            normalized_digest = str(candidate_digest or "").strip()
            if not normalized_digest or not any(
                candidate.candidate_digest == normalized_digest
                for candidate in candidate_set.candidates
            ):
                raise CandidateDigestMismatchError("candidate digest does not match")
        if validator_revision is not None and validator_revision.strip() != self.context.validator_revision:
            raise SolverContractError("validator_revision does not match configured validator")
        assert_candidate_set_shape(candidate_set)
        return validate_candidate_set(
            validation_run_id=validation_run_id,
            candidate_set=candidate_set,
            validator=self.validator,
            context=self.context,
        )
