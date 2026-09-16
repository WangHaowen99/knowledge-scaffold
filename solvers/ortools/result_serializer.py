# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Serialize engine output into the public ``ortools.solver.v1`` Result DTO."""

from __future__ import annotations

from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from .engine.candidate_selection import EngineResult


def serialize_result(
    *,
    job_id: str,
    request_id: str,
    problem: Mapping[str, Any],
    engine_result: EngineResult,
    service_build: str,
) -> dict[str, Any]:
    if problem.get("type") != "binary_ordered_route" or problem.get("version") != "1.0":
        raise ValueError("unsupported problem type")
    return {
        "api_version": "ortools.solver.v1",
        "job_id": job_id,
        "request_id": request_id,
        "problem_type": "binary_ordered_route",
        "problem_version": "1.0",
        "solve_outcome": engine_result.solve_outcome,
        "assignments": dict(engine_result.assignments),
        "objective_values": dict(engine_result.objective_values),
        "assurance": {
            "best_bound": engine_result.best_bound,
            "gap": engine_result.gap,
            "certificate": engine_result.certificate,
        },
        "engine": {
            "name": engine_result.engine_name,
            "version": engine_result.engine_version,
            "service_build": service_build,
        },
    }


__all__ = ["serialize_result"]
