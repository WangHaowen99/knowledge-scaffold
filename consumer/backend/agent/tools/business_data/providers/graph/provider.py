"""Provider wrapper for graph business data search."""

from __future__ import annotations

from typing import Any, AsyncIterator, Mapping

from agent.tools.business_data.providers.base import OntologySearchEvent
from .backends.base import GraphBackend
from .runtime import GraphSearchRuntime


class GraphSearchProvider:
    """Shared provider wrapper implementing BusinessDataProvider."""

    name = "graph"

    def __init__(
        self,
        *,
        provider_config: Mapping[str, Any],
        backend: GraphBackend,
        graph_agent: Any | None = None,
    ) -> None:
        self.provider_config = dict(provider_config or {})
        self.backend = backend
        self._runtime = GraphSearchRuntime(
            provider=self.name,
            provider_config=self.provider_config,
            backend=self.backend,
            graph_agent=graph_agent,
        )

    def runtime_provider_for_test(self) -> str:
        """Expose the bound runtime provider name for wrapper contract tests."""
        return self._runtime.provider

    async def asearch_stream(
        self,
        *,
        query: str,
        entity: str,
    ) -> AsyncIterator[OntologySearchEvent]:
        yield {"type": "start", "content": "开始图谱业务数据查询"}
        async for event in self._runtime.asearch_stream(
            query=query,
            entity=entity,
        ):
            yield event
        yield {"type": "done", "content": "图谱业务数据查询结束"}

    def close(self) -> None:
        close = getattr(self.backend, "close", None)
        if callable(close):
            close()
