"""Tests for the NL2SQL HTTP ontology search provider."""

from __future__ import annotations

import json

import pytest

from agent.tools.business_data.script_env import SCRIPT_BUSINESS_DATA_CONNECTION_ENV
from agent.tools.business_data.providers.nl2sql_http_provider import NL2SQLHttpProvider
from agent.tools.business_data.providers.clients.data_provider_client import DataProviderClient
from agent.tools.business_data.providers.clients.nl2sql import NL2SQLClient, NL2SQLError


def _config() -> dict:
    return {
        "ontology_search": {
            "nl2sql_http": {
                "url": "http://nl2sql.test/",
                "token": "token-x",
                "scene": "scene-x",
                "timeout_seconds": 88,
                "max_retries": 2,
            },
        },
    }


def test_nl2sql_http_provider_posts_question_and_formats_result():
    provider = NL2SQLHttpProvider(_config())
    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "success": True,
                "sql": "SELECT * FROM table_x",
                "data": [{"id": 1}],
                "row_count": 1,
            }

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse()

    provider._session.post = _fake_post

    events = list(provider.search_stream(query="查询零件", entity="TargetItem"))

    assert captured == {
        "url": "http://nl2sql.test/api/v1/query",
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer token-x",
        },
        "json": {
            "question": "查询零件",
            "scene": "scene-x",
        },
        "timeout": 88,
    }
    assert events[0] == {"type": "start", "content": "开始查询本体数据"}
    assert events[1] == {"type": "intermediate", "content": "生成 SQL: SELECT * FROM table_x"}
    assert events[2]["type"] == "final"
    assert json.loads(events[2]["content"]) == {
        "success": True,
        "data": [{"id": 1}],
        "message": "查询成功，返回 1 行数据",
    }
    assert events[3] == {"type": "done", "content": "查询流程结束"}


def test_nl2sql_http_provider_returns_error_payload_for_http_error():
    provider = NL2SQLHttpProvider(_config())

    class _FakeResponse:
        status_code = 500
        text = "server failed"

        def json(self):
            raise ValueError("not json")

    provider._session.post = lambda url, headers=None, json=None, timeout=None: _FakeResponse()

    events = list(provider.search_stream(query="查询零件", entity="TargetItem"))

    assert events[0] == {"type": "start", "content": "开始查询本体数据"}
    assert json.loads(events[1]["content"]) == {
        "success": False,
        "data": None,
        "message": "查询失败: HTTP 500: server failed",
    }
    assert events[2] == {"type": "done", "content": "查询流程结束"}


def test_nl2sql_client_query_sql_posts_sql_endpoint():
    client = NL2SQLClient(_config())
    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "success": True,
                "data": [{"id": 1}],
                "columns": ["id"],
                "row_count": 1,
            }

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse()

    client._session.post = _fake_post

    result = client.query_sql("SELECT 1")

    assert captured == {
        "url": "http://nl2sql.test/api/v1/sql",
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer token-x",
        },
        "json": {
            "sql": "SELECT 1",
            "scene": "scene-x",
        },
        "timeout": 88,
    }
    assert result["success"] is True
    assert result["data"] == [{"id": 1}]


def test_data_provider_client_routes_query_sql_to_nl2sql():
    client = DataProviderClient(
        {
            "provider": "nl2sql_http",
            "config": {
                "url": "http://nl2sql.test/",
                "token": "token-x",
                "scene": "scene-x",
                "timeout_seconds": 88,
                "max_retries": 2,
            },
        }
    )
    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "data": [{"id": 1}], "row_count": 1}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse()

    client._client._session.post = _fake_post

    result = client.query_sql("SELECT 1")

    assert captured["url"] == "http://nl2sql.test/api/v1/sql"
    assert captured["json"] == {"sql": "SELECT 1", "scene": "scene-x"}
    assert captured["headers"]["Authorization"] == "Bearer token-x"
    assert captured["timeout"] == 88
    assert result["success"] is True


def test_nl2sql_client_requires_script_injected_connection(monkeypatch):
    monkeypatch.delenv(SCRIPT_BUSINESS_DATA_CONNECTION_ENV, raising=False)

    def _fail_on_legacy_system_config():
        pytest.fail("legacy system_config fallback was used")

    monkeypatch.setattr(
        "agent.tools.business_data.providers.clients.nl2sql.load_system_config",
        _fail_on_legacy_system_config,
        raising=False,
    )

    with pytest.raises(NL2SQLError, match="business data source"):
        NL2SQLClient()
