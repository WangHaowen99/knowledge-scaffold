# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from copy import deepcopy

from solver_coordination.input_binding import SolverInputBinder
from solver_coordination.profiles import (
    SolverProfileRegistration,
    SolverProfileRegistry,
)
from solver_coordination.profiles.input_adapter import ProfileInputAdapterService
from solver_coordination.profiles.ordered_binary_route import (
    OrderedBinaryRouteInputAdapter,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "plan_month": {"type": "string", "minLength": 7, "maxLength": 7},
        "production_condition": {"type": "string", "enum": ["MODE_A", "MODE_B"]},
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {
                "type": "object",
                "properties": {
                    "item_order": {"type": "integer", "minimum": 1, "maximum": 64},
                    "item_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "quantity": {"type": "string", "minLength": 1, "maxLength": 128},
                    "quantity_wan_tons": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "input_weight_tons": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "duration": {"type": "string", "minLength": 1, "maxLength": 128},
                    "process_days": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "source": {"type": "string", "minLength": 1, "maxLength": 128},
                    "crude_origin": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                },
                "required": ["item_order", "item_id", "quantity", "duration"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["plan_month", "items"],
    "additionalProperties": False,
}


class _Port:
    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def resolve_facts(self, **_kwargs):
        return {}

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def compile_problem(self, **_kwargs):
        return {}

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def verify(self, **_kwargs):
        return {}

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def present(self, **_kwargs):
        return {}


def _adapter_service():
    port = _Port()
    registry = SolverProfileRegistry(
        [
            SolverProfileRegistration(
                profile_id="ordered_binary_route.v1",
                fact_provider=port,
                compiler=port,
                verifier=port,
                presenter=port,
                input_adapter=OrderedBinaryRouteInputAdapter(),
            )
        ]
    )
    return ProfileInputAdapterService(registry)


def _capability():
    return {"decision_model": {"profile_id": "ordered_binary_route.v1"}}


def _bind(values):
    return SolverInputBinder().bind(
        operation_key="solve_synthetic_route",
        schema=SCHEMA,
        values=values,
        input_adapter=_adapter_service(),
        capability=_capability(),
    )


def _generic_values():
    return {
        "plan_month": "2099-03",
        "items": [
            {
                "item_order": 1,
                "item_id": "ITEM_SYNTHETIC_0001",
                "quantity": "2.500",
                "duration": "1.50",
                "source": " SOURCE   SYNTHETIC A ",
                "input_weight_tons": "25000",
            }
        ],
    }


def test_ordered_route_adapter_canonicalizes_generic_item_aliases():
    result = _bind(_generic_values())

    assert result.status == "bound"
    assert result.normalized_input.values["items"][0] == {
        "item_order": 1,
        "item_id": "ITEM_SYNTHETIC_0001",
        "quantity_wan_tons": "2.5",
        "process_days": "1.5",
        "crude_origin": "SOURCE SYNTHETIC A",
    }
    assert result.normalized_input.normalization["input_adapter_id"] == "ordered_binary_route.input.v1"
    assert result.normalized_input.normalization["input_adapter_revision"] == "2"
    assert result.normalized_input.normalization["alias_mapping"] == {
        "items[].duration": "items[].process_days",
        "items[].quantity": "items[].quantity_wan_tons",
        "items[].source": "items[].crude_origin",
    }


def test_equal_alias_forms_produce_the_same_normalized_input_digest():
    generic = _bind(_generic_values())
    canonical_values = deepcopy(_generic_values())
    canonical_values["items"][0].update(
        {
            "quantity_wan_tons": "2.5",
            "process_days": "1.500",
            "crude_origin": "SOURCE SYNTHETIC A",
        }
    )

    canonical = _bind(canonical_values)

    assert generic.status == canonical.status == "bound"
    assert generic.normalized_input.digest == canonical.normalized_input.digest


def test_canonical_decimals_override_decorated_legacy_aliases():
    values = _generic_values()
    values["items"][0].update(
        {
            "quantity": "2.500万吨",
            "quantity_wan_tons": "2.5",
            "duration": "1.50天",
            "process_days": "1.5",
        }
    )

    result = _bind(values)

    assert result.status == "bound"
    assert result.normalized_input.values["items"][0]["quantity_wan_tons"] == "2.5"
    assert result.normalized_input.values["items"][0]["process_days"] == "1.5"


def test_decorated_legacy_aliases_accept_their_declared_units():
    values = _generic_values()
    values["items"][0].update(
        {
            "quantity": "2.500 万吨",
            "duration": "1.50 天",
        }
    )

    result = _bind(values)

    assert result.status == "bound"
    assert result.normalized_input.values["items"][0]["quantity_wan_tons"] == "2.5"
    assert result.normalized_input.values["items"][0]["process_days"] == "1.5"


def test_decorated_legacy_aliases_reject_undeclared_units():
    values = _generic_values()
    values["items"][0]["quantity"] = "25000 tons"

    result = _bind(values)

    assert result.status == "invalid_input"
    assert result.code == "invalid_solver_input"
    assert result.normalized_input is None


def test_conflicting_aliases_fail_with_a_stable_error_code():
    values = _generic_values()
    values["items"][0]["quantity_wan_tons"] = "3.0"

    result = _bind(values)

    assert result.status == "invalid_input"
    assert result.code == "invalid_solver_input"
    assert result.errors == (
        {
            "field": "items[0].quantity_wan_tons",
            "code": "input_alias_conflict",
            "message": "input aliases disagree",
        },
    )
    assert result.normalized_input is None


def test_optional_top_level_input_is_preserved_without_becoming_required():
    values = _generic_values()
    values["production_condition"] = "MODE_B"

    result = _bind(values)

    assert result.status == "bound"
    assert result.normalized_input.values["production_condition"] == "MODE_B"
