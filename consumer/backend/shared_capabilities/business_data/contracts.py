"""Normalized provider results, events, and structural protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Protocol


@dataclass(frozen=True)
class ProviderCapabilities:
    query: bool
    query_sql: bool
    native_streaming: bool
    probe: bool


@dataclass(frozen=True)
class ProviderResult:
    success: bool
    data: Any = None
    message: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderEvent:
    type: Literal["start", "intermediate", "final", "done", "error"]
    content: str = ""
    result: ProviderResult | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class QueryProvider(Protocol):
    capabilities: ProviderCapabilities

    def query(self, question: str) -> ProviderResult:
        return ...


class NativeStreamingProvider(QueryProvider, Protocol):
    def _iter_native_query_events(self, question: str) -> Iterator[ProviderEvent]:
        return ...
