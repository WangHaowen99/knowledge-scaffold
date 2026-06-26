"""Provider wrapper for graph business data search."""

from __future__ import annotations

from typing import Any, Iterator, Mapping

from ...base import OntologySearchEvent
from .agent import GraphQueryAgent
from .backends.base import GraphBackend
from .runtime import GraphSearchRuntime


class GraphSearchProvider:
    """Shared provider wrapper implementing OntologySearchProvider."""

    name = "graph"

    def __init__(
        self,
        *,
        provider_config: Mapping[str, Any],
        backend: GraphBackend,
        graph_agent: GraphQueryAgent | None = None,
    ) -> None:
        self.provider_config = dict(provider_config or {})
        self.backend = backend
        self._runtime = GraphSearchRuntime(
            provider=self.name,
            provider_config=self.provider_config,
            backend=self.backend,
            graph_agent=graph_agent,
        )

    def search_stream(
        self,
        *,
        query: str,
        entity: str,
    ) -> Iterator[OntologySearchEvent]:
        yield {"type": "start", "content": "开始图谱业务数据查询"}
        yield from self._runtime.search_stream(query=query, entity=entity)
        yield {"type": "done", "content": "图谱业务数据查询结束"}

    def close(self) -> None:
        close = getattr(self.backend, "close", None)
        if callable(close):
            close()
