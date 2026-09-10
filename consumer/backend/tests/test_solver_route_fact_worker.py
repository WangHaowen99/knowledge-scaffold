from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from integration.solver_route_fact_worker import (
    PackageSolverContractError,
    _candidate_assignments,
    _safety_margin,
    evaluate_payload,
)

from solvers.ortools.engine.refinery_route import evaluate_refinery_candidates


@dataclass
class _BatchPlan:
    batch_no: str
    crude_name: str
    input_weight_tons: float
    process_days: float
    vgo_route: str
    diesel_unit_no: str


class _EvaluatorModule:
    BatchPlan = _BatchPlan
    ATMOS_UNIT_ID = "ATMOS"
    DIESEL_UNIT_IDS = {"1#": "DIESEL_1", "2#": "DIESEL_2"}
    DEFAULT_DIESEL_UNIT_ID = "DIESEL_1"
    VGO_UNIT_ID = "VGO"
    RULE_DAILY_CAPACITIES = {
        "UNIT_ATMOS": 1000,
        "UNIT_DIESEL_1": 500,
        "UNIT_DIESEL_2": 500,
        "UNIT_VGO": 600,
    }
    SOLVER_CONTEXT = {
        "option_routes": {
            "OPTION_0": "SYNTHETIC_ROUTE_A",
            "OPTION_1": "SYNTHETIC_ROUTE_B",
        },
        "route_stream_products": ["ROUTE_STREAM"],
        "atmospheric_product_tanks": {},
        "special_tanks": {"diesel": "TANK_D", "vgo": "TANK_V"},
        "unit_capacity_keys": {
            "atmospheric": "UNIT_ATMOS",
            "diesel_by_unit": {
                "1#": "UNIT_DIESEL_1",
                "2#": "UNIT_DIESEL_2",
            },
            "vgo": "UNIT_VGO",
        },
        "correction_factor_prefix_by_unit": {
            "1#": "diesel_1",
            "2#": "diesel_2",
        },
        "production_condition": "NO_JET",
    }
    CORRECTION_FACTORS = {
        source: {
            "diesel_1_load_a": 1,
            "diesel_1_product_b": 1,
            "diesel_2_load_a": 1,
            "diesel_2_product_b": 1,
            "vgo_load_a": 1,
            "vgo_product_b": 1,
        }
        for source in ("SYNTHETIC_FEED_A", "SYNTHETIC_FEED_B")
    }
    YIELD_ATMOS = "yield_atmos"
    YIELD_DIESEL_WITH_VGO = "yield_diesel_with"
    YIELD_DIESEL_WITHOUT_VGO = "yield_diesel_without"
    YIELD_VGO_WITH_VGO = "yield_vgo_with"
    YIELD_VGO_WITHOUT_VGO = "yield_vgo_without"
    ATMOS_DIESEL_FEED = ("DIESEL_BASE",)
    ATMOS_HC_FEED = ("VGO_BASE",)
    VGO_INTERMEDIATE_NAMES = set()

    @staticmethod
    def normalize_crude_name(value: str) -> str:
        return value.replace(" ", "")

    @staticmethod
    def load_tables() -> dict[str, list[dict[str, Any]]]:
        sources = [
            {"OilYardID": "SOURCE_A", "CrudeOilYard Name": "SYNTHETIC_FEED_A"},
            {"OilYardID": "SOURCE_B", "CrudeOilYard Name": "SYNTHETIC_FEED_B"},
        ]
        atmospheric = []
        diesel_products = []
        vgo_products = []
        for source in sources:
            source_id = source["OilYardID"]
            atmospheric.append(
                {
                    "OilYardID": source_id,
                    "RefineryUnitID": "ATMOS",
                    "ProductName": "ROUTE_STREAM",
                    "yield_atmos": 100,
                }
            )
            diesel_products.append(
                {
                    "OilYardID": source_id,
                    "RefineryUnitID": "DIESEL_2",
                    "ProductName": "DIESEL_PRODUCT",
                    "LogicTankID": "TANK_PRODUCT_A",
                    "yield_diesel_with": 100,
                    "yield_diesel_without": 0,
                    "price_per_unit": 10 if source_id == "SOURCE_A" else 1,
                }
            )
            vgo_products.append(
                {
                    "OilYardID": source_id,
                    "VGOUnitID": "VGO",
                    "ProductName": "VGO_PRODUCT",
                    "LogicTankID": "TANK_PRODUCT_B",
                    "yield_vgo_with": 100,
                    "yield_vgo_without": 0,
                    "price_per_unit": 1 if source_id == "SOURCE_A" else 10,
                }
            )
        return {
            "crude_oil_yard": sources,
            "refinery_unit": [{"RefineryUnitID": "DIESEL_2", "price_per_unit": 0}],
            "vgo_ht_unit": [{"VGOUnitID": "VGO", "price_per_unit": 0}],
            "hydrogenation_product_yield": atmospheric + diesel_products,
            "vgo_product_yield": vgo_products,
            "logic_tank": [
                {
                    "LogicTankID": tank_id,
                    "AvailableCapacity_Tons": 100000,
                    "CurrentCapacity_Tons": 50000 if tank_id in {"TANK_D", "TANK_V"} else 0,
                    "LowSafetyThrd_Tons": 0,
                    "SafetyStockThrd_Tons": 100000,
                }
                for tank_id in ("TANK_D", "TANK_V", "TANK_PRODUCT_A", "TANK_PRODUCT_B")
            ],
            "energy_consume": [
                {
                    "RefineryUnitID": "DIESEL_2",
                    "UnitConsume_Value": 0,
                    "price_per_unit": 0,
                }
            ],
        }

    @staticmethod
    def build_report(
        _tables: dict[str, Any],
        plans: list[_BatchPlan],
        *,
        plan_month: str,
    ) -> dict[str, Any]:
        route_bonus = sum(1000 if plan.vgo_route == "SYNTHETIC_ROUTE_B" else 100 for plan in plans)
        month_capacity = 280000 if plan_month.endswith("-02") else 310000
        total_input = sum(plan.input_weight_tons for plan in plans)
        batches = [
            {
                "unit_loads": {
                    "unit": {
                        "input_tons": plan.input_weight_tons,
                        "capacity_tons": plan.input_weight_tons + 1000,
                    }
                },
                "tank_status": {
                    "1": {
                        "daily_input_tons": 1,
                        "available_capacity_tons": 10,
                    }
                },
            }
            for plan in plans
        ]
        special_batches = [
            {
                "TANK_D": {
                    "low_safety_threshold_tons": 0,
                    "safety_stock_threshold_tons": 100,
                    "end_capacity_tons": 50,
                },
                "TANK_V": {
                    "low_safety_threshold_tons": 0,
                    "safety_stock_threshold_tons": 100,
                    "end_capacity_tons": 50,
                },
            }
            for _plan in plans
        ]
        return {
            "cases": [
                {
                    "profit_yuan": total_input + route_bonus,
                    "pass_core_constraints": total_input <= month_capacity,
                    "batches": batches,
                    "monthly_unit_load": {
                        "units": {
                            "unit": {
                                "monthly_input_tons": total_input,
                                "monthly_capacity_tons": month_capacity,
                            }
                        }
                    },
                    "special_tank_status": {"batches": special_batches},
                }
            ]
        }


class _MissingOrdinaryTankModule(_EvaluatorModule):
    @staticmethod
    def load_tables() -> dict[str, list[dict[str, Any]]]:
        tables = _EvaluatorModule.load_tables()
        tables["logic_tank"] = [row for row in tables["logic_tank"] if row["LogicTankID"] in {"TANK_D", "TANK_V"}]
        return tables


class _MissingSpecialTankModule(_EvaluatorModule):
    @staticmethod
    def load_tables() -> dict[str, list[dict[str, Any]]]:
        tables = _EvaluatorModule.load_tables()
        tables["logic_tank"] = [row for row in tables["logic_tank"] if row["LogicTankID"] != "TANK_V"]
        return tables


def _payload(*, month: str, first_quantity: str) -> dict[str, Any]:
    item_count = 7
    candidates = [
        {"candidate_id": candidate_id, "assignments": list(assignments)}
        for candidate_id, assignments in _candidate_assignments(item_count)
    ]
    return {
        "normalized_input": {
            "plan_month": month,
            "diesel_unit_no": "2#",
            "items": [
                {
                    "item_order": index,
                    "item_id": "BATCH_{:03d}".format(index),
                    "crude_origin": "SYNTHETIC_FEED_A" if index % 2 else "SYNTHETIC_FEED_B",
                    "quantity_wan_tons": first_quantity if index == 1 else "1.000",
                    "process_days": "1.00",
                }
                for index in range(1, item_count + 1)
            ],
        },
        "candidates": candidates,
    }


def test_worker_evaluates_all_candidates_for_any_month() -> None:
    february = evaluate_payload(
        _EvaluatorModule,
        _payload(month="2099-02", first_quantity="2.500"),
    )
    march = evaluate_payload(
        _EvaluatorModule,
        _payload(month="2099-03", first_quantity="2.500"),
    )

    assert len(february["route_metrics"]["candidates"]) == 14
    assert len(march["route_metrics"]["candidates"]) == 14
    assert february["route_metrics"]["candidates"]["ALL_0"]["eligible"] is True
    assert march["route_metrics"]["candidates"]["ALL_0"]["eligible"] is True


def test_worker_emits_frozen_refinery_context_from_the_same_provider_read() -> None:
    result = evaluate_payload(
        _EvaluatorModule,
        _payload(month="2099-02", first_quantity="2.500"),
    )

    context = result["refinery_route_context"]
    assert context["schema_version"] == "refinery_route_context.v1"
    assert context["profile_id"] == "refinery_route.v1"
    assert context["plan_month"] == "2099-02"
    assert len(context["items"]) == 7
    assert context["items"][0] == {
        "item_id": "BATCH_001",
        "item_order": 1,
        "source_id": "SOURCE_A",
        "input_tons": "25000",
        "process_days": "1",
    }
    assert context["model"]["option_routes"] == {
        "OPTION_0": "diesel",
        "OPTION_1": "vgo",
    }
    assert {row["source_id"] for row in context["tables"]["atmospheric_yields"]} == {
        "SOURCE_A",
        "SOURCE_B",
    }
    assert "expected_candidate_id" not in str(context)


def test_february_seven_batch_context_runs_as_fourteen_solver_candidates() -> None:
    payload = _payload(month="2099-02", first_quantity="2.500")
    context = evaluate_payload(_EvaluatorModule, payload)["refinery_route_context"]
    item_ids = [item["item_id"] for item in context["items"]]
    candidates = [
        {
            "candidate_id": candidate["candidate_id"],
            "eligible": True,
            "assignment": dict(zip(item_ids, candidate["assignments"])),
            "metrics": {
                "primary_score": 0,
                "transition_count": 0,
                "safety_margin": 0,
                "candidate_rank": rank,
            },
        }
        for rank, candidate in enumerate(payload["candidates"])
    ]
    problem = {
        "schema_version": "solver.problem.v1",
        "problem_family": "binary_ordered_route",
        "family_version": "1.0",
        "payload": {
            "decision_space": {"variables": item_ids},
            "parameters": {
                "candidate_selection": {
                    "profile_id": "ordered_binary_route.v1",
                    "candidate_count": len(candidates),
                    "candidate_evaluation_digest": ("sha256:solver-coordination.v1:" + "0" * 64),
                    "candidates": candidates,
                },
                "domain_evaluation": context,
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

    evaluated, certificate = evaluate_refinery_candidates(problem)

    assert len(evaluated) == 14
    assert certificate["candidate_count"] == 14
    assert certificate["calendar_days"] == 28


def test_worker_preserves_legacy_untracked_ordinary_tank_semantics() -> None:
    result = evaluate_payload(
        _MissingOrdinaryTankModule,
        _payload(month="2099-02", first_quantity="2.500"),
    )

    context = result["refinery_route_context"]
    assert {row["tank_id"] for row in context["tables"]["unit_product_yields"]} == {None}
    assert {row["tank_id"] for row in context["tables"]["tanks"]} == {
        "TANK_D",
        "TANK_V",
    }


def test_worker_rejects_a_missing_required_special_tank() -> None:
    with pytest.raises(ValueError, match="special tank"):
        evaluate_payload(
            _MissingSpecialTankModule,
            _payload(month="2099-02", first_quantity="2.500"),
        )


def test_worker_classifies_a_missing_solver_context_as_package_contract_incompatible() -> None:
    with pytest.raises(PackageSolverContractError):
        evaluate_payload(
            object(),
            _payload(month="2099-02", first_quantity="2.500"),
        )


def test_worker_recomputes_different_inputs_in_the_same_month() -> None:
    first = evaluate_payload(
        _EvaluatorModule,
        _payload(month="2099-02", first_quantity="2.500"),
    )
    second = evaluate_payload(
        _EvaluatorModule,
        _payload(month="2099-02", first_quantity="8.750"),
    )

    first_metrics = first["route_metrics"]["candidates"]["ALL_0"]
    second_metrics = second["route_metrics"]["candidates"]["ALL_0"]
    assert first_metrics["primary_score"] != second_metrics["primary_score"]
    assert first_metrics["eligible"] is True
    assert second_metrics["eligible"] is True


def test_worker_excludes_advisory_batch_load_from_safety_margin() -> None:
    margin = _safety_margin(
        {
            "constraint_policy": {
                "batch_unit_load": "advisory",
                "monthly_unit_load": "hard",
                "tank_capacity": "hard",
            },
            "batches": [
                {
                    "unit_loads": {"unit": {"input_tons": 20, "capacity_tons": 10}},
                    "tank_status": {
                        "1": {
                            "daily_input_tons": 1,
                            "available_capacity_tons": 10,
                        }
                    },
                }
            ],
            "monthly_unit_load": {
                "units": {
                    "unit": {
                        "monthly_input_tons": 5,
                        "monthly_capacity_tons": 10,
                    }
                }
            },
            "special_tank_status": {
                "batches": [
                    {
                        "TANK_D": {
                            "low_safety_threshold_tons": 0,
                            "safety_stock_threshold_tons": 100,
                            "end_capacity_tons": 50,
                        },
                        "TANK_V": {
                            "low_safety_threshold_tons": 0,
                            "safety_stock_threshold_tons": 100,
                            "end_capacity_tons": 50,
                        },
                    }
                ]
            },
        }
    )

    assert margin == 50


def test_worker_accepts_batch_identifier_in_special_tank_status() -> None:
    margin = _safety_margin(
        {
            "constraint_policy": {
                "batch_unit_load": "advisory",
                "monthly_unit_load": "hard",
                "tank_capacity": "hard",
            },
            "batches": [
                {
                    "unit_loads": {},
                    "tank_status": {},
                }
            ],
            "monthly_unit_load": {"units": {}},
            "special_tank_status": {
                "batches": [
                    {
                        "batch_no": "1",
                        "TANK_D": {
                            "low_safety_threshold_tons": 0,
                            "safety_stock_threshold_tons": 100,
                            "end_capacity_tons": 50,
                        },
                        "TANK_V": {
                            "low_safety_threshold_tons": 0,
                            "safety_stock_threshold_tons": 100,
                            "end_capacity_tons": 50,
                        },
                    }
                ]
            },
        }
    )

    assert margin == 50
