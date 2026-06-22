"""Provider selection service for ontology_search."""

from __future__ import annotations

from typing import Iterator, Optional

from ..config.system_config import load_system_config
from .base import OntologySearchEvent
from .registry import (
    PROVIDER_SPECS,
    apply_runtime_overrides,
    configured_provider_name,
    get_provider_spec,
)

# Backward-compatible alias used by existing tests and callers.
PROVIDER_FACTORIES = {name: spec.factory for name, spec in PROVIDER_SPECS.items()}


class OntologySearchService:
    """Resolve and invoke the configured ontology search provider."""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or load_system_config()
        self.provider_name = configured_provider_name(self.config)
        provider_factory = PROVIDER_FACTORIES.get(self.provider_name)
        if provider_factory is None:
            provider_factory = get_provider_spec(self.provider_name).factory
        self._provider = provider_factory(self.config)

    def search_stream(
        self,
        *,
        query: str,
        entity: str,
    ) -> Iterator[OntologySearchEvent]:
        yield from self._provider.search_stream(query=query, entity=entity)

    def close(self) -> None:
        close = getattr(self._provider, "close", None)
        if callable(close):
            close()


def query_ontology_stream(
    question: str,
    *,
    entity: str = "",
    config: Optional[dict] = None,
    reuse_client: bool = True,
) -> Iterator[OntologySearchEvent]:
    del reuse_client
    if not question or not question.strip():
        raise ValueError("查询问题不能为空")

    service = OntologySearchService(config=config)
    try:
        yield from service.search_stream(query=question.strip(), entity=str(entity or "").strip())
    finally:
        service.close()


def build_ontology_search_config(
    *,
    api_url: Optional[str] = None,
    timeout: Optional[int] = None,
) -> dict:
    return apply_runtime_overrides(
        load_system_config(),
        api_url=api_url,
        timeout=timeout,
    )
