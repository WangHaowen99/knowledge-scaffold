from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from solver_coordination.connectors.errors import (
    ConnectorContractError,
    ConnectorResultError,
)
from solver_coordination.connectors.ortools import (
    ORTOOLS_CONNECTOR_CODE_BUILD,
    OrToolsConnector,
)
from solver_coordination.contracts import ProblemIR


class _Transport:
    def __init__(self):
        self.calls = []
        self.responses = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _problem():
    return ProblemIR(
        schema_version="solver.problem.v1",
        problem_id="sha256:solver.problem.v1:" + "1" * 64,
        problem_family="binary_ordered_route.v1",
        decision_space={"variables": ["selected_candidate"]},
        parameters={
            "candidate_selection": {
                "profile_id": "synthetic_profile.v1",
                "candidate_count": 1,
                "candidate_evaluation_digest": "sha256:solver-coordination.v1:" + "2" * 64,
                "candidates": [
                    {
                        "candidate_id": "CANDIDATE_SYNTHETIC_0001",
                        "eligible": True,
                        "assignment": {"item_0001": "OPTION_0"},
                        "metrics": {"primary_score": 10, "candidate_rank": 0},
                    }
                ],
            }
        },
        constraints=({"kind": "select_exactly_one_candidate"},),
        objectives=({"metric": "primary_score", "sense": "maximize"},),
        limits={"max_candidates": 8, "max_verification_candidates": 8},
    )


def _domain_context():
    return {
        "schema_version": "refinery_route_context.v1",
        "profile_id": "refinery_route.v1",
        "metric_scale": 100,
        "rounding": "ROUND_HALF_EVEN",
        "plan_month": "2099-02",
        "production_condition": "NO_JET",
        "items": [{"item_order": 1}],
        "model": {},
        "tables": {},
    }


def _domain_problem():
    base = _problem()
    return ProblemIR(
        schema_version=base.schema_version,
        problem_id="sha256:solver.problem.v1:" + "4" * 64,
        problem_family=base.problem_family,
        decision_space=base.decision_space,
        parameters={**dict(base.parameters), "domain_evaluation": _domain_context()},
        constraints=base.constraints,
        objectives=base.objectives,
        limits=base.limits,
    )


def _binding():
    return {
        "endpoint": "https://solver.example.com",
        "service_credential_ref": "secret://solver/service-token",
        "allowed_families": {"binary_ordered_route": ["1.0"]},
        "neutral_limits": {
            "max_payload_bytes": 1048576,
            "max_solve_time_ms": 60000,
            "max_workers": 1,
        },
    }


def _capabilities():
    return {
        "api_versions": ["ortools.solver.v1"],
        "service": {
            "name": "ortools-solver-service",
            "version": "1.0.0",
            "build": "sha256:" + "3" * 64,
        },
        "engine": {"name": "ortools-cp-sat", "version": "9.15.0"},
        "execution": {
            "async_jobs": True,
            "cancellation": True,
            "prefer_wait": True,
            "job_retention_seconds": 600,
            "max_concurrent_jobs": 2,
        },
        "problem_types": [
            {
                "name": "binary_ordered_route",
                "versions": ["1.0"],
                "features": ["lexicographic_objective"],
                "result_assurances": ["feasible_solution", "best_bound", "gap"],
                "limits": {
                    "max_payload_bytes": 1048576,
                    "max_solve_time_ms": 60000,
                    "max_search_workers": 1,
                },
            }
        ],
    }


def test_capabilities_maps_service_shape_and_binds_connector_code_build():
    transport = _Transport()
    transport.responses.append(_capabilities())
    connector = OrToolsConnector(transport=transport)

    capabilities = connector.capabilities(binding=_binding())

    assert capabilities["protocol_versions"] == ["ortools.solver.v1"]
    assert capabilities["problem_families"][0]["limits"]["max_workers"] == 1
    assert capabilities["connector"]["build"].startswith("sha256:")
    assert capabilities["connector"]["build"] != ORTOOLS_CONNECTOR_CODE_BUILD
    assert capabilities["service"] == {
        "name": "ortools-solver-service",
        "version": "1.0.0",
        "build": "sha256:" + "3" * 64,
    }
    assert capabilities["engine"] == {
        "name": "ortools-cp-sat",
        "version": "9.15.0",
    }
    assert transport.calls[0]["path"] == "/api/v1/capabilities"
    assert connector.connector_type == "ortools"


def test_submit_emits_only_service_dto_and_locks_four_correlation_fields():
    request_id = OrToolsConnector.request_id("hmac-sha256:" + "5" * 64)
    transport = _Transport()
    transport.responses.append(
        {
            "api_version": "ortools.solver.v1",
            "job_id": "job_synthetic_0001",
            "request_id": request_id,
            "problem_type": "binary_ordered_route",
            "problem_version": "1.0",
            "status": "accepted",
        }
    )
    connector = OrToolsConnector(transport=transport)

    job = connector.submit(
        binding=_binding(),
        problem=_problem(),
        idempotency_key="hmac-sha256:" + "5" * 64,
        deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
        result_quality="optimal",
    )

    body = transport.calls[0]["json_payload"]
    assert transport.calls[0]["path"] == "/api/v1/jobs"
    assert set(body) == {"api_version", "request_id", "problem", "options"}
    assert body["api_version"] == "ortools.solver.v1"
    assert body["problem"]["type"] == "binary_ordered_route"
    assert body["problem"]["version"] == "1.0"
    assert body["options"]["search_workers"] == 1
    assert "result_quality" not in body
    assert "capabilities" not in body
    assert job == {"job_id": "job_synthetic_0001", "status": "accepted"}


def test_submit_domain_problem_rejects_service_without_domain_evaluation_capability():
    transport = _Transport()
    transport.responses.append(_capabilities())
    connector = OrToolsConnector(transport=transport)

    with pytest.raises(ConnectorContractError, match="domain_candidate_evaluation"):
        connector.submit(
            binding=_binding(),
            problem=_domain_problem(),
            idempotency_key="hmac-sha256:" + "a" * 64,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
        )

    assert [call["path"] for call in transport.calls] == ["/api/v1/capabilities"]


def test_submit_domain_problem_preserves_context_after_capability_check():
    capabilities = deepcopy(_capabilities())
    capabilities["problem_types"][0]["features"].append("domain_candidate_evaluation")
    request_id = OrToolsConnector.request_id("hmac-sha256:" + "b" * 64)
    transport = _Transport()
    transport.responses.extend(
        [
            capabilities,
            {
                "api_version": "ortools.solver.v1",
                "job_id": "job_synthetic_domain_0001",
                "request_id": request_id,
                "problem_type": "binary_ordered_route",
                "problem_version": "1.0",
                "status": "accepted",
            },
        ]
    )
    connector = OrToolsConnector(transport=transport)

    job = connector.submit(
        binding=_binding(),
        problem=_domain_problem(),
        idempotency_key="hmac-sha256:" + "b" * 64,
        deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
    )

    assert job["status"] == "accepted"
    assert [call["path"] for call in transport.calls] == [
        "/api/v1/capabilities",
        "/api/v1/jobs",
    ]
    submitted_context = transport.calls[1]["json_payload"]["problem"]["data"]["parameters"]["domain_evaluation"]
    assert submitted_context == _domain_context()


def test_submit_rejects_a_service_job_with_the_wrong_request_id():
    transport = _Transport()
    transport.responses.append(
        {
            "api_version": "ortools.solver.v1",
            "job_id": "job_synthetic_0009",
            "request_id": "req_" + "9" * 40,
            "problem_type": "binary_ordered_route",
            "problem_version": "1.0",
            "status": "accepted",
        }
    )
    connector = OrToolsConnector(transport=transport)

    with pytest.raises(ConnectorContractError):
        connector.submit(
            binding=_binding(),
            problem=_problem(),
            idempotency_key="hmac-sha256:" + "5" * 64,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
        )


def test_result_validates_all_locked_correlation_and_maps_solver_result():
    transport = _Transport()
    connector = OrToolsConnector(transport=transport)
    problem = _problem()
    request_id = connector.request_id("hmac-sha256:" + "6" * 64)
    transport.responses.extend(
        [
            {
                "api_version": "ortools.solver.v1",
                "job_id": "job_synthetic_0002",
                "request_id": request_id,
                "problem_type": "binary_ordered_route",
                "problem_version": "1.0",
                "status": "accepted",
            },
            {
                "api_version": "ortools.solver.v1",
                "job_id": "job_synthetic_0002",
                "request_id": request_id,
                "problem_type": "binary_ordered_route",
                "problem_version": "1.0",
                "solve_outcome": "optimal",
                "assignments": {"candidate_id": "CANDIDATE_SYNTHETIC_0001"},
                "objective_values": {"primary_score": 10},
                "assurance": {"best_bound": 10, "gap": "0", "certificate": None},
                "engine": {
                    "name": "ortools-cp-sat",
                    "version": "9.15.0",
                    "service_build": "sha256:" + "3" * 64,
                },
            },
        ]
    )
    connector.submit(
        binding=_binding(),
        problem=problem,
        idempotency_key="hmac-sha256:" + "6" * 64,
        deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
    )

    result = connector.result(binding=_binding(), job_id="job_synthetic_0002")

    assert result.problem_id == problem.problem_id
    assert result.status == "optimal"
    assert result.assignment["candidate_id"] == "CANDIDATE_SYNTHETIC_0001"
    assert result.engine["connector_build"] == ORTOOLS_CONNECTOR_CODE_BUILD
    assert result.engine["service_build"] == "sha256:" + "3" * 64
    assert transport.calls[-1]["path"] == "/api/v1/jobs/job_synthetic_0002/result"


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("job_id", "job_synthetic_other"),
        ("request_id", "req_" + "7" * 40),
        ("problem_type", "other_problem"),
        ("problem_version", "2.0"),
    ],
)
def test_result_rejects_each_correlation_mismatch(field, bad_value):
    transport = _Transport()
    connector = OrToolsConnector(transport=transport)
    problem = _problem()
    idempotency_key = "hmac-sha256:" + "8" * 64
    request_id = connector.request_id(idempotency_key)
    transport.responses.append(
        {
            "api_version": "ortools.solver.v1",
            "job_id": "job_synthetic_0003",
            "request_id": request_id,
            "problem_type": "binary_ordered_route",
            "problem_version": "1.0",
            "status": "accepted",
        }
    )
    connector.submit(
        binding=_binding(),
        problem=problem,
        idempotency_key=idempotency_key,
        deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    payload = {
        "api_version": "ortools.solver.v1",
        "job_id": "job_synthetic_0003",
        "request_id": request_id,
        "problem_type": "binary_ordered_route",
        "problem_version": "1.0",
        "solve_outcome": "optimal",
        "assignments": {},
        "objective_values": {},
        "assurance": {"best_bound": None, "gap": "0", "certificate": None},
        "engine": {
            "name": "ortools-cp-sat",
            "version": "9.15.0",
            "service_build": "sha256:" + "3" * 64,
        },
    }
    payload[field] = bad_value
    transport.responses.append(payload)

    with pytest.raises(ConnectorResultError):
        connector.result(binding=_binding(), job_id="job_synthetic_0003")
