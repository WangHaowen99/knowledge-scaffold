from __future__ import annotations

import time
from copy import deepcopy

import pytest

import solvers.ortools.engine.candidate_selection as engine_module
from solvers.ortools.engine.candidate_selection import (
    EngineDeadlineExceeded,
    ProblemValidationError,
    solve_candidate_selection,
)
from solvers.ortools.result_serializer import serialize_result


def test_cp_sat_selects_candidates_by_all_lexicographic_objectives(submit_request):
    result = solve_candidate_selection(
        {
            "schema_version": "solver.problem.v1",
            "problem_family": submit_request["problem"]["type"],
            "family_version": submit_request["problem"]["version"],
            "payload": submit_request["problem"]["data"],
        },
        solve_time_ms=5000,
        workers=1,
    )

    assert result.solve_outcome == "optimal"
    assert result.assignments == {
        "candidate_id": "CANDIDATE_SYNTHETIC_0002",
        "item_0001": "OPTION_0",
    }
    assert result.objective_values == {
        "primary_score": 100,
        "transition_count": 1,
        "candidate_rank": 2,
    }
    assert result.best_bound == 100
    assert result.gap == "0"


def test_cp_sat_reports_infeasible_when_no_candidate_is_eligible(submit_request):
    for candidate in submit_request["problem"]["data"]["parameters"]["candidate_selection"]["candidates"]:
        candidate["eligible"] = False

    result = solve_candidate_selection(
        {
            "schema_version": "solver.problem.v1",
            "problem_family": submit_request["problem"]["type"],
            "family_version": submit_request["problem"]["version"],
            "payload": submit_request["problem"]["data"],
        },
        solve_time_ms=5000,
        workers=1,
    )

    assert result.solve_outcome == "infeasible"
    assert result.assignments == {}
    assert result.objective_values == {}
    assert result.best_bound is None
    assert result.gap is None


def test_engine_rejects_semantically_inconsistent_candidate_count(submit_request):
    problem = {
        "schema_version": "solver.problem.v1",
        "problem_family": submit_request["problem"]["type"],
        "family_version": submit_request["problem"]["version"],
        "payload": deepcopy(submit_request["problem"]["data"]),
    }
    problem["payload"]["parameters"]["candidate_selection"]["candidate_count"] = 99

    with pytest.raises(ProblemValidationError, match="candidate_count"):
        solve_candidate_selection(problem, solve_time_ms=5000, workers=1)


def test_result_normalizer_emits_only_the_public_result_contract(submit_request):
    engine_result = solve_candidate_selection(
        {
            "schema_version": "solver.problem.v1",
            "problem_family": submit_request["problem"]["type"],
            "family_version": submit_request["problem"]["version"],
            "payload": submit_request["problem"]["data"],
        },
        solve_time_ms=5000,
        workers=1,
    )

    result = serialize_result(
        job_id="job_SYNTHETIC0001",
        request_id=submit_request["request_id"],
        problem=submit_request["problem"],
        engine_result=engine_result,
        service_build="sha256:" + "a" * 64,
    )

    assert set(result) == {
        "api_version",
        "job_id",
        "request_id",
        "problem_type",
        "problem_version",
        "solve_outcome",
        "assignments",
        "objective_values",
        "assurance",
        "engine",
    }
    assert result["engine"]["name"] == "ortools-cp-sat"
    assert result["engine"]["service_build"] == "sha256:" + "a" * 64


def test_lexicographic_phases_share_one_total_deadline(submit_request, monkeypatch):
    observed_limits = []

    class SyntheticSolver:
        def __init__(self):
            self.parameters = type("Parameters", (), {})()

        def Solve(self, model):
            observed_limits.append(self.parameters.max_time_in_seconds)
            return 4  # cp_model.OPTIMAL

        def Value(self, expression):
            return 0

    monotonic_values = iter((10.0, 10.2, 10.8, 11.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        "solvers.ortools.engine.candidate_selection.cp_model.CpSolver",
        SyntheticSolver,
    )

    with pytest.raises(EngineDeadlineExceeded):
        solve_candidate_selection(
            {
                "schema_version": "solver.problem.v1",
                "problem_family": submit_request["problem"]["type"],
                "family_version": submit_request["problem"]["version"],
                "payload": submit_request["problem"]["data"],
            },
            solve_time_ms=1000,
            workers=1,
        )

    assert observed_limits == pytest.approx((0.8, 0.2))


def test_total_deadline_also_includes_problem_translation(submit_request, monkeypatch):
    now = [20.0]
    original_parse = engine_module._parse_problem

    def delayed_parse(problem):
        parsed = original_parse(problem)
        now[0] = 21.1
        return parsed

    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(engine_module, "_parse_problem", delayed_parse)

    with pytest.raises(EngineDeadlineExceeded):
        solve_candidate_selection(
            {
                "schema_version": "solver.problem.v1",
                "problem_family": submit_request["problem"]["type"],
                "family_version": submit_request["problem"]["version"],
                "payload": submit_request["problem"]["data"],
            },
            solve_time_ms=1000,
            workers=1,
        )
