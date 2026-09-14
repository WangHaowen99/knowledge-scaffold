# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from dataclasses import dataclass

import pytest
from solver_coordination.contracts import (
    FactSnapshot,
    NormalizedInput,
    ProblemIR,
    SolverResult,
    VerificationResult,
)
from solver_coordination.errors import (
    ArtifactPersistError,
    SolverResultRejectedError,
    VerificationDivergenceError,
    VerificationRuntimeFailure,
)
from solver_coordination.result_projection import AuthoritativeResultProjector
from solver_coordination.verification.service import IndependentVerificationService


def _normalized_input() -> NormalizedInput:
    return NormalizedInput(
        operation_key="solve_synthetic",
        values={"item_count": 2},
    )


def _snapshot() -> FactSnapshot:
    normalized_input = _normalized_input()
    return FactSnapshot(
        workspace_id="workspace-synthetic",
        owner_id="owner-synthetic",
        package_id="package-synthetic",
        package_revision="package-revision-synthetic",
        normalized_input_digest=normalized_input.digest,
        facts=({"name": "limit", "value": "10"},),
        bindings={"solver_binding_revision": "binding-revision-synthetic"},
        as_of="2026-08-17T00:00:00Z",
        checks={
            "required": ("limit",),
            "missing": (),
            "duplicates": (),
            "conflicts": (),
        },
    )


def _problem() -> ProblemIR:
    return ProblemIR(
        problem_id="sha256:" + "1" * 64,
        problem_family="synthetic_family.v1",
        decision_space={"options": ["OPTION_0", "OPTION_1"]},
        parameters={"item_count": 2},
        constraints=({"kind": "max_transitions", "value": 1},),
        objectives=({"name": "cost", "direction": "min"},),
        limits={"max_candidates": 4},
    )


def _solver_result(
    *,
    problem_id: str | None = None,
    status: str = "optimal",
) -> SolverResult:
    problem = _problem()
    return SolverResult(
        problem_id=problem_id or problem.problem_id,
        status=status,
        assignment={"candidate_id": "ALL_0", "options": ["OPTION_0", "OPTION_0"]},
        objective_values={"cost": "2.0"},
        diagnostics=(),
        engine={"name": "synthetic-engine", "version": "1"},
    )


def _verification(
    *,
    problem_id: str | None = None,
    status: str = "verified",
    feasibility: str = "feasible",
    optimality: str = "proven_optimal",
    proof_kind: str = "verifier_exhaustive_check",
) -> VerificationResult:
    return VerificationResult(
        problem_id=problem_id or _problem().problem_id,
        status=status,
        feasibility=feasibility,
        optimality=optimality,
        proof_kind=proof_kind,
        diagnostics=(),
        evidence=({"kind": "synthetic_verification"},),
    )


@dataclass
class _Verifier:
    result: VerificationResult
    calls: int = 0

    def verify(self, **kwargs):
        assert kwargs["problem"] == _problem()
        assert kwargs["capability"] == {"problem_family": "synthetic_family.v1"}
        assert kwargs["normalized_input"] == _normalized_input()
        assert kwargs["fact_snapshot"] == _snapshot()
        assert kwargs["solver_result"].problem_id == _problem().problem_id
        self.calls += 1
        return self.result


def test_independent_verification_rejects_solver_problem_identity_before_verifier_runs():
    verifier = _Verifier(_verification())
    service = IndependentVerificationService(verifier=verifier)

    with pytest.raises(VerificationDivergenceError):
        service.verify(
            problem=_problem(),
            capability={"problem_family": "synthetic_family.v1"},
            normalized_input=_normalized_input(),
            fact_snapshot=_snapshot(),
            solver_result=_solver_result(problem_id="sha256:" + "2" * 64),
        )

    assert verifier.calls == 0


def test_independent_verification_rejects_verifier_identity_divergence():
    service = IndependentVerificationService(verifier=_Verifier(_verification(problem_id="sha256:" + "3" * 64)))

    with pytest.raises(VerificationDivergenceError):
        service.verify(
            problem=_problem(),
            capability={"problem_family": "synthetic_family.v1"},
            normalized_input=_normalized_input(),
            fact_snapshot=_snapshot(),
            solver_result=_solver_result(),
        )


def test_independent_verification_maps_unknown_verifier_failures_to_runtime_error():
    class _ExplodingVerifier:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def verify(self, **kwargs):
            raise RuntimeError("synthetic verifier failure")

    service = IndependentVerificationService(verifier=_ExplodingVerifier())

    with pytest.raises(VerificationRuntimeFailure):
        service.verify(
            problem=_problem(),
            capability={"problem_family": "synthetic_family.v1"},
            normalized_input=_normalized_input(),
            fact_snapshot=_snapshot(),
            solver_result=_solver_result(),
        )


@pytest.mark.parametrize(
    (
        "solver_result",
        "verification",
        "expected_feasibility",
        "expected_optimality",
    ),
    [
        (
            _solver_result(status="optimal"),
            _verification(optimality="not_proven", proof_kind="constraint_recalculation"),
            "feasible",
            "not_proven",
        ),
        (
            _solver_result(status="optimal"),
            _verification(),
            "feasible",
            "proven_optimal",
        ),
        (
            SolverResult(
                problem_id=_problem().problem_id,
                status="infeasible",
                assignment={},
                objective_values={},
                diagnostics=(),
                engine={"name": "synthetic-engine", "version": "1"},
            ),
            _verification(
                feasibility="infeasible",
                optimality="not_applicable",
                proof_kind="verifier_infeasibility_certificate",
            ),
            "infeasible",
            "not_applicable",
        ),
    ],
)
def test_projection_uses_verifier_semantics_not_solver_claims(
    solver_result, verification, expected_feasibility, expected_optimality
):
    result = AuthoritativeResultProjector().project(
        run_id="run-synthetic",
        job_id="job-synthetic",
        package_revision="package-revision-synthetic",
        fact_snapshot=_snapshot(),
        problem=_problem(),
        solver_result=solver_result,
        verification=verification,
        result_artifact_ref="artifact://solver-result-synthetic",
        verification_evidence_ref="artifact://verification-synthetic",
        evidence_persisted=True,
    )

    assert result.authoritative is True
    assert result.feasibility == expected_feasibility
    assert result.optimality == expected_optimality
    public_summary = result.to_dict()["summary"]
    solver_payload = solver_result.to_dict()
    assert public_summary["assignment"] == solver_payload["assignment"]
    assert public_summary["objective_values"] == solver_payload["objective_values"]


@pytest.mark.parametrize(
    ("solver_result", "verification"),
    [
        (
            _solver_result(status="optimal"),
            _verification(
                feasibility="infeasible",
                optimality="not_applicable",
                proof_kind="verifier_infeasibility_certificate",
            ),
        ),
        (
            SolverResult(
                problem_id=_problem().problem_id,
                status="infeasible",
                assignment={},
                objective_values={},
                diagnostics=(),
                engine={"name": "synthetic-engine", "version": "1"},
            ),
            _verification(),
        ),
    ],
)
def test_projection_rejects_solver_and_verifier_feasibility_divergence(solver_result, verification):
    with pytest.raises(SolverResultRejectedError, match="feasibility"):
        AuthoritativeResultProjector().project(
            run_id="run-synthetic",
            job_id="job-synthetic",
            package_revision="package-revision-synthetic",
            fact_snapshot=_snapshot(),
            problem=_problem(),
            solver_result=solver_result,
            verification=verification,
            result_artifact_ref="artifact://solver-result-synthetic",
            verification_evidence_ref="artifact://verification-synthetic",
            evidence_persisted=True,
        )


def test_projection_preserves_a_prevalidated_profile_summary():
    summary = {
        "candidate_id": "ALL_0",
        "assignment": {"choice": "OPTION_0"},
        "items": [{"item_id": "ITEM_SYNTHETIC_0001", "route": "SYNTHETIC_ROUTE_A"}],
        "objective_values": {"cost": "2.0"},
    }

    result = AuthoritativeResultProjector().project(
        run_id="run-synthetic",
        job_id="job-synthetic",
        package_revision="package-revision-synthetic",
        fact_snapshot=_snapshot(),
        problem=_problem(),
        solver_result=_solver_result(status="optimal"),
        verification=_verification(),
        result_artifact_ref="artifact://solver-result-synthetic",
        verification_evidence_ref="artifact://verification-synthetic",
        evidence_persisted=True,
        summary=summary,
    )

    assert result.to_dict()["summary"] == summary


def test_projection_carries_a_verified_candidate_results_artifact_reference():
    result = AuthoritativeResultProjector().project(
        run_id="run-synthetic",
        job_id="job-synthetic",
        package_revision="package-revision-synthetic",
        fact_snapshot=_snapshot(),
        problem=_problem(),
        solver_result=_solver_result(status="optimal"),
        verification=_verification(),
        result_artifact_ref="artifact://solver_result/" + "a" * 64,
        verification_evidence_ref="artifact://verification_evidence/" + "b" * 64,
        candidate_results_artifact_ref="artifact://candidate_results/" + "c" * 64,
        candidate_results_artifact_digest="sha256:" + "c" * 64,
        evidence_persisted=True,
    )

    assert result.to_dict()["candidate_results_artifact_ref"] == ("artifact://candidate_results/" + "c" * 64)
    assert result.to_dict()["candidate_results_artifact_digest"] == ("sha256:" + "c" * 64)


def test_projection_rejects_a_noncanonical_prevalidated_summary():
    with pytest.raises(SolverResultRejectedError, match="summary"):
        AuthoritativeResultProjector().project(
            run_id="run-synthetic",
            job_id="job-synthetic",
            package_revision="package-revision-synthetic",
            fact_snapshot=_snapshot(),
            problem=_problem(),
            solver_result=_solver_result(status="optimal"),
            verification=_verification(),
            result_artifact_ref="artifact://solver-result-synthetic",
            verification_evidence_ref="artifact://verification-synthetic",
            evidence_persisted=True,
            summary={"solver_endpoint": "https://synthetic.example.net"},
        )


@pytest.mark.parametrize(
    "verification",
    [
        _verification(
            optimality="proven_optimal",
            proof_kind="constraint_recalculation",
        ),
        _verification(
            feasibility="infeasible",
            optimality="not_applicable",
            proof_kind="constraint_recalculation",
        ),
    ],
)
def test_projection_rejects_proof_kinds_below_the_claimed_authority(verification):
    with pytest.raises(SolverResultRejectedError) as exc_info:
        AuthoritativeResultProjector().project(
            run_id="run-synthetic",
            job_id="job-synthetic",
            package_revision="package-revision-synthetic",
            fact_snapshot=_snapshot(),
            problem=_problem(),
            solver_result=_solver_result(),
            verification=verification,
            result_artifact_ref="artifact://solver-result-synthetic",
            verification_evidence_ref="artifact://verification-synthetic",
            evidence_persisted=True,
        )

    assert exc_info.value.code == "solver_result_rejected"


def test_projection_rejects_unproven_no_solution_as_non_authoritative():
    with pytest.raises(SolverResultRejectedError) as exc_info:
        AuthoritativeResultProjector().project(
            run_id="run-synthetic",
            job_id="job-synthetic",
            package_revision="package-revision-synthetic",
            fact_snapshot=_snapshot(),
            problem=_problem(),
            solver_result=_solver_result(status="no_solution"),
            verification=_verification(
                status="unproven",
                feasibility="not_found",
                optimality="not_proven",
                proof_kind="none",
            ),
            result_artifact_ref="artifact://solver-result-synthetic",
            verification_evidence_ref="artifact://verification-synthetic",
            evidence_persisted=True,
        )

    assert exc_info.value.code == "no_feasible_solution_unproven"


@pytest.mark.parametrize(
    ("evidence_ref", "persisted"),
    [
        ("", True),
        ("artifact://verification-synthetic", False),
        ("artifact://verification-synthetic", "false"),
        ("artifact://verification-synthetic", 1),
    ],
)
def test_projection_requires_persisted_verification_evidence(evidence_ref, persisted):
    with pytest.raises(ArtifactPersistError) as exc_info:
        AuthoritativeResultProjector().project(
            run_id="run-synthetic",
            job_id="job-synthetic",
            package_revision="package-revision-synthetic",
            fact_snapshot=_snapshot(),
            problem=_problem(),
            solver_result=_solver_result(),
            verification=_verification(),
            result_artifact_ref="artifact://solver-result-synthetic",
            verification_evidence_ref=evidence_ref,
            evidence_persisted=persisted,
        )

    assert exc_info.value.code == "artifact_persist_failed"


def test_projection_rejects_failed_verification_even_when_solver_completed():
    with pytest.raises(SolverResultRejectedError) as exc_info:
        AuthoritativeResultProjector().project(
            run_id="run-synthetic",
            job_id="job-synthetic",
            package_revision="package-revision-synthetic",
            fact_snapshot=_snapshot(),
            problem=_problem(),
            solver_result=_solver_result(),
            verification=_verification(
                status="rejected",
                feasibility="invalid",
                optimality="not_applicable",
                proof_kind="verifier_divergence",
            ),
            result_artifact_ref="artifact://solver-result-synthetic",
            verification_evidence_ref="artifact://verification-synthetic",
            evidence_persisted=True,
        )

    assert exc_info.value.code == "solver_result_rejected"
