# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Profile-scoped presentation of already verified solver results."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from .candidate_results import ordered_binary_combination_no
from .contracts import (
    NormalizedInput,
    SolverContractError,
    SolverResult,
    VerificationResult,
)
from .errors import SolverResultRejectedError
from .profiles.registry import ProfileNotFound


_OPTIONS = ("OPTION_0", "OPTION_1")
_PROFILE_ID = "ordered_binary_route.v1"
_TEXT_PRESENTATION_KIND = "ordered_route_plan_zh.v1"
_TEXT_PRESENTATION_FIELDS = {"kind", "process_name", "stream_name"}
_PLAN_MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_POSITIVE_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


class OrderedBinaryRoutePresenter:
    """Map opaque option identifiers to approved display labels after verification."""

    def present(
        self,
        *,
        capability: Any,
        normalized_input: NormalizedInput,
        solver_result: SolverResult,
        verification: VerificationResult,
    ) -> Mapping[str, Any]:
        self._require_verified(
            solver_result=solver_result,
            verification=verification,
        )
        if not isinstance(normalized_input, NormalizedInput):
            raise SolverResultRejectedError("normalized input is invalid")

        model = _value(capability, "decision_model")
        if not isinstance(model, Mapping):
            raise SolverResultRejectedError("locked decision model is missing")
        profile_id = model.get("profile_id")
        if profile_id != _PROFILE_ID:
            raise SolverResultRejectedError("presentation profile is unsupported")

        display_mapping = model.get("display_mapping")
        if not isinstance(display_mapping, Mapping) or set(display_mapping) != set(_OPTIONS):
            raise SolverResultRejectedError("display mapping keys are invalid")
        labels: dict[str, str] = {}
        for option in _OPTIONS:
            label = display_mapping.get(option)
            if not isinstance(label, str) or not label.strip():
                raise SolverResultRejectedError("display mapping label is invalid")
            labels[option] = label.strip()

        item_ids = _ordered_item_ids(normalized_input)
        assignment = solver_result.assignment
        expected_keys = {"candidate_id", *item_ids}
        if set(assignment) != expected_keys:
            raise SolverResultRejectedError("solver assignment item keys are invalid")
        candidate_id = assignment.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise SolverResultRejectedError("solver candidate_id is invalid")
        candidate_id = candidate_id.strip()
        try:
            combination_no = ordered_binary_combination_no(candidate_id, len(item_ids))
        except SolverContractError as exc:
            raise SolverResultRejectedError("solver candidate_id is unsupported") from exc

        item_assignment: dict[str, str] = {}
        items: list[dict[str, str]] = []
        for item_id in item_ids:
            option = assignment.get(item_id)
            if option not in labels:
                raise SolverResultRejectedError("solver assignment option is invalid")
            item_assignment[item_id] = option
            items.append({"item_id": item_id, "route": labels[option]})

        summary: dict[str, Any] = {
            "candidate_id": candidate_id,
            "combination_no": combination_no,
            "assignment": item_assignment,
            "items": items,
            "objective_values": dict(solver_result.objective_values),
        }
        if "text_presentation" in model:
            summary.update(
                _render_ordered_route_plan_zh(
                    configuration=model.get("text_presentation"),
                    normalized_input=normalized_input,
                    routes=tuple(item["route"] for item in items),
                )
            )
        return summary

    @staticmethod
    def _require_verified(
        *,
        solver_result: SolverResult,
        verification: VerificationResult,
    ) -> None:
        if not isinstance(solver_result, SolverResult):
            raise SolverResultRejectedError("solver result is invalid")
        if not isinstance(verification, VerificationResult):
            raise SolverResultRejectedError("verification result is required before presentation")
        if verification.problem_id != solver_result.problem_id:
            raise SolverResultRejectedError("verification identity does not match solver result")
        if verification.status != "verified":
            raise SolverResultRejectedError("presentation requires a verified result")
        if verification.feasibility != "feasible":
            raise SolverResultRejectedError("infeasible results have no presentation assignment")
        if verification.optimality not in {"not_proven", "proven_optimal"}:
            raise SolverResultRejectedError("verification optimality is invalid")


class ProfilePresentationService:
    """Dispatch presentation through the profile selected by the locked model."""

    def __init__(self, profile_registry: Any) -> None:
        if not callable(getattr(profile_registry, "get", None)):
            raise SolverResultRejectedError("profile registry is unavailable")
        self.profile_registry = profile_registry

    def present(
        self,
        *,
        capability: Any,
        normalized_input: NormalizedInput,
        solver_result: SolverResult,
        verification: VerificationResult,
    ) -> Mapping[str, Any]:
        model = _value(capability, "decision_model")
        if not isinstance(model, Mapping):
            raise SolverResultRejectedError("locked decision model is missing")
        profile_id = model.get("profile_id")
        if (
            not isinstance(profile_id, str)
            or not profile_id
            or profile_id != profile_id.strip()
        ):
            raise SolverResultRejectedError("presentation profile_id is noncanonical")
        try:
            registration = self.profile_registry.get(profile_id)
        except ProfileNotFound as exc:
            raise SolverResultRejectedError("presentation profile is unavailable") from exc
        except Exception as exc:
            raise SolverResultRejectedError("presentation profile lookup failed") from exc
        presenter = getattr(registration, "presenter", None)
        if not callable(getattr(presenter, "present", None)):
            raise SolverResultRejectedError("presentation profile has no presenter")
        try:
            summary = presenter.present(
                capability=capability,
                normalized_input=normalized_input,
                solver_result=solver_result,
                verification=verification,
            )
        except Exception as exc:
            if isinstance(exc, SolverResultRejectedError):
                raise
            raise SolverResultRejectedError("profile presentation failed") from exc
        if not isinstance(summary, Mapping):
            raise SolverResultRejectedError("profile presentation returned invalid data")
        return dict(summary)


def _ordered_item_ids(normalized_input: NormalizedInput) -> tuple[str, ...]:
    raw_items = normalized_input.values.get("items")
    if not isinstance(raw_items, (list, tuple)) or not raw_items:
        raise SolverResultRejectedError("normalized input items are invalid")
    item_ids: list[str] = []
    for expected_order, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, Mapping) or raw_item.get("item_order") != expected_order:
            raise SolverResultRejectedError("normalized input item order is invalid")
        item_id = raw_item.get("item_id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise SolverResultRejectedError("normalized input item_id is invalid")
        item_id = item_id.strip()
        if item_id in item_ids:
            raise SolverResultRejectedError("normalized input item IDs are duplicated")
        item_ids.append(item_id)
    return tuple(item_ids)


def _render_ordered_route_plan_zh(
    *,
    configuration: Any,
    normalized_input: NormalizedInput,
    routes: tuple[str, ...],
) -> dict[str, Any]:
    if (
        not isinstance(configuration, Mapping)
        or set(configuration) != _TEXT_PRESENTATION_FIELDS
        or configuration.get("kind") != _TEXT_PRESENTATION_KIND
    ):
        raise SolverResultRejectedError("text presentation configuration is invalid")
    process_name = _clean_display_text(
        configuration.get("process_name"), "text presentation process_name"
    )
    stream_name = _clean_display_text(
        configuration.get("stream_name"), "text presentation stream_name"
    )
    unit = _clean_display_text(
        normalized_input.values.get("diesel_unit_no"),
        "text presentation diesel_unit_no",
    )
    plan_month = normalized_input.values.get("plan_month")
    if not isinstance(plan_month, str) or _PLAN_MONTH_RE.fullmatch(plan_month) is None:
        raise SolverResultRejectedError("text presentation plan_month is invalid")
    month = int(plan_month[-2:])

    raw_items = normalized_input.values.get("items")
    if not isinstance(raw_items, (list, tuple)) or len(raw_items) != len(routes):
        raise SolverResultRejectedError("text presentation items are invalid")
    lines: list[str] = []
    for expected_order, (raw_item, route) in enumerate(zip(raw_items, routes), start=1):
        if not isinstance(raw_item, Mapping) or raw_item.get("item_order") != expected_order:
            raise SolverResultRejectedError("text presentation item order is invalid")
        origin = _clean_display_text(
            raw_item.get("crude_origin"), "text presentation crude_origin"
        )
        quantity = _positive_decimal_text(
            raw_item.get("quantity_wan_tons"),
            "text presentation quantity_wan_tons",
        )
        days = _positive_decimal_text(
            raw_item.get("process_days"),
            "text presentation process_days",
            trim_fractional_zeros=True,
        )
        route_label = _clean_display_text(route, "text presentation route")
        lines.append(
            "第 {} 批：{}，{} 万吨，周期为 {} 天，{}→{}；".format(
                expected_order,
                origin,
                quantity,
                days,
                stream_name,
                route_label,
            )
        )

    title = "{}{}的{}月度生产规划如下：".format(unit, process_name, month)
    return {"title": title, "lines": lines, "text": "  \n".join([title, *lines])}


def _clean_display_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise SolverResultRejectedError("{} is invalid".format(field_name))
    if not value or value != value.strip():
        raise SolverResultRejectedError("{} is invalid".format(field_name))
    if len(value) > 128:
        raise SolverResultRejectedError("{} is invalid".format(field_name))
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise SolverResultRejectedError("{} is invalid".format(field_name))
    return value


def _positive_decimal_text(
    value: Any, field_name: str, *, trim_fractional_zeros: bool = False
) -> str:
    if not isinstance(value, str) or _POSITIVE_DECIMAL_RE.fullmatch(value) is None:
        raise SolverResultRejectedError("{} is invalid".format(field_name))
    try:
        decimal_value = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise SolverResultRejectedError("{} is invalid".format(field_name)) from exc
    if not decimal_value.is_finite() or decimal_value <= 0:
        raise SolverResultRejectedError("{} is invalid".format(field_name))
    if trim_fractional_zeros and "." in value:
        return value.rstrip("0").rstrip(".")
    return value


def _value(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


__all__ = [
    "OrderedBinaryRoutePresenter",
    "ProfilePresentationService",
]
