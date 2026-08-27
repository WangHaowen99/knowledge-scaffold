from __future__ import annotations

from copy import deepcopy

import pytest

from solver_coordination.candidate_results import (
    CandidateResultsArtifact,
    build_candidate_results,
)
from solver_coordination.canonical import sha256_digest
from solver_coordination.contracts import (
    ProblemIR,
    SolverContractError,
    SolverResult,
    VerificationResult,
)


ITEM_IDS = tuple(
    "ITEM_SYNTHETIC_{:04d}".format(index) for index in range(1, 10)
)


def _candidate(candidate_id, assignment, rank):
    return {
        "candidate_id": candidate_id,
        "eligible": True,
        "assignment": dict(zip(ITEM_IDS, assignment)),
        "metrics": {
            "primary_score": 900 if candidate_id == "OPTION_0_TO_OPTION_1_K0004" else 100,
            "transition_count": int(len(set(assignment)) > 1),
            "safety_margin": 100,
            "candidate_rank": rank,
            "total_load": 9,
        },
    }


def _candidates():
    candidates = [_candidate("ALL_0", ["OPTION_0"] * 9, 0)]
    candidates.append(_candidate("ALL_1", ["OPTION_1"] * 9, 1))
    for switch in range(1, 9):
        candidates.append(
            _candidate(
                "OPTION_0_TO_OPTION_1_K{:04d}".format(switch),
                ["OPTION_0"] * switch + ["OPTION_1"] * (9 - switch),
                len(candidates),
            )
        )
    for switch in range(1, 9):
        candidates.append(
            _candidate(
                "OPTION_1_TO_OPTION_0_K{:04d}".format(switch),
                ["OPTION_1"] * switch + ["OPTION_0"] * (9 - switch),
                len(candidates),
            )
        )
    return candidates


def _problem():
    candidates = _candidates()
    return ProblemIR(
        problem_id="sha256:" + "1" * 64,
        problem_family="candidate_selection.v1",
        decision_space={"variables": ["selected_candidate"]},
        parameters={
            "candidate_selection": {
                "profile_id": "ordered_binary_route.v1",
                "candidate_count": len(candidates),
                "candidate_evaluation_digest": sha256_digest(candidates),
                "candidates": candidates,
            }
        },
        constraints=({"kind": "select_exactly_one_candidate"},),
        objectives=(
            {"metric": "primary_score", "sense": "maximize"},
            {"metric": "transition_count", "sense": "minimize"},
            {"metric": "safety_margin", "sense": "maximize"},
            {"metric": "candidate_rank", "sense": "minimize"},
        ),
        limits={"max_candidates": 128, "max_verification_candidates": 128},
    )


def _capability():
    return {
        "decision_model": {
            "profile_id": "ordered_binary_route.v1",
            "display_mapping": {
                "OPTION_0": "ROUTE_SYNTHETIC_A",
                "OPTION_1": "ROUTE_SYNTHETIC_B",
            },
        }
    }


def _solver_result():
    assignment = {
        "candidate_id": "OPTION_0_TO_OPTION_1_K0004",
        **{
            item_id: ("OPTION_0" if index <= 4 else "OPTION_1")
            for index, item_id in enumerate(ITEM_IDS, start=1)
        },
    }
    return SolverResult(
        problem_id=_problem().problem_id,
        status="optimal",
        assignment=assignment,
        objective_values={
            "primary_score": 900,
            "transition_count": 1,
            "safety_margin": 100,
            "candidate_rank": 5,
        },
        diagnostics=(),
        engine={"name": "ortools-cp-sat", "version": "synthetic"},
    )


def _verification(*, status="verified", candidate_digest=None):
    return VerificationResult(
        problem_id=_problem().problem_id,
        status=status,
        feasibility="feasible",
        optimality="proven_optimal",
        proof_kind="verifier_exhaustive_check",
        evidence=(
            {
                "kind": "verifier_exhaustive_check",
                "candidate_count": 18,
                "eligible_count": 18,
                "candidate_digest": candidate_digest
                or _problem().parameters["candidate_selection"][
                    "candidate_evaluation_digest"
                ],
                "selected_candidate_id": "OPTION_0_TO_OPTION_1_K0004",
            },
        ),
    )


def _artifact():
    return build_candidate_results(
        problem=_problem(),
        capability=_capability(),
        solver_result=_solver_result(),
        verification=_verification(),
    )


def test_candidate_results_are_complete_business_ordered_and_select_combination_10():
    artifact = _artifact()
    payload = artifact.to_dict()

    assert payload["schema_version"] == "solver.candidate-results.v1"
    assert payload["candidate_count"] == 18
    assert payload["candidate_source_kind"] == "problem_ir.candidate_selection"
    assert payload["candidate_source_digest"] == _problem().parameters[
        "candidate_selection"
    ]["candidate_evaluation_digest"]
    assert payload["option_labels"] == {
        "OPTION_0": "ROUTE_SYNTHETIC_A",
        "OPTION_1": "ROUTE_SYNTHETIC_B",
    }
    assert [row["combination_no"] for row in payload["candidates"]] == list(
        range(1, 19)
    )
    winner = next(row for row in payload["candidates"] if row["selected"])
    assert winner["combination_no"] == 10
    assert winner["candidate_id"] == "OPTION_0_TO_OPTION_1_K0004"
    assert winner["assignment"] == ["OPTION_0"] * 4 + ["OPTION_1"] * 5
    assert winner["metrics"]["candidate_rank"] == 5


def test_candidate_results_round_trip_revalidates_the_complete_payload():
    artifact = _artifact()

    restored = CandidateResultsArtifact.from_dict(artifact.to_dict())

    assert restored == artifact
    assert restored.digest == artifact.digest


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(candidate_count=17),
        lambda payload: payload["candidates"][0].update(combination_no=2),
        lambda payload: payload["candidates"][9].update(selected=False),
        lambda payload: payload.update(selected_candidate_id="UNKNOWN_CANDIDATE"),
        lambda payload: payload.update(candidate_source_digest="sha256:" + "2" * 64),
    ],
)
def test_candidate_results_reject_tampered_content(mutate):
    payload = deepcopy(_artifact().to_dict())
    mutate(payload)

    with pytest.raises(SolverContractError):
        CandidateResultsArtifact.from_dict(payload)


def test_candidate_results_reject_unverified_or_digest_divergent_evidence():
    with pytest.raises(SolverContractError, match="verified"):
        build_candidate_results(
            problem=_problem(),
            capability=_capability(),
            solver_result=_solver_result(),
            verification=_verification(status="rejected"),
        )

    with pytest.raises(SolverContractError, match="digest"):
        build_candidate_results(
            problem=_problem(),
            capability=_capability(),
            solver_result=_solver_result(),
            verification=_verification(candidate_digest="sha256:" + "3" * 64),
        )
