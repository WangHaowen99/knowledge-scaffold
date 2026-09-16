# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from copy import deepcopy

import pytest

from solvers.ortools.engine.refinery_route import (
    RefineryRouteContextError,
    evaluate_refinery_candidates,
    solve_refinery_ordered_route,
)


ITEM_IDS = tuple("ITEM_SYNTHETIC_{:04d}".format(index) for index in range(1, 5))


def _context(*, month: str = "2099-03") -> dict:
    items = [
        {
            "item_id": item_id,
            "item_order": index,
            "source_id": "SOURCE_A" if index <= 2 else "SOURCE_B",
            "input_tons": "100.00",
            "process_days": "1.00",
        }
        for index, item_id in enumerate(ITEM_IDS, start=1)
    ]
    atmospheric_yields = [
        {
            "source_id": source_id,
            "product_id": "ROUTE_STREAM",
            "feed_role": "route_stream",
            "yield_percent": "100.00",
            "tank_id": None,
        }
        for source_id in ("SOURCE_A", "SOURCE_B")
    ]
    unit_product_yields = []
    for source_id, diesel_price, vgo_price in (
        ("SOURCE_A", "10.00", "1.00"),
        ("SOURCE_B", "1.00", "10.00"),
    ):
        unit_product_yields.extend(
            [
                {
                    "source_id": source_id,
                    "unit_role": "diesel",
                    "product_id": "DIESEL_PRODUCT",
                    "tank_id": "TANK_DIESEL_PRODUCT",
                    "yield_with_route_percent": "100.00",
                    "yield_without_route_percent": "0.00",
                    "price_per_ton": diesel_price,
                    "feeds_vgo": False,
                },
                {
                    "source_id": source_id,
                    "unit_role": "vgo",
                    "product_id": "VGO_PRODUCT",
                    "tank_id": "TANK_VGO_PRODUCT",
                    "yield_with_route_percent": "100.00",
                    "yield_without_route_percent": "0.00",
                    "price_per_ton": vgo_price,
                    "feeds_vgo": False,
                },
            ]
        )
    return {
        "schema_version": "refinery_route_context.v1",
        "profile_id": "refinery_route.v1",
        "metric_scale": 100,
        "rounding": "ROUND_HALF_EVEN",
        "plan_month": month,
        "production_condition": "NO_JET",
        "items": items,
        "model": {
            "option_routes": {"OPTION_0": "diesel", "OPTION_1": "vgo"},
            "special_tanks": {"diesel": "TANK_DIESEL", "vgo": "TANK_VGO"},
            "constraint_policy": {
                "batch_unit_load": "advisory",
                "monthly_unit_load": "hard",
                "tank_capacity": "hard",
            },
            "correction_factors": {
                source_id: {
                    "diesel_load": "1.00",
                    "diesel_product": "1.00",
                    "vgo_load": "1.00",
                    "vgo_product": "1.00",
                }
                for source_id in ("SOURCE_A", "SOURCE_B")
            },
        },
        "tables": {
            "atmospheric_yields": atmospheric_yields,
            "unit_product_yields": unit_product_yields,
            "units": [
                {
                    "unit_role": "atmospheric",
                    "daily_capacity_tons": "1000.00",
                    "feed_cost_per_ton": "0.00",
                },
                {
                    "unit_role": "diesel",
                    "daily_capacity_tons": "1000.00",
                    "feed_cost_per_ton": "0.00",
                },
                {
                    "unit_role": "vgo",
                    "daily_capacity_tons": "1000.00",
                    "feed_cost_per_ton": "0.00",
                },
            ],
            "tanks": [
                {
                    "tank_id": "TANK_DIESEL",
                    "kind": "special",
                    "available_capacity_tons": "100000.00",
                    "current_inventory_tons": "50000.00",
                    "minimum_inventory_tons": "0.00",
                    "maximum_inventory_tons": "100000.00",
                },
                {
                    "tank_id": "TANK_VGO",
                    "kind": "special",
                    "available_capacity_tons": "100000.00",
                    "current_inventory_tons": "50000.00",
                    "minimum_inventory_tons": "0.00",
                    "maximum_inventory_tons": "100000.00",
                },
                {
                    "tank_id": "TANK_DIESEL_PRODUCT",
                    "kind": "ordinary",
                    "available_capacity_tons": "100000.00",
                    "current_inventory_tons": "0.00",
                    "minimum_inventory_tons": "0.00",
                    "maximum_inventory_tons": "100000.00",
                },
                {
                    "tank_id": "TANK_VGO_PRODUCT",
                    "kind": "ordinary",
                    "available_capacity_tons": "100000.00",
                    "current_inventory_tons": "0.00",
                    "minimum_inventory_tons": "0.00",
                    "maximum_inventory_tons": "100000.00",
                },
            ],
            "energy_costs": [],
        },
    }


def _candidate_assignments() -> tuple[tuple[str, tuple[str, ...]], ...]:
    assignments = [
        ("ALL_0", ("OPTION_0",) * len(ITEM_IDS)),
        ("ALL_1", ("OPTION_1",) * len(ITEM_IDS)),
    ]
    assignments.extend(
        (
            "OPTION_0_TO_OPTION_1_K{:04d}".format(boundary),
            ("OPTION_0",) * boundary + ("OPTION_1",) * (len(ITEM_IDS) - boundary),
        )
        for boundary in range(1, len(ITEM_IDS))
    )
    assignments.extend(
        (
            "OPTION_1_TO_OPTION_0_K{:04d}".format(boundary),
            ("OPTION_1",) * boundary + ("OPTION_0",) * (len(ITEM_IDS) - boundary),
        )
        for boundary in range(1, len(ITEM_IDS))
    )
    return tuple(assignments)


def _problem() -> dict:
    candidates = []
    for rank, (candidate_id, options) in enumerate(_candidate_assignments()):
        candidates.append(
            {
                "candidate_id": candidate_id,
                "eligible": True,
                "assignment": dict(zip(ITEM_IDS, options)),
                # Deliberately wrong: the generic input claims ALL_1 is best.
                "metrics": {
                    "primary_score": 999999 if candidate_id == "ALL_1" else 0,
                    "transition_count": int(len(set(options)) > 1),
                    "safety_margin": 0,
                    "candidate_rank": rank,
                },
            }
        )
    return {
        "schema_version": "solver.problem.v1",
        "problem_family": "binary_ordered_route",
        "family_version": "1.0",
        "payload": {
            "decision_space": {"variables": list(ITEM_IDS)},
            "parameters": {
                "candidate_selection": {
                    "profile_id": "ordered_binary_route.v1",
                    "candidate_count": len(candidates),
                    "candidate_evaluation_digest": "sha256:" + "0" * 64,
                    "candidates": candidates,
                },
                "domain_evaluation": _context(),
            },
            "constraints": [{"kind": "select_exactly_one_candidate"}],
            "objectives": [
                {"metric": "primary_score", "sense": "maximize"},
                {"metric": "transition_count", "sense": "minimize"},
                {"metric": "safety_margin", "sense": "maximize"},
                {"metric": "candidate_rank", "sense": "minimize"},
            ],
            "limits": {"max_candidates": 128},
        },
    }


def test_domain_evaluator_generates_two_n_candidates_and_profit_metrics() -> None:
    candidates, certificate = evaluate_refinery_candidates(_problem())

    assert len(candidates) == 2 * len(ITEM_IDS)
    assert [candidate["candidate_id"] for candidate in candidates] == [
        candidate_id for candidate_id, _assignment in _candidate_assignments()
    ]
    winner = next(
        candidate
        for candidate in candidates
        if candidate["candidate_id"] == "OPTION_0_TO_OPTION_1_K0002"
    )
    assert winner["eligible"] is True
    assert winner["metrics"]["primary_score"] == 400000
    assert certificate["evaluation_source"] == "refinery_route.v1"
    assert certificate["candidate_count"] == 8
    assert certificate["candidate_digest"].startswith(
        "sha256:solver-coordination.v1:"
    )


def test_domain_solver_ignores_poisoned_precalculated_business_metrics() -> None:
    result = solve_refinery_ordered_route(
        _problem(), solve_time_ms=5000, workers=1
    )

    assert result.solve_outcome == "optimal"
    assert result.assignments["candidate_id"] == "OPTION_0_TO_OPTION_1_K0002"
    assert result.objective_values["primary_score"] == 400000
    assert result.certificate["evaluation_source"] == "refinery_route.v1"


def test_monthly_capacity_is_hard_but_batch_capacity_is_advisory() -> None:
    problem = _problem()
    context = problem["payload"]["parameters"]["domain_evaluation"]
    for unit in context["tables"]["units"]:
        if unit["unit_role"] == "diesel":
            unit["daily_capacity_tons"] = "7.50"

    candidates, certificate = evaluate_refinery_candidates(problem)

    # Each diesel-routed batch is above 7.5 tons/day, but the March monthly
    # capacity is 232.5 tons. Two diesel batches remain monthly-feasible.
    boundary = next(
        candidate
        for candidate in candidates
        if candidate["candidate_id"] == "OPTION_0_TO_OPTION_1_K0002"
    )
    all_diesel = next(candidate for candidate in candidates if candidate["candidate_id"] == "ALL_0")
    assert boundary["eligible"] is True
    assert all_diesel["eligible"] is False
    all_diesel_diagnostics = next(
        candidate
        for candidate in certificate["candidates"]
        if candidate["candidate_id"] == "ALL_0"
    )
    assert "monthly_unit_capacity_exceeded" in all_diesel_diagnostics["rejection_codes"]


def test_context_rejects_unsupported_policy_instead_of_approximating_it() -> None:
    problem = _problem()
    problem["payload"]["parameters"]["domain_evaluation"][
        "production_condition"
    ] = "JET_MN"

    with pytest.raises(RefineryRouteContextError, match="production condition"):
        evaluate_refinery_candidates(problem)


def test_context_uses_calendar_month_capacity_for_other_batch_counts() -> None:
    problem = _problem()
    context = deepcopy(problem["payload"]["parameters"]["domain_evaluation"])
    context["plan_month"] = "2099-02"
    context["items"] = context["items"][:3]
    problem["payload"]["parameters"]["domain_evaluation"] = context
    item_ids = ITEM_IDS[:3]
    candidates = []
    assignments = [
        ("ALL_0", ("OPTION_0",) * 3),
        ("ALL_1", ("OPTION_1",) * 3),
        ("OPTION_0_TO_OPTION_1_K0001", ("OPTION_0", "OPTION_1", "OPTION_1")),
        ("OPTION_0_TO_OPTION_1_K0002", ("OPTION_0", "OPTION_0", "OPTION_1")),
        ("OPTION_1_TO_OPTION_0_K0001", ("OPTION_1", "OPTION_0", "OPTION_0")),
        ("OPTION_1_TO_OPTION_0_K0002", ("OPTION_1", "OPTION_1", "OPTION_0")),
    ]
    for rank, (candidate_id, options) in enumerate(assignments):
        candidates.append(
            {
                "candidate_id": candidate_id,
                "eligible": True,
                "assignment": dict(zip(item_ids, options)),
                "metrics": {
                    "primary_score": 0,
                    "transition_count": int(len(set(options)) > 1),
                    "safety_margin": 0,
                    "candidate_rank": rank,
                },
            }
        )
    problem["payload"]["parameters"]["candidate_selection"]["candidates"] = candidates
    problem["payload"]["parameters"]["candidate_selection"]["candidate_count"] = 6

    evaluated, certificate = evaluate_refinery_candidates(problem)

    assert len(evaluated) == 6
    assert certificate["calendar_days"] == 28
