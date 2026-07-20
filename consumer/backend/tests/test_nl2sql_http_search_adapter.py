"""Compatibility tests for the Agent-facing NL2SQL HTTP presentation."""

from __future__ import annotations

import json

from agent.tools.business_data.adapters.nl2sql_http_search import (
    NL2SQLHttpSearchAdapter,
)
from shared_capabilities.business_data import AuthSpec, EffectiveConnection


def _connection() -> EffectiveConnection:
    return EffectiveConnection(
        version=1,
        provider="nl2sql_http",
        endpoint="http://nl2sql.test",
        auth=AuthSpec(type="bearer", token="token-x"),
        options={"scene": "scene-x", "timeout_seconds": 88},
    )


class _Response:
    status_code = 200
    text = ""

    def json(self):
        return {
            "success": True,
            "sql": "SELECT * FROM table_x",
            "data": [{"id": 1}],
            "row_count": 1,
            "message": "provider custom message",
        }


def test_agent_nl2sql_ignores_provider_success_message_like_baseline():
    adapter = NL2SQLHttpSearchAdapter(_connection())
    adapter._session.post = lambda *args, **kwargs: _Response()

    events = list(adapter.search_stream(query="查询零件", entity="TargetItem"))

    assert events[0] == {"type": "start", "content": "开始查询本体数据"}
    assert events[1] == {
        "type": "intermediate",
        "content": "生成 SQL: SELECT * FROM table_x",
    }
    assert json.loads(events[2]["content"]) == {
        "success": True,
        "data": [{"id": 1}],
        "message": "查询成功，返回 1 行数据",
    }
    assert events[3] == {"type": "done", "content": "查询流程结束"}
