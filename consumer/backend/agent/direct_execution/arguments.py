"""Business and runtime parameter handling for the direct execution branch.

Responsible for:
- Detecting <runtime.xxx> placeholders in script argument definitions.
- Separating business arguments from runtime requirements.
- Building complete CLI argument lists with both business and runtime values.

Supports two formats:
1. Structured ``script_parameters`` — list of ``{name, flag, type, required, ...}``.
2. Legacy flat ``script_args`` — ``["--flag", "<Placeholder>", ...]``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from utils.logging import get_logger
from agent.direct_execution.models import RuntimeContext
from agent.tools.script_parameters import (
    append_script_parameter_cli_argument,
    is_runtime_parameter,
    runtime_parameter_field,
    script_parameter_argument_keys,
)

logger = get_logger("direct_execution.arguments")

_RUNTIME_PLACEHOLDER_PATTERN = re.compile(r"^<runtime\.([\w\-]+)>$")
_OPTIONAL_PLACEHOLDER_PATTERN = re.compile(r"^<\?([^>]+)>$")

# Canonical runtime key to RuntimeContext field. Dashed aliases are normalized
# at the boundary instead of being duplicated in this table.
_RUNTIME_KEY_TO_CTX_FIELD: Dict[str, str] = {
    "session_id": "session_id",
    "run_id": "run_id",
    "turn_id": "turn_id",
    "user_id": "user_id",
    "workspace_id": "workspace_id",
    "selected_ontology_id": "selected_ontology_id",
    "ontology_id": "selected_ontology_id",
    "selected_ontology_version": "selected_ontology_version",
    "ontology_version": "selected_ontology_version",
}

def _canonical_runtime_key(key: str) -> str:
    return str(key or "").strip().replace("-", "_")


def runtime_context_field(key: str) -> Optional[str]:
    """Resolve dashed or underscored runtime keys to one context field."""
    return _RUNTIME_KEY_TO_CTX_FIELD.get(_canonical_runtime_key(key))


def is_runtime_placeholder(value: str) -> bool:
    """Check if a string is a <runtime.xxx> placeholder."""
    return bool(_RUNTIME_PLACEHOLDER_PATTERN.match(value.strip()))


def is_optional_placeholder(value: str) -> bool:
    """Check if a string is an optional <?xxx> placeholder."""
    return bool(_OPTIONAL_PLACEHOLDER_PATTERN.match(value.strip()))


def extract_business_placeholder_keys(
    script_args: Sequence[str] = (),
    script_parameters: Sequence[Mapping[str, Any]] = (),
) -> Set[str]:
    """Extract declared business placeholder keys from script argument definitions.

    Supports both flat ``script_args`` and structured ``script_parameters``.
    ``<runtime.xxx>`` placeholders are excluded.
    """
    if script_parameters:
        keys: Set[str] = set()
        for param in script_parameters:
            if not _is_runtime_param(param):
                keys.update(script_parameter_argument_keys(param))
        return keys

    # Legacy flat format
    keys = set()
    for item in script_args:
        stripped = str(item).strip()
        if stripped.startswith("--"):
            flag_key = stripped.lstrip("-")
            keys.update({stripped, flag_key, flag_key.replace("-", "_")})
            continue
        if not stripped.startswith("<") or not stripped.endswith(">"):
            continue
        if _RUNTIME_PLACEHOLDER_PATTERN.match(stripped):
            continue
        key = stripped[1:-1].strip()
        if key.startswith("?"):
            key = key[1:]
        if key:
            keys.update({key, key.replace("-", "_"), key.replace("_", "-")})
    return keys


def extract_runtime_requirements(
    script_args: Sequence[str] = (),
    script_parameters: Sequence[Mapping[str, Any]] = (),
) -> List[str]:
    """Extract runtime parameter names.

    For structured ``script_parameters``, identifies only parameters explicitly
    marked as runtime (or carrying a ``runtime.`` placeholder).
    For flat ``script_args``, parses ``<runtime.xxx>`` placeholders.
    """
    if script_parameters:
        requirements: List[str] = []
        for param in script_parameters:
            if not bool(param.get("required", True)) or not is_runtime_parameter(param):
                continue
            field = runtime_parameter_field(param)
            requirements.append(
                field or "unsupported_runtime:{}".format(_parameter_name(param) or "<unnamed>")
            )
        return requirements

    # Legacy flat format
    requirements = []
    for arg in script_args:
        m = _RUNTIME_PLACEHOLDER_PATTERN.match(str(arg).strip())
        if m:
            key = _canonical_runtime_key(m.group(1))
            if runtime_context_field(key) is not None:
                requirements.append(key)
    return requirements


def _runtime_key_from_flag(flag: str) -> Optional[str]:
    """Extract a runtime key from a CLI flag, e.g. ``--session-id`` → ``session-id``."""
    key = flag.lstrip("-").strip()
    canonical_key = _canonical_runtime_key(key)
    return canonical_key if runtime_context_field(canonical_key) is not None else None


def _is_runtime_param(parameter: Mapping[str, Any]) -> bool:
    """Check if a structured parameter is a runtime parameter."""
    return is_runtime_parameter(parameter)


def filter_business_args(script_args: Sequence[str]) -> List[str]:
    """Remove runtime placeholders and their preceding flags from the args list.

    Used to produce the public script_catalog (visible to the agent) that only
    contains business parameters.
    """
    result: List[str] = []
    i = 0
    args_list = list(script_args)
    while i < len(args_list):
        item = str(args_list[i])
        if item.startswith("--") and i + 1 < len(args_list) and is_runtime_placeholder(str(args_list[i + 1])):
            i += 2  # Skip --flag and its <runtime.xxx> value
            continue
        if is_runtime_placeholder(item):
            i += 1  # Orphaned runtime placeholder (should not happen)
            continue
        result.append(item)
        i += 1
    return result


def build_direct_cli_args(
    script_args: Sequence[str] = (),
    script_parameters: Sequence[Mapping[str, Any]] = (),
    business_arguments: Optional[Dict[str, Any]] = None,
    runtime_context: Optional[RuntimeContext] = None,
) -> Tuple[List[str], List[str], Optional[str]]:
    """Build the complete CLI argument list for direct script execution.

    Parameters
    ----------
    script_args : Legacy flat arg definitions (``["--flag", "<Placeholder>"]``).
    script_parameters : Structured parameter definitions.
    business_arguments : Business argument values keyed by name.
    runtime_context : Runtime context for injecting ``<runtime.xxx>`` values.

    Returns
    -------
    Tuple of (cli_args, missing_business_args, system_error)
        - cli_args: complete list of CLI arguments
        - missing_business_args: business parameter names the user needs to supply
        - system_error: None if OK, error string if system/runtime context is incomplete
    """
    if business_arguments is None:
        business_arguments = {}

    if script_parameters:
        return _build_direct_cli_args_from_parameters(
            script_parameters=script_parameters,
            business_arguments=business_arguments,
            runtime_context=runtime_context,
        )

    # Legacy flat format path
    return _build_direct_cli_args_legacy(
        script_args=script_args,
        business_arguments=business_arguments,
        runtime_context=runtime_context,
    )


# ---------------------------------------------------------------------------
# Structured parameter path  (mirrors main branch's _build_cli_args_from_parameters)
# ---------------------------------------------------------------------------


def _build_direct_cli_args_from_parameters(
    *,
    script_parameters: Sequence[Mapping[str, Any]],
    business_arguments: Dict[str, Any],
    runtime_context: Optional[RuntimeContext] = None,
) -> Tuple[List[str], List[str], Optional[str]]:
    """Build CLI arguments from structured parameter definitions.

    Returns (result, missing, system_error).  Missing are business parameter
    labels the caller did not supply.  ``system_error`` is set for type
    conversion errors or unknown arguments.
    """
    result: List[str] = []
    consumed: Set[str] = set()
    missing: List[str] = []
    system_error: Optional[str] = None

    for parameter in script_parameters:
        name = _parameter_name(parameter)
        if not name:
            continue
        flag = str(parameter.get("flag") or "").strip()
        required = bool(parameter.get("required", True))

        runtime_owned = is_runtime_parameter(parameter)
        runtime_field = runtime_parameter_field(parameter)
        if runtime_owned:
            matched_key = None
            # Treat user-supplied aliases as consumed only to avoid reporting a
            # duplicate unknown argument; their value is intentionally ignored.
            for candidate in _parameter_argument_keys(parameter):
                if candidate in business_arguments:
                    consumed.add(candidate)
            if not runtime_field:
                system_error = "Unsupported runtime parameter '{}'".format(name)
                break
            context_field = runtime_context_field(runtime_field) or runtime_field
            value = runtime_context.get(context_field) if runtime_context else None
        else:
            value, matched_key = _resolve_parameter_value(
                parameter=parameter,
                arguments=business_arguments,
            )

        if matched_key:
            consumed.add(matched_key)
        if value is None:
            if runtime_field:
                if required:
                    system_error = "Runtime context missing '{}'".format(runtime_field)
                    break
            elif required:
                missing.append(_parameter_label(parameter))
            continue

        multiple = bool(parameter.get("multiple", False))
        if multiple:
            values = _coerce_multiple_values(value)
            if not values:
                if required:
                    missing.append(_parameter_label(parameter))
                continue
            for item in values:
                converted, error = _convert_parameter_value(item, parameter)
                if error:
                    system_error = "{}: {}".format(name, error)
                    break
                append_script_parameter_cli_argument(
                    result,
                    parameter=parameter,
                    flag=flag,
                    value=converted,
                )
            if system_error:
                break
            continue

        if isinstance(value, (list, tuple, set, dict)) and not _accepts_structured_value(parameter):
            system_error = "{} does not accept multiple values".format(name)
            break
        converted, error = _convert_parameter_value(value, parameter)
        if error:
            system_error = "{}: {}".format(name, error)
            break
        append_script_parameter_cli_argument(
            result,
            parameter=parameter,
            flag=flag,
            value=converted,
        )

    if system_error:
        return [], [], system_error

    # Check for unknown arguments
    for key, value in business_arguments.items():
        if value is None or str(key) in consumed:
            continue
        system_error = "Unknown argument '{}'".format(key)
        break

    if system_error:
        return [], [], system_error

    return result, missing, None


# ---------------------------------------------------------------------------
# Legacy flat args path
# ---------------------------------------------------------------------------


def _build_direct_cli_args_legacy(
    *,
    script_args: Sequence[str],
    business_arguments: Dict[str, Any],
    runtime_context: Optional[RuntimeContext] = None,
) -> Tuple[List[str], List[str], Optional[str]]:
    """Build CLI args from legacy flat script_args format."""
    result: List[str] = []
    consumed: Set[str] = set()
    missing_business: List[str] = []
    system_error: Optional[str] = None
    i = 0

    while i < len(script_args) and system_error is None:
        item = str(script_args[i])

        # Plain flag (starts with --)
        if item.startswith("--"):
            result.append(item)
            placeholder = str(script_args[i + 1]) if i + 1 < len(script_args) else ""
            if not placeholder or placeholder.startswith("--"):
                # Flag with no value — literal boolean flag
                i += 1
                continue

            # Resolve placeholder
            value, matched_key, error = _resolve_placeholder_legacy(
                placeholder=placeholder,
                business_arguments=business_arguments,
                runtime_context=runtime_context,
            )
            if is_runtime_placeholder(placeholder):
                for candidate in _runtime_placeholder_argument_keys(placeholder, flag=item):
                    if candidate in business_arguments:
                        consumed.add(candidate)
            if error:
                system_error = error
            elif value is not None:
                result.append(str(value))
                if matched_key:
                    consumed.add(matched_key)
            elif is_optional_placeholder(placeholder):
                # Optional parameter not provided — remove the flag and skip
                result.pop()
            elif placeholder.startswith("<") and placeholder.endswith(">"):
                name = placeholder[1:-1].strip()
                if name and not is_runtime_placeholder(placeholder):
                    missing_business.append(name)
            i += 2
            continue

        # Non-flag item
        i += 1

    if system_error:
        return [], [], system_error

    # Append any business arguments not already included
    for key, value in business_arguments.items():
        if value is None or str(key) in consumed:
            continue
        flag = str(key)
        result.append(flag if flag.startswith("--") else "--{}".format(flag.replace("_", "-")))
        result.append(str(value))

    return result, missing_business, None


def _resolve_placeholder_legacy(
    *,
    placeholder: str,
    business_arguments: Dict[str, Any],
    runtime_context: Optional[RuntimeContext] = None,
) -> Tuple[Optional[Any], Optional[str], Optional[str]]:
    """Resolve a single placeholder value (legacy flat format).

    Returns:
        Tuple of (value, matched_key, system_error)
    """
    stripped = placeholder.strip()

    # Runtime placeholder?
    runtime_match = _RUNTIME_PLACEHOLDER_PATTERN.match(stripped)
    if runtime_match:
        key = runtime_match.group(1)
        ctx_field = runtime_context_field(key)
        if ctx_field is None:
            return None, None, "Unknown runtime placeholder: <runtime.{}>".format(key)

        value = runtime_context.get(ctx_field) if runtime_context else None
        if value is None:
            return None, None, "Runtime context missing '{}' for <runtime.{}>".format(ctx_field, key)
        return value, None, None

    # Business placeholder?
    if stripped.startswith("<") and stripped.endswith(">"):
        placeholder_key = stripped[1:-1].strip()
        # Strip ? prefix for optional <?xxx> placeholders
        if placeholder_key.startswith("?"):
            placeholder_key = placeholder_key[1:]
        # Also try alternate key forms
        for candidate in (placeholder_key, placeholder_key.replace("-", "_"), placeholder_key.replace("_", "-")):
            if candidate in business_arguments:
                val = business_arguments[candidate]
                if val is not None:
                    return val, candidate, None
        return None, None, None

    # Plain value — return as-is
    return stripped, None, None


# ---------------------------------------------------------------------------
# Shared helpers (mirror main branch's helpers)
# ---------------------------------------------------------------------------


def _parameter_name(parameter: Mapping[str, Any]) -> str:
    return str(parameter.get("name") or "").strip()


def _parameter_label(parameter: Mapping[str, Any]) -> str:
    description = str(parameter.get("description") or "").strip()
    return description or _parameter_name(parameter)


def _resolve_parameter_value(
    *,
    parameter: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> Tuple[Optional[Any], Optional[str]]:
    """Resolve a structured parameter value from the arguments mapping.

    Returns (value, matched_key).  *value* is None when not found.
    """
    for candidate in script_parameter_argument_keys(parameter):
        if candidate in arguments:
            return arguments[candidate], candidate
    return None, None


def _parameter_argument_keys(parameter: Mapping[str, Any]) -> Set[str]:
    """Return all user-facing aliases for a structured parameter."""
    return set(script_parameter_argument_keys(parameter))


def _runtime_placeholder_argument_keys(placeholder: str, *, flag: str = "") -> Set[str]:
    match = _RUNTIME_PLACEHOLDER_PATTERN.match(str(placeholder or "").strip())
    if match is None:
        return set()
    raw_key = match.group(1)
    context_field = runtime_context_field(raw_key) or _canonical_runtime_key(raw_key)
    keys: Set[str] = set()
    for candidate in (raw_key, context_field, str(flag or "").strip()):
        if not candidate:
            continue
        keys.update(
            {
                candidate,
                candidate.lstrip("-"),
                candidate.replace("-", "_"),
                candidate.replace("_", "-"),
                "--" + candidate.lstrip("-").replace("_", "-"),
            }
        )
    return keys


def _coerce_multiple_values(value: Any) -> List[Any]:
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if item is not None]
    return [value]


def _convert_parameter_value(
    value: Any,
    parameter: Mapping[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """Convert a resolved parameter value to its CLI string representation.

    Returns (converted_value, error).  Exactly one is None.
    """
    param_type = str(parameter.get("type") or "string").strip().lower()
    if param_type == "string" and isinstance(value, (list, tuple, set, dict)):
        serializable = list(value) if isinstance(value, (tuple, set)) else value
        try:
            return json.dumps(serializable, ensure_ascii=False, separators=(",", ":")), None
        except (TypeError, ValueError):
            return None, "expected string or JSON-serializable structured value"
    if param_type in {"array", "list"} or param_type.endswith("[]"):
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None, "expected array"
        if isinstance(parsed, (tuple, set)):
            parsed = list(parsed)
        if not isinstance(parsed, list):
            return None, "expected array"
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), None
    if param_type in {"object", "dict"}:
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None, "expected object"
        if not isinstance(parsed, dict):
            return None, "expected object"
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), None
    if param_type == "json":
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None, "expected JSON"
        try:
            return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), None
        except (TypeError, ValueError):
            return None, "expected JSON"
    if param_type in ("int", "integer"):
        try:
            return str(int(value)), None
        except (TypeError, ValueError):
            return None, "expected integer"
    if param_type in ("float", "number"):
        try:
            return str(float(value)), None
        except (TypeError, ValueError):
            return None, "expected number"
    if param_type in {"bool", "boolean"}:
        if isinstance(value, bool):
            return str(value).lower(), None
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return "true", None
        if normalized in {"false", "0", "no", "n"}:
            return "false", None
        return None, "expected boolean"
    return str(value), None


def _is_json_collection_parameter(parameter: Mapping[str, Any]) -> bool:
    param_type = str(parameter.get("type") or "string").strip().lower()
    return param_type in {"array", "list", "object", "dict", "json"} or param_type.endswith("[]")


def _accepts_structured_value(parameter: Mapping[str, Any]) -> bool:
    param_type = str(parameter.get("type") or "string").strip().lower()
    return param_type == "string" or _is_json_collection_parameter(parameter)


def _append_cli_argument(result: List[str], *, flag: str, value: str) -> None:
    """Append a flag-value pair to the CLI argument list."""
    if flag:
        result.append(flag)
    result.append(value)
