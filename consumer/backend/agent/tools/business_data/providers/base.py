"""Shared ontology search provider types."""

from __future__ import annotations

from typing import Any, Dict, Iterator, Protocol

OntologySearchEvent = Dict[str, str]


class OntologySearchProvider(Protocol):
    """Provider interface behind the public ontology_search tool."""

    name: str

    def search_stream(
        self,
        *,
        query: str,
        entity: str,
    ) -> Iterator[OntologySearchEvent]:
        """Yield normalized search progress events."""


def normalize_event(event: Dict[str, Any]) -> OntologySearchEvent:
    return {
        "type": str(event.get("type") or "").strip(),
        "content": str(event.get("content") or "").strip(),
    }
