"""Compatibility tests for the Agent-facing NL2SQL HTTP presentation."""

from __future__ import annotations

from internal_access import internal
import json

from agent.tools.business_data.adapters.nl2sql_http_search import (
    NL2SQLHttpSearchAdapter,
)
from backend_synthetic_credentials import synthetic_secret
from shared_capabilities.business_data import AuthSpec, EffectiveConnection


def _connection() -> EffectiveConnection:
    return EffectiveConnection(
        version=1,
        provider="nl2sql_http",
        endpoint="https://nl2sql.example.net",
        auth=AuthSpec(type="bearer", token=synthetic_secret("nl2sql_http_search_adapter-credential-01")),
        options={"scene": "scene-x", "timeout_seconds": 88},
    )


class _Response:
    status_code = 200
    text = ""

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def json(self):
        return {
            "success": True,
            "sql": "SELECT * FROM table_x",
            "data": [{"id": 1}],
            "row_count": 1,
            "message": "provider custom message",
        }


class _TextAnswerResponse:
    """Provider returns a text answer with no rows (value-domain lookup)."""

    status_code = 200
    text = ""

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def json(self):
        return {
            "success": True,
            "data": None,
            "row_count": 0,
            "message": "KUL 对应合成机场名",
        }


class _FailureResponse:
    status_code = 503
    text = "service unavailable"

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def json(self):
        return {"error": "service unavailable"}


def test_agent_nl2sql_ignores_provider_success_message_like_baseline():
    adapter = NL2SQLHttpSearchAdapter(_connection())

    def post_response(*_args, **_kwargs):
        return _Response()

    internal(adapter).session.post = post_response

    events = list(adapter.search_stream(query="查询零件", entity="TargetItem"))

    assert events[0] == {"type": "start", "content": "开始查询本体数据"}
    assert events[1] == {
        "type": "intermediate",
        "content": "生成 SQL: SELECT * FROM table_x",
        "public_summary": "查询语句已生成",
        "public_summary_allowed": True,
    }
    assert json.loads(events[2]["content"]) == {
        "success": True,
        "data": [{"id": 1}],
        "message": "查询成功，返回 1 行数据",
    }
    assert events[3] == {"type": "done", "content": "查询流程结束"}


def test_nl2sql_value_lookup_returns_structured_content():
    adapter = NL2SQLHttpSearchAdapter(_connection())

    def post_response(*_args, **_kwargs):
        return _Response()

    internal(adapter).session.post = post_response

    events = list(adapter.search_stream(query="查询合成值域", entity="flight"))

    assert [event["type"] for event in events] == ["start", "intermediate", "final", "done"]
    assert json.loads(events[2]["content"]) == {
        "success": True,
        "data": [{"id": 1}],
        "message": "查询成功，返回 1 行数据",
    }


def test_nl2sql_value_lookup_preserves_text_answer_when_no_rows():
    """A text answer with row_count=0 must keep the provider message."""
    adapter = NL2SQLHttpSearchAdapter(_connection())

    def post_response(*_args, **_kwargs):
        return _TextAnswerResponse()

    internal(adapter).session.post = post_response

    events = list(adapter.search_stream(query="查询合成值域", entity="flight"))

    final_event = next(e for e in events if e["type"] == "final")
    output = json.loads(final_event["content"])
    assert output["success"] is True
    assert output["message"] == "KUL 对应合成机场名"


def test_nl2sql_value_lookup_emits_provider_failure_in_final():
    adapter = NL2SQLHttpSearchAdapter(_connection())

    def post_response(*_args, **_kwargs):
        return _FailureResponse()

    internal(adapter).session.post = post_response

    events = list(adapter.search_stream(query="查询合成值域", entity="flight"))

    assert [event["type"] for event in events] == ["start", "final", "done"]
    output = json.loads(events[1]["content"])
    assert output["success"] is False
    assert "503" in output["message"]
