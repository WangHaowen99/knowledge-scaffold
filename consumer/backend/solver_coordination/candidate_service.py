"""Application service for deterministic neutral candidate-set generation."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from .contracts import Option, OrderedPlan, PlanItem, SolverContractError
from .problem_families import build_default_problem_family_registry


def _reject_unknown_fields(payload: Mapping[str, Any], allowed: set[str], *, scope: str) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise SolverContractError("{} contains unsupported fields".format(scope))


def build_ordered_plan(payload: Mapping[str, Any]) -> OrderedPlan:
    if not isinstance(payload, Mapping):
        raise SolverContractError("plan must be an object")
    _reject_unknown_fields(
        payload,
        {"items", "option_domain", "max_transitions"},
        scope="plan",
    )
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise SolverContractError("items must be a non-empty array")
    raw_options = payload.get("option_domain")
    if not isinstance(raw_options, list):
        raise SolverContractError("option_domain must be an array")
    items = []
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, Mapping):
            raise SolverContractError("items[{}] must be an object".format(index - 1))
        _reject_unknown_fields(
            raw_item,
            {"item_order", "item_id", "quantity", "duration"},
            scope="items[{}]".format(index - 1),
        )
        supplied_order = raw_item.get("item_order")
        if supplied_order != index:
            raise SolverContractError("item_order values must be ordered and contiguous")
        items.append(
            PlanItem(
                item_order=supplied_order,
                item_id=raw_item.get("item_id"),
                quantity=raw_item.get("quantity"),
                duration=raw_item.get("duration"),
            )
        )
    try:
        option_domain = tuple(Option(value) for value in raw_options)
    except (TypeError, ValueError) as exc:
        raise SolverContractError("option_domain contains an unsupported option") from exc
    return OrderedPlan(
        items=tuple(items),
        option_domain=option_domain,
        max_transitions=payload.get("max_transitions"),
    )


def create_candidate_set(
    *,
    plan_payload: Mapping[str, Any],
    knowledge_package_id: str,
    knowledge_package_revision: str,
    source_run_id: str | None = None,
    registry: object | None = None,
):
    if not isinstance(plan_payload, Mapping):
        raise SolverContractError("plan must be an object")
    plan = build_ordered_plan(plan_payload)
    run_id = source_run_id or "solve_{}".format(uuid.uuid4().hex)
    selected_registry = registry or build_default_problem_family_registry()
    return selected_registry.enumerate(
        "binary_ordered_route.v1",
        plan=plan,
        source_run_id=run_id,
        knowledge_package_id=knowledge_package_id,
        knowledge_package_revision=knowledge_package_revision,
    )
