"""DataAgent REST business data provider."""

from __future__ import annotations

import json
from typing import Iterator

from agent.tools.business_data.providers.clients.dataagent import call_query, format_output
from agent.tools.business_data.providers.base import OntologySearchEvent


class DataAgentProvider:
    name = "dataagent"

    def __init__(self, config: dict):
        ontology_search = dict(config.get("ontology_search") or {})
        provider_config = dict(ontology_search.get("dataagent") or {})
        self.base_url = str(provider_config.get("url") or "").rstrip("/")
        self.token = str(provider_config.get("token") or "").strip()
        self.timeout = int(provider_config.get("timeout_seconds") or 600)
        retries = provider_config.get("max_retries")
        self.max_retries = 3 if retries in {None, ""} else max(0, int(retries))

    def search_stream(
        self,
        *,
        query: str,
        entity: str,
    ) -> Iterator[OntologySearchEvent]:
        del entity
        yield {"type": "start", "content": "开始调用 DataAgent 查询数据"}

        result = call_query(
            self.base_url,
            self.token,
            query,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )
        output = format_output(result, show_sql=False)

        yield {
            "type": "final",
            "content": json.dumps(output, ensure_ascii=False),
        }
        yield {"type": "done", "content": "查询流程结束"}

    def close(self) -> None:
        pass
