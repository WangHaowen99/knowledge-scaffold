# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""DataAgent REST business data provider."""

from __future__ import annotations

import json
from typing import Iterator

from agent.tools.business_data.adapters.base import BusinessDataSearchEvent
from shared_capabilities.business_data import DataAgentProvider as SharedDataAgentProvider


class DataAgentSearchAdapter:
    name = "dataagent"

    def __init__(self, connection):
        self._client = SharedDataAgentProvider(connection)

    def search_stream(
        self,
        *,
        query: str,
        entity: str,
    ) -> Iterator[BusinessDataSearchEvent]:
        del entity
        yield {"type": "start", "content": "开始调用 DataAgent 查询数据"}

        result = self._client.query(query)
        output = {
            "success": result.success,
            "data": result.data,
            "row_count": result.metadata.get("row_count", 0),
        }
        if result.metadata.get("confidence") is not None:
            output["confidence"] = result.metadata["confidence"]
        if result.metadata.get("sql"):
            output["has_sql"] = True
        output["message"] = result.message if result.success else "查询失败: {}".format(result.error or "未知错误")

        yield {
            "type": "final",
            "content": json.dumps(output, ensure_ascii=False),
        }
        yield {"type": "done", "content": "查询流程结束"}

    def close(self) -> None:
        self._client.close()
