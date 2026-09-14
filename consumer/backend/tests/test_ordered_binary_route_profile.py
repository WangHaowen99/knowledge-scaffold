# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest
from solver_coordination.canonical import sha256_digest
from solver_coordination.contracts import (
    MAX_DECIMAL_DIGITS,
    MAX_DECIMAL_EXPONENT,
    MAX_TEXT_LENGTH,
    FactSnapshot,
    NormalizedInput,
    SolverContractError,
    _freeze_object,
)
from solver_coordination.fact_snapshot import FactSnapshotInvalidError
from solver_coordination.problem_families.candidate_selection import INT64_MAX, INT64_MIN
from solver_coordination.profiles import (
    LockedModelFactProvider,
    OrderedBinaryRouteCompiler,
    OrderedBinaryRouteEvaluator,
    scaled_int,
)

ROUTE_FACT = {
    "metric_scale": 100,
    "rounding": "ROUND_HALF_EVEN",
    "capacity_limit": "10.00",
    "items": {
        "ITEM_SYNTHETIC_0001": {
            "OPTION_0": {"primary_score": "3.00", "load": "1.00", "safety": "4.00"},
            "OPTION_1": {"primary_score": "1.00", "load": "1.20", "safety": "3.00"},
        },
        "ITEM_SYNTHETIC_0002": {
            "OPTION_0": {"primary_score": "3.00", "load": "1.00", "safety": "4.00"},
            "OPTION_1": {"primary_score": "1.00", "load": "1.20", "safety": "3.00"},
        },
        "ITEM_SYNTHETIC_0003": {
            "OPTION_0": {"primary_score": "1.00", "load": "1.20", "safety": "3.00"},
            "OPTION_1": {"primary_score": "4.00", "load": "1.00", "safety": "4.00"},
        },
        "ITEM_SYNTHETIC_0004": {
            "OPTION_0": {"primary_score": "1.00", "load": "1.20", "safety": "3.00"},
            "OPTION_1": {"primary_score": "4.00", "load": "1.00", "safety": "4.00"},
        },
    },
}


def _candidate_route_fact(item_count: int = 4) -> dict[str, Any]:
    candidate_ids = ["ALL_0", "ALL_1"]
    candidate_ids.extend("OPTION_0_TO_OPTION_1_K{:04d}".format(index) for index in range(1, item_count))
    candidate_ids.extend("OPTION_1_TO_OPTION_0_K{:04d}".format(index) for index in range(1, item_count))
    candidates = {
        candidate_id: {
            "eligible": True,
            "primary_score": "1.00",
            "safety_margin": "2.00",
        }
        for candidate_id in candidate_ids
    }
    candidates["OPTION_0_TO_OPTION_1_K0002"]["primary_score"] = "100.00"
    candidates["ALL_1"]["eligible"] = False
    return {
        "metric_scale": 100,
        "rounding": "ROUND_HALF_EVEN",
        "candidates": candidates,
    }


OBJECTIVES = [
    {"metric": "primary_score", "sense": "maximize"},
    {"metric": "transition_count", "sense": "minimize"},
    {"metric": "safety_margin", "sense": "maximize"},
    {"metric": "candidate_rank", "sense": "minimize"},
]


def _normalized_input(
    *,
    item_count: int = 4,
    items: list[dict[str, Any]] | None = None,
) -> NormalizedInput:
    return NormalizedInput(
        operation_key="solve_synthetic_route",
        values={
            "items": (
                items
                if items is not None
                else [
                    {"item_order": index, "item_id": f"ITEM_SYNTHETIC_{index:04d}"}
                    for index in range(1, item_count + 1)
                ]
            )
        },
    )


def _snapshot(
    normalized_input: NormalizedInput,
    *,
    fact_value: Any = None,
) -> FactSnapshot:
    if fact_value is None:
        fact_value = ROUTE_FACT
    return FactSnapshot(
        workspace_id="workspace-synthetic",
        owner_id="owner-synthetic",
        package_id="package-synthetic",
        package_revision="revision-synthetic",
        normalized_input_digest=normalized_input.digest,
        facts=(
            {
                "name": "route_metrics",
                "value": fact_value,
            },
        ),
        bindings={"solver_binding_revision": "binding-synthetic"},
        as_of="2026-08-18T00:00:00+00:00",
        checks={
            "required": ("route_metrics",),
            "missing": (),
            "duplicates": (),
            "conflicts": (),
        },
    )


def _snapshot_with_domain_context(normalized_input: NormalizedInput, *, input_tons: str = "1") -> FactSnapshot:
    context = {
        "schema_version": "refinery_route_context.v1",
        "profile_id": "refinery_route.v1",
        "metric_scale": 100,
        "rounding": "ROUND_HALF_EVEN",
        "plan_month": "2099-03",
        "production_condition": "NO_JET",
        "items": [
            {
                "item_id": "ITEM_SYNTHETIC_{:04d}".format(index),
                "item_order": index,
                "source_id": "SOURCE_SYNTHETIC",
                "input_tons": input_tons,
                "process_days": "1",
            }
            for index in range(1, 5)
        ],
        "model": {},
        "tables": {},
    }
    return FactSnapshot(
        workspace_id="workspace-synthetic",
        owner_id="owner-synthetic",
        package_id="package-synthetic",
        package_revision="revision-synthetic",
        normalized_input_digest=normalized_input.digest,
        facts=(
            {"name": "route_metrics", "value": ROUTE_FACT},
            {"name": "refinery_route_context", "value": context},
        ),
        bindings={"solver_binding_revision": "binding-synthetic"},
        as_of="2099-03-01T00:00:00+00:00",
        checks={
            "required": ("route_metrics", "refinery_route_context"),
            "missing": (),
            "duplicates": (),
            "conflicts": (),
        },
    )


def _capability(
    *,
    objectives: list[dict[str, str]] | None = None,
    model_updates: dict[str, Any] | None = None,
) -> dict[str, object]:
    model: dict[str, Any] = {
        "profile_id": "ordered_binary_route.v1",
        "objectives": deepcopy(OBJECTIVES if objectives is None else objectives),
    }
    model.update(model_updates or {})
    return {
        "decision_model": model,
    }


def _best_candidate_id(compiled: dict[str, object]) -> str:
    selection = compiled["parameters"]["candidate_selection"]
    candidates = [candidate for candidate in selection["candidates"] if candidate["eligible"]]
    best = min(
        candidates,
        key=lambda candidate: (
            -candidate["metrics"]["primary_score"],
            candidate["metrics"]["transition_count"],
            -candidate["metrics"]["safety_margin"],
            candidate["metrics"]["candidate_rank"],
        ),
    )
    return best["candidate_id"]


def test_four_item_profile_compiles_canonical_candidate_selection():
    normalized_input = _normalized_input()
    compiled = OrderedBinaryRouteCompiler().compile_problem(
        capability=_capability(),
        normalized_input=normalized_input,
        fact_snapshot=_snapshot(normalized_input),
    )

    selection = compiled["parameters"]["candidate_selection"]
    assert selection["candidate_count"] == 8
    assert _best_candidate_id(compiled) == "OPTION_0_TO_OPTION_1_K0002"
    assert selection["profile_id"] == "ordered_binary_route.v1"
    assert selection["candidate_evaluation_digest"] == sha256_digest(selection["candidates"])
    assert [candidate["metrics"]["candidate_rank"] for candidate in selection["candidates"]] == list(range(8))
    assert compiled["objectives"] == OBJECTIVES
    assert compiled["limits"] == {
        "max_candidates": 128,
        "max_verification_candidates": 128,
    }


def test_profile_passes_frozen_domain_context_to_the_standalone_service():
    normalized_input = _normalized_input()
    snapshot = _snapshot_with_domain_context(normalized_input)

    compiled = OrderedBinaryRouteCompiler().compile_problem(
        capability=_capability(),
        normalized_input=normalized_input,
        fact_snapshot=snapshot,
    )

    expected = next(fact["value"] for fact in snapshot.facts if fact["name"] == "refinery_route_context")
    assert compiled["parameters"]["domain_evaluation"] == expected


def test_profile_rejects_domain_context_quantity_that_diverges_from_input():
    normalized_input = _normalized_input()

    with pytest.raises(SolverContractError, match="quantity diverged"):
        OrderedBinaryRouteCompiler().compile_problem(
            capability=_capability(),
            normalized_input=normalized_input,
            fact_snapshot=_snapshot_with_domain_context(normalized_input, input_tons="2"),
        )


def test_profile_compiles_complete_candidate_metrics_from_package_evaluation():
    normalized_input = _normalized_input()

    compiled = OrderedBinaryRouteCompiler().compile_problem(
        capability=_capability(),
        normalized_input=normalized_input,
        fact_snapshot=_snapshot(
            normalized_input,
            fact_value=_candidate_route_fact(),
        ),
    )

    selection = compiled["parameters"]["candidate_selection"]
    candidates = selection["candidates"]
    assert selection["candidate_count"] == 8
    assert _best_candidate_id(compiled) == "OPTION_0_TO_OPTION_1_K0002"
    assert candidates[1]["candidate_id"] == "ALL_1"
    assert candidates[1]["eligible"] is False
    assert candidates[3]["metrics"] == {
        "primary_score": 10000,
        "transition_count": 1,
        "safety_margin": 200,
        "candidate_rank": 3,
        "total_load": 0,
    }


def test_profile_rejects_incomplete_candidate_metrics_snapshot():
    normalized_input = _normalized_input()
    fact = _candidate_route_fact()
    fact["candidates"].pop("OPTION_1_TO_OPTION_0_K0003")

    with pytest.raises(SolverContractError, match="candidate"):
        OrderedBinaryRouteCompiler().compile_problem(
            capability=_capability(),
            normalized_input=normalized_input,
            fact_snapshot=_snapshot(normalized_input, fact_value=fact),
        )


def test_profile_prefers_business_quantity_and_duration_over_redundant_agent_fields():
    items = [
        {
            "item_order": index,
            "item_id": f"ITEM_SYNTHETIC_{index:04d}",
            "quantity_wan_tons": str(index),
            "process_days": "1.5",
            "quantity": str(index),
            "duration": "1.5天",
        }
        for index in range(1, 5)
    ]
    normalized_input = _normalized_input(items=items)

    compiled = OrderedBinaryRouteCompiler().compile_problem(
        capability=_capability(),
        normalized_input=normalized_input,
        fact_snapshot=_snapshot(normalized_input),
    )

    assert compiled["parameters"]["candidate_selection"]["candidate_count"] == 8
    assert _best_candidate_id(compiled) == "OPTION_0_TO_OPTION_1_K0002"


def test_same_size_fact_set_selects_a_different_candidate():
    fact = deepcopy(ROUTE_FACT)
    for item in fact["items"].values():
        item["OPTION_0"]["primary_score"] = "1.00"
        item["OPTION_1"]["primary_score"] = "5.00"
    normalized_input = _normalized_input()

    compiled = OrderedBinaryRouteCompiler().compile_problem(
        capability=_capability(),
        normalized_input=normalized_input,
        fact_snapshot=_snapshot(normalized_input, fact_value=fact),
    )

    assert _best_candidate_id(compiled) == "ALL_1"


def test_option_metric_mapping_order_does_not_change_compilation():
    normalized_input = _normalized_input()
    baseline_snapshot = _snapshot(normalized_input)
    reordered_fact = deepcopy(ROUTE_FACT)
    item_metrics = reordered_fact["items"]["ITEM_SYNTHETIC_0001"]
    reordered_fact["items"]["ITEM_SYNTHETIC_0001"] = {
        "OPTION_1": item_metrics["OPTION_1"],
        "OPTION_0": item_metrics["OPTION_0"],
    }
    reordered_snapshot = _snapshot(normalized_input, fact_value=reordered_fact)

    assert reordered_snapshot.digest == baseline_snapshot.digest

    compiler = OrderedBinaryRouteCompiler()
    baseline = compiler.compile_problem(
        capability=_capability(),
        normalized_input=normalized_input,
        fact_snapshot=baseline_snapshot,
    )
    reordered = compiler.compile_problem(
        capability=_capability(),
        normalized_input=normalized_input,
        fact_snapshot=reordered_snapshot,
    )

    assert reordered["parameters"]["candidate_selection"] == baseline["parameters"]["candidate_selection"]


def test_every_candidate_metric_is_an_int64_and_rank_is_canonical():
    normalized_input = _normalized_input()
    compiled = OrderedBinaryRouteCompiler().compile_problem(
        capability=_capability(),
        normalized_input=normalized_input,
        fact_snapshot=_snapshot(normalized_input),
    )

    candidates = compiled["parameters"]["candidate_selection"]["candidates"]
    for index, candidate in enumerate(candidates):
        assert candidate["metrics"]["candidate_rank"] == index
        assert all(
            isinstance(value, int) and not isinstance(value, bool) and INT64_MIN <= value <= INT64_MAX
            for value in candidate["metrics"].values()
        )


def test_profile_compiler_marks_capacity_rejections_without_hiding_candidates():
    fact = deepcopy(ROUTE_FACT)
    fact["capacity_limit"] = "4.10"
    normalized_input = _normalized_input()

    compiler = OrderedBinaryRouteCompiler()
    compiled = compiler.compile_problem(
        capability=_capability(),
        normalized_input=normalized_input,
        fact_snapshot=_snapshot(normalized_input, fact_value=fact),
    )
    evaluations = OrderedBinaryRouteEvaluator().evaluate_candidates(
        normalized_input=normalized_input,
        fact_snapshot=_snapshot(normalized_input, fact_value=fact),
    )

    candidates = compiled["parameters"]["candidate_selection"]["candidates"]
    assert len(candidates) == 8
    assert any(candidate["eligible"] is False for candidate in candidates)
    assert {evaluation.rejection_codes for evaluation in evaluations} <= {(), ("capacity_exceeded",)}
    assert {evaluation.rejection_codes for evaluation in evaluations} == {
        (),
        ("capacity_exceeded",),
    }
    assert all(set(candidate) == {"candidate_id", "eligible", "assignment", "metrics"} for candidate in candidates)


def test_profile_compiler_uses_round_half_even():
    fact = deepcopy(ROUTE_FACT)
    fact["metric_scale"] = 10
    fact["items"]["ITEM_SYNTHETIC_0001"]["OPTION_0"]["primary_score"] = "1.25"
    normalized_input = _normalized_input()

    compiled = OrderedBinaryRouteCompiler().compile_problem(
        capability=_capability(),
        normalized_input=normalized_input,
        fact_snapshot=_snapshot(normalized_input, fact_value=fact),
    )

    all_zero = compiled["parameters"]["candidate_selection"]["candidates"][0]
    assert all_zero["metrics"]["primary_score"] == 62
    assert scaled_int("1.25", scale=10, field_name="synthetic") == 12
    assert scaled_int("1.35", scale=10, field_name="synthetic") == 14


def test_scaled_int_accepts_negative_half_even_and_int64_boundaries():
    assert scaled_int("-1.25", scale=10, field_name="synthetic") == -12
    assert scaled_int("-1.35", scale=10, field_name="synthetic") == -14
    assert scaled_int(str(INT64_MIN), scale=1, field_name="synthetic") == INT64_MIN
    assert scaled_int(str(INT64_MAX), scale=1, field_name="synthetic") == INT64_MAX


@pytest.mark.parametrize(
    "value",
    [
        "1" * (MAX_DECIMAL_DIGITS + 1),
        "1" * (MAX_TEXT_LENGTH + 1),
        "1e{}".format(MAX_DECIMAL_EXPONENT + 1),
        "1e1000000000",
        Decimal("1e{}".format(MAX_DECIMAL_EXPONENT + 1)),
    ],
)
def test_scaled_int_rejects_oversized_decimal_inputs_before_scaling(value):
    with pytest.raises(SolverContractError, match="precision|exponent|length|decimal"):
        scaled_int(value, scale=1, field_name="synthetic")


def test_default_compilers_do_not_share_an_evaluator_instance():
    first = OrderedBinaryRouteCompiler()
    second = OrderedBinaryRouteCompiler()

    assert first.evaluator is not second.evaluator


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda fact: fact["items"]["ITEM_SYNTHETIC_0001"]["OPTION_0"].pop("load"), "load"),
        (lambda fact: fact.__setitem__("rounding", "ROUND_UP"), "rounding"),
        (lambda fact: fact["items"]["ITEM_SYNTHETIC_0001"]["OPTION_0"].__setitem__("primary_score", "NaN"), "finite"),
        (
            lambda fact: fact["items"]["ITEM_SYNTHETIC_0001"]["OPTION_0"].__setitem__("primary_score", "Infinity"),
            "finite",
        ),
        (lambda fact: fact.__setitem__("metric_scale", INT64_MAX + 1), "metric_scale"),
        (
            lambda fact: fact["items"]["ITEM_SYNTHETIC_0001"]["OPTION_0"].__setitem__("primary_score", str(INT64_MAX)),
            "int64",
        ),
    ],
)
def test_profile_compiler_rejects_malformed_or_overflowing_facts(mutation, message: str):
    fact = deepcopy(ROUTE_FACT)
    mutation(fact)
    normalized_input = _normalized_input()

    with pytest.raises(SolverContractError, match=message):
        OrderedBinaryRouteCompiler().compile_problem(
            capability=_capability(),
            normalized_input=normalized_input,
            fact_snapshot=_snapshot(normalized_input, fact_value=fact),
        )


def test_profile_compiler_rejects_aggregate_int64_overflow():
    fact = deepcopy(ROUTE_FACT)
    fact["metric_scale"] = 1
    value = str(INT64_MAX // 4 + 1)
    for item in fact["items"].values():
        for option in item.values():
            option["primary_score"] = value
    normalized_input = _normalized_input()

    with pytest.raises(SolverContractError, match="primary_score.*int64"):
        OrderedBinaryRouteCompiler().compile_problem(
            capability=_capability(),
            normalized_input=normalized_input,
            fact_snapshot=_snapshot(normalized_input, fact_value=fact),
        )


def test_profile_compiler_rejects_capacity_subtraction_overflow():
    fact = deepcopy(ROUTE_FACT)
    fact["metric_scale"] = 1
    fact["capacity_limit"] = str(INT64_MIN)
    for item in fact["items"].values():
        for option in item.values():
            option["load"] = "1"
    normalized_input = _normalized_input()

    with pytest.raises(SolverContractError, match="safety_margin.*int64"):
        OrderedBinaryRouteCompiler().compile_problem(
            capability=_capability(),
            normalized_input=normalized_input,
            fact_snapshot=_snapshot(normalized_input, fact_value=fact),
        )


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ([], "at least one"),
        ([{"item_order": index, "item_id": f"ITEM_SYNTHETIC_{index:04d}"} for index in range(1, 66)], "at most 64"),
        (
            [{"item_order": 1, "item_id": "ITEM_SYNTHETIC_0001"}, {"item_order": 1, "item_id": "ITEM_SYNTHETIC_0002"}],
            "contiguous",
        ),
        (
            [{"item_order": 1, "item_id": "ITEM_SYNTHETIC_0001"}, {"item_order": 2, "item_id": "ITEM_SYNTHETIC_0001"}],
            "unique",
        ),
    ],
)
def test_profile_compiler_rejects_invalid_ordered_items(items, message: str):
    normalized_input = _normalized_input(items=items)

    with pytest.raises(SolverContractError, match=message):
        OrderedBinaryRouteCompiler().compile_problem(
            capability=_capability(),
            normalized_input=normalized_input,
            fact_snapshot=_snapshot(normalized_input),
        )


def test_profile_compiler_rejects_a_missing_item_fact():
    fact = deepcopy(ROUTE_FACT)
    fact["items"].pop("ITEM_SYNTHETIC_0004")
    normalized_input = _normalized_input()

    with pytest.raises(SolverContractError, match="ITEM_SYNTHETIC_0004"):
        OrderedBinaryRouteCompiler().compile_problem(
            capability=_capability(),
            normalized_input=normalized_input,
            fact_snapshot=_snapshot(normalized_input, fact_value=fact),
        )


def test_profile_compiler_rejects_the_wrong_locked_objective_order():
    normalized_input = _normalized_input()
    objectives = deepcopy(OBJECTIVES)
    objectives[0], objectives[1] = objectives[1], objectives[0]

    with pytest.raises(SolverContractError, match="objective"):
        OrderedBinaryRouteCompiler().compile_problem(
            capability=_capability(objectives=objectives),
            normalized_input=normalized_input,
            fact_snapshot=_snapshot(normalized_input),
        )


def test_locked_model_fact_provider_returns_materialized_facts_and_binding():
    normalized_input = _normalized_input()
    capability = _capability(
        model_updates={
            "fact_source": {"kind": "locked_model"},
            "materialized_facts": [{"name": "route_metrics", "value": ROUTE_FACT}],
        }
    )

    resolved = LockedModelFactProvider().resolve_facts(
        capability=capability,
        normalized_input=normalized_input,
        required_facts=("route_metrics",),
        binding_revision_id="binding-synthetic",
    )

    assert resolved["facts"] == capability["decision_model"]["materialized_facts"]
    assert resolved["bindings"] == {"solver_binding_revision": "binding-synthetic"}
    assert resolved["source_evidence"] == ({"kind": "locked_model_materialized_facts"},)


def test_locked_model_fact_provider_accepts_only_trusted_frozen_tuple_facts():
    normalized_input = _normalized_input()
    raw_model = _capability(
        model_updates={
            "fact_source": {"kind": "locked_model"},
            "materialized_facts": [{"name": "route_metrics", "value": ROUTE_FACT}],
        }
    )["decision_model"]
    frozen_model = _freeze_object(raw_model, "decision model")

    resolved = LockedModelFactProvider().resolve_facts(
        capability={"decision_model": frozen_model},
        normalized_input=normalized_input,
        required_facts=("route_metrics",),
        binding_revision_id="binding-synthetic",
    )

    assert isinstance(frozen_model["materialized_facts"], tuple)
    assert resolved["facts"] == [{"name": "route_metrics", "value": ROUTE_FACT}]


@pytest.mark.parametrize(
    "model_updates",
    [
        {},
        {"fact_source": {"kind": "live_provider"}, "materialized_facts": []},
        {"fact_source": {"kind": "locked_model"}, "materialized_facts": {}},
        {"fact_source": {"kind": "locked_model"}, "materialized_facts": ()},
    ],
)
def test_locked_model_fact_provider_fails_closed_for_unapproved_sources(model_updates):
    with pytest.raises(FactSnapshotInvalidError, match="facts are unavailable|facts are invalid"):
        LockedModelFactProvider().resolve_facts(
            capability=_capability(model_updates=model_updates),
            normalized_input=_normalized_input(),
            required_facts=("route_metrics",),
            binding_revision_id="binding-synthetic",
        )
