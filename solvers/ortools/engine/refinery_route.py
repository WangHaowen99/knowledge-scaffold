"""Independent refinery-route candidate evaluation for the OR-Tools service."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import re
from typing import Any, Mapping

from .candidate_selection import EngineResult, solve_candidate_selection


_OPTIONS = ("OPTION_0", "OPTION_1")
_UNIT_ROLES = ("atmospheric", "diesel", "vgo")
_CONTEXT_SCHEMA = "refinery_route_context.v1"
_PROFILE_ID = "refinery_route.v1"
_MONTH_RE = re.compile(r"^(?P<year>[0-9]{4})-(?P<month>0[1-9]|1[0-2])$")
_DECIMAL_RE = re.compile(
    r"^[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?)|(?:\.[0-9]+))(?:[eE][+-]?[0-9]+)?$"
)
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


class RefineryRouteContextError(ValueError):
    """The frozen refinery context is incomplete or internally inconsistent."""


@dataclass(frozen=True)
class _Item:
    item_id: str
    order: int
    source_id: str
    input_tons: Decimal
    process_days: Decimal


@dataclass(frozen=True)
class _Unit:
    role: str
    daily_capacity: Decimal
    feed_cost: Decimal


@dataclass(frozen=True)
class _Tank:
    tank_id: str
    kind: str
    available_capacity: Decimal
    current_inventory: Decimal
    minimum_inventory: Decimal
    maximum_inventory: Decimal


@dataclass(frozen=True)
class _Context:
    metric_scale: int
    plan_month: str
    calendar_days: int
    items: tuple[_Item, ...]
    option_routes: Mapping[str, str]
    special_tanks: Mapping[str, str]
    batch_unit_policy: str
    correction_factors: Mapping[str, Mapping[str, Decimal]]
    atmospheric_yields: Mapping[str, tuple[Mapping[str, Any], ...]]
    product_yields: Mapping[tuple[str, str], tuple[Mapping[str, Any], ...]]
    units: Mapping[str, _Unit]
    tanks: Mapping[str, _Tank]
    energy_costs: Mapping[str, tuple[tuple[Decimal, Decimal], ...]]


@dataclass(frozen=True)
class _BatchEvaluation:
    item: _Item
    route: str
    profit: Decimal
    atmospheric_input: Decimal
    diesel_raw_feed: Decimal
    diesel_effective_load: Decimal
    vgo_raw_feed: Decimal
    vgo_effective_load: Decimal
    ordinary_tank_daily_inputs: Mapping[str, Decimal]
    batch_unit_loads: Mapping[str, tuple[Decimal, Decimal]]


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RefineryRouteContextError("{} must be canonical text".format(field))
    return value


def _decimal(
    value: Any,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise RefineryRouteContextError("{} must be a decimal string".format(field))
    text = str(value)
    if len(text) > 96 or _DECIMAL_RE.fullmatch(text) is None:
        raise RefineryRouteContextError("{} must be a decimal string".format(field))
    try:
        parsed = Decimal(text)
    except (DecimalException, ValueError) as exc:
        raise RefineryRouteContextError("{} is invalid".format(field)) from exc
    if not parsed.is_finite():
        raise RefineryRouteContextError("{} must be finite".format(field))
    if positive and parsed <= 0:
        raise RefineryRouteContextError("{} must be positive".format(field))
    if nonnegative and parsed < 0:
        raise RefineryRouteContextError("{} must be nonnegative".format(field))
    return parsed


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RefineryRouteContextError("{} must be an object".format(field))
    return value


def _array(value: Any, field: str) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise RefineryRouteContextError("{} must be an array".format(field))
    return tuple(value)


def _calendar_days(plan_month: str) -> int:
    match = _MONTH_RE.fullmatch(plan_month)
    if match is None:
        raise RefineryRouteContextError("plan_month must use YYYY-MM")
    year = int(match.group("year"))
    month = int(match.group("month"))
    if month == 12:
        following = date(year + 1, 1, 1)
    else:
        following = date(year, month + 1, 1)
    return (following - date(year, month, 1)).days


def _items(raw: Any) -> tuple[_Item, ...]:
    values = _array(raw, "items")
    if not 1 <= len(values) <= 64:
        raise RefineryRouteContextError("items must contain between 1 and 64 entries")
    result: list[_Item] = []
    seen: set[str] = set()
    for expected_order, raw_item in enumerate(values, start=1):
        item = _mapping(raw_item, "item")
        if set(item) != {
            "item_id",
            "item_order",
            "source_id",
            "input_tons",
            "process_days",
        }:
            raise RefineryRouteContextError("item fields are invalid")
        item_id = _required_text(item.get("item_id"), "item_id")
        source_id = _required_text(item.get("source_id"), "source_id")
        order = item.get("item_order")
        if isinstance(order, bool) or not isinstance(order, int) or order != expected_order:
            raise RefineryRouteContextError("item_order must be contiguous from one")
        if item_id in seen:
            raise RefineryRouteContextError("item_id values must be unique")
        seen.add(item_id)
        result.append(
            _Item(
                item_id=item_id,
                order=order,
                source_id=source_id,
                input_tons=_decimal(item.get("input_tons"), "input_tons", positive=True),
                process_days=_decimal(
                    item.get("process_days"), "process_days", positive=True
                ),
            )
        )
    return tuple(result)


def _units(raw: Any) -> Mapping[str, _Unit]:
    values = _array(raw, "tables.units")
    result: dict[str, _Unit] = {}
    for raw_unit in values:
        unit = _mapping(raw_unit, "unit")
        if set(unit) != {
            "unit_role",
            "daily_capacity_tons",
            "feed_cost_per_ton",
        }:
            raise RefineryRouteContextError("unit fields are invalid")
        role = _required_text(unit.get("unit_role"), "unit_role")
        if role not in _UNIT_ROLES or role in result:
            raise RefineryRouteContextError("unit roles are invalid")
        result[role] = _Unit(
            role=role,
            daily_capacity=_decimal(
                unit.get("daily_capacity_tons"),
                "daily_capacity_tons",
                positive=True,
            ),
            feed_cost=_decimal(
                unit.get("feed_cost_per_ton"),
                "feed_cost_per_ton",
                nonnegative=True,
            ),
        )
    if set(result) != set(_UNIT_ROLES):
        raise RefineryRouteContextError("all refinery unit roles are required")
    return result


def _tanks(raw: Any) -> Mapping[str, _Tank]:
    values = _array(raw, "tables.tanks")
    result: dict[str, _Tank] = {}
    for raw_tank in values:
        tank = _mapping(raw_tank, "tank")
        if set(tank) != {
            "tank_id",
            "kind",
            "available_capacity_tons",
            "current_inventory_tons",
            "minimum_inventory_tons",
            "maximum_inventory_tons",
        }:
            raise RefineryRouteContextError("tank fields are invalid")
        tank_id = _required_text(tank.get("tank_id"), "tank_id")
        kind = _required_text(tank.get("kind"), "tank kind")
        if tank_id in result or kind not in {"ordinary", "special"}:
            raise RefineryRouteContextError("tank identity is invalid")
        minimum = _decimal(
            tank.get("minimum_inventory_tons"),
            "minimum_inventory_tons",
            nonnegative=True,
        )
        maximum = _decimal(
            tank.get("maximum_inventory_tons"),
            "maximum_inventory_tons",
            positive=True,
        )
        current = _decimal(
            tank.get("current_inventory_tons"),
            "current_inventory_tons",
            nonnegative=True,
        )
        if maximum <= minimum or current < minimum or current > maximum:
            raise RefineryRouteContextError("tank inventory bounds are invalid")
        result[tank_id] = _Tank(
            tank_id=tank_id,
            kind=kind,
            available_capacity=_decimal(
                tank.get("available_capacity_tons"),
                "available_capacity_tons",
                positive=True,
            ),
            current_inventory=current,
            minimum_inventory=minimum,
            maximum_inventory=maximum,
        )
    if not result:
        raise RefineryRouteContextError("at least one tank is required")
    return result


def _atmospheric_yields(
    raw: Any, tanks: Mapping[str, _Tank]
) -> Mapping[str, tuple[Mapping[str, Any], ...]]:
    values = _array(raw, "tables.atmospheric_yields")
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for raw_row in values:
        row = _mapping(raw_row, "atmospheric yield")
        if set(row) != {
            "source_id",
            "product_id",
            "feed_role",
            "yield_percent",
            "tank_id",
        }:
            raise RefineryRouteContextError("atmospheric yield fields are invalid")
        source_id = _required_text(row.get("source_id"), "source_id")
        product_id = _required_text(row.get("product_id"), "product_id")
        feed_role = _required_text(row.get("feed_role"), "feed_role")
        if feed_role not in {"diesel_base", "vgo_base", "route_stream", "other"}:
            raise RefineryRouteContextError("atmospheric feed role is invalid")
        identity = (source_id, product_id)
        if identity in seen:
            raise RefineryRouteContextError("atmospheric yield rows are duplicated")
        seen.add(identity)
        tank_id = row.get("tank_id")
        if tank_id is not None:
            tank_id = _required_text(tank_id, "tank_id")
            if tank_id not in tanks:
                raise RefineryRouteContextError("atmospheric tank is unknown")
        grouped[source_id].append(
            {
                "product_id": product_id,
                "feed_role": feed_role,
                "yield_percent": _decimal(
                    row.get("yield_percent"), "yield_percent", nonnegative=True
                ),
                "tank_id": tank_id,
            }
        )
    return {source: tuple(rows) for source, rows in grouped.items()}


def _product_yields(
    raw: Any, tanks: Mapping[str, _Tank]
) -> Mapping[tuple[str, str], tuple[Mapping[str, Any], ...]]:
    values = _array(raw, "tables.unit_product_yields")
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str, str]] = set()
    for raw_row in values:
        row = _mapping(raw_row, "unit product yield")
        if set(row) != {
            "source_id",
            "unit_role",
            "product_id",
            "tank_id",
            "yield_with_route_percent",
            "yield_without_route_percent",
            "price_per_ton",
            "feeds_vgo",
        }:
            raise RefineryRouteContextError("unit product yield fields are invalid")
        source_id = _required_text(row.get("source_id"), "source_id")
        role = _required_text(row.get("unit_role"), "unit_role")
        product_id = _required_text(row.get("product_id"), "product_id")
        if role not in {"diesel", "vgo"}:
            raise RefineryRouteContextError("product yield unit role is invalid")
        identity = (source_id, role, product_id)
        if identity in seen:
            raise RefineryRouteContextError("unit product yield rows are duplicated")
        seen.add(identity)
        tank_id = row.get("tank_id")
        if tank_id is not None:
            tank_id = _required_text(tank_id, "tank_id")
            if tank_id not in tanks:
                raise RefineryRouteContextError("product tank is unknown")
        feeds_vgo = row.get("feeds_vgo")
        if not isinstance(feeds_vgo, bool):
            raise RefineryRouteContextError("feeds_vgo must be boolean")
        grouped[(source_id, role)].append(
            {
                "product_id": product_id,
                "tank_id": tank_id,
                "yield_with_route": _decimal(
                    row.get("yield_with_route_percent"),
                    "yield_with_route_percent",
                    nonnegative=True,
                ),
                "yield_without_route": _decimal(
                    row.get("yield_without_route_percent"),
                    "yield_without_route_percent",
                    nonnegative=True,
                ),
                "price": _decimal(
                    row.get("price_per_ton"), "price_per_ton", nonnegative=True
                ),
                "feeds_vgo": feeds_vgo,
            }
        )
    return {identity: tuple(rows) for identity, rows in grouped.items()}


def _energy_costs(raw: Any) -> Mapping[str, tuple[tuple[Decimal, Decimal], ...]]:
    values = _array(raw, "tables.energy_costs")
    grouped: dict[str, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    for raw_row in values:
        row = _mapping(raw_row, "energy cost")
        if set(row) != {"unit_role", "consumption_per_ton", "price_per_unit"}:
            raise RefineryRouteContextError("energy cost fields are invalid")
        role = _required_text(row.get("unit_role"), "unit_role")
        if role not in {"diesel", "vgo"}:
            raise RefineryRouteContextError("energy unit role is invalid")
        grouped[role].append(
            (
                _decimal(
                    row.get("consumption_per_ton"),
                    "consumption_per_ton",
                    nonnegative=True,
                ),
                _decimal(
                    row.get("price_per_unit"),
                    "price_per_unit",
                    nonnegative=True,
                ),
            )
        )
    return {role: tuple(rows) for role, rows in grouped.items()}


def _parse_context(problem: Mapping[str, Any]) -> _Context:
    if not isinstance(problem, Mapping):
        raise RefineryRouteContextError("problem must be an object")
    if (
        problem.get("schema_version") != "solver.problem.v1"
        or problem.get("problem_family") != "binary_ordered_route"
        or problem.get("family_version") != "1.0"
    ):
        raise RefineryRouteContextError("problem identity is unsupported")
    payload = _mapping(problem.get("payload"), "problem payload")
    parameters = _mapping(payload.get("parameters"), "problem parameters")
    raw = _mapping(parameters.get("domain_evaluation"), "domain_evaluation")
    if set(raw) != {
        "schema_version",
        "profile_id",
        "metric_scale",
        "rounding",
        "plan_month",
        "production_condition",
        "items",
        "model",
        "tables",
    }:
        raise RefineryRouteContextError("domain evaluation fields are invalid")
    if raw.get("schema_version") != _CONTEXT_SCHEMA:
        raise RefineryRouteContextError("domain evaluation schema is unsupported")
    if raw.get("profile_id") != _PROFILE_ID:
        raise RefineryRouteContextError("domain evaluation profile is unsupported")
    if raw.get("rounding") != "ROUND_HALF_EVEN":
        raise RefineryRouteContextError("domain rounding mode is unsupported")
    scale = raw.get("metric_scale")
    if isinstance(scale, bool) or not isinstance(scale, int) or not 1 <= scale <= 1_000_000:
        raise RefineryRouteContextError("metric_scale is invalid")
    if raw.get("production_condition") != "NO_JET":
        raise RefineryRouteContextError("production condition is unsupported")
    plan_month = _required_text(raw.get("plan_month"), "plan_month")
    items = _items(raw.get("items"))
    model = _mapping(raw.get("model"), "model")
    if set(model) != {
        "option_routes",
        "special_tanks",
        "constraint_policy",
        "correction_factors",
    }:
        raise RefineryRouteContextError("route model fields are invalid")
    option_routes = dict(_mapping(model.get("option_routes"), "option_routes"))
    if option_routes != {"OPTION_0": "diesel", "OPTION_1": "vgo"}:
        raise RefineryRouteContextError("option route mapping is unsupported")
    special_tanks = dict(_mapping(model.get("special_tanks"), "special_tanks"))
    if set(special_tanks) != {"diesel", "vgo"}:
        raise RefineryRouteContextError("special tank mapping is invalid")
    special_tanks = {
        role: _required_text(tank_id, "special tank")
        for role, tank_id in special_tanks.items()
    }
    policy = _mapping(model.get("constraint_policy"), "constraint_policy")
    if set(policy) != {"batch_unit_load", "monthly_unit_load", "tank_capacity"}:
        raise RefineryRouteContextError("constraint policy fields are invalid")
    if policy.get("batch_unit_load") not in {"hard", "advisory"}:
        raise RefineryRouteContextError("batch unit policy is invalid")
    if policy.get("monthly_unit_load") != "hard" or policy.get("tank_capacity") != "hard":
        raise RefineryRouteContextError("required hard constraints are missing")

    raw_factors = _mapping(model.get("correction_factors"), "correction_factors")
    factors: dict[str, Mapping[str, Decimal]] = {}
    factor_names = {"diesel_load", "diesel_product", "vgo_load", "vgo_product"}
    for source_id, raw_source_factors in raw_factors.items():
        source = _required_text(source_id, "correction source")
        source_factors = _mapping(raw_source_factors, "source correction factors")
        if set(source_factors) != factor_names:
            raise RefineryRouteContextError("source correction factors are incomplete")
        factors[source] = {
            name: _decimal(source_factors[name], name, positive=True)
            for name in factor_names
        }

    tables = _mapping(raw.get("tables"), "tables")
    if set(tables) != {
        "atmospheric_yields",
        "unit_product_yields",
        "units",
        "tanks",
        "energy_costs",
    }:
        raise RefineryRouteContextError("refinery table fields are invalid")
    tanks = _tanks(tables.get("tanks"))
    for role, tank_id in special_tanks.items():
        tank = tanks.get(tank_id)
        if tank is None or tank.kind != "special":
            raise RefineryRouteContextError("{} special tank is unavailable".format(role))
    atmospheric = _atmospheric_yields(tables.get("atmospheric_yields"), tanks)
    products = _product_yields(tables.get("unit_product_yields"), tanks)
    sources = {item.source_id for item in items}
    if any(
        source not in factors
        or source not in atmospheric
        or (source, "diesel") not in products
        or (source, "vgo") not in products
        for source in sources
    ):
        raise RefineryRouteContextError("business facts are missing for an input source")

    return _Context(
        metric_scale=scale,
        plan_month=plan_month,
        calendar_days=_calendar_days(plan_month),
        items=items,
        option_routes=option_routes,
        special_tanks=special_tanks,
        batch_unit_policy=str(policy["batch_unit_load"]),
        correction_factors=factors,
        atmospheric_yields=atmospheric,
        product_yields=products,
        units=_units(tables.get("units")),
        tanks=tanks,
        energy_costs=_energy_costs(tables.get("energy_costs")),
    )


def _candidate_assignments(
    item_ids: tuple[str, ...]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    count = len(item_ids)
    result: list[tuple[str, tuple[str, ...]]] = [
        ("ALL_0", ("OPTION_0",) * count),
        ("ALL_1", ("OPTION_1",) * count),
    ]
    result.extend(
        (
            "OPTION_0_TO_OPTION_1_K{:04d}".format(boundary),
            ("OPTION_0",) * boundary + ("OPTION_1",) * (count - boundary),
        )
        for boundary in range(1, count)
    )
    result.extend(
        (
            "OPTION_1_TO_OPTION_0_K{:04d}".format(boundary),
            ("OPTION_1",) * boundary + ("OPTION_0",) * (count - boundary),
        )
        for boundary in range(1, count)
    )
    return tuple(result)


def _validate_topology(problem: Mapping[str, Any], context: _Context) -> None:
    payload = _mapping(problem.get("payload"), "problem payload")
    parameters = _mapping(payload.get("parameters"), "problem parameters")
    selection = _mapping(parameters.get("candidate_selection"), "candidate_selection")
    raw_candidates = _array(selection.get("candidates"), "candidates")
    expected = _candidate_assignments(tuple(item.item_id for item in context.items))
    declared_count = selection.get("candidate_count")
    if isinstance(declared_count, bool) or not isinstance(declared_count, int):
        raise RefineryRouteContextError("candidate count does not match the domain context")
    if declared_count != len(expected) or len(raw_candidates) != len(expected):
        raise RefineryRouteContextError("candidate count does not match the domain context")
    item_ids = tuple(item.item_id for item in context.items)
    for index, (raw_candidate, (candidate_id, assignment)) in enumerate(
        zip(raw_candidates, expected)
    ):
        candidate = _mapping(raw_candidate, "candidate")
        if candidate.get("candidate_id") != candidate_id:
            raise RefineryRouteContextError("candidate IDs are not canonical")
        raw_assignment = _mapping(candidate.get("assignment"), "candidate assignment")
        if tuple(raw_assignment) != item_ids or tuple(raw_assignment.values()) != assignment:
            raise RefineryRouteContextError("candidate assignments are not canonical")
        metrics = _mapping(candidate.get("metrics"), "candidate metrics")
        if metrics.get("candidate_rank") != index:
            raise RefineryRouteContextError("candidate rank is not canonical")


def _energy_cost(context: _Context, role: str, effective_input: Decimal) -> Decimal:
    return sum(
        effective_input * consumption * price
        for consumption, price in context.energy_costs.get(role, ())
    )


def _batch_evaluation(context: _Context, item: _Item, option: str) -> _BatchEvaluation:
    route = context.option_routes[option]
    factors = context.correction_factors[item.source_id]
    atmospheric_outputs: list[tuple[Mapping[str, Any], Decimal]] = []
    diesel_raw_feed = Decimal(0)
    vgo_hc_feed = Decimal(0)
    ordinary_tank_inputs: dict[str, Decimal] = defaultdict(Decimal)
    for row in context.atmospheric_yields[item.source_id]:
        amount = item.input_tons * row["yield_percent"] / Decimal(100)
        atmospheric_outputs.append((row, amount))
        feed_role = row["feed_role"]
        if feed_role == "diesel_base" or (
            feed_role == "route_stream" and route == "diesel"
        ):
            diesel_raw_feed += amount
        elif feed_role == "vgo_base" or (
            feed_role == "route_stream" and route == "vgo"
        ):
            vgo_hc_feed += amount
        elif row["tank_id"] is not None:
            ordinary_tank_inputs[row["tank_id"]] += amount

    diesel_effective_load = diesel_raw_feed / factors["diesel_load"]
    diesel_product_input = diesel_raw_feed / factors["diesel_product"]
    diesel_intermediate = Decimal(0)
    product_value = Decimal(0)
    for row in context.product_yields[(item.source_id, "diesel")]:
        yield_percent = (
            row["yield_with_route"] if route == "diesel" else row["yield_without_route"]
        )
        amount = diesel_product_input * yield_percent / Decimal(100)
        product_value += amount * row["price"]
        if row["feeds_vgo"]:
            diesel_intermediate += amount
        if row["tank_id"] is not None:
            ordinary_tank_inputs[row["tank_id"]] += amount

    vgo_raw_feed = vgo_hc_feed + diesel_intermediate
    vgo_effective_load = vgo_hc_feed / factors["vgo_load"]
    vgo_product_input = vgo_raw_feed / factors["vgo_product"]
    for row in context.product_yields[(item.source_id, "vgo")]:
        yield_percent = (
            row["yield_with_route"] if route == "vgo" else row["yield_without_route"]
        )
        amount = vgo_product_input * yield_percent / Decimal(100)
        product_value += amount * row["price"]
        if row["tank_id"] is not None:
            ordinary_tank_inputs[row["tank_id"]] += amount

    feed_cost = (
        diesel_raw_feed * context.units["diesel"].feed_cost
        + vgo_hc_feed * context.units["vgo"].feed_cost
    )
    energy_cost = _energy_cost(
        context, "diesel", diesel_product_input
    ) + _energy_cost(context, "vgo", vgo_product_input)
    batch_unit_loads = {
        "atmospheric": (
            item.input_tons,
            context.units["atmospheric"].daily_capacity * item.process_days,
        ),
        "diesel": (
            diesel_effective_load,
            context.units["diesel"].daily_capacity * item.process_days,
        ),
        "vgo": (
            vgo_effective_load,
            context.units["vgo"].daily_capacity * item.process_days,
        ),
    }
    return _BatchEvaluation(
        item=item,
        route=route,
        profit=product_value - feed_cost - energy_cost,
        atmospheric_input=item.input_tons,
        diesel_raw_feed=diesel_raw_feed,
        diesel_effective_load=diesel_effective_load,
        vgo_raw_feed=vgo_hc_feed,
        vgo_effective_load=vgo_effective_load,
        ordinary_tank_daily_inputs={
            tank_id: amount / item.process_days
            for tank_id, amount in ordinary_tank_inputs.items()
            if context.tanks[tank_id].kind == "ordinary"
        },
        batch_unit_loads=batch_unit_loads,
    )


def _headroom(load: Decimal, capacity: Decimal) -> Decimal:
    if capacity <= 0:
        raise RefineryRouteContextError("capacity must be positive")
    return (capacity - load) / capacity


def _scaled_int(value: Decimal, scale: int, field: str) -> int:
    try:
        with localcontext() as context:
            context.prec = 80
            result = int(
                (value * Decimal(scale)).quantize(
                    Decimal("1"), rounding=ROUND_HALF_EVEN
                )
            )
    except (DecimalException, ValueError, OverflowError) as exc:
        raise RefineryRouteContextError("{} cannot be scaled".format(field)) from exc
    if not _INT64_MIN <= result <= _INT64_MAX:
        raise RefineryRouteContextError("{} exceeds int64".format(field))
    return result


def _special_tank_margins(
    context: _Context, batches: tuple[_BatchEvaluation, ...]
) -> tuple[list[Decimal], bool]:
    total_days = sum((batch.item.process_days for batch in batches), Decimal(0))
    if total_days <= 0:
        raise RefineryRouteContextError("total processing days must be positive")
    diesel_inflow = Decimal(0)
    diesel_outflow = Decimal(0)
    vgo_inflow = Decimal(0)
    vgo_outflow = Decimal(0)
    for batch in batches:
        diesel_inflow += batch.diesel_raw_feed
        diesel_outflow += batch.diesel_effective_load
        vgo_inflow += batch.vgo_raw_feed
        vgo_outflow += batch.vgo_effective_load
    rates = (
        ("diesel", (diesel_inflow / total_days, diesel_outflow / total_days)),
        ("vgo", (vgo_inflow / total_days, vgo_outflow / total_days)),
    )
    margins: list[Decimal] = []
    valid = True
    for role, (inflow, outflow) in rates:
        tank = context.tanks[context.special_tanks[role]]
        inventory = tank.current_inventory
        span = tank.maximum_inventory - tank.minimum_inventory
        for batch in batches:
            inventory += (inflow - outflow) * batch.item.process_days
            margins.extend(
                (
                    (inventory - tank.minimum_inventory) / span,
                    (tank.maximum_inventory - inventory) / span,
                )
            )
            if inventory < tank.minimum_inventory or inventory > tank.maximum_inventory:
                valid = False
    return margins, valid


def _evaluate_assignment(
    context: _Context,
    candidate_id: str,
    assignment: tuple[str, ...],
    rank: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    batches = tuple(
        _batch_evaluation(context, item, option)
        for item, option in zip(context.items, assignment)
    )
    margins: list[Decimal] = []
    batch_units_ok = True
    ordinary_tanks_ok = True
    for batch in batches:
        for load, capacity in batch.batch_unit_loads.values():
            margin = _headroom(load, capacity)
            if context.batch_unit_policy == "hard":
                margins.append(margin)
            if load > capacity:
                batch_units_ok = False
        for tank_id, daily_input in batch.ordinary_tank_daily_inputs.items():
            tank = context.tanks[tank_id]
            margins.append(_headroom(daily_input, tank.available_capacity))
            if daily_input > tank.available_capacity:
                ordinary_tanks_ok = False

    monthly_loads = {
        "atmospheric": sum(
            (batch.atmospheric_input for batch in batches), Decimal(0)
        ),
        "diesel": sum(
            (batch.diesel_effective_load for batch in batches), Decimal(0)
        ),
        # Approved monthly VGO capacity uses raw HC feed, not corrected load.
        "vgo": sum((batch.vgo_raw_feed for batch in batches), Decimal(0)),
    }
    monthly_units_ok = True
    for role, load in monthly_loads.items():
        capacity = context.units[role].daily_capacity * context.calendar_days
        margins.append(_headroom(load, capacity))
        if load > capacity:
            monthly_units_ok = False

    special_margins, special_tanks_ok = _special_tank_margins(context, batches)
    margins.extend(special_margins)
    tanks_ok = ordinary_tanks_ok and special_tanks_ok
    eligible = monthly_units_ok and tanks_ok
    if context.batch_unit_policy == "hard":
        eligible = eligible and batch_units_ok
    rejection_codes: list[str] = []
    if not monthly_units_ok:
        rejection_codes.append("monthly_unit_capacity_exceeded")
    if not tanks_ok:
        rejection_codes.append("tank_capacity_exceeded")
    if context.batch_unit_policy == "hard" and not batch_units_ok:
        rejection_codes.append("batch_unit_capacity_exceeded")
    profit = sum((batch.profit for batch in batches), Decimal(0))
    safety_margin = min(margins) * Decimal(100)
    transitions = sum(
        left != right for left, right in zip(assignment, assignment[1:])
    )
    candidate = {
        "candidate_id": candidate_id,
        "eligible": eligible,
        "assignment": {
            item.item_id: option for item, option in zip(context.items, assignment)
        },
        "metrics": {
            "primary_score": _scaled_int(
                profit, context.metric_scale, "primary_score"
            ),
            "transition_count": transitions,
            "safety_margin": _scaled_int(
                safety_margin, context.metric_scale, "safety_margin"
            ),
            "candidate_rank": rank,
        },
    }
    diagnostics = {
        "candidate_id": candidate_id,
        "eligible": eligible,
        "rejection_codes": rejection_codes,
        "batch_unit_load_advisory": (
            context.batch_unit_policy == "advisory" and not batch_units_ok
        ),
    }
    return candidate, diagnostics


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:solver-coordination.v1:" + hashlib.sha256(encoded).hexdigest()


def evaluate_refinery_candidates(
    problem: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Recompute every canonical candidate from the frozen domain context."""

    context = _parse_context(problem)
    _validate_topology(problem, context)
    assignments = _candidate_assignments(tuple(item.item_id for item in context.items))
    candidates: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for rank, (candidate_id, assignment) in enumerate(assignments):
        candidate, candidate_diagnostics = _evaluate_assignment(
            context, candidate_id, assignment, rank
        )
        candidates.append(candidate)
        diagnostics.append(candidate_diagnostics)
    certificate = {
        "schema_version": "refinery_route_evaluation.v1",
        "evaluation_source": _PROFILE_ID,
        "candidate_count": len(candidates),
        "candidate_digest": _canonical_digest(candidates),
        "calendar_days": context.calendar_days,
        "candidates": diagnostics,
    }
    return candidates, certificate


def solve_refinery_ordered_route(
    problem: Mapping[str, Any], *, solve_time_ms: int, workers: int
) -> EngineResult:
    """Evaluate domain candidates, then select one with the existing CP-SAT model."""

    candidates, certificate = evaluate_refinery_candidates(problem)
    materialized = deepcopy(dict(problem))
    selection = materialized["payload"]["parameters"]["candidate_selection"]
    selection["candidate_count"] = len(candidates)
    selection["candidate_evaluation_digest"] = certificate["candidate_digest"]
    selection["candidates"] = candidates
    result = solve_candidate_selection(
        materialized, solve_time_ms=solve_time_ms, workers=workers
    )
    return replace(result, certificate=certificate)


__all__ = [
    "RefineryRouteContextError",
    "evaluate_refinery_candidates",
    "solve_refinery_ordered_route",
]
