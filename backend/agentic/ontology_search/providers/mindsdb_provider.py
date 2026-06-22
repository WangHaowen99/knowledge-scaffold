"""MindsDB ontology search provider adapter."""

from __future__ import annotations

import logging
from typing import Iterator

from ...services.mindsdb import MindsDBClient
from ..base import OntologySearchEvent

logger = logging.getLogger(__name__)


class MindsDBOntologySearchProvider:
    name = "mindsdb"

    def __init__(self, config: dict):
        self._client = MindsDBClient(config)
        self._session = getattr(self._client, "_session", None)

    def search_stream(
        self,
        *,
        query: str,
        entity: str,
    ) -> Iterator[OntologySearchEvent]:
        logger.info(
            "MindsDB provider search: project=%s agent=%s entity=%s query_chars=%d",
            self._client.project_name,
            self._client.agent_name,
            entity,
            len(str(query or "")),
        )
        yield from self._client.query_agent_stream(query)

    def close(self) -> None:
        self._client.close()
