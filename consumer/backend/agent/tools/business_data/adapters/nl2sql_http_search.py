"""NL2SQL HTTP ontology search provider adapter."""

from __future__ import annotations

import json
from typing import Iterator

from agent.tools.business_data.adapters.base import BusinessDataSearchEvent
from shared_capabilities.business_data import NL2SQLProvider


class NL2SQLHttpSearchAdapter:
    name = "nl2sql_http"

    def __init__(self, connection):
        self._client = NL2SQLProvider(connection)
        self._session = self._client._session

    def search_stream(
        self,
        *,
        query: str,
        entity: str,
    ) -> Iterator[BusinessDataSearchEvent]:
        del entity
        yield {"type": "start", "content": "开始查询本体数据"}
        result = self._client.query(query)
        sql_text = str(result.metadata.get("sql") or "").strip()
        if sql_text:
            yield {
                "type": "intermediate",
                "content": "生成 SQL: {}".format(sql_text),
                "public_summary": "查询语句已生成",
                "public_summary_allowed": True,
            }
        yield {
            "type": "final",
            "content": json.dumps(_format_output(result), ensure_ascii=False),
        }
        yield {"type": "done", "content": "查询流程结束"}

    def query(self, question: str):
        return self._client.query(question)

    def close(self) -> None:
        self._client.close()


def _format_output(result) -> dict:
    output = {
        "success": result.success,
        "data": result.data,
    }
    if result.success:
        row_count = result.metadata.get("row_count", 0)
        if row_count > 0:
            output["message"] = "查询成功，返回 {} 行数据".format(row_count)
        elif result.message:
            # Value-domain lookups return a text answer with no rows; preserve
            # the provider's original message instead of discarding it.
            output["message"] = result.message
        else:
            output["message"] = "查询成功，无数据返回"
    else:
        output["message"] = "查询失败: {}".format(result.error or "未知错误")
    return output
