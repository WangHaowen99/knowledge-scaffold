# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

import hashlib
import json

from solvers.ortools.app import create_app
from solvers.ortools.config import OrToolsSolverConfig
from contracts.solver_services.ortools.validation import validate_document


_ITEM_IDS = tuple("ITEM_SYNTHETIC_{:04d}".format(index) for index in range(1, 10))


def _configuration(tmp_path) -> OrToolsSolverConfig:
    return OrToolsSolverConfig(
        database_path=tmp_path / "attempts.sqlite3",
        attempt_ttl_seconds=3600,
        max_payload_bytes=1024 * 1024,
        max_solve_time_ms=30_000,
        max_search_workers=1,
        max_concurrent_jobs=1,
        memory_limit_bytes=512 * 1024 * 1024,
        file_limit_bytes=1024 * 1024,
        prefer_wait_max_ms=5_000,
        service_token=None,
        enforce_os_limits=False,
    )


def _assignments() -> tuple[tuple[str, dict[str, str]], ...]:
    values: list[tuple[str, dict[str, str]]] = [
        ("ALL_0", {item_id: "OPTION_0" for item_id in _ITEM_IDS}),
        ("ALL_1", {item_id: "OPTION_1" for item_id in _ITEM_IDS}),
    ]
    for boundary in range(1, len(_ITEM_IDS)):
        values.append(
            (
                "OPTION_0_TO_OPTION_1_K{:04d}".format(boundary),
                {
                    item_id: "OPTION_0" if index <= boundary else "OPTION_1"
                    for index, item_id in enumerate(_ITEM_IDS, start=1)
                },
            )
        )
    for boundary in range(1, len(_ITEM_IDS)):
        values.append(
            (
                "OPTION_1_TO_OPTION_0_K{:04d}".format(boundary),
                {
                    item_id: "OPTION_1" if index <= boundary else "OPTION_0"
                    for index, item_id in enumerate(_ITEM_IDS, start=1)
                },
            )
        )
    return tuple(values)


def _request() -> dict:
    candidates = []
    for rank, (candidate_id, assignment) in enumerate(_assignments()):
        # Fully synthetic locked metrics: OPTION_0 is preferred for the first
        # four items and OPTION_1 for the final five. The service receives only
        # these neutral pre-evaluated integers, never customer data or labels.
        score = 0
        for index, item_id in enumerate(_ITEM_IDS, start=1):
            preferred_option = "OPTION_0" if index <= 4 else "OPTION_1"
            if assignment[item_id] == preferred_option:
                score += 100
        transition_count = 0
        assignment_values = tuple(assignment.values())
        for left, right in zip(assignment_values, assignment_values[1:]):
            if left != right:
                transition_count += 1
        candidates.append(
            {
                "candidate_id": candidate_id,
                "eligible": True,
                "assignment": assignment,
                "metrics": {
                    "primary_score": score,
                    "transition_count": transition_count,
                    "safety_margin": 100,
                    "candidate_rank": rank,
                },
            }
        )
    digest = "sha256:solver-coordination.v1:" + hashlib.sha256(
        json.dumps(
            candidates,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "api_version": "ortools.solver.v1",
        "request_id": "req_SYNTHETIC_NINE_ITEM_0001",
        "problem": {
            "type": "binary_ordered_route",
            "version": "1.0",
            "data": {
                "decision_space": {"variables": list(_ITEM_IDS)},
                "parameters": {
                    "candidate_selection": {
                        "profile_id": "synthetic_nine_item.v1",
                        "candidate_count": len(candidates),
                        "candidate_evaluation_digest": digest,
                        "candidates": candidates,
                    }
                },
                "constraints": [{"kind": "select_exactly_one_candidate"}],
                "objectives": [
                    {"metric": "primary_score", "sense": "maximize"},
                    {"metric": "transition_count", "sense": "minimize"},
                    {"metric": "safety_margin", "sense": "maximize"},
                    {"metric": "candidate_rank", "sense": "minimize"},
                ],
                "limits": {
                    "max_candidates": 128,
                    "max_verification_candidates": 128,
                },
            },
        },
        "options": {"time_limit_ms": 5_000, "search_workers": 1},
    }


def test_nine_item_synthetic_job_selects_the_fourth_ordered_boundary(tmp_path):
    app = create_app(_configuration(tmp_path))
    service = app.extensions["ortools_solver"]
    client = app.test_client()
    try:
        submitted = client.post(
            "/api/v1/jobs",
            json=_request(),
            headers={
                "Idempotency-Key": "synthetic-nine-item-key-0001",
                "Prefer": "wait=5000",
            },
        )
        assert submitted.status_code == 202
        job = submitted.get_json()
        validate_document("job", job)
        assert job["status"] == "completed"

        response = client.get("/api/v1/jobs/{}/result".format(job["job_id"]))
        assert response.status_code == 200
        result = response.get_json()
        validate_document("result", result)
        assert result["solve_outcome"] == "optimal"
        assert result["assignments"]["candidate_id"] == (
            "OPTION_0_TO_OPTION_1_K0004"
        )
        assert [result["assignments"][item_id] for item_id in _ITEM_IDS] == [
            "OPTION_0",
            "OPTION_0",
            "OPTION_0",
            "OPTION_0",
            "OPTION_1",
            "OPTION_1",
            "OPTION_1",
            "OPTION_1",
            "OPTION_1",
        ]
    finally:
        service.close()
