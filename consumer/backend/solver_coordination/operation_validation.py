# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Run-scoped port for validating one immutable candidate-set batch."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .candidate_store import CandidateSetStore
from .contracts import CandidateSet, SolverContractError
from .errors import (
    CandidateDigestMismatchError,
    VerificationDivergenceError,
    VerificationRuntimeFailure,
)
from .validation import (
    CandidateValidationResult,
    ValidationBatchResult,
    aggregate_validation_results,
)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SolverContractError("{} is required and must be a string".format(field_name))
    return value.strip()


@dataclass(frozen=True)
class ResolvedValidationContext:
    """Immutable package, operation, rules, and fact identities for one run."""

    workspace_id: str
    owner_id: str
    parent_solve_run_id: str
    validation_run_id: str
    knowledge_package_id: str
    knowledge_package_revision: str
    problem_family: str
    validation_operation: str
    validator_revision: str
    rules_revision: str
    fact_snapshot_ref: str
    fact_snapshot_digest: str
    fact_snapshot_path: Path
    # The following fields are resolved from the exact package revision.  They
    # are optional for callers that only exercise the domain port, but a real
    # operation-backed runner requires them before invoking a script.
    validation_script_path: Path | None = None
    validation_script_digest: str | None = None
    ontology_dir: Path | None = None
    fact_snapshot_payload: object | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "workspace_id",
            "owner_id",
            "parent_solve_run_id",
            "validation_run_id",
            "knowledge_package_id",
            "knowledge_package_revision",
            "problem_family",
            "validation_operation",
            "validator_revision",
            "rules_revision",
            "fact_snapshot_ref",
            "fact_snapshot_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        if not isinstance(self.fact_snapshot_path, Path):
            raise SolverContractError("fact_snapshot_path must be a Path")
        for field_name in ("validation_script_path", "ontology_dir"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, Path):
                raise SolverContractError("{} must be a Path".format(field_name))
        if self.validation_script_digest is not None:
            object.__setattr__(
                self,
                "validation_script_digest",
                _required_text(self.validation_script_digest, "validation_script_digest"),
            )


class ValidationContextResolver(Protocol):
    """Resolve trusted run-scoped validation inputs from durable artifacts."""

    def resolve(
        self,
        *,
        candidate_set: CandidateSet,
        workspace_id: str,
        owner_id: str,
        parent_solve_run_id: str,
        validation_run_id: str,
    ) -> ResolvedValidationContext:
        """Return the immutable context bound to the supplied parent run."""


class CandidateSetBatchValidator(Protocol):
    """Validate every candidate through one batch operation invocation."""

    def validate(
        self,
        *,
        candidate_set: CandidateSet,
        context: ResolvedValidationContext,
    ) -> tuple[CandidateValidationResult, ...]:
        """Return one ordered validation result for every candidate."""


class OperationValidationRunService:
    """Bind a stored candidate set to one resolved batch validation operation."""

    validator_revision = "ontology_operation_batch.v1"

    def __init__(
        self,
        *,
        context_resolver: ValidationContextResolver,
        batch_validator: CandidateSetBatchValidator,
    ) -> None:
        if not callable(getattr(context_resolver, "resolve", None)):
            raise SolverContractError("validation context resolver is not configured")
        if not callable(getattr(batch_validator, "validate", None)):
            raise SolverContractError("batch validator is not configured")
        self.context_resolver = context_resolver
        self.batch_validator = batch_validator

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
        source_run = _required_text(source_run_id, "source_run_id")
        parent_run = _required_text(
            parent_solve_run_id if parent_solve_run_id is not None else source_run,
            "parent_solve_run_id",
        )
        if source_run != parent_run or candidate_set.source_run_id != source_run:
            raise CandidateDigestMismatchError("candidate source does not match")
        if candidate_set.candidate_set_digest != candidate_set_digest:
            raise CandidateDigestMismatchError("candidate set digest does not match")
        if candidate_digest is not None:
            normalized_candidate_digest = str(candidate_digest or "").strip()
            if not normalized_candidate_digest or not any(
                candidate.candidate_digest == normalized_candidate_digest
                for candidate in candidate_set.candidates
            ):
                raise CandidateDigestMismatchError("candidate digest does not match")
        if validator_revision is not None:
            requested_revision = _required_text(
                validator_revision,
                "validator_revision",
            )
            if requested_revision != self.validator_revision:
                raise SolverContractError(
                    "validator_revision does not match configured validator"
                )

        try:
            context = self.context_resolver.resolve(
                candidate_set=candidate_set,
                workspace_id=workspace_id,
                owner_id=owner_id,
                parent_solve_run_id=parent_run,
                validation_run_id=validation_run_id,
            )
        except Exception as exc:
            if isinstance(exc, VerificationRuntimeFailure):
                raise
            raise VerificationRuntimeFailure(
                "validation context resolution failed"
            ) from exc

        try:
            self._assert_context_identity(
                context,
                candidate_set=candidate_set,
                workspace_id=workspace_id,
                owner_id=owner_id,
                parent_solve_run_id=parent_run,
                validation_run_id=validation_run_id,
            )
        except SolverContractError as exc:
            raise VerificationDivergenceError(
                "resolved validation context identity diverged"
            ) from exc

        try:
            results = self.batch_validator.validate(
                candidate_set=candidate_set,
                context=context,
            )
        except Exception as exc:
            if isinstance(exc, VerificationRuntimeFailure):
                raise
            raise VerificationRuntimeFailure("batch validation failed") from exc

        try:
            if not isinstance(results, tuple):
                raise SolverContractError("batch validator results must be a tuple")
            self._assert_result_identity(results, context=context)
            return aggregate_validation_results(
                validation_run_id=validation_run_id,
                candidate_set=candidate_set,
                results=results,
                expected_candidate_count=None,
            )
        except SolverContractError as exc:
            raise VerificationDivergenceError(
                "batch validation result diverged"
            ) from exc

    def _assert_context_identity(
        self,
        context: object,
        *,
        candidate_set: CandidateSet,
        workspace_id: str,
        owner_id: str,
        parent_solve_run_id: str,
        validation_run_id: str,
    ) -> None:
        if not isinstance(context, ResolvedValidationContext):
            raise SolverContractError(
                "validation context resolver returned an invalid context"
            )
        expected = {
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "parent_solve_run_id": parent_solve_run_id,
            "validation_run_id": validation_run_id,
            "knowledge_package_id": candidate_set.knowledge_package_id,
            "knowledge_package_revision": candidate_set.knowledge_package_revision,
            "problem_family": candidate_set.problem_family,
            "validator_revision": self.validator_revision,
        }
        if any(
            getattr(context, field_name) != value
            for field_name, value in expected.items()
        ):
            raise SolverContractError(
                "resolved validation context identity does not match"
            )

    @staticmethod
    def _assert_result_identity(
        results: tuple[CandidateValidationResult, ...],
        *,
        context: ResolvedValidationContext,
    ) -> None:
        for result in results:
            if not isinstance(result, CandidateValidationResult):
                raise SolverContractError("batch validator returned an invalid result")
            if result.knowledge_package_id != context.knowledge_package_id:
                raise SolverContractError("batch result identity does not match context")
            if (
                result.knowledge_package_revision
                != context.knowledge_package_revision
            ):
                raise SolverContractError("batch result identity does not match context")
            if result.fact_snapshot_digest != context.fact_snapshot_digest:
                raise SolverContractError("batch result identity does not match context")
            if result.rules_revision != context.rules_revision:
                raise SolverContractError("batch result identity does not match context")
