"""Project verified, persisted solver output into the only trusted result DTO."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .contracts import (
    AuthoritativeResult,
    FactSnapshot,
    ProblemIR,
    SolverResult,
    VerificationResult,
    _assert_model_payload_is_platform_neutral,
    _assert_authoritative_proof_strength,
    _freeze_object,
)
from .errors import (
    ArtifactPersistError,
    SolverResultRejectedError,
    VerificationDivergenceError,
)


def _required_reference(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactPersistError("{} is required".format(field_name))
    return value.strip()


def validate_authoritative_verification(
    *,
    fact_snapshot: FactSnapshot,
    problem: ProblemIR,
    solver_result: SolverResult,
    verification: VerificationResult,
) -> None:
    """Validate the authority boundary before any profile presentation side effect."""

    if not isinstance(fact_snapshot, FactSnapshot):
        raise SolverResultRejectedError("fact snapshot is invalid")
    if not isinstance(problem, ProblemIR):
        raise SolverResultRejectedError("compiled problem is invalid")
    if not isinstance(solver_result, SolverResult):
        raise SolverResultRejectedError("solver result is invalid")
    if not isinstance(verification, VerificationResult):
        raise SolverResultRejectedError("verification result is invalid")
    if solver_result.problem_id != problem.problem_id or verification.problem_id != problem.problem_id:
        raise VerificationDivergenceError("result identities do not match the compiled problem")

    if verification.status == "unproven" and verification.feasibility == "not_found":
        raise SolverResultRejectedError(
            "no feasible solution was found but infeasibility was not proven",
            code="no_feasible_solution_unproven",
        )
    if verification.status != "verified":
        raise SolverResultRejectedError("solver output was rejected by verification")
    if verification.feasibility not in {"feasible", "infeasible"}:
        raise SolverResultRejectedError("verification feasibility is not authoritative")
    if verification.feasibility == "infeasible":
        if solver_result.status != "infeasible" or solver_result.assignment or solver_result.objective_values:
            raise SolverResultRejectedError("solver and verification feasibility claims diverge")
    elif solver_result.status not in {"optimal", "feasible"}:
        raise SolverResultRejectedError("solver and verification feasibility claims diverge")
    if verification.feasibility == "feasible" and verification.optimality not in {
        "not_proven",
        "proven_optimal",
    }:
        raise SolverResultRejectedError("verification optimality is inconsistent")
    if verification.feasibility == "infeasible" and verification.optimality != "not_applicable":
        raise SolverResultRejectedError("infeasibility optimality is inconsistent")
    try:
        _assert_authoritative_proof_strength(
            feasibility=verification.feasibility,
            optimality=verification.optimality,
            proof_kind=verification.proof_kind,
        )
    except ValueError as exc:
        raise SolverResultRejectedError("verification proof is insufficient for the claimed authority") from exc


@dataclass(frozen=True)
class AuthoritativeResultProjector:
    """Publish only verifier-backed semantics and compact public-safe fields."""

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def project(
        self,
        *,
        run_id: str,
        job_id: str,
        package_revision: str,
        fact_snapshot: FactSnapshot,
        problem: ProblemIR,
        solver_result: SolverResult,
        verification: VerificationResult,
        result_artifact_ref: str,
        verification_evidence_ref: str,
        candidate_results_artifact_ref: str | None = None,
        candidate_results_artifact_digest: str | None = None,
        evidence_persisted: bool,
        summary: Mapping[str, object] | None = None,
    ) -> AuthoritativeResult:
        if evidence_persisted is not True:
            raise ArtifactPersistError("verification evidence is not durable")
        result_ref = _required_reference(result_artifact_ref, "result_artifact_ref")
        evidence_ref = _required_reference(verification_evidence_ref, "verification_evidence_ref")
        if (candidate_results_artifact_ref is None) != (candidate_results_artifact_digest is None):
            raise ArtifactPersistError("candidate results artifact reference and digest must be provided together")
        candidate_ref = (
            _required_reference(
                candidate_results_artifact_ref,
                "candidate_results_artifact_ref",
            )
            if candidate_results_artifact_ref is not None
            else None
        )
        candidate_digest = (
            _required_reference(
                candidate_results_artifact_digest,
                "candidate_results_artifact_digest",
            )
            if candidate_results_artifact_digest is not None
            else None
        )
        validate_authoritative_verification(
            fact_snapshot=fact_snapshot,
            problem=problem,
            solver_result=solver_result,
            verification=verification,
        )

        solver_payload = solver_result.to_dict()
        if summary is None:
            published_summary = {
                "assignment": solver_payload["assignment"],
                "objective_values": solver_payload["objective_values"],
            }
        else:
            if not isinstance(summary, Mapping):
                raise SolverResultRejectedError("published summary is invalid")
            try:
                published_summary = _freeze_object(dict(summary), "summary")
                _assert_model_payload_is_platform_neutral(published_summary, "summary")
            except Exception as exc:
                raise SolverResultRejectedError("published summary is not platform neutral") from exc
        return AuthoritativeResult(
            run_id=run_id,
            job_id=job_id,
            status="completed",
            feasibility=verification.feasibility,
            optimality=verification.optimality,
            proof_kind=verification.proof_kind,
            authoritative=True,
            summary=published_summary,
            result_artifact_ref=result_ref,
            verification_evidence_ref=evidence_ref,
            knowledge_revision_id=package_revision,
            fact_snapshot_digest=fact_snapshot.digest,
            problem_digest=problem.problem_id,
            candidate_results_artifact_ref=candidate_ref,
            candidate_results_artifact_digest=candidate_digest,
        )
