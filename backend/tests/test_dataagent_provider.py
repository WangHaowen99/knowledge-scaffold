"""Tests for the DataAgent REST provider and service layer."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from agentic.business_data.provider_registry import (
    PROVIDER_SPECS,
    get_provider_spec,
    list_provider_schemas,
    normalize_provider_config,
    publicize_provider_config,
)
from agentic.ontology_search.providers.dataagent_provider import DataAgentProvider
from agentic.services.dataagent import (
    _extract_fields,
    _normalize_rows,
    _parse_a2a_response,
    call_query,
    format_output,
)


def test_dataagent_provider_is_registered():
    assert "dataagent" in PROVIDER_SPECS
    spec = get_provider_spec("dataagent")
    assert spec.name == "dataagent"
    assert spec.label == "DataAgent"
    assert spec.factory is DataAgentProvider


def test_dataagent_provider_schema_fields():
    spec = get_provider_spec("dataagent")
    fields = {field.name: field for field in spec.fields}

    assert set(fields) == {"url", "token", "timeout_seconds", "max_retries"}
    assert fields["url"].required is True
    assert fields["token"].secret is True
    assert fields["timeout_seconds"].default == 600
    assert fields["max_retries"].default == 3


def test_dataagent_provider_schema_in_public_list():
    schemas = list_provider_schemas()
    names = {schema["name"] for schema in schemas}
    assert "dataagent" in names


def test_dataagent_normalize_config_and_publicize_secret():
    normalized = normalize_provider_config(
        "dataagent",
        {"url": "https://dataagent.example.com/", "token": "secret", "timeout_seconds": "120"},
    )
    assert normalized["url"] == "https://dataagent.example.com"
    assert normalized["token"] == "secret"
    assert normalized["timeout_seconds"] == 120
    assert normalized["max_retries"] == 3

    public = publicize_provider_config("dataagent", normalized)
    assert public["url"] == "https://dataagent.example.com"
    assert public["has_token"] is True
    assert "token" not in public


def test_dataagent_normalize_config_rejects_missing_url():
    with pytest.raises(Exception, match="DataAgent 服务地址"):
        normalize_provider_config("dataagent", {"token": "secret"})


def test_dataagent_normalize_config_rejects_unknown_field():
    with pytest.raises(Exception, match="不支持字段"):
        normalize_provider_config("dataagent", {"url": "https://x.test", "scene": "demo"})


def _make_provider(provider_config: dict[str, Any]) -> DataAgentProvider:
    return DataAgentProvider({"ontology_search": {"dataagent": provider_config}})


def test_provider_search_stream_yields_formatted_events(monkeypatch):
    captured = {}

    def fake_call_query(server, token, question, timeout=300, *, max_retries=3):
        captured.update(
            {
                "server": server,
                "token": token,
                "question": question,
                "timeout": timeout,
                "max_retries": max_retries,
            }
        )
        return {
            "success": True,
            "data": {"columns": ["region"], "rows": [["华东"]]},
            "sql": "SELECT region FROM sales",
            "row_count": 1,
            "confidence": 0.95,
            "error": None,
            "raw": {},
        }

    monkeypatch.setattr(
        "agentic.ontology_search.providers.dataagent_provider.call_query",
        fake_call_query,
    )
    provider = _make_provider(
        {
            "url": "https://dataagent.example.com",
            "token": "secret",
            "timeout_seconds": 120,
            "max_retries": 2,
        }
    )

    events = list(provider.search_stream(query="查询销售额", entity="Sales"))

    assert captured == {
        "server": "https://dataagent.example.com",
        "token": "secret",
        "question": "查询销售额",
        "timeout": 120,
        "max_retries": 2,
    }
    assert [event["type"] for event in events] == ["start", "final", "done"]
    final = json.loads(events[1]["content"])
    assert final["success"] is True
    assert final["data"] == {"columns": ["region"], "rows": [["华东"]]}
    assert final["row_count"] == 1
    assert final["confidence"] == 0.95
    assert final["has_sql"] is True
    assert "sql" not in final


def test_provider_search_stream_formats_failure(monkeypatch):
    monkeypatch.setattr(
        "agentic.ontology_search.providers.dataagent_provider.call_query",
        lambda *args, **kwargs: {
            "success": False,
            "data": None,
            "sql": None,
            "row_count": 0,
            "confidence": None,
            "error": "Connection refused",
            "raw": {},
        },
    )
    provider = _make_provider({"url": "https://dataagent.example.com"})

    events = list(provider.search_stream(query="query", entity="E"))

    final = json.loads(events[1]["content"])
    assert final["success"] is False
    assert final["data"] is None
    assert "Connection refused" in final["message"]


def test_call_query_builds_a2a_rest_request(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "task": {
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {
                            "parts": [
                                {"text": "{'sql': 'SELECT 1', 'columns': ['n'], 'rows_preview': [(1,)]}"}
                            ],
                        },
                    },
                },
            }

    def fake_post(self, url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("requests.Session.post", fake_post)

    result = call_query("https://dataagent.test", "my-token", "查询数据", 120)

    assert captured["url"] == "https://dataagent.test/a2a/rest/message:send"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["headers"]["A2A-Version"] == "1.0"
    assert captured["headers"]["Authorization"] == "Bearer my-token"
    assert captured["timeout"] == 120
    assert captured["json"]["message"]["role"] == "ROLE_USER"
    assert captured["json"]["message"]["parts"] == [{"text": "查询数据"}]
    assert captured["json"]["configuration"] == {"returnImmediately": False}
    assert result["success"] is True
    assert result["sql"] == "SELECT 1"
    assert result["data"]["columns"] == ["n"]
    assert result["row_count"] == 1


def test_call_query_returns_error_on_http_failure(monkeypatch):
    class FakeResponse:
        status_code = 500
        text = "boom"

        def json(self):
            return {"error": {"message": "server error"}}

    monkeypatch.setattr("requests.Session.post", lambda *args, **kwargs: FakeResponse())

    result = call_query("https://dataagent.test", "", "query")

    assert result["success"] is False
    assert "HTTP 500" in result["error"]
    assert "server error" in result["error"]


def test_parse_response_extracts_structured_result():
    body = {
        "task": {
            "status": {
                "state": "TASK_STATE_COMPLETED",
                "message": {
                    "parts": [
                        {
                            "text": "{'sql': 'SELECT name FROM users', 'columns': ['name'], 'rows_preview': [('Alice',), ('Bob',)], 'confidence': 0.88}",
                        }
                    ],
                },
            },
        },
    }

    result = _parse_a2a_response(body)

    assert result["success"] is True
    assert result["sql"] == "SELECT name FROM users"
    assert result["data"]["columns"] == ["name"]
    assert result["data"]["rows"] == [["Alice"], ["Bob"]]
    assert result["row_count"] == 2
    assert result["confidence"] == 0.88


def test_parse_response_accepts_lowercase_completed_state_and_artifact_text():
    body = {
        "task": {
            "status": {"state": "completed", "message": {"parts": []}},
            "artifacts": [
                {"parts": [{"text": "根据分析，销售额增长了15%。"}]},
            ],
        },
    }

    result = _parse_a2a_response(body)

    assert result["success"] is True
    assert result["sql"] is None
    assert result["row_count"] == 0
    assert "销售额增长了15%" in result["answer"]


def test_parse_response_handles_failed_state_and_inner_error():
    failed = _parse_a2a_response(
        {
            "task": {
                "status": {
                    "state": "TASK_STATE_FAILED",
                    "message": {"parts": [{"text": "SQL 执行失败"}]},
                },
            },
        }
    )
    assert failed["success"] is False
    assert "SQL 执行失败" in failed["error"]

    inner_error = _parse_a2a_response(
        {
            "task": {
                "status": {
                    "state": "TASK_STATE_COMPLETED",
                    "message": {
                        "parts": [
                            {"text": "{'sql': 'SELECT 1', 'columns': [], 'rows_preview': [], 'error': 'table not found'}"}
                        ],
                    },
                },
            },
        }
    )
    assert inner_error["success"] is False
    assert inner_error["error"] == "table not found"


def test_format_output_success_failure_and_answer():
    success = format_output(
        {
            "success": True,
            "data": {"columns": ["a"], "rows": [[1], [2]]},
            "sql": "SELECT a FROM t",
            "row_count": 2,
            "confidence": 0.9,
        }
    )
    assert success["success"] is True
    assert success["has_sql"] is True
    assert "sql" not in success
    assert "返回 2 行" in success["message"]

    failure = format_output({"success": False, "data": None, "row_count": 0, "error": "timeout"})
    assert failure["success"] is False
    assert "timeout" in failure["message"]

    answer = format_output({"success": True, "data": None, "row_count": 0, "answer": "销售额为100万"})
    assert answer["success"] is True
    assert "销售额为100万" in answer["message"]


def test_extract_fields_and_normalize_rows():
    fields = _extract_fields(
        "{'sql': 'SELECT * FROM t', 'columns': ['id', 'name'], 'rows_preview': [(1, 'Alice')], 'confidence': 0.92, 'error': None}"
    )
    assert fields["sql"] == "SELECT * FROM t"
    assert fields["columns"] == ["id", "name"]
    assert fields["rows_preview"] == [(1, "Alice")]
    assert fields["confidence"] == 0.92
    assert fields["error"] is None

    assert _normalize_rows([(Decimal("1.5"),)]) == [["1.5"]]
    assert _normalize_rows(None) == []
