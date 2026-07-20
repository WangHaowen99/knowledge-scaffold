"""Shared CLI normalization helpers for structured script parameters."""

from __future__ import annotations

from typing import Any, List, Mapping


def normalize_boolean_action(value: Any) -> str:
    """Return a supported argparse boolean action, if one is declared."""
    action = str(value or "").strip().lower()
    return action if action in {"store_true", "store_false"} else ""


def append_script_parameter_cli_argument(
    result: List[str],
    *,
    parameter: Mapping[str, Any],
    flag: str,
    value: str,
) -> None:
    """Append one parameter using value-style or presence-style boolean semantics."""
    parameter_type = str(parameter.get("type") or "string").strip().lower()
    if parameter_type in {"bool", "boolean"} and flag:
        action = normalize_boolean_action(parameter.get("action"))
        if (
            not action
            and str(parameter.get("name") or "").strip().lower() == "json"
            and flag.strip().lower() == "--json"
        ):
            action = "store_true"

        normalized_value = str(value).strip().lower()
        if action == "store_true":
            if normalized_value == "true":
                result.append(flag)
            return
        if action == "store_false":
            if normalized_value == "false":
                result.append(flag)
            return

    if flag:
        result.append(flag)
    result.append(value)
