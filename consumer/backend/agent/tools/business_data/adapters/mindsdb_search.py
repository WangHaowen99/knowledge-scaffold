# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""MindsDB ontology search provider adapter."""

from __future__ import annotations

from typing import Iterator

from agent.tools.business_data.adapters.base import BusinessDataSearchEvent
from shared_capabilities.business_data import MindsDBProvider, iter_query_events


class MindsDBSearchAdapter:
    name = "mindsdb"

    def __init__(self, connection):
        self._client = MindsDBProvider(connection)
        self._session = getattr(self._client, "_session", None)

    def search_stream(
        self,
        *,
        query: str,
        entity: str,
    ) -> Iterator[BusinessDataSearchEvent]:
        del entity
        for event in iter_query_events(self._client, query):
            yield {"type": event.type, "content": event.content}

    def close(self) -> None:
        self._client.close()
