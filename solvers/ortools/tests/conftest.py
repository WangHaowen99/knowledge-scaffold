from __future__ import annotations

from copy import deepcopy

import pytest


@pytest.fixture
def submit_request() -> dict:
    return {
        "api_version": "ortools.solver.v1",
        "request_id": "req_SYNTHETIC0123456789AB",
        "problem": {
            "type": "binary_ordered_route",
            "version": "1.0",
            "data": {
                "decision_space": {"variables": ["selected_candidate"]},
                "parameters": {
                    "candidate_selection": {
                        "profile_id": "synthetic_profile.v1",
                        "candidate_count": 4,
                        "candidate_evaluation_digest": (
                            "sha256:solver-coordination.v1:" + "2" * 64
                        ),
                        "candidates": [
                            {
                                "candidate_id": "CANDIDATE_SYNTHETIC_0000",
                                "eligible": True,
                                "assignment": {"item_0001": "OPTION_0"},
                                "metrics": {
                                    "primary_score": 90,
                                    "transition_count": 0,
                                    "candidate_rank": 0,
                                },
                            },
                            {
                                "candidate_id": "CANDIDATE_SYNTHETIC_0001",
                                "eligible": True,
                                "assignment": {"item_0001": "OPTION_1"},
                                "metrics": {
                                    "primary_score": 100,
                                    "transition_count": 2,
                                    "candidate_rank": 1,
                                },
                            },
                            {
                                "candidate_id": "CANDIDATE_SYNTHETIC_0002",
                                "eligible": True,
                                "assignment": {"item_0001": "OPTION_0"},
                                "metrics": {
                                    "primary_score": 100,
                                    "transition_count": 1,
                                    "candidate_rank": 2,
                                },
                            },
                            {
                                "candidate_id": "CANDIDATE_SYNTHETIC_0003",
                                "eligible": True,
                                "assignment": {"item_0001": "OPTION_1"},
                                "metrics": {
                                    "primary_score": 100,
                                    "transition_count": 1,
                                    "candidate_rank": 3,
                                },
                            },
                        ],
                    }
                },
                "constraints": [{"kind": "select_exactly_one_candidate"}],
                "objectives": [
                    {"metric": "primary_score", "sense": "maximize"},
                    {"metric": "transition_count", "sense": "minimize"},
                    {"metric": "candidate_rank", "sense": "minimize"},
                ],
                "limits": {
                    "max_candidates": 128,
                    "max_verification_candidates": 128,
                },
            },
        },
        "options": {"time_limit_ms": 5000, "search_workers": 1},
    }


@pytest.fixture
def copied_request(submit_request: dict) -> dict:
    return deepcopy(submit_request)


@pytest.fixture
def engine_problem(submit_request: dict) -> dict:
    return {
        "schema_version": "solver.problem.v1",
        "problem_family": submit_request["problem"]["type"],
        "family_version": submit_request["problem"]["version"],
        "payload": deepcopy(submit_request["problem"]["data"]),
    }
