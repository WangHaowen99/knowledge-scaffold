"""Strict W3C ``traceparent`` parsing shared by HTTP and MCP entrypoints."""

from __future__ import annotations

import re
from dataclasses import dataclass


_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-fA-F]{2})-"
    r"(?P<trace_id>[0-9a-fA-F]{32})-"
    r"(?P<parent_id>[0-9a-fA-F]{16})-"
    r"(?P<trace_flags>[0-9a-fA-F]{2})$"
)


class TraceParentValidationError(ValueError):
    """Raised when an incoming traceparent is present but invalid."""


@dataclass(frozen=True)
class W3CTraceParent:
    """Normalized W3C trace context carried across runtime boundaries."""

    version: str
    trace_id: str
    parent_id: str
    trace_flags: str

    @property
    def value(self) -> str:
        return "{}-{}-{}-{}".format(
            self.version,
            self.trace_id,
            self.parent_id,
            self.trace_flags,
        )


def parse_traceparent(value: str | None) -> W3CTraceParent | None:
    """Parse a canonical version-00 traceparent or return ``None`` when absent.

    The consumer accepts the current W3C 55-character wire format. Invalid
    supplied headers are rejected instead of being silently detached from the
    caller trace.
    """

    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) != 55:
        raise TraceParentValidationError("traceparent must be exactly 55 characters")

    match = _TRACEPARENT_RE.fullmatch(raw)
    if match is None:
        raise TraceParentValidationError("traceparent must use the W3C hexadecimal format")

    parts = {key: item.lower() for key, item in match.groupdict().items()}
    if parts["version"] != "00":
        raise TraceParentValidationError("traceparent version must be 00")
    if parts["trace_id"] == "0" * 32:
        raise TraceParentValidationError("traceparent trace-id must not be all zero")
    if parts["parent_id"] == "0" * 16:
        raise TraceParentValidationError("traceparent parent-id must not be all zero")
    return W3CTraceParent(**parts)


def normalize_traceparent(value: str | None) -> str:
    """Return a normalized header value, or an empty string when absent."""

    parsed = parse_traceparent(value)
    return parsed.value if parsed is not None else ""
