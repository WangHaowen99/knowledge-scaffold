# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Integrity checks and aggregate status for candidate batch validation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Iterable

from .contracts import CandidateSet, SolverContractError


class CandidateValidationStatus(str, Enum):
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    VALIDATION_ERROR = "validation_error"


_VERSIONED_DIGEST_RE = re.compile(
    r"^sha256:solver-coordination\.v1:[0-9a-f]{64}$"
)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise SolverContractError("{} must be a non-empty string".format(field_name))
    normalized = value.strip()
    if not normalized:
        raise SolverContractError("{} is required".format(field_name))
    return normalized


def _required_digest(value: object, field_name: str) -> str:
    normalized = _required_text(value, field_name)
    if not _VERSIONED_DIGEST_RE.fullmatch(normalized):
        raise SolverContractError(
            "{} must use the versioned solver-coordination digest format".format(field_name)
        )
    return normalized


@dataclass(frozen=True)
class CandidateValidationResult:
    candidate_id: str
    candidate_digest: str
    status: CandidateValidationStatus
    knowledge_package_id: str
    knowledge_package_revision: str
    fact_snapshot_digest: str
    rules_revision: str
    evidence_ref: str
    error_code: str = ""
    error_stage: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "candidate_id", _required_text(self.candidate_id, "candidate_id")
        )
        object.__setattr__(
            self,
            "candidate_digest",
            _required_digest(self.candidate_digest, "candidate_digest"),
        )
        try:
            status = (
                self.status
                if isinstance(self.status, CandidateValidationStatus)
                else CandidateValidationStatus(self.status)
            )
        except ValueError as exc:
            raise SolverContractError("unsupported candidate validation status") from exc
        object.__setattr__(self, "status", status)
        for field_name in (
            "knowledge_package_id",
            "knowledge_package_revision",
            "fact_snapshot_digest",
            "rules_revision",
            "evidence_ref",
        ):
            value = getattr(self, field_name)
            if field_name == "fact_snapshot_digest":
                value = _required_digest(value, field_name)
            else:
                value = _required_text(value, field_name)
            object.__setattr__(self, field_name, value)

        error_code = str(self.error_code or "").strip()
        error_stage = str(self.error_stage or "").strip()
        if status is CandidateValidationStatus.VALIDATION_ERROR:
            if not error_code:
                raise SolverContractError("error_code is required for validation_error")
            if not error_stage:
                raise SolverContractError("error_stage is required for validation_error")
        elif error_code or error_stage:
            raise SolverContractError(
                "error_code and error_stage are only allowed for validation_error"
            )
        object.__setattr__(self, "error_code", error_code)
        object.__setattr__(self, "error_stage", error_stage)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "candidate_id": self.candidate_id,
            "candidate_digest": self.candidate_digest,
            "status": self.status.value,
            "knowledge_package_id": self.knowledge_package_id,
            "knowledge_package_revision": self.knowledge_package_revision,
            "fact_snapshot_digest": self.fact_snapshot_digest,
            "rules_revision": self.rules_revision,
            "evidence_ref": self.evidence_ref,
        }
        if self.status is CandidateValidationStatus.VALIDATION_ERROR:
            payload["error_code"] = self.error_code
            payload["error_stage"] = self.error_stage
        return payload


@dataclass(frozen=True)
class ValidationBatchResult:
    validation_run_id: str
    source_run_id: str
    candidate_set_digest: str
    all_candidate_results: tuple[CandidateValidationResult, ...]
    feasible_candidate_ids: tuple[str, ...]
    published_candidate_ids: tuple[str, ...]
    batch_status: str
    selection_status: str

    @property
    def result_count(self) -> int:
        return len(self.all_candidate_results)

    @property
    def checked_candidate_count(self) -> int:
        return self.result_count

    @property
    def completed_count(self) -> int:
        return sum(
            result.status is not CandidateValidationStatus.VALIDATION_ERROR
            for result in self.all_candidate_results
        )

    @property
    def error_count(self) -> int:
        return self.result_count - self.completed_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "validation_run_id": self.validation_run_id,
            "source_run_id": self.source_run_id,
            "candidate_set_digest": self.candidate_set_digest,
            "result_count": self.result_count,
            "checked_candidate_count": self.checked_candidate_count,
            "completed_count": self.completed_count,
            "error_count": self.error_count,
            "batch_status": self.batch_status,
            "selection_status": self.selection_status,
            "feasible_candidate_ids": list(self.feasible_candidate_ids),
            "published_candidate_ids": list(self.published_candidate_ids),
            "all_candidate_results": [
                result.to_dict() for result in self.all_candidate_results
            ],
        }


def aggregate_validation_results(
    *,
    validation_run_id: str,
    candidate_set: CandidateSet,
    results: Iterable[CandidateValidationResult],
    expected_candidate_count: int | None = None,
) -> ValidationBatchResult:
    normalized_run_id = _required_text(validation_run_id, "validation_run_id")
    normalized_results = tuple(results)
    if expected_candidate_count is not None and candidate_set.candidate_count != expected_candidate_count:
        raise SolverContractError(
            "candidate set must contain exactly {} candidates".format(expected_candidate_count)
        )
    expected_ids = tuple(candidate.candidate_id for candidate in candidate_set.candidates)
    actual_ids = tuple(result.candidate_id for result in normalized_results)
    if actual_ids != expected_ids:
        raise SolverContractError(
            "validation results must match the exact candidate order"
        )
    if any(
        result.candidate_digest != candidate.candidate_digest
        for result, candidate in zip(normalized_results, candidate_set.candidates)
    ):
        raise SolverContractError(
            "all results must preserve the candidate_digest identity"
        )
    if any(
        result.knowledge_package_id != candidate_set.knowledge_package_id
        for result in normalized_results
    ):
        raise SolverContractError(
            "all results must use the candidate set knowledge_package_id"
        )
    if any(
        result.knowledge_package_revision != candidate_set.knowledge_package_revision
        for result in normalized_results
    ):
        raise SolverContractError(
            "all results must use the candidate set knowledge_package_revision"
        )
    fact_snapshot_digests = {
        result.fact_snapshot_digest for result in normalized_results
    }
    if len(fact_snapshot_digests) != 1:
        raise SolverContractError("all results must use one fact_snapshot_digest")
    rules_revisions = {result.rules_revision for result in normalized_results}
    if len(rules_revisions) != 1:
        raise SolverContractError("all results must use one rules_revision")

    has_error = any(
        result.status is CandidateValidationStatus.VALIDATION_ERROR
        for result in normalized_results
    )
    feasible_ids = tuple(
        result.candidate_id
        for result in normalized_results
        if result.status is CandidateValidationStatus.FEASIBLE
    )
    return ValidationBatchResult(
        validation_run_id=normalized_run_id,
        source_run_id=candidate_set.source_run_id,
        candidate_set_digest=candidate_set.candidate_set_digest,
        all_candidate_results=normalized_results,
        feasible_candidate_ids=feasible_ids,
        published_candidate_ids=feasible_ids if not has_error else (),
        batch_status="validation_failed" if has_error else "validation_completed",
        selection_status="not_publishable" if has_error else "published",
    )
