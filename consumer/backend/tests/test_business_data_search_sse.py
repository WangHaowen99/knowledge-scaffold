"""Tests for the business_data_search tool backed by the MindsDB streaming API."""

from __future__ import annotations

from agent.tools.business_data.adapters.mindsdb_search import MindsDBSearchAdapter
from shared_capabilities.business_data import AuthSpec, EffectiveConnection
from shared_capabilities.business_data.providers.mindsdb import (
    _THOUGHT_MARKER,
    _parse_mindsdb_chunk_to_events,
)
from agent.tools.business_data_search import (
    BUSINESS_DATA_SEARCH_TOOL_SCHEMA,
    handle_business_data_search,
)


def _fake_ontology(ontology_id):
    del ontology_id
    return object(), None


def test_business_data_search_rejects_empty_query(monkeypatch):
    monkeypatch.setattr("agent.tools.business_data_search.get_ontology_manifest", _fake_ontology)
    assert handle_business_data_search(ontology_id="demo", query="   ", entity="TargetItem") == "Error: Search query cannot be empty"


def test_business_data_search_schema_requires_ontology_entity_and_query():
    function_schema = BUSINESS_DATA_SEARCH_TOOL_SCHEMA["function"]
    entity_description = function_schema["parameters"]["properties"]["entity"]["description"]
    query_description = function_schema["parameters"]["properties"]["query"]["description"]

    assert "near-natural-language sentence" in query_description
    assert "object key" in entity_description
    assert "filter conditions" in query_description
    assert function_schema["parameters"]["required"] == ["ontology_id", "entity", "query"]
    assert function_schema["parameters"]["additionalProperties"] is False
    assert "action" in function_schema["parameters"]["properties"]
    assert "value_inputs" in function_schema["parameters"]["properties"]


def test_parse_mindsdb_chunk_to_events_handles_actions_and_final_output():
    action_events = _parse_mindsdb_chunk_to_events(
        {
            "actions": [
                {"log": "先读取对象定义{}\n继续".format(_THOUGHT_MARKER)},
                {"log": "先读取对象定义{}\n继续".format(_THOUGHT_MARKER)},
            ]
        }
    )
    final_events = _parse_mindsdb_chunk_to_events(
        {"output": "中间内容\n## 最终回答:\n最终结果"}
    )

    assert action_events == [{"type": "intermediate", "content": "先读取对象定义"}]
    assert final_events == [{"type": "final", "content": "最终结果"}]


def test_mindsdb_adapter_query_stream_parses_sse_events():
    provider = MindsDBSearchAdapter(
        EffectiveConnection(
            version=1,
            provider="mindsdb",
            endpoint="http://unit.test",
            auth=AuthSpec(type="none"),
            options={
                "default_database": "",
                "default_agent_name": "agent_x",
                "project_name": "project_x",
                "timeout_seconds": 120,
                "max_retries": 3,
            },
        )
    )

    class _FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, lines):
            self._lines = list(lines)

        def iter_lines(self, decode_unicode=True):
            del decode_unicode
            for line in self._lines:
                yield line

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return None

    def _fake_post(url, headers=None, json=None, timeout=None, stream=None):
        del headers, json, timeout, stream
        assert url == "http://unit.test/api/projects/project_x/agents/agent_x/completions/stream"
        return _FakeResponse(
            [
                'data: {"type":"start","prompt":"开始执行本体探索"}',
                "",
                'data: {"type":"data","content":"## 最终回答:\\n查询成功"}',
                "",
                'data: {"type":"end"}',
                "",
            ]
        )

    provider._session.post = _fake_post

    events = list(provider.search_stream(query="查询快艇 B001", entity="TargetItem"))

    assert events == [
        {"type": "start", "content": "开始执行本体探索"},
        {"type": "final", "content": "查询成功"},
        {"type": "done", "content": "查询流程结束"},
    ]
