"""NL2SQL HTTP ontology search provider adapter."""

from __future__ import annotations

import json
from typing import Iterator

from ...services.nl2sql import NL2SQLClient
from ..base import OntologySearchEvent


class NL2SQLHttpProvider:
    name = "nl2sql_http"

    def __init__(self, config: dict):
        self._client = NL2SQLClient(config)
        self._session = self._client._session

    def search_stream(
        self,
        *,
        query: str,
        entity: str,
    ) -> Iterator[OntologySearchEvent]:
        del entity
        yield {"type": "start", "content": "开始查询本体数据"}
        result = self.query(query)
        sql_text = str(result.get("sql") or "").strip()
        if sql_text:
            yield {"type": "intermediate", "content": "生成 SQL: {}".format(sql_text)}
        yield {
            "type": "final",
            "content": json.dumps(_format_output(result, show_sql=False), ensure_ascii=False),
        }
        yield {"type": "done", "content": "查询流程结束"}

    def query(self, question: str) -> dict:
        return self._client.query(question)

    def close(self) -> None:
        self._client.close()


def _format_output(result: dict, show_sql: bool = False) -> dict:
    output = {
        "success": result.get("success", False),
        "data": result.get("data"),
    }
    if show_sql and "sql" in result:
        output["sql"] = result["sql"]

    if result.get("success"):
        row_count = result.get("row_count", 0)
        if row_count > 0:
            output["message"] = "查询成功，返回 {} 行数据".format(row_count)
        else:
            output["message"] = "查询成功，无数据返回"
    else:
        output["message"] = "查询失败: {}".format(result.get("error", "未知错误"))
    return output
