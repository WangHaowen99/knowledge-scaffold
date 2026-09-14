# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Shared CLI normalization helpers for structured script parameters."""

from __future__ import annotations

from typing import Any, List, Mapping, Optional

from agent.runtime_fields import DETERMINISTIC_ROUTING_ARGUMENT_FIELDS


# These values are supplied by the request lifecycle.  They must never be
# sourced from model-provided operation arguments.  Keep both dashed and
# underscored spellings because catalogs are authored by hand and argparse
# flags conventionally use dashes.
_RUNTIME_PARAMETER_FIELDS = {
    "session_id",
    "run_id",
    "turn_id",
    "user_id",
    "workspace_id",
    "selected_ontology_id",
}

_EXPLICIT_RUNTIME_PARAMETER_FIELDS = (
    _RUNTIME_PARAMETER_FIELDS
    | {"ontology_id", "ontology_version"}
    | set(DETERMINISTIC_ROUTING_ARGUMENT_FIELDS)
)

# Runtime identity and package-selection fields are reserved by the execution
# layer.  Catalog metadata is untrusted configuration and cannot downgrade one
# of these names to a user/business parameter.
RUNTIME_RESERVED_PARAMETER_FIELDS = frozenset(_EXPLICIT_RUNTIME_PARAMETER_FIELDS)


def runtime_reserved_parameter_field(parameter: Mapping[str, Any]) -> Optional[str]:
    candidates = [
        parameter.get("name"),
        parameter.get("flag"),
        parameter.get("placeholder"),
    ]
    aliases = parameter.get("aliases")
    if isinstance(aliases, list):
        candidates.extend(aliases)
    for candidate in candidates:
        normalized = (
            str(candidate or "")
            .strip()
            .strip("<>")
            .removeprefix("runtime.")
            .lstrip("-")
            .replace("-", "_")
        )
        if normalized in RUNTIME_RESERVED_PARAMETER_FIELDS:
            return normalized
    return None


def _explicit_runtime_requested(parameter: Mapping[str, Any]) -> bool:
    explicit = parameter.get("runtime")
    source = str(parameter.get("source") or "").strip().lower()
    if explicit is False or source in {"business", "user"}:
        return False
    if explicit is True or source == "runtime":
        return True
    if isinstance(explicit, str):
        normalized = explicit.strip().lower()
        if normalized in {"false", "no", "0", "off"}:
            return False
        if normalized:
            return True
    for key in ("placeholder", "value", "default"):
        raw_value = parameter.get(key)
        if isinstance(raw_value, str) and raw_value.strip().startswith("<runtime."):
            return True
    name = str(parameter.get("name") or "").strip()
    return name.startswith("runtime.") or name.startswith("<runtime.")


def runtime_parameter_field(parameter: Mapping[str, Any]) -> Optional[str]:
    """Return the canonical runtime field for a structured parameter.

    A catalog may mark a parameter explicitly with ``runtime``/``source`` or
    use the conventional field/flag name.  The latter keeps old catalogs
    compatible while still ensuring that identity-bearing values are backend
    controlled.
    """
    explicit = parameter.get("runtime")
    source = str(parameter.get("source") or "").strip().lower()
    explicit_disabled = isinstance(explicit, str) and explicit.strip().lower() in {
        "false",
        "no",
        "0",
        "off",
    }
    explicit_runtime = _explicit_runtime_requested(parameter)
    if explicit is False or explicit_disabled or source in {"business", "user"}:
        return None
    candidates = []
    if isinstance(explicit, str) and explicit.strip().lower() not in {"true", "yes", "1"}:
        candidates.append(explicit)
    for key in ("placeholder", "value", "default"):
        raw_value = parameter.get(key)
        if isinstance(raw_value, str) and raw_value.strip().startswith("<runtime."):
            candidates.append(raw_value)
    name = str(parameter.get("name") or "").strip()
    if name.startswith("runtime.") or name.startswith("<runtime."):
        candidates.append(name)
    if explicit_runtime and not candidates:
        candidates.extend(
            [
                str(parameter.get("name") or "").strip(),
                str(parameter.get("flag") or "").strip().lstrip("-"),
            ]
        )
    for candidate in candidates:
        normalized = str(candidate).strip().strip("<>").removeprefix("runtime.").replace("-", "_")
        if normalized in (
            _EXPLICIT_RUNTIME_PARAMETER_FIELDS if explicit_runtime else _RUNTIME_PARAMETER_FIELDS
        ):
            return normalized

    # Structured catalogs generated from argparse historically did not carry
    # a runtime/source column.  Preserve the established convention for the
    # identity/context flags, while deliberately excluding ``ontology_id``:
    # that name is also a legitimate business input for many operations.
    if not explicit_runtime:
        for candidate in (
            str(parameter.get("name") or "").strip(),
            str(parameter.get("flag") or "").strip().lstrip("-"),
        ):
            normalized = candidate.replace("-", "_")
            if normalized in _RUNTIME_PARAMETER_FIELDS:
                return normalized
    return None


def is_runtime_parameter(parameter: Mapping[str, Any]) -> bool:
    """Whether a structured catalog parameter is backend-controlled."""
    return _explicit_runtime_requested(parameter) or runtime_parameter_field(parameter) is not None


def script_parameter_argument_keys(parameter: Mapping[str, Any]) -> List[str]:
    """Return deterministic business/runtime aliases accepted for one parameter."""
    keys: List[str] = []

    def add(value: Any, *, strip_flag: bool = False, strip_placeholder: bool = False) -> None:
        text = str(value or "").strip()
        if not text:
            return
        if strip_placeholder and text.startswith("<") and text.endswith(">"):
            text = text[1:-1].strip()
            if text.startswith("?"):
                text = text[1:].strip()
        if not text:
            return
        variants = [text]
        if strip_flag and text.startswith("-"):
            variants.append(text.lstrip("-"))
        for candidate in list(variants):
            variants.extend(
                [
                    candidate.replace("-", "_"),
                    candidate.replace("_", "-"),
                ]
            )
        for candidate in variants:
            if candidate and candidate not in keys:
                keys.append(candidate)

    add(parameter.get("name"))
    add(parameter.get("flag"), strip_flag=True)
    aliases = parameter.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            add(alias)
    add(parameter.get("placeholder"), strip_placeholder=True)
    add(parameter.get("description"))
    return keys


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
