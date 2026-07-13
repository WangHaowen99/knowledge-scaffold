"""Tests for the MindsDB HTTP client helpers."""

from __future__ import annotations

import pytest

from agent.tools.business_data.script_env import SCRIPT_BUSINESS_DATA_CONNECTION_ENV
from agent.tools.business_data.providers.clients.data_provider_client import (
    DataProviderClient,
    DataProviderError,
)
from agent.tools.business_data.providers.clients.mindsdb import (
    MindsDBClient,
    MindsDBConfig,
    MindsDBError,
)


def test_client_query_agent_stream_parses_raw_mindsdb_chunks():
    config = MindsDBConfig(
        url="http://unit.test",
        login=None,
        password=None,
        default_database=None,
        default_agent_name="my_agent",
        project_name="mindsdb",
        requires_auth=False,
        timeout_seconds=120,
        max_retries=3,
    )
    client = MindsDBClient(config)
    chunks = [
        {"type": "start", "prompt": "start prompt"},
        {
            "actions": [
                {
                    "tool": "sql_db_list_tables",
                    "log": "准备执行SQL\n```\nThought: Do I need to use a tool? Yes\nAction: sql_db_list_tables",
                }
            ],
        },
        {
            "steps": [
                {
                    "action": {"tool": "sql_db_list_tables"},
                    "observation": "mindsdb.table_a, mindsdb.table_b",
                }
            ],
        },
        {
            "type": "context",
            "content": "Executing final SQL query: SELECT CHANGE_ORDER_ID FROM ontology.example_db.CHANGE_ORDER",
        },
        {"output": "final answer"},
        {"type": "end"},
    ]

    client.agent_completion_stream = lambda question, agent_name=None: iter(chunks)

    events = list(client.query_agent_stream("query"))

    assert events == [
        {"type": "start", "content": "start prompt"},
        {"type": "intermediate", "content": "准备执行SQL"},
        {"type": "intermediate", "content": "工具调用: sql_db_list_tables"},
        {"type": "intermediate", "content": "结果: table_a, table_b"},
        {"type": "final", "content": "final answer"},
        {"type": "done", "content": "查询流程结束"},
    ]


def test_client_query_sql_posts_expected_payload():
    config = MindsDBConfig(
        url="http://unit.test",
        login=None,
        password=None,
        default_database=None,
        default_agent_name="my_agent",
        project_name="mindsdb",
        requires_auth=False,
        timeout_seconds=120,
        max_retries=3,
    )
    client = MindsDBClient(config)
    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"type": "table", "data": [{"id": 1}]}

        def close(self):
            return None

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse()

    client._session.post = _fake_post

    result = client.query_sql("SELECT 1")

    assert captured["url"] == "http://unit.test/api/sql/query"
    assert captured["json"] == {
        "query": "SELECT 1",
        "context": {
            "db": "mindsdb",
            "show_secrets": False,
        },
    }
    assert result == {"type": "table", "data": [{"id": 1}]}


def test_client_query_sql_uses_configured_default_database():
    config = MindsDBConfig(
        url="http://unit.test",
        login=None,
        password=None,
        default_database="analytics",
        default_agent_name="my_agent",
        project_name="mindsdb",
        requires_auth=False,
        timeout_seconds=120,
        max_retries=3,
    )
    client = MindsDBClient(config)
    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"type": "table", "data": []}

        def close(self):
            return None

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _FakeResponse()

    client._session.post = _fake_post

    client.query_sql("SELECT 2")

    assert captured["json"]["context"]["db"] == "analytics"


def test_client_query_sql_authenticates_via_mindsdb_login_endpoint():
    config = MindsDBConfig(
        url="http://unit.test",
        login="mindsdb",
        password="secret-password",
        default_database="analytics",
        default_agent_name="my_agent",
        project_name="mindsdb",
        requires_auth=True,
        timeout_seconds=120,
        max_retries=3,
    )
    client = MindsDBClient(config)
    calls = []

    class _FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def close(self):
            return None

    def _fake_post(url, headers=None, json=None, timeout=None):
        calls.append({
            "url": url,
            "headers": headers or {},
            "json": json,
            "timeout": timeout,
        })
        if url.endswith("/api/login"):
            return _FakeResponse({"token": "jwt-token"})
        return _FakeResponse({"type": "table", "data": []})

    client._session.post = _fake_post

    client.query_sql("SELECT 2")

    assert calls == [
        {
            "url": "http://unit.test/api/login",
            "headers": {},
            "json": {"username": "mindsdb", "password": "secret-password"},
            "timeout": 10,
        },
        {
            "url": "http://unit.test/api/sql/query",
            "headers": {
                "Content-Type": "application/json",
                "Authorization": "Bearer jwt-token",
            },
            "json": {
                "query": "SELECT 2",
                "context": {"db": "analytics", "show_secrets": False},
            },
            "timeout": 120,
        },
    ]


def test_data_provider_client_routes_mindsdb_default_database_to_sql_context():
    client = DataProviderClient(
        {
            "provider": "mindsdb",
            "config": {
                "url": "http://unit.test",
                "default_database": "analytics",
                "default_agent_name": "agent_x",
                "project_name": "project_x",
                "requires_auth": False,
                "timeout_seconds": 120,
                "max_retries": 3,
            },
        }
    )
    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"type": "table", "data": []}

        def close(self):
            return None

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse()

    client._client._session.post = _fake_post

    client.query_sql("SELECT 3")

    assert captured["url"] == "http://unit.test/api/sql/query"
    assert captured["json"]["context"]["db"] == "analytics"
    assert captured["timeout"] == 120


def test_data_provider_client_requires_explicit_or_script_injected_connection(monkeypatch):
    monkeypatch.delenv(SCRIPT_BUSINESS_DATA_CONNECTION_ENV, raising=False)

    with pytest.raises(DataProviderError, match="business data source"):
        DataProviderClient()


def test_mindsdb_client_requires_script_injected_connection(monkeypatch):
    monkeypatch.delenv(SCRIPT_BUSINESS_DATA_CONNECTION_ENV, raising=False)

    def _fail_on_legacy_system_config():
        pytest.fail("legacy system_config fallback was used")

    monkeypatch.setattr(
        "agent.tools.business_data.providers.clients.mindsdb.load_system_config",
        _fail_on_legacy_system_config,
        raising=False,
    )

    with pytest.raises(MindsDBError, match="business data source"):
        MindsDBClient()


def test_dict_config_does_not_read_legacy_database_field():
    client = MindsDBClient(
        {
            "ontology_search": {
                "mindsdb": {
                    "url": "http://unit.test",
                    "database": "legacy_database",
                    "default_agent_name": "agent_x",
                    "project_name": "project_x",
                    "requires_auth": False,
                    "timeout_seconds": 120,
                    "max_retries": 3,
                }
            }
        }
    )

    assert client.config.default_database is None
