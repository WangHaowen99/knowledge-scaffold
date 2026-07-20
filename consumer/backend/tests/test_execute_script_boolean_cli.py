"""Regression tests for execute_script boolean CLI argument semantics."""

from __future__ import annotations

import pytest

from agent.tools.execute_script import _build_cli_args_from_parameters


@pytest.mark.parametrize(
    ("parameter", "value", "expected"),
    [
        ({"name": "enabled", "flag": "--enabled", "type": "bool"}, True, ["--enabled", "true"]),
        ({"name": "enabled", "flag": "--enabled", "type": "boolean"}, False, ["--enabled", "false"]),
        (
            {"name": "verbose", "flag": "--verbose", "type": "boolean", "action": "store_true"},
            True,
            ["--verbose"],
        ),
        (
            {"name": "verbose", "flag": "--verbose", "type": "boolean", "action": "store_true"},
            False,
            [],
        ),
        (
            {"name": "cache", "flag": "--cache", "type": "bool", "action": "store_false"},
            True,
            [],
        ),
        (
            {"name": "cache", "flag": "--cache", "type": "bool", "action": "store_false"},
            False,
            ["--cache"],
        ),
        ({"name": "json", "flag": "--json", "type": "boolean"}, True, ["--json"]),
        ({"name": "json", "flag": "--json", "type": "boolean"}, False, []),
    ],
)
def test_execute_script_builds_boolean_cli_arguments(parameter, value, expected):
    parameter = {**parameter, "required": False}

    args, missing, invalid = _build_cli_args_from_parameters(
        script_parameters=[parameter],
        arguments={parameter["name"]: value},
    )

    assert (args, missing, invalid) == (expected, [], [])
