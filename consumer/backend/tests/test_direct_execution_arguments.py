"""Regression tests for direct-execution script argument normalization."""

from __future__ import annotations

import pytest

from agent.direct_execution.arguments import build_direct_cli_args


@pytest.mark.parametrize(
    ("parameter_type", "value", "expected"),
    [
        ("bool", True, ["--enabled", "true"]),
        ("bool", False, ["--enabled", "false"]),
        ("boolean", True, ["--enabled", "true"]),
        ("boolean", False, ["--enabled", "false"]),
    ],
)
def test_value_style_boolean_keeps_explicit_value(parameter_type, value, expected):
    args, missing, error = build_direct_cli_args(
        script_parameters=[
            {
                "name": "enabled",
                "flag": "--enabled",
                "required": False,
                "type": parameter_type,
            }
        ],
        business_arguments={"enabled": value},
    )

    assert (args, missing, error) == (expected, [], None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, ["--verbose"]), (False, [])],
)
def test_store_true_boolean_uses_flag_presence(value, expected):
    args, missing, error = build_direct_cli_args(
        script_parameters=[
            {
                "name": "verbose",
                "flag": "--verbose",
                "required": False,
                "type": "boolean",
                "action": "store_true",
            }
        ],
        business_arguments={"verbose": value},
    )

    assert (args, missing, error) == (expected, [], None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, []), (False, ["--cache"])],
)
def test_store_false_boolean_uses_flag_presence(value, expected):
    args, missing, error = build_direct_cli_args(
        script_parameters=[
            {
                "name": "cache",
                "flag": "--cache",
                "required": False,
                "type": "bool",
                "action": "store_false",
            }
        ],
        business_arguments={"cache": value},
    )

    assert (args, missing, error) == (expected, [], None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, ["--json"]), (False, [])],
)
def test_legacy_json_boolean_uses_switch_convention(value, expected):
    args, missing, error = build_direct_cli_args(
        script_parameters=[
            {
                "name": "json",
                "flag": "--json",
                "required": False,
                "type": "boolean",
            }
        ],
        business_arguments={"json": value},
    )

    assert (args, missing, error) == (expected, [], None)
