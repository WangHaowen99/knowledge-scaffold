#!/usr/bin/env python3
"""Evaluate every ordered route candidate with one knowledge-package evaluator."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping


_MAX_INPUT_BYTES = 4 * 1024 * 1024
_MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_PACKAGE_SOLVER_CONTRACT_ERROR_CODE = (
    "knowledge_package_solver_contract_incompatible"
)
_REQUIRED_TABLES = (
    "crude_oil_yard",
    "refinery_unit",
    "vgo_ht_unit",
    "hydrogenation_product_yield",
    "vgo_product_yield",
    "logic_tank",
    "energy_consume",
)


class PackageSolverContractError(ValueError):
    """Raised when the package cannot satisfy the evaluator contract."""


class BusinessSummaryInconsistentError(ValueError):
    """Raised when structured batch and monthly business facts disagree."""


def _write_json_result(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    sys.stdout.write("\n")
    sys.stdout.flush()


def _strict_json(raw: str) -> dict[str, Any]:
    if not raw or len(raw.encode("utf-8")) > _MAX_INPUT_BYTES:
        raise ValueError("worker input is invalid")

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    def reject_float(_value: str) -> None:
        raise ValueError("JSON floats are not allowed")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(
        raw,
        parse_constant=reject_constant,
        parse_float=reject_float,
        object_pairs_hook=unique_object,
    )
    if not isinstance(value, dict):
        raise ValueError("worker input is invalid")
    return value


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("{} must be a positive number".format(field_name))
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("{} must be a positive number".format(field_name)) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("{} must be a positive number".format(field_name))
    return parsed


def _finite_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("{} is invalid".format(field_name))
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("{} is invalid".format(field_name)) from exc
    if not parsed.is_finite():
        raise ValueError("{} is invalid".format(field_name))
    return parsed


def _decimal_text(value: Decimal, places: str = "0.01") -> str:
    return format(value.quantize(Decimal(places), rounding=ROUND_HALF_EVEN), "f")


def _candidate_assignments(item_count: int) -> tuple[tuple[str, tuple[str, ...]], ...]:
    assignments: list[tuple[str, tuple[str, ...]]] = [
        ("ALL_0", ("OPTION_0",) * item_count),
        ("ALL_1", ("OPTION_1",) * item_count),
    ]
    assignments.extend(
        (
            "OPTION_0_TO_OPTION_1_K{:04d}".format(index),
            ("OPTION_0",) * index + ("OPTION_1",) * (item_count - index),
        )
        for index in range(1, item_count)
    )
    assignments.extend(
        (
            "OPTION_1_TO_OPTION_0_K{:04d}".format(index),
            ("OPTION_1",) * index + ("OPTION_0",) * (item_count - index),
        )
        for index in range(1, item_count)
    )
    return tuple(assignments)


def _load_evaluator(path: Path) -> Any:
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.suffix.lower() != ".py":
        raise ValueError("package evaluator path is invalid")
    module_name = "_ontology_solver_evaluator"
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        raise ValueError("package evaluator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    for symbol in (
        "BatchPlan",
        "build_report",
        "load_tables",
        "normalize_crude_name",
    ):
        if not hasattr(module, symbol):
            raise PackageSolverContractError(
                "package evaluator contract is incomplete: {}".format(symbol)
            )
    return module


def _normalize_request(
    module: Any,
    values: Mapping[str, Any],
    solver_context: Mapping[str, Any],
) -> dict[str, Any]:
    month = str(values.get("plan_month") or "").strip()
    if _MONTH_RE.fullmatch(month) is None:
        raise ValueError("plan_month must use YYYY-MM")
    diesel_unit = str(values.get("diesel_unit_no") or "").strip()
    diesel_unit_ids = _required_module_value(module, "DIESEL_UNIT_IDS")
    if not isinstance(diesel_unit_ids, Mapping) or diesel_unit not in diesel_unit_ids:
        raise ValueError("diesel_unit_no is unsupported by the package evaluator")
    production_condition = str(
        values.get("production_condition") or solver_context["production_condition"]
    ).strip()
    if production_condition != solver_context["production_condition"]:
        raise ValueError(
            "the selected package evaluator does not support this production_condition"
        )
    raw_items = values.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 64:
        raise ValueError("items must contain between 1 and 64 batches")

    items: list[dict[str, Any]] = []
    item_ids: set[str] = set()
    for expected_order, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, Mapping) or raw_item.get("item_order") != expected_order:
            raise ValueError("item_order must be contiguous from 1")
        item_id = str(raw_item.get("item_id") or "").strip()
        if not item_id or item_id in item_ids:
            raise ValueError("item_id values must be non-empty and unique")
        item_ids.add(item_id)
        crude_origin = module.normalize_crude_name(
            str(raw_item.get("crude_origin") or "").strip()
        )
        if not crude_origin:
            raise ValueError("crude_origin is required for {}".format(item_id))
        if raw_item.get("input_weight_tons") is not None:
            input_weight_tons = _decimal(
                raw_item["input_weight_tons"], "{}.input_weight_tons".format(item_id)
            )
        elif raw_item.get("quantity_wan_tons") is not None:
            input_weight_tons = _decimal(
                raw_item["quantity_wan_tons"],
                "{}.quantity_wan_tons".format(item_id),
            ) * Decimal("10000")
        else:
            raise ValueError("explicit batch weight is required for {}".format(item_id))
        process_days = _decimal(
            raw_item.get("process_days"), "{}.process_days".format(item_id)
        )
        items.append(
            {
                "item_id": item_id,
                "crude_origin": crude_origin,
                "input_weight_tons": input_weight_tons,
                "process_days": process_days,
            }
        )
    return {
        "plan_month": month,
        "diesel_unit_no": diesel_unit,
        "production_condition": production_condition,
        "items": items,
    }


def _validate_tables(module: Any, tables: Any, items: list[dict[str, Any]]) -> None:
    if not isinstance(tables, Mapping):
        raise ValueError("business data tables are invalid")
    missing_tables = [
        name
        for name in _REQUIRED_TABLES
        if not isinstance(tables.get(name), list) or not tables[name]
    ]
    if missing_tables:
        raise ValueError(
            "business facts missing for tables: {}".format(", ".join(missing_tables))
        )
    available_origins = {
        module.normalize_crude_name(str(row.get("CrudeOilYard Name") or ""))
        for row in tables["crude_oil_yard"]
        if isinstance(row, Mapping)
    }
    missing_origins = sorted(
        {
            item["crude_origin"]
            for item in items
            if item["crude_origin"] not in available_origins
        }
    )
    if missing_origins:
        raise ValueError(
            "business facts missing for crude origins: {}".format(
                ", ".join(missing_origins)
            )
        )


def _validate_candidates(
    raw_candidates: Any,
    item_count: int,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    expected = _candidate_assignments(item_count)
    if not isinstance(raw_candidates, list) or len(raw_candidates) != len(expected):
        raise ValueError("candidate set does not match the ordered plan")
    normalized: list[tuple[str, tuple[str, ...]]] = []
    for raw, (expected_id, expected_assignment) in zip(raw_candidates, expected):
        if not isinstance(raw, Mapping):
            raise ValueError("candidate set is invalid")
        candidate_id = str(raw.get("candidate_id") or "").strip()
        assignments = raw.get("assignments")
        if (
            candidate_id != expected_id
            or not isinstance(assignments, list)
            or tuple(assignments) != expected_assignment
        ):
            raise ValueError("candidate set is not canonical")
        normalized.append((candidate_id, expected_assignment))
    return tuple(normalized)


def _context_decimal_text(value: Any, field_name: str, *, default: str = "0") -> str:
    if value in (None, ""):
        value = default
    parsed = _finite_decimal(value, field_name)
    if parsed == 0:
        return "0"
    text = format(parsed, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _required_module_value(module: Any, name: str) -> Any:
    if not hasattr(module, name):
        raise PackageSolverContractError(
            "package evaluator context contract is incomplete"
        )
    return getattr(module, name)


def _solver_context_config(module: Any) -> dict[str, Any]:
    raw = _required_module_value(module, "SOLVER_CONTEXT")
    if not isinstance(raw, Mapping):
        raise PackageSolverContractError("package evaluator solver context is invalid")

    def text(value: Any, field_name: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise PackageSolverContractError("{} is invalid".format(field_name))
        return normalized

    def text_mapping(value: Any, field_name: str) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise PackageSolverContractError("{} is invalid".format(field_name))
        result = {
            text(key, field_name): text(item, field_name)
            for key, item in value.items()
        }
        if not result:
            raise PackageSolverContractError("{} is invalid".format(field_name))
        return result

    option_routes = text_mapping(raw.get("option_routes"), "option_routes")
    if set(option_routes) != {"OPTION_0", "OPTION_1"} or len(set(option_routes.values())) != 2:
        raise PackageSolverContractError("option_routes is invalid")
    route_streams = raw.get("route_stream_products")
    if not isinstance(route_streams, (list, tuple)):
        raise PackageSolverContractError("route_stream_products is invalid")
    route_stream_products = tuple(
        text(value, "route_stream_products") for value in route_streams
    )
    if not route_stream_products or len(set(route_stream_products)) != len(route_stream_products):
        raise PackageSolverContractError("route_stream_products is invalid")
    atmospheric_product_tanks = raw.get("atmospheric_product_tanks") or {}
    if not isinstance(atmospheric_product_tanks, Mapping):
        raise PackageSolverContractError("atmospheric_product_tanks is invalid")
    atmospheric_product_tanks = {
        text(key, "atmospheric_product_tanks"): text(
            value, "atmospheric_product_tanks"
        )
        for key, value in atmospheric_product_tanks.items()
    }
    special_tanks = text_mapping(raw.get("special_tanks"), "special_tanks")
    if set(special_tanks) != {"diesel", "vgo"} or len(set(special_tanks.values())) != 2:
        raise PackageSolverContractError("special_tanks is invalid")
    unit_capacity_keys = raw.get("unit_capacity_keys")
    if not isinstance(unit_capacity_keys, Mapping):
        raise PackageSolverContractError("unit_capacity_keys is invalid")
    diesel_capacity_keys = text_mapping(
        unit_capacity_keys.get("diesel_by_unit"), "unit_capacity_keys.diesel_by_unit"
    )
    correction_prefixes = text_mapping(
        raw.get("correction_factor_prefix_by_unit"),
        "correction_factor_prefix_by_unit",
    )
    return {
        "option_routes": option_routes,
        "route_stream_products": route_stream_products,
        "atmospheric_product_tanks": atmospheric_product_tanks,
        "special_tanks": special_tanks,
        "unit_capacity_keys": {
            "atmospheric": text(
                unit_capacity_keys.get("atmospheric"),
                "unit_capacity_keys.atmospheric",
            ),
            "diesel_by_unit": diesel_capacity_keys,
            "vgo": text(unit_capacity_keys.get("vgo"), "unit_capacity_keys.vgo"),
        },
        "correction_factor_prefix_by_unit": correction_prefixes,
        "production_condition": text(
            raw.get("production_condition"), "production_condition"
        ),
    }


def _row_id(row: Mapping[str, Any], name: str) -> str:
    value = str(row.get(name) or "").strip()
    if not value:
        raise ValueError("business facts contain a blank identifier")
    return value


def _refinery_route_context(
    module: Any,
    request: Mapping[str, Any],
    tables: Mapping[str, Any],
    solver_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Convert one provider read into the standalone solver's frozen context."""

    source_rows = tables["crude_oil_yard"]
    source_by_name: dict[str, Mapping[str, Any]] = {}
    for row in source_rows:
        if not isinstance(row, Mapping):
            raise ValueError("crude source facts are invalid")
        source_id = _row_id(row, "OilYardID")
        source_name = module.normalize_crude_name(
            str(row.get("CrudeOilYard Name") or "")
        )
        if not source_name or source_name in source_by_name:
            raise ValueError("crude source facts are ambiguous")
        source_by_name[source_name] = {**row, "OilYardID": source_id}

    item_context: list[dict[str, Any]] = []
    requested_source_ids: set[str] = set()
    source_name_by_id: dict[str, str] = {}
    for item_order, item in enumerate(request["items"], start=1):
        source_name = module.normalize_crude_name(str(item["crude_origin"]))
        source = source_by_name.get(source_name)
        if source is None:
            raise ValueError("business facts are missing for an input source")
        source_id = _row_id(source, "OilYardID")
        requested_source_ids.add(source_id)
        source_name_by_id[source_id] = source_name
        item_context.append(
            {
                "item_id": str(item["item_id"]),
                "item_order": item_order,
                "source_id": source_id,
                "input_tons": _context_decimal_text(
                    item["input_weight_tons"], "input_weight_tons"
                ),
                "process_days": _context_decimal_text(
                    item["process_days"], "process_days"
                ),
            }
        )

    atmos_unit_id = str(_required_module_value(module, "ATMOS_UNIT_ID"))
    diesel_unit_ids = dict(_required_module_value(module, "DIESEL_UNIT_IDS"))
    diesel_unit_id = str(diesel_unit_ids[request["diesel_unit_no"]])
    default_diesel_unit_id = str(
        _required_module_value(module, "DEFAULT_DIESEL_UNIT_ID")
    )
    vgo_unit_id = str(_required_module_value(module, "VGO_UNIT_ID"))
    yield_atmos = str(_required_module_value(module, "YIELD_ATMOS"))
    yield_diesel_with = str(
        _required_module_value(module, "YIELD_DIESEL_WITH_VGO")
    )
    yield_diesel_without = str(
        _required_module_value(module, "YIELD_DIESEL_WITHOUT_VGO")
    )
    yield_vgo_with = str(_required_module_value(module, "YIELD_VGO_WITH_VGO"))
    yield_vgo_without = str(
        _required_module_value(module, "YIELD_VGO_WITHOUT_VGO")
    )
    diesel_feed_names = set(_required_module_value(module, "ATMOS_DIESEL_FEED"))
    vgo_feed_names = set(_required_module_value(module, "ATMOS_HC_FEED"))
    intermediate_names = set(
        _required_module_value(module, "VGO_INTERMEDIATE_NAMES")
    )
    route_stream_names = set(solver_context["route_stream_products"])
    atmospheric_other_tanks = dict(solver_context["atmospheric_product_tanks"])
    special_tanks = dict(solver_context["special_tanks"])

    hydrogen_rows = []
    for row in tables["hydrogenation_product_yield"]:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("OilYardID") or "") in requested_source_ids:
            hydrogen_rows.append(row)
    atmospheric_by_product: dict[tuple[str, str], dict[str, Any]] = {}
    for row in hydrogen_rows:
        if str(row.get("RefineryUnitID") or "") != atmos_unit_id:
            continue
        source_id = _row_id(row, "OilYardID")
        product_id = _row_id(row, "ProductName")
        if product_id in diesel_feed_names:
            feed_role = "diesel_base"
        elif product_id in vgo_feed_names:
            feed_role = "vgo_base"
        elif product_id in route_stream_names:
            feed_role = "route_stream"
        else:
            feed_role = "other"
        tank_id = None
        if feed_role == "other":
            tank_id = str(
                row.get("LogicTankID")
                or atmospheric_other_tanks.get(product_id)
                or ""
            ).strip() or None
        normalized = {
            "source_id": source_id,
            "product_id": product_id,
            "feed_role": feed_role,
            "yield_percent": _context_decimal_text(
                row.get(yield_atmos), "atmospheric yield"
            ),
            "tank_id": tank_id,
        }
        identity = (source_id, product_id)
        if product_id in route_stream_names or identity not in atmospheric_by_product:
            atmospheric_by_product[identity] = normalized

    def selected_diesel_rows(source_id: str) -> list[Mapping[str, Any]]:
        selected = []
        for row in hydrogen_rows:
            if str(row.get("OilYardID") or "") != source_id:
                continue
            if str(row.get("RefineryUnitID") or "") == diesel_unit_id:
                selected.append(row)
        if selected or diesel_unit_id == default_diesel_unit_id:
            return selected
        fallback = []
        for row in hydrogen_rows:
            if str(row.get("OilYardID") or "") != source_id:
                continue
            if (
                str(row.get("RefineryUnitID") or "")
                == default_diesel_unit_id
            ):
                fallback.append(row)
        return fallback

    unit_product_yields: list[dict[str, Any]] = []
    for source_id in sorted(requested_source_ids):
        diesel_rows = selected_diesel_rows(source_id)
        if not diesel_rows:
            raise ValueError("diesel product facts are missing for an input source")
        for row in diesel_rows:
            product_id = _row_id(row, "ProductName")
            unit_product_yields.append(
                {
                    "source_id": source_id,
                    "unit_role": "diesel",
                    "product_id": product_id,
                    "tank_id": str(row.get("LogicTankID") or "").strip() or None,
                    "yield_with_route_percent": _context_decimal_text(
                        row.get(yield_diesel_with), "diesel yield with route"
                    ),
                    "yield_without_route_percent": _context_decimal_text(
                        row.get(yield_diesel_without), "diesel yield without route"
                    ),
                    "price_per_ton": _context_decimal_text(
                        row.get("price_per_unit"), "diesel product price"
                    ),
                    "feeds_vgo": product_id in intermediate_names,
                }
            )
        vgo_rows = []
        for row in tables["vgo_product_yield"]:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("OilYardID") or "") != source_id:
                continue
            if str(row.get("VGOUnitID") or vgo_unit_id) == vgo_unit_id:
                vgo_rows.append(row)
        if not vgo_rows:
            raise ValueError("VGO product facts are missing for an input source")
        for row in vgo_rows:
            unit_product_yields.append(
                {
                    "source_id": source_id,
                    "unit_role": "vgo",
                    "product_id": _row_id(row, "ProductName"),
                    "tank_id": str(row.get("LogicTankID") or "").strip() or None,
                    "yield_with_route_percent": _context_decimal_text(
                        row.get(yield_vgo_with), "VGO yield with route"
                    ),
                    "yield_without_route_percent": _context_decimal_text(
                        row.get(yield_vgo_without), "VGO yield without route"
                    ),
                    "price_per_ton": _context_decimal_text(
                        row.get("price_per_unit"), "VGO product price"
                    ),
                    "feeds_vgo": False,
                }
            )

    tank_by_id = {}
    for row in tables["logic_tank"]:
        if not isinstance(row, Mapping):
            continue
        tank_id = str(row.get("LogicTankID") or "").strip()
        if tank_id:
            tank_by_id[tank_id] = row
    special_tank_ids = set(special_tanks.values())
    if not special_tank_ids.issubset(tank_by_id):
        raise ValueError("required special tank facts are missing")

    # The package evaluator constrains only ordinary tanks declared by the
    # logic-tank table. Product rows may carry legacy, untracked tank IDs; they
    # still contribute yield and value but do not create a capacity constraint.
    for row in atmospheric_by_product.values():
        if row["tank_id"] is not None and row["tank_id"] not in tank_by_id:
            row["tank_id"] = None
    for row in unit_product_yields:
        if row["tank_id"] is not None and row["tank_id"] not in tank_by_id:
            row["tank_id"] = None

    referenced_tanks = set(special_tank_ids)
    for row in atmospheric_by_product.values():
        if row["tank_id"] is not None:
            referenced_tanks.add(row["tank_id"])
    for row in unit_product_yields:
        if row["tank_id"] is not None:
            referenced_tanks.add(row["tank_id"])
    tank_context: list[dict[str, Any]] = []
    for tank_id in sorted(referenced_tanks):
        row = tank_by_id.get(tank_id)
        if row is None:  # guarded above and filtered for ordinary tanks
            raise ValueError("required tank facts are missing")
        available = _finite_decimal(
            row.get("AvailableCapacity_Tons") or row.get("SafetyStockThrd_Tons"),
            "tank available capacity",
        )
        current = _finite_decimal(
            row.get("CurrentCapacity_Tons") or 0, "tank current inventory"
        )
        if tank_id in special_tank_ids:
            minimum = _finite_decimal(
                row.get("LowSafetyThrd_Tons") or 0, "tank minimum inventory"
            )
            maximum = _finite_decimal(
                row.get("SafetyStockThrd_Tons") or available,
                "tank maximum inventory",
            )
            kind = "special"
        else:
            minimum = Decimal(0)
            maximum = max(available, current, Decimal(1))
            kind = "ordinary"
        if available <= 0 or maximum <= minimum or not minimum <= current <= maximum:
            raise ValueError("tank capacity facts are invalid")
        tank_context.append(
            {
                "tank_id": tank_id,
                "kind": kind,
                "available_capacity_tons": _context_decimal_text(
                    available, "tank available capacity"
                ),
                "current_inventory_tons": _context_decimal_text(
                    current, "tank current inventory"
                ),
                "minimum_inventory_tons": _context_decimal_text(
                    minimum, "tank minimum inventory"
                ),
                "maximum_inventory_tons": _context_decimal_text(
                    maximum, "tank maximum inventory"
                ),
            }
        )

    refinery_units = {}
    for row in tables["refinery_unit"]:
        if isinstance(row, Mapping):
            refinery_units[str(row.get("RefineryUnitID") or "").strip()] = row
    diesel_unit = refinery_units.get(diesel_unit_id) or refinery_units.get(
        default_diesel_unit_id
    )
    vgo_units = {}
    for row in tables["vgo_ht_unit"]:
        if isinstance(row, Mapping):
            vgo_units[str(row.get("VGOUnitID") or "").strip()] = row
    vgo_unit = vgo_units.get(vgo_unit_id)
    if diesel_unit is None or vgo_unit is None:
        raise ValueError("processing unit facts are missing")
    capacities = dict(_required_module_value(module, "RULE_DAILY_CAPACITIES"))
    capacity_keys = solver_context["unit_capacity_keys"]
    diesel_capacity_key = capacity_keys["diesel_by_unit"].get(
        request["diesel_unit_no"]
    )
    if diesel_capacity_key is None:
        raise ValueError("diesel unit capacity mapping is missing")
    unit_context = [
        {
            "unit_role": "atmospheric",
            "daily_capacity_tons": _context_decimal_text(
                capacities[capacity_keys["atmospheric"]], "atmospheric capacity"
            ),
            "feed_cost_per_ton": "0",
        },
        {
            "unit_role": "diesel",
            "daily_capacity_tons": _context_decimal_text(
                capacities[diesel_capacity_key],
                "diesel capacity",
            ),
            "feed_cost_per_ton": _context_decimal_text(
                diesel_unit.get("price_per_unit"), "diesel feed cost"
            ),
        },
        {
            "unit_role": "vgo",
            "daily_capacity_tons": _context_decimal_text(
                capacities[capacity_keys["vgo"]], "VGO capacity"
            ),
            "feed_cost_per_ton": _context_decimal_text(
                vgo_unit.get("price_per_unit"), "VGO feed cost"
            ),
        },
    ]

    energy_context: list[dict[str, Any]] = []
    diesel_energy_ids = {diesel_unit_id}
    has_diesel_energy = False
    for row in tables["energy_consume"]:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("RefineryUnitID") or "") == diesel_unit_id:
            has_diesel_energy = True
            break
    if not has_diesel_energy:
        diesel_energy_ids = {default_diesel_unit_id}
    for row in tables["energy_consume"]:
        if not isinstance(row, Mapping):
            continue
        refinery_id = str(row.get("RefineryUnitID") or "")
        row_vgo_id = str(row.get("VGOUnitID") or "")
        if refinery_id in diesel_energy_ids:
            role = "diesel"
        elif row_vgo_id == vgo_unit_id:
            role = "vgo"
        else:
            continue
        energy_context.append(
            {
                "unit_role": role,
                "consumption_per_ton": _context_decimal_text(
                    row.get("UnitConsume_Value"), "energy consumption"
                ),
                "price_per_unit": _context_decimal_text(
                    row.get("price_per_unit"), "energy price"
                ),
            }
        )

    raw_factors = dict(_required_module_value(module, "CORRECTION_FACTORS"))
    diesel_prefix = solver_context["correction_factor_prefix_by_unit"].get(
        request["diesel_unit_no"]
    )
    if diesel_prefix is None:
        raise ValueError("diesel correction factor mapping is missing")
    correction_context: dict[str, Any] = {}
    for source_id in sorted(requested_source_ids):
        source_factors = raw_factors.get(source_name_by_id[source_id])
        if not isinstance(source_factors, Mapping):
            raise ValueError("correction factors are missing for an input source")
        correction_context[source_id] = {
            "diesel_load": _context_decimal_text(
                source_factors["{}_load_a".format(diesel_prefix)],
                "diesel load correction",
            ),
            "diesel_product": _context_decimal_text(
                source_factors["{}_product_b".format(diesel_prefix)],
                "diesel product correction",
            ),
            "vgo_load": _context_decimal_text(
                source_factors["vgo_load_a"], "VGO load correction"
            ),
            "vgo_product": _context_decimal_text(
                source_factors["vgo_product_b"], "VGO product correction"
            ),
        }

    return {
        "schema_version": "refinery_route_context.v1",
        "profile_id": "refinery_route.v1",
        "metric_scale": 100,
        "rounding": "ROUND_HALF_EVEN",
        "plan_month": request["plan_month"],
        "production_condition": request["production_condition"],
        "items": item_context,
        "model": {
            "option_routes": {"OPTION_0": "diesel", "OPTION_1": "vgo"},
            "special_tanks": special_tanks,
            "constraint_policy": {
                "batch_unit_load": "advisory",
                "monthly_unit_load": "hard",
                "tank_capacity": "hard",
            },
            "correction_factors": correction_context,
        },
        "tables": {
            "atmospheric_yields": list(atmospheric_by_product.values()),
            "unit_product_yields": unit_product_yields,
            "units": unit_context,
            "tanks": tank_context,
            "energy_costs": energy_context,
        },
    }


def _headroom(load: Any, capacity: Any, field_name: str) -> Decimal | None:
    capacity_value = _finite_decimal(capacity, "{}.capacity".format(field_name))
    if capacity_value <= 0:
        return None
    load_value = _finite_decimal(load, "{}.load".format(field_name))
    return (capacity_value - load_value) / capacity_value


def _validate_case_consistency(case: Mapping[str, Any]) -> None:
    """Reject silent differences between batch facts and monthly summaries.

    A package may intentionally apply a documented adjustment (maintenance,
    inventory carry-over, etc.), but that adjustment must be represented in an
    explicit ledger.  Without one, a monthly value that differs from the sum
    of the batch values is not safe to present as a capacity conclusion.
    """
    batches = case.get("batches")
    monthly = case.get("monthly_unit_load")
    if not isinstance(batches, list) or not isinstance(monthly, Mapping):
        return
    units = monthly.get("units")
    if not isinstance(units, Mapping):
        return

    batch_totals: dict[str, Decimal] = {}
    for batch in batches:
        if not isinstance(batch, Mapping):
            raise ValueError("package evaluator batch is invalid")
        unit_loads = batch.get("unit_loads")
        if not isinstance(unit_loads, Mapping):
            raise ValueError("package evaluator unit loads are invalid")
        for unit_name, status in unit_loads.items():
            if not isinstance(status, Mapping) or status.get("input_tons") is None:
                continue
            value = _finite_decimal(
                status.get("input_tons"), "batch.{}.input_tons".format(unit_name)
            )
            batch_totals[str(unit_name)] = batch_totals.get(str(unit_name), Decimal(0)) + value

    adjustments: dict[str, Decimal] = {}
    ledger = case.get("adjustment_ledger")
    if ledger is not None:
        if not isinstance(ledger, list):
            raise ValueError("package evaluator adjustment ledger is invalid")
        for entry in ledger:
            if not isinstance(entry, Mapping) or not entry.get("unit") or entry.get("tons") is None:
                raise ValueError("package evaluator adjustment ledger is invalid")
            reason = str(entry.get("reason") or "").strip()
            if not reason:
                raise ValueError("package evaluator adjustment ledger reason is missing")
            unit = str(entry["unit"])
            adjustments[unit] = adjustments.get(unit, Decimal(0)) + _finite_decimal(
                entry["tons"], "adjustment_ledger.{}.tons".format(unit)
            )

    for unit_name, status in units.items():
        if not isinstance(status, Mapping) or status.get("monthly_input_tons") is None:
            continue
        expected = batch_totals.get(str(unit_name), Decimal(0)) + adjustments.get(
            str(unit_name), Decimal(0)
        )
        actual = _finite_decimal(
            status.get("monthly_input_tons"),
            "monthly.{}.monthly_input_tons".format(unit_name),
        )
        if abs(actual - expected) > Decimal("0.01"):
            raise BusinessSummaryInconsistentError(
                "monthly load is inconsistent for {}: batch_total={} adjustment={} monthly={}".format(
                    unit_name, expected - adjustments.get(str(unit_name), Decimal(0)),
                    adjustments.get(str(unit_name), Decimal(0)), actual
                )
            )


def _safety_margin(case: Mapping[str, Any]) -> Decimal:
    margins: list[Decimal] = []
    policy = case.get("constraint_policy")
    batch_unit_policy = "hard"
    if isinstance(policy, Mapping):
        declared = policy.get("batch_unit_load")
        if declared is not None:
            batch_unit_policy = str(declared).strip().lower()
    if batch_unit_policy not in {"hard", "advisory"}:
        raise ValueError("package evaluator batch unit constraint policy is invalid")
    batches = case.get("batches")
    if not isinstance(batches, list):
        raise ValueError("package evaluator batches are invalid")
    for batch_index, batch in enumerate(batches, start=1):
        if not isinstance(batch, Mapping):
            raise ValueError("package evaluator batch is invalid")
        unit_loads = batch.get("unit_loads")
        if not isinstance(unit_loads, Mapping):
            raise ValueError("package evaluator unit loads are invalid")
        for unit_name, status in unit_loads.items():
            if not isinstance(status, Mapping):
                raise ValueError("package evaluator unit load is invalid")
            margin = _headroom(
                status.get("input_tons"),
                status.get("capacity_tons"),
                "batch{}.{}".format(batch_index, unit_name),
            )
            if margin is not None and batch_unit_policy == "hard":
                margins.append(margin)
        tank_status = batch.get("tank_status")
        if not isinstance(tank_status, Mapping):
            raise ValueError("package evaluator tank status is invalid")
        for tank_id, status in tank_status.items():
            if not isinstance(status, Mapping):
                raise ValueError("package evaluator tank status is invalid")
            margin = _headroom(
                status.get("daily_input_tons"),
                status.get("available_capacity_tons"),
                "batch{}.tank{}".format(batch_index, tank_id),
            )
            if margin is not None:
                margins.append(margin)

    monthly = case.get("monthly_unit_load")
    if isinstance(monthly, Mapping):
        units = monthly.get("units")
        if not isinstance(units, Mapping):
            raise ValueError("package evaluator monthly unit loads are invalid")
        for unit_name, status in units.items():
            if not isinstance(status, Mapping):
                raise ValueError("package evaluator monthly unit load is invalid")
            margin = _headroom(
                status.get("monthly_input_tons"),
                status.get("monthly_capacity_tons"),
                "monthly.{}".format(unit_name),
            )
            if margin is not None:
                margins.append(margin)

    special = case.get("special_tank_status")
    if not isinstance(special, Mapping) or not isinstance(special.get("batches"), list):
        raise ValueError("package evaluator special tank status is invalid")
    for batch in special["batches"]:
        if not isinstance(batch, Mapping):
            raise ValueError("package evaluator special tank batch is invalid")
        batch_no = batch.get("batch_no")
        if batch_no is not None and (
            not isinstance(batch_no, str) or not batch_no.strip()
        ):
            raise ValueError("package evaluator special tank batch is invalid")
        tank_statuses = {
            tank_id: status
            for tank_id, status in batch.items()
            if tank_id != "batch_no"
        }
        if not tank_statuses:
            raise ValueError("package evaluator special tank batch is invalid")
        for tank_id, status in tank_statuses.items():
            if not isinstance(status, Mapping):
                raise ValueError("package evaluator special tank is invalid")
            low = _finite_decimal(
                status.get("low_safety_threshold_tons"), "tank{}.low".format(tank_id)
            )
            high = _finite_decimal(
                status.get("safety_stock_threshold_tons"),
                "tank{}.high".format(tank_id),
            )
            end = _finite_decimal(
                status.get("end_capacity_tons"), "tank{}.end".format(tank_id)
            )
            span = high - low
            if span <= 0:
                raise ValueError("package evaluator special tank bounds are invalid")
            margins.extend(((end - low) / span, (high - end) / span))
    if not margins:
        raise ValueError("package evaluator produced no safety constraints")
    return min(margins) * Decimal("100")


def evaluate_payload(module: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_values = payload.get("normalized_input")
    if not isinstance(raw_values, Mapping):
        raise ValueError("normalized_input is invalid")
    solver_context = _solver_context_config(module)
    request = _normalize_request(module, raw_values, solver_context)
    candidates = _validate_candidates(payload.get("candidates"), len(request["items"]))
    tables = module.load_tables()
    _validate_tables(module, tables, request["items"])
    refinery_route_context = _refinery_route_context(
        module,
        request,
        tables,
        solver_context,
    )

    metrics: dict[str, Any] = {}
    for candidate_id, assignments in candidates:
        plans = [
            module.BatchPlan(
                batch_no=item["item_id"],
                crude_name=item["crude_origin"],
                input_weight_tons=float(item["input_weight_tons"]),
                process_days=float(item["process_days"]),
                vgo_route=solver_context["option_routes"][option],
                diesel_unit_no=request["diesel_unit_no"],
            )
            for item, option in zip(request["items"], assignments)
        ]
        report = module.build_report(
            tables,
            plans,
            plan_month=request["plan_month"],
        )
        cases = report.get("cases") if isinstance(report, Mapping) else None
        if not isinstance(cases, list) or len(cases) != 1 or not isinstance(cases[0], Mapping):
            raise ValueError("package evaluator report is invalid")
        case = cases[0]
        _validate_case_consistency(case)
        eligible = case.get("pass_core_constraints")
        if not isinstance(eligible, bool):
            raise ValueError("package evaluator feasibility is invalid")
        metrics[candidate_id] = {
            "eligible": eligible,
            "primary_score": _decimal_text(
                _finite_decimal(case.get("profit_yuan"), "profit_yuan")
            ),
            "safety_margin": _decimal_text(_safety_margin(case)),
        }
    return {
        "route_metrics": {
            "metric_scale": 100,
            "rounding": "ROUND_HALF_EVEN",
            "candidates": metrics,
        },
        "refinery_route_context": refinery_route_context,
        "as_of": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_evidence": [
            {
                "kind": "package_business_evaluation",
                "ref": "package-evaluator",
            }
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evaluator", required=True, type=Path)
    args = parser.parse_args()
    try:
        payload = _strict_json(sys.stdin.read(_MAX_INPUT_BYTES + 1))
        result = evaluate_payload(_load_evaluator(args.evaluator), payload)
        _write_json_result(result)
        return 0
    except PackageSolverContractError:
        _write_json_result(
            {
                "error": {
                    "code": _PACKAGE_SOLVER_CONTRACT_ERROR_CODE,
                    "detail": "knowledge package solver contract is incompatible",
                }
            }
        )
        return 2
    except BusinessSummaryInconsistentError:
        _write_json_result(
            {
                "error": {
                    "code": "BUSINESS_SUMMARY_INCONSISTENT",
                    "detail": "structured batch and monthly business totals are inconsistent",
                }
            }
        )
        return 2
    except Exception as exc:
        detail = " ".join(str(exc).split())[:512] or "package evaluator failed"
        _write_json_result(
            {"error": {"code": "business_evaluation_failed", "detail": detail}}
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
