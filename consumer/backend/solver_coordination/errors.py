# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Stable error vocabulary for the solver-coordination HTTP contract.

The route layer deliberately exposes these lower-case, machine-readable codes
instead of exception messages.  Exception text is useful in server logs, but
must never become part of the trust boundary or an API compatibility promise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class ErrorDefinition:
    code: str
    http_status: int
    retryable: bool = False


ERRORS: Final[dict[str, ErrorDefinition]] = {
    "awaiting_input": ErrorDefinition("awaiting_input", 200),
    "invalid_solver_input": ErrorDefinition("invalid_solver_input", 400),
    "fact_snapshot_invalid": ErrorDefinition("fact_snapshot_invalid", 422),
    "model_compile_failed": ErrorDefinition("model_compile_failed", 422),
    "solver_tool_schema_unsupported": ErrorDefinition(
        "solver_tool_schema_unsupported", 409
    ),
    "solver_unavailable": ErrorDefinition(
        "solver_unavailable", 503, retryable=True
    ),
    "solver_capability_drift": ErrorDefinition("solver_capability_drift", 409),
    "solver_contract_drift": ErrorDefinition("solver_contract_drift", 409),
    "knowledge_package_solver_contract_incompatible": ErrorDefinition(
        "knowledge_package_solver_contract_incompatible", 409
    ),
    "timed_out_no_solution": ErrorDefinition(
        "timed_out_no_solution", 200, retryable=True
    ),
    "feasible_not_proven_optimal": ErrorDefinition(
        "feasible_not_proven_optimal", 200
    ),
    "verified_infeasible": ErrorDefinition("verified_infeasible", 200),
    "cancelled": ErrorDefinition("cancelled", 200),
    "standalone_validation_retired": ErrorDefinition(
        "standalone_validation_retired", 410
    ),
    "validation_input_invalid": ErrorDefinition("validation_input_invalid", 400),
    "candidate_not_found": ErrorDefinition("candidate_not_found", 404),
    "candidate_digest_mismatch": ErrorDefinition("candidate_digest_mismatch", 409),
    "idempotency_conflict": ErrorDefinition("idempotency_conflict", 409),
    "solver_retry_not_allowed": ErrorDefinition(
        "solver_retry_not_allowed", 409
    ),
    "validation_service_unavailable": ErrorDefinition(
        "validation_service_unavailable", 503, retryable=True
    ),
    "candidate_store_unavailable": ErrorDefinition(
        "candidate_store_unavailable", 503, retryable=True
    ),
    "validation_store_unavailable": ErrorDefinition(
        "validation_store_unavailable", 503, retryable=True
    ),
    "verification_runtime_failed": ErrorDefinition(
        "verification_runtime_failed", 503, retryable=True
    ),
    "artifact_persist_failed": ErrorDefinition(
        "artifact_persist_failed", 503, retryable=True
    ),
    "verification_divergence": ErrorDefinition("verification_divergence", 409),
    "solver_result_rejected": ErrorDefinition("solver_result_rejected", 409),
    "quality_requirement_not_met": ErrorDefinition(
        "quality_requirement_not_met", 409
    ),
    "no_feasible_solution_unproven": ErrorDefinition(
        "no_feasible_solution_unproven", 200
    ),
    # A completed validation run may contain candidate-level validation_error
    # results.  This is a domain terminal state, not an HTTP transport error.
    "validation_failed": ErrorDefinition("validation_failed", 200),
    "run_not_found": ErrorDefinition("run_not_found", 404),
}


class SolverApiError(Exception):
    """An internal exception carrying a stable public error code."""

    code = "invalid_solver_input"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail)
        self.detail = detail


class CandidateNotFoundError(SolverApiError):
    code = "candidate_not_found"


class CandidateDigestMismatchError(SolverApiError):
    code = "candidate_digest_mismatch"


class ValidationInputError(SolverApiError):
    code = "validation_input_invalid"


class IdempotencyConflictError(SolverApiError):
    code = "idempotency_conflict"


class RunNotFoundError(SolverApiError):
    code = "run_not_found"


class KnowledgePackageSolverContractIncompatibleError(SolverApiError):
    """Raised when a package evaluator cannot satisfy the solver contract."""

    code = "knowledge_package_solver_contract_incompatible"


class VerificationRuntimeFailure(SolverApiError):
    """Raised when batch verification infrastructure cannot produce a result."""

    code = "verification_runtime_failed"


class VerificationDivergenceError(SolverApiError):
    """Raised when independently verified identities or results disagree."""

    code = "verification_divergence"


class ArtifactPersistError(SolverApiError):
    """Raised when a required authoritative artifact was not durably stored."""

    code = "artifact_persist_failed"


class SolverResultRejectedError(SolverApiError):
    """Raised when verifier semantics do not permit authoritative projection."""

    code = "solver_result_rejected"

    def __init__(
        self,
        detail: str = "",
        *,
        code: str = "solver_result_rejected",
    ) -> None:
        if code not in {
            "solver_result_rejected",
            "no_feasible_solution_unproven",
            "quality_requirement_not_met",
        }:
            code = "solver_result_rejected"
        super().__init__(detail)
        self.code = code


def error_payload(code: str, detail: str | None = None) -> dict[str, object]:
    """Build a safe response body without leaking exception text by default."""

    definition = ERRORS.get(code)
    if definition is None:
        definition = ERRORS["invalid_solver_input"]
        code = definition.code
    payload: dict[str, object] = {"code": code}
    if detail:
        payload["error"] = detail
    if definition.retryable:
        payload["retryable"] = True
    return payload


def public_error_status(code: str) -> int:
    return ERRORS.get(code, ERRORS["invalid_solver_input"]).http_status
