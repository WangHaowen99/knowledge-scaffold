from __future__ import annotations

import pytest
from solver_coordination import (
    FactSnapshotBuilder,
    NormalizedInput,
    Option,
    OrderedPlan,
    PlanItem,
    SolverContractError,
)
from solver_coordination.problem_families.binary_ordered_route import BinaryOrderedRouteAdapter
from solver_coordination.problem_families.registry import (
    ProblemFamilyAlreadyRegistered,
    ProblemFamilyNotFound,
    ProblemFamilyRegistry,
)


class _Adapter:
    family_key = "synthetic_family.v1"


def _plan() -> OrderedPlan:
    return OrderedPlan(
        items=(
            PlanItem(item_order=1, item_id="item-synthetic-1", quantity="1", duration="1"),
            PlanItem(item_order=2, item_id="item-synthetic-2", quantity="2", duration="1"),
        ),
        option_domain=(Option.OPTION_0, Option.OPTION_1),
        max_transitions=1,
    )


def _normalized_input() -> NormalizedInput:
    return NormalizedInput(
        operation_key="solve_synthetic_route",
        values={"item_count": 2},
    )


def _fact_snapshot():
    return FactSnapshotBuilder().build(
        workspace_id="workspace-synthetic",
        owner_id="owner-synthetic",
        package_id="package-synthetic",
        package_revision="revision-synthetic",
        normalized_input=_normalized_input(),
        facts=[{"name": "limit", "value": "10"}],
        bindings={"provider_binding_revision": "binding-synthetic"},
        as_of="2026-08-17T00:00:00Z",
        required_facts=("limit",),
    )


def test_registry_registers_and_resolves_families_without_concrete_type_dispatch():
    adapter = _Adapter()
    registry = ProblemFamilyRegistry()
    registry.register(adapter)
    assert registry.get("synthetic_family.v1") is adapter
    assert registry.families() == ("synthetic_family.v1",)
    with pytest.raises(ProblemFamilyAlreadyRegistered):
        registry.register(adapter)
    with pytest.raises(ProblemFamilyNotFound):
        registry.get("missing_family.v1")


def test_binary_ordered_route_is_exposed_as_a_registry_adapter_and_preserves_candidate_api():
    adapter = BinaryOrderedRouteAdapter()
    registry = ProblemFamilyRegistry([adapter])
    candidate_set = registry.enumerate(
        "binary_ordered_route.v1",
        plan=_plan(),
        source_run_id="run-synthetic",
        knowledge_package_id="package-synthetic",
        knowledge_package_revision="revision-synthetic",
    )
    assert candidate_set.problem_family == "binary_ordered_route.v1"
    assert candidate_set.candidate_count == 4
    assert [candidate.candidate_id for candidate in candidate_set.candidates] == [
        "ALL_0",
        "ALL_1",
        "OPTION_0_TO_OPTION_1_K0001",
        "OPTION_1_TO_OPTION_0_K0001",
    ]


@pytest.mark.parametrize(
    "damaged_field",
    ["decision_space", "constraints", "objectives", "limits"],
)
def test_binary_adapter_rejects_damaged_decision_model_sections(damaged_field):
    model = {
        "required_facts": ["limit"],
        "decision_space": {
            "variables": ["choice"],
            "option_domain": ["OPTION_0", "OPTION_1"],
        },
        "constraints": [{"kind": "max_transitions", "value": 1}],
        "objectives": [{"name": "cost", "direction": "min"}],
        "limits": {"max_candidates": 4},
    }
    model[damaged_field] = None

    with pytest.raises(SolverContractError, match=damaged_field):
        BinaryOrderedRouteAdapter().compile_problem(
            capability={"decision_model": model},
            normalized_input=_normalized_input(),
            fact_snapshot=_fact_snapshot(),
        )


@pytest.mark.parametrize(
    "decision_space",
    [
        {},
        {"choices": []},
        {"variables": [], "option_domain": ["OPTION_0", "OPTION_1"]},
        {"variables": ["choice"], "option_domain": []},
    ],
)
def test_binary_adapter_rejects_an_empty_or_malformed_binary_decision_space(
    decision_space,
):
    model = {
        "required_facts": ["limit"],
        "decision_space": decision_space,
        "constraints": [],
        "objectives": [],
        "limits": {},
    }

    with pytest.raises(SolverContractError, match="decision_space|decision space|domain|variables"):
        BinaryOrderedRouteAdapter().compile_problem(
            capability={"decision_model": model},
            normalized_input=_normalized_input(),
            fact_snapshot=_fact_snapshot(),
        )


@pytest.mark.parametrize(
    "decision_space",
    [
        {"choices": ["OPTION_0", "OPTION_0"]},
        {"choices": ["OPTION_0", "OPTION_1"], "variables": ["choice"]},
        {
            "variables": ["choice", "choice"],
            "option_domain": ["OPTION_0", "OPTION_1"],
        },
        {
            "variables": ["choice"],
            "domains": {
                "choice": ["OPTION_0", "OPTION_1"],
                "unexpected": ["OPTION_0", "OPTION_1"],
            },
        },
        {
            "variables": [
                {
                    "name": "choice",
                    "domain": ["OPTION_0", "OPTION_1"],
                    "extra": "unsupported",
                }
            ]
        },
    ],
)
def test_binary_adapter_rejects_ambiguous_or_non_binary_decision_space_shape(
    decision_space,
):
    model = {
        "required_facts": ["limit"],
        "decision_space": decision_space,
        "constraints": [],
        "objectives": [],
        "limits": {},
    }

    with pytest.raises(SolverContractError, match="decision_space|decision space|domain|variables"):
        BinaryOrderedRouteAdapter().compile_problem(
            capability={"decision_model": model},
            normalized_input=_normalized_input(),
            fact_snapshot=_fact_snapshot(),
        )


def test_task2_public_surface_is_available_from_solver_coordination_package():
    from solver_coordination import (
        FactSnapshotBuilder,
        ProblemFamilyRegistry,
        ProblemIRCompiler,
        SolverInputBinder,
    )

    assert SolverInputBinder is not None
    assert FactSnapshotBuilder is not None
    assert ProblemIRCompiler is not None
    assert ProblemFamilyRegistry is not None


def test_legacy_candidate_service_dispatches_through_injected_registry():
    from solver_coordination.candidate_service import create_candidate_set

    class _SpyRegistry:
        def enumerate(self, family_key, **kwargs):
            assert family_key == "binary_ordered_route.v1"
            return BinaryOrderedRouteAdapter().enumerate(**kwargs)

    candidate_set = create_candidate_set(
        plan_payload={
            "items": [{"item_order": 1, "item_id": "item-synthetic-1", "quantity": "1", "duration": "1"}],
            "option_domain": ["OPTION_0", "OPTION_1"],
            "max_transitions": 1,
        },
        knowledge_package_id="package-synthetic",
        knowledge_package_revision="revision-synthetic",
        source_run_id="run-synthetic",
        registry=_SpyRegistry(),
    )
    assert candidate_set.candidate_count == 2


def test_legacy_candidate_service_does_not_import_a_concrete_problem_family():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "solver_coordination" / "candidate_service.py").read_text(
        encoding="utf-8"
    )
    assert "problem_families.binary_ordered_route import" not in source
