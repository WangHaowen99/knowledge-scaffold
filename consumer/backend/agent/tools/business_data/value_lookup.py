"""Value-domain lookup pipeline for ``business_data_search(action="lookup_values")``.

Groups user-submitted raw values by their Data Mapping binding, appends the
validated ``table / fields / values`` coordinates to the model's original query,
and reuses the existing natural-language provider entry.  The Consumer never
generates a replacement query or parses canonical/semantic results.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from agent.tools.business_data.service import BusinessDataSearchRequest, query_business_data_stream
from ontology.ontology_catalog import (
    get_object_field_bindings,
    get_object_mapping_field_keys,
    load_value_bindings,
)
from utils.logging import get_logger

logger = get_logger("tools.business_data.value_lookup")

_UNSUPPORTED_PROVIDERS = {"neo4j", "hugegraph"}

_MAX_FIELD_INPUTS = 32
_MAX_VALUES_PER_FIELD = 32
_MAX_TOTAL_VALUES = 64
_MAX_FIELD_KEY_LEN = 128
_MAX_VALUE_LEN = 256


# -- error codes (stable, see design §6.3) -----------------------------------

class ValueLookupErrorCode:
    INVALID_ACTION_ARGUMENTS = "INVALID_ACTION_ARGUMENTS"
    VALUE_INPUT_INVALID = "VALUE_INPUT_INVALID"
    UNKNOWN_VALUE_INPUT_FIELD = "UNKNOWN_VALUE_INPUT_FIELD"
    UNBOUND_VALUE_INPUT_FIELD = "UNBOUND_VALUE_INPUT_FIELD"
    VALUE_BINDING_UNSUPPORTED = "VALUE_BINDING_UNSUPPORTED"
    VALUE_PROVIDER_FAILED = "VALUE_PROVIDER_FAILED"


# -- data structures ----------------------------------------------------------

@dataclass(frozen=True)
class ValueBindingGroup:
    binding_key: str
    resource: str
    value_field: str
    semantic_field: str
    alias_fields: Tuple[str, ...]
    field_keys: Tuple[str, ...]
    input_values: Tuple[str, ...]


@dataclass(frozen=True)
class ValueLookupResultEntry:
    binding_key: str
    field_keys: List[str]
    input_values: List[str]
    content: str


@dataclass(frozen=True)
class ValueLookupError:
    code: str
    message: str
    binding_key: str = ""


@dataclass
class ValueLookupResponse:
    action: str = "lookup_values"
    entity: str = ""
    status: str = "completed"
    value_results: List[ValueLookupResultEntry] = field(default_factory=list)
    errors: List[ValueLookupError] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "action": self.action,
                "entity": self.entity,
                "status": self.status,
                "value_results": [
                    {
                        "binding_key": entry.binding_key,
                        "field_keys": list(entry.field_keys),
                        "input_values": list(entry.input_values),
                        "content": entry.content,
                    }
                    for entry in self.value_results
                ],
                "errors": [
                    {
                        "code": err.code,
                        "message": err.message,
                        "binding_key": err.binding_key,
                    }
                    for err in self.errors
                ],
            },
            ensure_ascii=False,
        )


# -- validation ---------------------------------------------------------------

def _normalize_value(value: str) -> str:
    return unicodedata.normalize("NFC", str(value or "").strip())


def _has_control_chars(value: str) -> bool:
    for char in str(value or ""):
        if char in ("\n", "\r", "\v", "\f", "\u0085", "\u2028", "\u2029"):
            return True
        category = unicodedata.category(char)
        if category == "Cc" or category == "Cf":
            return True
    return False


def validate_value_inputs(
    value_inputs: Any,
    field_bindings: Dict[str, str],
    all_field_keys: Any = (),
) -> Tuple[List[Dict[str, Any]], Optional[ValueLookupError]]:
    """Validate ``value_inputs`` against the object's bound fields.

    Returns ``(validated_inputs, error)``.  ``validated_inputs`` is a list of
    ``{"field_key": str, "values": [str, ...]}`` with NFC-normalized, deduplicated
    values.  ``error`` is set when validation fails, in which case no Provider
    call should be made.

    ``all_field_keys`` is the full set of logical field keys declared in the
    object's data mapping.  It distinguishes
    ``UNKNOWN_VALUE_INPUT_FIELD`` (not a mapping field at all) from
    ``UNBOUND_VALUE_INPUT_FIELD`` (a mapping field without ``value_binding_ref``).
    """
    known_fields = set(str(key) for key in all_field_keys)
    if not isinstance(value_inputs, list) or not value_inputs:
        return None, ValueLookupError(
            code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
            message="value_inputs must be a non-empty array.",
        )
    if len(value_inputs) > _MAX_FIELD_INPUTS:
        return None, ValueLookupError(
            code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
            message="value_inputs exceeds the maximum of {} field entries.".format(
                _MAX_FIELD_INPUTS
            ),
        )

    seen_fields: set[str] = set()
    validated: List[Dict[str, Any]] = []
    total_value_count = 0

    for index, item in enumerate(value_inputs):
        if not isinstance(item, dict):
            return None, ValueLookupError(
                code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                message="value_inputs[{}] must be an object.".format(index),
            )
        if any(key not in {"field_key", "values"} for key in item):
            return None, ValueLookupError(
                code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                message="value_inputs[{}] has unexpected fields.".format(index),
            )
        raw_field_key_value = item.get("field_key")
        if not isinstance(raw_field_key_value, str):
            return None, ValueLookupError(
                code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                message="value_inputs[{}].field_key must be a string.".format(index),
            )
        raw_field_key = raw_field_key_value.strip()
        if not raw_field_key:
            return None, ValueLookupError(
                code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                message="value_inputs[{}].field_key is required.".format(index),
            )
        if len(raw_field_key) > _MAX_FIELD_KEY_LEN:
            return None, ValueLookupError(
                code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                message="value_inputs[{}].field_key exceeds {} characters.".format(
                    index, _MAX_FIELD_KEY_LEN
                ),
            )
        if _has_control_chars(raw_field_key):
            return None, ValueLookupError(
                code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                message="value_inputs[{}].field_key contains control characters.".format(index),
            )
        field_key = _normalize_value(raw_field_key)
        if field_key in seen_fields:
            return None, ValueLookupError(
                code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                message="field_key '{}' appears more than once in value_inputs.".format(
                    field_key
                ),
            )
        if field_key not in field_bindings:
            if field_key in known_fields:
                return None, ValueLookupError(
                    code=ValueLookupErrorCode.UNBOUND_VALUE_INPUT_FIELD,
                    message=(
                        "field '{}' does not have a value_binding_ref configured."
                    ).format(field_key),
                )
            return None, ValueLookupError(
                code=ValueLookupErrorCode.UNKNOWN_VALUE_INPUT_FIELD,
                message="field '{}' does not belong to the current object.".format(field_key),
            )

        raw_values = item.get("values")
        if not isinstance(raw_values, list) or not raw_values:
            return None, ValueLookupError(
                code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                message="value_inputs[{}].values must be a non-empty array.".format(index),
            )
        if len(raw_values) > _MAX_VALUES_PER_FIELD:
            return None, ValueLookupError(
                code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                message=(
                    "value_inputs[{}].values exceeds {} values per field."
                ).format(index, _MAX_VALUES_PER_FIELD),
            )

        normalized_values: list[str] = []
        seen_values: set[str] = set()
        for v_index, raw_value in enumerate(raw_values):
            if not isinstance(raw_value, str):
                return None, ValueLookupError(
                    code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                    message="value_inputs[{}].values[{}] must be a string.".format(
                        index, v_index
                    ),
                )
            value = _normalize_value(raw_value)
            if not value:
                return None, ValueLookupError(
                    code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                    message="value_inputs[{}].values[{}] is empty.".format(index, v_index),
                )
            if len(value) > _MAX_VALUE_LEN:
                return None, ValueLookupError(
                    code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                    message="value_inputs[{}].values[{}] exceeds {} characters.".format(
                        index, v_index, _MAX_VALUE_LEN
                    ),
                )
            if _has_control_chars(value):
                return None, ValueLookupError(
                    code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                    message="value_inputs[{}].values[{}] contains control characters.".format(
                        index, v_index
                    ),
                )
            if value in seen_values:
                return None, ValueLookupError(
                    code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                    message=(
                        "value_inputs[{}].values contains duplicate value '{}'."
                    ).format(index, value),
                )
            seen_values.add(value)
            normalized_values.append(value)

        total_value_count += len(normalized_values)
        if total_value_count > _MAX_TOTAL_VALUES:
            return None, ValueLookupError(
                code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                message="total value count exceeds the maximum of {}.".format(
                    _MAX_TOTAL_VALUES
                ),
            )

        seen_fields.add(field_key)
        validated.append({"field_key": field_key, "values": normalized_values})

    return validated, None


# -- grouping -----------------------------------------------------------------

def group_value_inputs_by_binding(
    validated_inputs: List[Dict[str, Any]],
    field_bindings: Dict[str, str],
    value_bindings: Dict[str, Dict[str, Any]],
) -> Tuple[List[ValueBindingGroup], Optional[ValueLookupError]]:
    """Group validated inputs by their binding key, merging values per binding."""
    groups: Dict[str, Dict[str, Any]] = {}
    for item in validated_inputs:
        field_key = item["field_key"]
        binding_key = field_bindings[field_key]
        binding_def = value_bindings.get(binding_key)
        if not isinstance(binding_def, dict):
            return None, ValueLookupError(
                code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                message="binding '{}' is not defined in value_bindings.".format(binding_key),
            )
        if binding_key not in groups:
            resource = _mapping_coordinate(binding_def.get("resource"))
            value_field = _mapping_coordinate(binding_def.get("value_field"))
            semantic_field = _mapping_coordinate(binding_def.get("semantic_field"))
            if resource is None or value_field is None or semantic_field is None:
                return None, ValueLookupError(
                    code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                    message="binding '{}' has invalid resource/value_field/semantic_field.".format(
                        binding_key
                    ),
                )
            raw_alias_fields = binding_def.get("alias_fields")
            if raw_alias_fields is not None and not isinstance(raw_alias_fields, list):
                return None, ValueLookupError(
                    code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                    message="binding '{}' has invalid alias_fields.".format(binding_key),
                )
            alias_fields_list: list[str] = []
            for alias in raw_alias_fields or []:
                normalized_alias = _mapping_coordinate(alias)
                if normalized_alias is None:
                    return None, ValueLookupError(
                        code=ValueLookupErrorCode.VALUE_INPUT_INVALID,
                        message="binding '{}' has invalid alias_fields.".format(binding_key),
                    )
                alias_fields_list.append(normalized_alias)
            alias_fields = tuple(alias_fields_list)
            groups[binding_key] = {
                "resource": resource,
                "value_field": value_field,
                "semantic_field": semantic_field,
                "alias_fields": alias_fields,
                "field_keys": [],
                "input_values": [],
                "seen_values": set(),
            }
        group = groups[binding_key]
        if field_key not in group["field_keys"]:
            group["field_keys"].append(field_key)
        for value in item["values"]:
            if value not in group["seen_values"]:
                group["seen_values"].add(value)
                group["input_values"].append(value)

    ordered: List[ValueBindingGroup] = []
    for binding_key in sorted(groups):
        g = groups[binding_key]
        ordered.append(
            ValueBindingGroup(
                binding_key=binding_key,
                resource=g["resource"],
                value_field=g["value_field"],
                semantic_field=g["semantic_field"],
                alias_fields=g["alias_fields"],
                field_keys=tuple(g["field_keys"]),
                input_values=tuple(g["input_values"]),
            )
        )
    return ordered, None


def _mapping_coordinate(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 256 or _has_control_chars(normalized):
        return None
    return normalized


# -- coordinate rendering -----------------------------------------------------

def render_lookup_coordinates(group: ValueBindingGroup) -> str:
    """Render the minimal ``[Data Mapping]`` coordinate block for one binding."""
    fields: list[str] = []
    seen: set[str] = set()
    for name in (group.value_field, group.semantic_field, *group.alias_fields):
        normalized = str(name or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        fields.append(normalized)

    values_json = json.dumps(list(group.input_values), ensure_ascii=False)
    fields_json = json.dumps(fields, ensure_ascii=False)
    return (
        "[Data Mapping]\n"
        "table: {table}\n"
        "fields: {fields}\n"
        "values: {values}\n"
        "match these values in the fields above (not necessarily exact match)"
    ).format(
        table=group.resource,
        fields=fields_json,
        values=values_json,
    )


# -- orchestration ------------------------------------------------------------

def execute_value_lookups(
    *,
    ontology_dir: Path,
    object_key: str,
    model_query: str,
    value_inputs: Any,
    connection: ResolvedBusinessDataSource,
    workspace_id: str,
    ontology_id: str,
    ontology_version: str = "",
    event_emitter: Optional[Any] = None,
    tool_call_id: Optional[str] = None,
    provider_runtime_context: Optional[Mapping[str, Any]] = None,
) -> ValueLookupResponse:
    """Run the full lookup_values pipeline and return a structured response."""
    entity = object_key

    if connection.provider in _UNSUPPORTED_PROVIDERS:
        return ValueLookupResponse(
            entity=entity,
            status="failed",
            errors=[ValueLookupError(
                code=ValueLookupErrorCode.VALUE_BINDING_UNSUPPORTED,
                message=(
                    "Provider '{}' cannot query value-binding resources via "
                    "natural language."
                ).format(connection.provider),
            )],
        )

    field_bindings = get_object_field_bindings(ontology_dir, object_key)
    all_field_keys = get_object_mapping_field_keys(ontology_dir, object_key)
    if not field_bindings and not all_field_keys:
        return ValueLookupResponse(
            entity=entity,
            status="failed",
            errors=[ValueLookupError(
                code=ValueLookupErrorCode.VALUE_BINDING_UNSUPPORTED,
                message="Object '{}' has no data mapping.".format(object_key),
            )],
        )

    value_bindings = load_value_bindings(ontology_dir)
    if not value_bindings:
        return ValueLookupResponse(
            entity=entity,
            status="failed",
            errors=[ValueLookupError(
                code=ValueLookupErrorCode.VALUE_BINDING_UNSUPPORTED,
                message="No value_bindings defined in the data mapping.",
            )],
        )

    validated, validation_error = validate_value_inputs(
        value_inputs, field_bindings, all_field_keys,
    )
    if validation_error is not None:
        return ValueLookupResponse(
            entity=entity,
            status="failed",
            errors=[validation_error],
        )
    if validated is None:
        raise RuntimeError(
            "validate_value_inputs returned no error but validated is None"
        )

    if not validated:
        return ValueLookupResponse(
            entity=entity,
            status="failed",
            errors=[ValueLookupError(
                code=ValueLookupErrorCode.UNBOUND_VALUE_INPUT_FIELD,
                message="Object '{}' has no fields with value_binding_ref.".format(object_key),
            )],
        )

    groups, group_error = group_value_inputs_by_binding(
        validated, field_bindings, value_bindings,
    )
    if group_error is not None:
        return ValueLookupResponse(
            entity=entity,
            status="failed",
            errors=[group_error],
        )
    if groups is None:
        raise RuntimeError(
            "group_value_inputs_by_binding returned no error but groups is None"
        )

    results: List[ValueLookupResultEntry] = []
    errors: List[ValueLookupError] = []

    for request_index, group in enumerate(groups, start=1):
        provider_query = "{}\n\n{}".format(
            model_query,
            render_lookup_coordinates(group),
        )
        logger.info(
            "value_lookup: provider=%s field_count=%d value_count=%d "
            "tool_call_id=%s",
            connection.provider,
            len(group.field_keys),
            len(group.input_values),
            tool_call_id or "n/a",
        )
        request = BusinessDataSearchRequest(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            entity=entity,
            query=provider_query,
            field_alias_mappings=(),
            ontology_version=ontology_version,
            provider_runtime_context=dict(provider_runtime_context or {}),
            stage="value_lookup",
        )
        content, provider_error = _query_single_business_data(
            request=request,
            connection=connection,
            event_emitter=event_emitter,
            tool_call_id=tool_call_id,
            ontology_id=connection.ontology_id,
            provider=connection.provider,
            binding_key=group.binding_key,
            internal_request_id="{}:value_lookup:{}".format(
                tool_call_id or "lookup",
                request_index,
            ),
            value_input_field_count=len(group.field_keys),
            value_input_value_count=len(group.input_values),
        )
        if provider_error:
            errors.append(ValueLookupError(
                code=ValueLookupErrorCode.VALUE_PROVIDER_FAILED,
                message=provider_error,
                binding_key=group.binding_key,
            ))
        else:
            results.append(ValueLookupResultEntry(
                binding_key=group.binding_key,
                field_keys=list(group.field_keys),
                input_values=list(group.input_values),
                content=content,
            ))

    if errors and not results:
        status = "failed"
    elif errors:
        status = "partial"
    else:
        status = "completed"

    return ValueLookupResponse(
        entity=entity,
        status=status,
        value_results=results,
        errors=errors,
    )


def _query_single_business_data(
    *,
    request: BusinessDataSearchRequest,
    connection: ResolvedBusinessDataSource,
    event_emitter: Optional[Any] = None,
    tool_call_id: Optional[str] = None,
    ontology_id: str = "",
    provider: str = "",
    binding_key: str = "",
    internal_request_id: str = "",
    value_input_field_count: int = 0,
    value_input_value_count: int = 0,
) -> Tuple[str, Optional[str]]:
    """Consume the event stream for one provider query, return (content, error).

    Shares the same ``search_stream`` path as business queries; provider
    failures are detected from the ``final`` JSON payload (``success: false``)
    or from an explicit ``error`` event.
    """
    final_content = ""
    error_content = ""
    saw_final = False
    try:
        for event in query_business_data_stream(request, connection=connection):
            if event_emitter:
                try:
                    event_emitter({
                        **event,
                        "ontology_id": str(ontology_id or "").strip(),
                        "provider": str(provider or "").strip(),
                        "stage": "value_lookup",
                        "binding_key": str(binding_key or "").strip(),
                        "internal_request_id": str(internal_request_id or "").strip(),
                        "value_input_field_count": max(0, int(value_input_field_count)),
                        "value_input_value_count": max(0, int(value_input_value_count)),
                    })
                except Exception:
                    logger.warning(
                        "Failed to emit value_lookup event (tool_call_id=%s)",
                        tool_call_id or "n/a",
                        exc_info=True,
                    )
            event_type = str(event.get("type") or "").strip().lower()
            event_content = str(event.get("content") or "").strip()
            if event_type == "final":
                saw_final = True
                final_content = event_content
            elif event_type == "error":
                error_content = event_content or "数据源查询失败"
    except Exception as exc:
        logger.error(
            "Unexpected error during value lookup query: exception_type=%s",
            type(exc).__name__,
        )
        return "", "Unexpected error during value lookup query."

    if error_content:
        return "", error_content
    if not saw_final:
        return "", "数据源未返回查询结果"
    # Providers signal failure via a JSON final payload with success=false.
    failure_message = _detect_provider_failure(final_content)
    if failure_message is not None:
        return "", failure_message
    return final_content, None


def _detect_provider_failure(content: str) -> Optional[str]:
    """Return an error message if ``content`` is a JSON failure payload, else None."""
    if not content:
        return None
    stripped = content.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("success") is False:
        message = parsed.get("message") or parsed.get("error")
        return str(message) if message else "数据源查询失败"
    return None
