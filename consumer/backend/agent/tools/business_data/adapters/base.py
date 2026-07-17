"""Agent-side adapters for the public business_data_search tool."""

from __future__ import annotations

from typing import Any, Dict, Iterator, Protocol

BusinessDataSearchEvent = Dict[str, str]


class BusinessDataSearchAdapter(Protocol):
    """Convert shared Provider events/results into Agent search events."""

    name: str

    def search_stream(
        self,
        *,
        query: str,
        entity: str,
    ) -> Iterator[BusinessDataSearchEvent]:
        """Yield normalized search progress events."""


def normalize_event(event: Dict[str, Any]) -> BusinessDataSearchEvent:
    return {
        "type": str(event.get("type") or "").strip(),
        "content": str(event.get("content") or "").strip(),
    }
