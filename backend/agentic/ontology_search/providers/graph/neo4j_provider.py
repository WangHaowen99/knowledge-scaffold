"""Neo4J GraphSearchProvider."""

from __future__ import annotations

from .backends.neo4j import Neo4jGraphBackend
from .provider import GraphSearchProvider


class Neo4jGraphSearchProvider(GraphSearchProvider):
    name = "neo4j"

    def __init__(self, config: dict):
        ontology_search = dict(config.get("ontology_search") or {})
        provider_config = dict(ontology_search.get("neo4j") or {})
        super().__init__(
            provider_config=provider_config,
            backend=Neo4jGraphBackend(provider_config),
        )
