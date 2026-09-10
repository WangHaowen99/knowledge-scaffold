"""Locked-fact evaluation for the ordered binary route solver profile."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, localcontext
from types import MappingProxyType
from typing import Any

from solver_coordination.canonical import sha256_digest
from solver_coordination.contracts import (
    MAX_DECIMAL_DIGITS,
    MAX_DECIMAL_EXPONENT,
    MAX_PLAN_ITEMS,
    MAX_TEXT_LENGTH,
    MIN_DECIMAL_EXPONENT,
    Candidate,
    FactSnapshot,
    NormalizedInput,
    Option,
    OrderedPlan,
    PlanItem,
    SolverContractError,
)
from solver_coordination.fact_snapshot import FactSnapshotInvalidError
from solver_coordination.problem_families.binary_ordered_route import (
    enumerate_ordered_routes,
)
from solver_coordination.problem_families.candidate_selection import (
    INT64_MAX,
    INT64_MIN,
    validate_candidate_selection_components,
)
from solver_coordination.profiles.input_adapter import InputAliasConflict

PROFILE_ID = "ordered_binary_route.v1"
ROUTE_FACT_NAME = "route_metrics"
DOMAIN_CONTEXT_FACT_NAME = "refinery_route_context"
_ROUNDING_MODE = "ROUND_HALF_EVEN"
_OPTION_NAMES = (Option.OPTION_0.value, Option.OPTION_1.value)
_ROUTE_ITEM_FACT_FIELDS = {"metric_scale", "rounding", "capacity_limit", "items"}
_ROUTE_CANDIDATE_FACT_FIELDS = {"metric_scale", "rounding", "candidates"}
_OPTION_METRIC_FIELDS = {"primary_score", "load", "safety"}
_CANDIDATE_METRIC_FIELDS = {"eligible", "primary_score", "safety_margin"}
_DECIMAL_INPUT_RE = re.compile(
    r"^[+-]?(?:(?P<integer>[0-9]+)(?:\.(?P<fraction>[0-9]*))?"
    r"|\.(?P<fraction_only>[0-9]+))(?:[eE](?P<exponent>[+-]?[0-9]+))?$"
)
LOCKED_OBJECTIVES = (
    MappingProxyType({"metric": "primary_score", "sense": "maximize"}),
    MappingProxyType({"metric": "transition_count", "sense": "minimize"}),
    MappingProxyType({"metric": "safety_margin", "sense": "maximize"}),
    MappingProxyType({"metric": "candidate_rank", "sense": "minimize"}),
)

_INPUT_ALIAS_MAPPING = MappingProxyType(
    {
        "items[].duration": "items[].process_days",
        "items[].quantity": "items[].quantity_wan_tons",
        "items[].source": "items[].crude_origin",
    }
)
_INPUT_ALIAS_FIELDS = frozenset(
    {
        "quantity",
        "quantity_wan_tons",
        "input_weight_tons",
        "duration",
        "process_days",
        "source",
        "crude_origin",
    }
)

_DECIMAL_ALIAS_UNITS = MappingProxyType(
    {
        "duration": "天",
        "quantity": "万吨",
    }
)


@dataclass(frozen=True)
class OrderedBinaryRouteInputAdapter:
    adapter_id: str = "ordered_binary_route.input.v1"
    revision: str = "2"

    def adapt(
        self,
        *,
        operation_key: str,
        values: Mapping[str, Any],
        normalization: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        del operation_key
        raw_items = values.get("items")
        if not isinstance(raw_items, (list, tuple)):
            raise SolverContractError("ordered route input items are invalid")
        items = []
        conflicts: list[Mapping[str, str]] = []
        for index, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, Mapping):
                raise SolverContractError("ordered route input item is invalid")
            item: dict[str, Any] = {}
            for key, value in raw_item.items():
                if key in _INPUT_ALIAS_FIELDS:
                    continue
                item[str(key)] = value
            self._decimal_alias(
                raw_item,
                item,
                index=index,
                alias="quantity",
                canonical="quantity_wan_tons",
                conflicts=conflicts,
            )
            self._decimal_alias(
                raw_item,
                item,
                index=index,
                alias="duration",
                canonical="process_days",
                conflicts=conflicts,
            )
            self._text_alias(
                raw_item,
                item,
                index=index,
                alias="source",
                canonical="crude_origin",
                conflicts=conflicts,
            )
            items.append(item)
        if conflicts:
            raise InputAliasConflict(tuple(conflicts))

        canonical_values: dict[str, Any] = {}
        for key, value in values.items():
            if key != "items":
                canonical_values[str(key)] = value
        canonical_values["items"] = items
        canonical_normalization = dict(normalization)
        canonical_normalization.update(
            {
                "input_adapter_id": self.adapter_id,
                "input_adapter_revision": self.revision,
                "alias_mapping": dict(_INPUT_ALIAS_MAPPING),
                "unit_conversions": [
                    {
                        "field": "items[].quantity_wan_tons",
                        "from": "wan_tons",
                        "to": "wan_tons",
                    }
                ],
                "conflict_checks": [
                    "items[].quantity_wan_tons",
                    "items[].process_days",
                    "items[].crude_origin",
                ],
            }
        )
        return {
            "values": canonical_values,
            "normalization": canonical_normalization,
        }

    @staticmethod
    def _decimal_alias(
        source: Mapping[str, Any],
        target: dict[str, Any],
        *,
        index: int,
        alias: str,
        canonical: str,
        conflicts: list[Mapping[str, str]],
    ) -> None:
        alias_value = source.get(alias)
        canonical_value = source.get(canonical)
        if alias_value is None and canonical_value is None:
            return
        canonical_text = (
            _canonical_positive_decimal(canonical_value, canonical) if canonical_value is not None else None
        )
        try:
            alias_text = _canonical_decimal_alias(alias_value, alias) if alias_value is not None else None
        except SolverContractError:
            if canonical_text is None:
                raise
            target[canonical] = canonical_text
            return
        if alias_text is not None and canonical_text is not None and alias_text != canonical_text:
            conflicts.append(
                {
                    "field": "items[{}].{}".format(index, canonical),
                    "code": "input_alias_conflict",
                    "message": "input aliases disagree",
                }
            )
            return
        target[canonical] = canonical_text or alias_text

    @staticmethod
    def _text_alias(
        source: Mapping[str, Any],
        target: dict[str, Any],
        *,
        index: int,
        alias: str,
        canonical: str,
        conflicts: list[Mapping[str, str]],
    ) -> None:
        alias_value = source.get(alias)
        canonical_value = source.get(canonical)
        if alias_value is None and canonical_value is None:
            return
        alias_text = _canonical_text(alias_value, alias) if alias_value is not None else None
        canonical_text = _canonical_text(canonical_value, canonical) if canonical_value is not None else None
        if alias_text is not None and canonical_text is not None and alias_text != canonical_text:
            conflicts.append(
                {
                    "field": "items[{}].{}".format(index, canonical),
                    "code": "input_alias_conflict",
                    "message": "input aliases disagree",
                }
            )
            return
        target[canonical] = canonical_text or alias_text


def _canonical_positive_decimal(value: Any, field_name: str) -> str:
    decimal_value = _bounded_decimal(value, field_name=field_name)
    if not decimal_value.is_finite() or decimal_value <= 0:
        raise SolverContractError("{} must be positive".format(field_name))
    text = format(decimal_value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _canonical_decimal_alias(value: Any, field_name: str) -> str:
    """Parse a decimal alias with only its explicitly declared display unit."""

    unit = _DECIMAL_ALIAS_UNITS.get(field_name)
    if unit is not None and isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith(unit):
            value = normalized[: -len(unit)].strip()
            if not value:
                raise SolverContractError("{} must contain a value before its unit".format(field_name))
    return _canonical_positive_decimal(value, field_name)


def _canonical_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise SolverContractError("{} must be text".format(field_name))
    normalized = " ".join(value.split())
    if not normalized:
        raise SolverContractError("{} must not be blank".format(field_name))
    return normalized


def _checked_scale(scale: Any) -> int:
    if isinstance(scale, bool) or not isinstance(scale, int) or not 1 <= scale <= INT64_MAX:
        raise SolverContractError("metric_scale must be a positive int64 integer")
    return scale


def scaled_int(value: Any, *, scale: int, field_name: str) -> int:
    """Convert one finite decimal to a half-even rounded signed int64."""

    normalized_scale = _checked_scale(scale)
    decimal_value = _bounded_decimal(value, field_name=field_name)
    if not decimal_value.is_finite():
        raise SolverContractError("{} must be finite".format(field_name))

    if decimal_value:
        maximum_adjusted = decimal_value.copy_abs().adjusted() + len(str(normalized_scale))
        if maximum_adjusted > 20:
            raise SolverContractError("{} exceeds int64".format(field_name))
        if maximum_adjusted < -1:
            return 0

    digits = len(decimal_value.as_tuple().digits)
    precision = max(64, digits + len(str(normalized_scale)) + 24)
    try:
        with localcontext() as context:
            context.prec = precision
            scaled = (decimal_value * Decimal(normalized_scale)).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
        integer = int(scaled)
    except (DecimalException, OverflowError, ValueError) as exc:
        raise SolverContractError("{} exceeds int64".format(field_name)) from exc
    if not INT64_MIN <= integer <= INT64_MAX:
        raise SolverContractError("{} exceeds int64".format(field_name))
    return integer


def _bounded_decimal(value: Any, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise SolverContractError("{} must be a decimal input".format(field_name))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise SolverContractError("{} must be finite".format(field_name))
        _validate_decimal_shape(
            digits=len(value.as_tuple().digits),
            exponent=value.as_tuple().exponent,
            field_name=field_name,
        )
        return value
    if not isinstance(value, (str, int)):
        raise SolverContractError("{} must be a decimal input".format(field_name))

    text = str(value)
    if len(text) > MAX_TEXT_LENGTH:
        raise SolverContractError("{} exceeds supported decimal length".format(field_name))
    match = _DECIMAL_INPUT_RE.fullmatch(text)
    if match is None:
        raise SolverContractError("{} must be a finite decimal".format(field_name))
    integer = match.group("integer") or ""
    fraction = match.group("fraction")
    if fraction is None:
        fraction = match.group("fraction_only") or ""
    coefficient = (integer + fraction).lstrip("0") or "0"
    raw_exponent = match.group("exponent") or "0"
    explicit_exponent = _bounded_exponent(raw_exponent, field_name=field_name)
    _validate_decimal_shape(
        digits=len(coefficient),
        exponent=explicit_exponent - len(fraction),
        field_name=field_name,
    )
    try:
        return Decimal(text)
    except (DecimalException, ValueError) as exc:
        raise SolverContractError("{} must be a finite decimal".format(field_name)) from exc


def _bounded_exponent(value: str, *, field_name: str) -> int:
    unsigned = value.lstrip("+-").lstrip("0") or "0"
    largest_magnitude = max(abs(MIN_DECIMAL_EXPONENT), abs(MAX_DECIMAL_EXPONENT))
    if len(unsigned) > len(str(largest_magnitude)):
        raise SolverContractError("{} exceeds supported decimal exponent".format(field_name))
    return int(value)


def _validate_decimal_shape(*, digits: int, exponent: int | str, field_name: str) -> None:
    if isinstance(exponent, str):
        raise SolverContractError("{} exceeds supported decimal precision or exponent".format(field_name))
    if digits > MAX_DECIMAL_DIGITS or exponent < MIN_DECIMAL_EXPONENT or exponent > MAX_DECIMAL_EXPONENT:
        raise SolverContractError("{} exceeds supported decimal precision or exponent".format(field_name))


def _checked_add(left: int, right: int, *, field_name: str) -> int:
    result = left + right
    if not INT64_MIN <= result <= INT64_MAX:
        raise SolverContractError("{} aggregate exceeds int64".format(field_name))
    return result


def _checked_subtract(left: int, right: int, *, field_name: str) -> int:
    result = left - right
    if not INT64_MIN <= result <= INT64_MAX:
        raise SolverContractError("{} subtraction exceeds int64".format(field_name))
    return result


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate_id: str
    assignment: tuple[str, ...]
    eligible: bool
    rejection_codes: tuple[str, ...]
    objective_metrics: Mapping[str, int]
    constraint_metrics: Mapping[str, int]
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "assignment", tuple(self.assignment))
        object.__setattr__(self, "rejection_codes", tuple(self.rejection_codes))
        object.__setattr__(
            self,
            "objective_metrics",
            MappingProxyType(dict(self.objective_metrics)),
        )
        object.__setattr__(
            self,
            "constraint_metrics",
            MappingProxyType(dict(self.constraint_metrics)),
        )
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))


@dataclass(frozen=True)
class _RouteFacts:
    scale: int
    capacity_limit: int | None = None
    item_metrics: Mapping[str, Mapping[str, Mapping[str, int]]] | None = None
    candidate_metrics: Mapping[str, Mapping[str, Any]] | None = None


class OrderedBinaryRouteEvaluator:
    """Pure production evaluator over a normalized input and frozen facts."""

    def evaluate_candidates(
        self,
        *,
        normalized_input: NormalizedInput,
        fact_snapshot: FactSnapshot,
    ) -> tuple[CandidateEvaluation, ...]:
        plan = _ordered_plan(normalized_input)
        candidate_set = enumerate_ordered_routes(
            plan,
            source_run_id="profile-evaluation",
            knowledge_package_id="profile-package",
            knowledge_package_revision="profile-revision",
        )
        facts = _route_facts(
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            plan=plan,
            candidates=candidate_set.candidates,
        )
        return tuple(
            self._evaluate(
                plan=plan,
                facts=facts,
                candidate=candidate,
                candidate_rank=rank,
            )
            for rank, candidate in enumerate(candidate_set.candidates)
        )

    @staticmethod
    def _evaluate(
        *,
        plan: OrderedPlan,
        facts: _RouteFacts,
        candidate: Candidate,
        candidate_rank: int,
    ) -> CandidateEvaluation:
        if facts.candidate_metrics is not None:
            metrics = facts.candidate_metrics[candidate.candidate_id]
            eligible = metrics["eligible"]
            return CandidateEvaluation(
                candidate_id=candidate.candidate_id,
                assignment=tuple(option.value for option in candidate.assignments),
                eligible=eligible,
                rejection_codes=() if eligible else ("business_constraints_failed",),
                objective_metrics={
                    "primary_score": metrics["primary_score"],
                    "transition_count": candidate.transition_count,
                    "safety_margin": metrics["safety_margin"],
                    "candidate_rank": candidate_rank,
                },
                constraint_metrics={"total_load": 0},
                evidence_refs=(ROUTE_FACT_NAME,),
            )

        if facts.item_metrics is None or facts.capacity_limit is None:
            raise SolverContractError("route_metrics facts are incomplete")
        primary_score = 0
        total_load = 0
        safety_values: list[int] = []
        for item, selected_option in zip(plan.items, candidate.assignments):
            metrics = facts.item_metrics[item.item_id][selected_option.value]
            primary_score = _checked_add(
                primary_score,
                metrics["primary_score"],
                field_name="primary_score",
            )
            total_load = _checked_add(
                total_load,
                metrics["load"],
                field_name="total_load",
            )
            safety_values.append(metrics["safety"])

        remaining_capacity = _checked_subtract(
            facts.capacity_limit,
            total_load,
            field_name="safety_margin",
        )
        safety_margin = min(remaining_capacity, *safety_values)
        eligible = total_load <= facts.capacity_limit
        return CandidateEvaluation(
            candidate_id=candidate.candidate_id,
            assignment=tuple(option.value for option in candidate.assignments),
            eligible=eligible,
            rejection_codes=() if eligible else ("capacity_exceeded",),
            objective_metrics={
                "primary_score": primary_score,
                "transition_count": candidate.transition_count,
                "safety_margin": safety_margin,
                "candidate_rank": candidate_rank,
            },
            constraint_metrics={"total_load": total_load},
            evidence_refs=(ROUTE_FACT_NAME,),
        )


@dataclass(frozen=True)
class OrderedBinaryRouteCompiler:
    profile_id: str = PROFILE_ID
    evaluator: OrderedBinaryRouteEvaluator = field(default_factory=OrderedBinaryRouteEvaluator)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.profile_id, str)
            or not self.profile_id.strip()
            or self.profile_id != self.profile_id.strip()
        ):
            raise SolverContractError("profile_id must be a nonblank canonical string")
        if not callable(getattr(self.evaluator, "evaluate_candidates", None)):
            raise SolverContractError("evaluator must provide evaluate_candidates")

    def compile_problem(
        self,
        *,
        capability: Any,
        normalized_input: NormalizedInput,
        fact_snapshot: FactSnapshot,
    ) -> Mapping[str, Any]:
        model = _decision_model(capability)
        if model.get("profile_id") != self.profile_id:
            raise SolverContractError("decision model profile_id is unsupported")
        _validate_locked_objectives(model.get("objectives"))

        plan = _ordered_plan(normalized_input)
        evaluations = self.evaluator.evaluate_candidates(
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
        )
        candidate_payloads = []
        for evaluation in evaluations:
            metrics = {
                **dict(evaluation.objective_metrics),
                **dict(evaluation.constraint_metrics),
            }
            candidate_payloads.append(
                {
                    "candidate_id": evaluation.candidate_id,
                    "eligible": evaluation.eligible,
                    "assignment": {item.item_id: option for item, option in zip(plan.items, evaluation.assignment)},
                    "metrics": metrics,
                }
            )

        parameters: dict[str, Any] = {
            "candidate_selection": {
                "profile_id": self.profile_id,
                "candidate_count": len(candidate_payloads),
                "candidate_evaluation_digest": sha256_digest(candidate_payloads),
                "candidates": candidate_payloads,
            }
        }
        domain_context = _domain_evaluation_context(
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            plan=plan,
        )
        if domain_context is not None:
            parameters["domain_evaluation"] = domain_context

        compiled = {
            "decision_space": {"variables": ["selected_candidate"]},
            "parameters": parameters,
            "constraints": [{"kind": "select_exactly_one_candidate"}],
            "objectives": [dict(objective) for objective in LOCKED_OBJECTIVES],
            "limits": {
                "max_candidates": 128,
                "max_verification_candidates": 128,
            },
        }
        validate_candidate_selection_components(compiled)
        return compiled


class LockedModelFactProvider:
    """Expose only facts already embedded in the locked decision model."""

    def resolve_facts(
        self,
        *,
        capability: Any,
        normalized_input: NormalizedInput,
        binding_revision_id: str,
        **_identity: Any,
    ) -> Mapping[str, Any]:
        if not isinstance(normalized_input, NormalizedInput):
            raise FactSnapshotInvalidError("normalized input is invalid")
        model = _decision_model(capability, error_type=FactSnapshotInvalidError)
        fact_source = model.get("fact_source")
        if not isinstance(fact_source, Mapping) or dict(fact_source) != {"kind": "locked_model"}:
            raise FactSnapshotInvalidError("ordered route facts are unavailable")
        facts = model.get("materialized_facts")
        trusted_frozen_facts = (
            isinstance(model, MappingProxyType)
            and isinstance(facts, tuple)
            and all(isinstance(fact, MappingProxyType) for fact in facts)
        )
        if not isinstance(facts, list) and not trusted_frozen_facts:
            raise FactSnapshotInvalidError("ordered route facts are invalid")
        if not isinstance(binding_revision_id, str) or not binding_revision_id.strip():
            raise FactSnapshotInvalidError("solver binding revision is invalid")
        if any(not isinstance(fact, Mapping) for fact in facts):
            raise FactSnapshotInvalidError("ordered route facts are invalid")
        return {
            "facts": [dict(fact) for fact in facts],
            "bindings": {
                "solver_binding_revision": binding_revision_id.strip(),
            },
            "as_of": "locked-model",
            "source_evidence": ({"kind": "locked_model_materialized_facts"},),
        }


def _decision_model(
    capability: Any,
    *,
    error_type: type[SolverContractError] = SolverContractError,
) -> Mapping[str, Any]:
    if isinstance(capability, Mapping):
        model = capability.get("decision_model")
    else:
        model = getattr(capability, "decision_model", None)
    if not isinstance(model, Mapping):
        raise error_type("decision_model must be an object")
    return model


def _validate_locked_objectives(value: Any) -> None:
    if not isinstance(value, (list, tuple)) or len(value) != len(LOCKED_OBJECTIVES):
        raise SolverContractError("decision model objective order is invalid")
    if any(not isinstance(item, Mapping) for item in value):
        raise SolverContractError("decision model objective order is invalid")
    normalized = tuple(dict(item) for item in value)
    expected = tuple(dict(item) for item in LOCKED_OBJECTIVES)
    if normalized != expected:
        raise SolverContractError("decision model objective order is invalid")


def _ordered_plan(normalized_input: NormalizedInput) -> OrderedPlan:
    if not isinstance(normalized_input, NormalizedInput):
        raise SolverContractError("normalized_input must be NormalizedInput")
    raw_items = normalized_input.values.get("items")
    if not isinstance(raw_items, (list, tuple)):
        raise SolverContractError("items must be an ordered array")
    if not raw_items:
        raise SolverContractError("items must contain at least one item")
    if len(raw_items) > MAX_PLAN_ITEMS:
        raise SolverContractError("items support at most 64 entries")

    items: list[PlanItem] = []
    item_ids: set[str] = set()
    for expected_order, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, Mapping):
            raise SolverContractError("items must contain objects")
        item_order = raw_item.get("item_order")
        if isinstance(item_order, bool) or not isinstance(item_order, int) or item_order != expected_order:
            raise SolverContractError("item orders must be strictly increasing and contiguous from 1")
        item_id = raw_item.get("item_id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise SolverContractError("item_id is required")
        item_id = item_id.strip()
        if item_id in item_ids:
            raise SolverContractError("item_id values must be unique")
        item_ids.add(item_id)
        if raw_item.get("quantity_wan_tons") is not None:
            quantity = _bounded_decimal(
                raw_item.get("quantity_wan_tons"),
                field_name="quantity_wan_tons",
            ) * Decimal("10000")
        elif raw_item.get("input_weight_tons") is not None:
            quantity = _bounded_decimal(
                raw_item.get("input_weight_tons"),
                field_name="input_weight_tons",
            )
        else:
            quantity = raw_item.get("quantity", "1")
        duration = (
            _bounded_decimal(raw_item.get("process_days"), field_name="process_days")
            if raw_item.get("process_days") is not None
            else raw_item.get("duration", "1")
        )
        items.append(
            PlanItem(
                item_order=item_order,
                item_id=item_id,
                quantity=quantity,
                duration=duration,
            )
        )
    return OrderedPlan(
        items=tuple(items),
        option_domain=(Option.OPTION_0, Option.OPTION_1),
        max_transitions=1,
    )


def _route_facts(
    *,
    normalized_input: NormalizedInput,
    fact_snapshot: FactSnapshot,
    plan: OrderedPlan,
    candidates: tuple[Candidate, ...],
) -> _RouteFacts:
    if not isinstance(fact_snapshot, FactSnapshot):
        raise SolverContractError("fact_snapshot must be FactSnapshot")
    if fact_snapshot.normalized_input_digest != normalized_input.digest:
        raise SolverContractError("fact_snapshot does not match normalized_input")
    matching = [fact for fact in fact_snapshot.facts if fact["name"] == ROUTE_FACT_NAME]
    if len(matching) != 1:
        raise SolverContractError("route_metrics fact is required")
    raw = matching[0]["value"]
    if not isinstance(raw, Mapping) or frozenset(raw) not in {
        frozenset(_ROUTE_ITEM_FACT_FIELDS),
        frozenset(_ROUTE_CANDIDATE_FACT_FIELDS),
    }:
        raise SolverContractError("route_metrics fact fields are invalid")

    scale = _checked_scale(raw.get("metric_scale"))
    if raw.get("rounding") != _ROUNDING_MODE:
        raise SolverContractError("rounding must be ROUND_HALF_EVEN")
    if set(raw) == _ROUTE_CANDIDATE_FACT_FIELDS:
        raw_candidates = raw.get("candidates")
        if not isinstance(raw_candidates, Mapping):
            raise SolverContractError("route_metrics candidates must be an object")
        expected_ids = tuple(candidate.candidate_id for candidate in candidates)
        missing = tuple(candidate_id for candidate_id in expected_ids if candidate_id not in raw_candidates)
        unexpected = tuple(str(candidate_id) for candidate_id in raw_candidates if candidate_id not in expected_ids)
        if missing:
            raise SolverContractError("route_metrics candidate is missing: {}".format(missing[0]))
        if unexpected:
            raise SolverContractError("route_metrics contains unexpected candidate: {}".format(unexpected[0]))
        candidate_metrics: dict[str, Mapping[str, Any]] = {}
        for candidate_id in expected_ids:
            raw_metrics = raw_candidates[candidate_id]
            if (
                not isinstance(raw_metrics, Mapping)
                or set(raw_metrics) != _CANDIDATE_METRIC_FIELDS
                or not isinstance(raw_metrics.get("eligible"), bool)
            ):
                raise SolverContractError("route_metrics candidate fields are invalid: {}".format(candidate_id))
            candidate_metrics[candidate_id] = MappingProxyType(
                {
                    "eligible": raw_metrics["eligible"],
                    "primary_score": scaled_int(
                        raw_metrics["primary_score"],
                        scale=scale,
                        field_name="{}.primary_score".format(candidate_id),
                    ),
                    "safety_margin": scaled_int(
                        raw_metrics["safety_margin"],
                        scale=scale,
                        field_name="{}.safety_margin".format(candidate_id),
                    ),
                }
            )
        return _RouteFacts(
            scale=scale,
            candidate_metrics=MappingProxyType(candidate_metrics),
        )

    capacity_limit = scaled_int(
        raw.get("capacity_limit"),
        scale=scale,
        field_name="capacity_limit",
    )
    raw_items = raw.get("items")
    if not isinstance(raw_items, Mapping):
        raise SolverContractError("route_metrics items must be an object")

    expected_ids = tuple(item.item_id for item in plan.items)
    actual_ids = tuple(str(item_id) for item_id in raw_items)
    missing = tuple(item_id for item_id in expected_ids if item_id not in raw_items)
    unexpected = tuple(item_id for item_id in actual_ids if item_id not in expected_ids)
    if missing:
        raise SolverContractError("route_metrics item is missing: {}".format(missing[0]))
    if unexpected:
        raise SolverContractError("route_metrics contains unexpected item: {}".format(unexpected[0]))

    item_metrics: dict[str, Mapping[str, Mapping[str, int]]] = {}
    for item_id in expected_ids:
        raw_options = raw_items[item_id]
        if not isinstance(raw_options, Mapping) or set(raw_options) != set(_OPTION_NAMES):
            raise SolverContractError("{} options must be OPTION_0 and OPTION_1".format(item_id))
        options: dict[str, Mapping[str, int]] = {}
        for option_name in _OPTION_NAMES:
            raw_metrics = raw_options[option_name]
            if not isinstance(raw_metrics, Mapping) or set(raw_metrics) != _OPTION_METRIC_FIELDS:
                missing_metrics = (
                    sorted(_OPTION_METRIC_FIELDS - set(raw_metrics or ())) if isinstance(raw_metrics, Mapping) else []
                )
                detail = missing_metrics[0] if missing_metrics else "fields"
                raise SolverContractError("{}.{} {} is invalid".format(item_id, option_name, detail))
            options[option_name] = MappingProxyType(
                {
                    metric_name: scaled_int(
                        raw_metrics[metric_name],
                        scale=scale,
                        field_name="{}.{}.{}".format(item_id, option_name, metric_name),
                    )
                    for metric_name in sorted(_OPTION_METRIC_FIELDS)
                }
            )
        item_metrics[item_id] = MappingProxyType(options)
    return _RouteFacts(
        scale=scale,
        capacity_limit=capacity_limit,
        item_metrics=MappingProxyType(item_metrics),
    )


def _domain_evaluation_context(
    *,
    normalized_input: NormalizedInput,
    fact_snapshot: FactSnapshot,
    plan: OrderedPlan,
) -> Mapping[str, Any] | None:
    matching = [fact for fact in fact_snapshot.facts if fact["name"] == DOMAIN_CONTEXT_FACT_NAME]
    if not matching:
        return None
    if len(matching) != 1:
        raise SolverContractError("refinery route context must be unique")
    raw = matching[0].get("value")
    expected_fields = {
        "schema_version",
        "profile_id",
        "metric_scale",
        "rounding",
        "plan_month",
        "production_condition",
        "items",
        "model",
        "tables",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected_fields:
        raise SolverContractError("refinery route context fields are invalid")
    if (
        raw.get("schema_version") != "refinery_route_context.v1"
        or raw.get("profile_id") != "refinery_route.v1"
        or raw.get("rounding") != "ROUND_HALF_EVEN"
    ):
        raise SolverContractError("refinery route context version is unsupported")
    scale = raw.get("metric_scale")
    if isinstance(scale, bool) or not isinstance(scale, int) or scale < 1:
        raise SolverContractError("refinery route context metric scale is invalid")
    raw_items = raw.get("items")
    if not isinstance(raw_items, (list, tuple)) or len(raw_items) != len(plan.items):
        raise SolverContractError("refinery route context items are invalid")
    expected_ids = tuple(item.item_id for item in plan.items)
    actual_ids: list[str] = []
    for expected_order, item in enumerate(raw_items, start=1):
        if (
            not isinstance(item, Mapping)
            or item.get("item_order") != expected_order
            or not isinstance(item.get("item_id"), str)
        ):
            raise SolverContractError("refinery route context item order is invalid")
        actual_ids.append(str(item["item_id"]))
        plan_item = plan.items[expected_order - 1]
        context_quantity = _bounded_decimal(item.get("input_tons"), field_name="domain_evaluation.items[].input_tons")
        if context_quantity != plan_item.quantity:
            raise SolverContractError("refinery route context quantity diverged")
        context_duration = _bounded_decimal(
            item.get("process_days"),
            field_name="domain_evaluation.items[].process_days",
        )
        if context_duration != plan_item.duration:
            raise SolverContractError("refinery route context duration diverged")
    if tuple(actual_ids) != expected_ids:
        raise SolverContractError("refinery route context item identity diverged")
    input_month = normalized_input.values.get("plan_month")
    if input_month is not None and raw.get("plan_month") != input_month:
        raise SolverContractError("refinery route context month diverged")
    if not isinstance(raw.get("model"), Mapping) or not isinstance(raw.get("tables"), Mapping):
        raise SolverContractError("refinery route context model is invalid")
    return raw


__all__ = [
    "CandidateEvaluation",
    "LOCKED_OBJECTIVES",
    "LockedModelFactProvider",
    "OrderedBinaryRouteCompiler",
    "OrderedBinaryRouteEvaluator",
    "OrderedBinaryRouteInputAdapter",
    "PROFILE_ID",
    "ROUTE_FACT_NAME",
    "DOMAIN_CONTEXT_FACT_NAME",
    "scaled_int",
]
