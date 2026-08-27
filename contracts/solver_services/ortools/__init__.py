"""Public ``ortools.solver.v1`` HTTP contract."""

from .validation import (
    ContractValidationError,
    canonical_json_bytes,
    load_schema,
    parse_json_bytes,
    validate_document,
    validate_traceparent,
)

__all__ = [
    "ContractValidationError",
    "canonical_json_bytes",
    "load_schema",
    "parse_json_bytes",
    "validate_document",
    "validate_traceparent",
]
