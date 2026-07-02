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

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..utils.logging import get_logger
from .models import RuntimeContext

logger = get_logger("direct_execution.arguments")

_RUNTIME_PLACEHOLDER_PATTERN = re.compile(r"^<runtime\.([\w\-]+)>$")
_OPTIONAL_PLACEHOLDER_PATTERN = re.compile(r"^<\?([^>]+)>$")

# Mapping from runtime key (the part after <runtime.>) to RuntimeContext field name
_RUNTIME_KEY_TO_CTX_FIELD: Dict[str, str] = {
    "session-id": "session_id",
    "run_id": "run_id",
    "turn_id": "turn_id",
    "user_id": "user_id",
    "workspace_id": "workspace_id",
    "selected_ontology_id": "selected_ontology_id",
}

# Allowed runtime placeholder keys — acts as an allowlist
_ALLOWED_RUNTIME_KEYS: set[str] = set(_RUNTIME_KEY_TO_CTX_FIELD.keys())


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
            name = _parameter_name(param)
            if name and not _is_runtime_param(param):
                keys.add(name)
        return keys

    # Legacy flat format
    keys = set()
    for item in script_args:
        stripped = str(item).strip()
        if not stripped.startswith("<") or not stripped.endswith(">"):
            continue
        if _RUNTIME_PLACEHOLDER_PATTERN.match(stripped):
            continue
        key = stripped[1:-1].strip()
        if key.startswith("?"):
            key = key[1:]
        if key:
            keys.add(key)
    return keys


def extract_runtime_requirements(
    script_args: Sequence[str] = (),
    script_parameters: Sequence[Mapping[str, Any]] = (),
) -> List[str]:
    """Extract runtime parameter names.

    For structured ``script_parameters``, identifies runtime params by flag
    convention matching known runtime keys.
    For flat ``script_args``, parses ``<runtime.xxx>`` placeholders.
    """
    if script_parameters:
        requirements: List[str] = []
        for param in script_parameters:
            flag = str(param.get("flag") or "").strip()
            # Try to match --flag to a known runtime key, e.g. --session-id -> session-id
            key = _runtime_key_from_flag(flag)
            if key and key in _ALLOWED_RUNTIME_KEYS:
                requirements.append(key)
        return requirements

    # Legacy flat format
    requirements = []
    for arg in script_args:
        m = _RUNTIME_PLACEHOLDER_PATTERN.match(str(arg).strip())
        if m:
            key = m.group(1)
            if key in _ALLOWED_RUNTIME_KEYS:
                requirements.append(key)
    return requirements


def _runtime_key_from_flag(flag: str) -> Optional[str]:
    """Extract a runtime key from a CLI flag, e.g. ``--session-id`` → ``session-id``."""
    key = flag.lstrip("-").strip()
    # Direct match
    if key in _ALLOWED_RUNTIME_KEYS:
        return key
    # Try underscore variant (RuntimeContext field names are underscore)
    if key.replace("_", "-") in _ALLOWED_RUNTIME_KEYS:
        return key.replace("_", "-")
    return None


def _is_runtime_param(parameter: Mapping[str, Any]) -> bool:
    """Check if a structured parameter is a runtime parameter."""
    name = _parameter_name(parameter)
    # Structured params from new-format catalogs won't have runtime. prefix;
    # runtime params are identified by matching the flag against known keys.
    if name.startswith("runtime."):
        return True
    flag = str(parameter.get("flag") or "").strip()
    return _runtime_key_from_flag(flag) is not None


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
    business_arguments: Dict[str, Any] = {},
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
    # Build merged argument lookup: business values first, runtime values override
    merged_args: Dict[str, Any] = dict(business_arguments)
    if runtime_context is not None:
        for runtime_key, ctx_field in _RUNTIME_KEY_TO_CTX_FIELD.items():
            ctx_value = runtime_context.get(ctx_field)
            if ctx_value is not None:
                merged_args[runtime_key] = ctx_value
                merged_args[runtime_key.replace("-", "_")] = ctx_value

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

        value, matched_key = _resolve_parameter_value(
            parameter=parameter,
            arguments=merged_args,
        )

        if matched_key:
            consumed.add(matched_key)
        if value is None:
            if required and not _is_runtime_param(parameter):
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
                _append_cli_argument(result, flag=flag, value=converted)
            if system_error:
                break
            continue

        if isinstance(value, (list, tuple, set)):
            system_error = "{} does not accept multiple values".format(name)
            break
        converted, error = _convert_parameter_value(value, parameter)
        if error:
            system_error = "{}: {}".format(name, error)
            break
        _append_cli_argument(result, flag=flag, value=converted)

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
        if key not in _ALLOWED_RUNTIME_KEYS:
            return None, None, "Unknown runtime placeholder: <runtime.{}>".format(key)

        ctx_field = _RUNTIME_KEY_TO_CTX_FIELD.get(key)
        value = runtime_context.get(ctx_field) if runtime_context and ctx_field else None
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
    candidates: List[str] = []
    name = _parameter_name(parameter)
    flag = str(parameter.get("flag") or "").strip()
    description = str(parameter.get("description") or "").strip()
    if name:
        candidates.append(name)
    if flag:
        stripped_flag = flag.lstrip("-")
        candidates.extend([flag, stripped_flag, stripped_flag.replace("-", "_")])
    if description:
        candidates.append(description)
    aliases = parameter.get("aliases")
    if isinstance(aliases, list):
        candidates.extend(str(alias).strip() for alias in aliases if str(alias or "").strip())

    for candidate in candidates:
        if candidate in arguments:
            return arguments[candidate], candidate
    return None, None


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
    if param_type == "bool":
        if isinstance(value, bool):
            return str(value).lower(), None
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return "true", None
        if normalized in {"false", "0", "no", "n"}:
            return "false", None
        return None, "expected boolean"
    return str(value), None


def _append_cli_argument(result: List[str], *, flag: str, value: str) -> None:
    """Append a flag-value pair to the CLI argument list."""
    if flag:
        result.append(flag)
    result.append(value)
