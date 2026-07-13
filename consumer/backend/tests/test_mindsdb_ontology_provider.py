"""Tests for the MindsDB business data provider adapter."""

from __future__ import annotations

from agent.tools.business_data.providers.mindsdb_provider import (
    MindsDBOntologySearchProvider,
)


def _config() -> dict:
    return {
        "ontology_search": {
            "mindsdb": {
                "url": "http://unit.test",
                "login": "demo",
                "password": "",
                "default_database": "",
                "default_agent_name": "agent_x",
                "project_name": "project_x",
                "requires_auth": False,
                "timeout_seconds": 120,
                "max_retries": 3,
            }
        }
    }


def test_mindsdb_provider_streams_through_service_client():
    provider = MindsDBOntologySearchProvider(_config())
    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def iter_lines(self, decode_unicode=True):
            del decode_unicode
            yield 'data: {"type":"start","prompt":"开始执行本体探索"}'
            yield ""
            yield 'data: {"type":"data","content":"## 最终回答:\\n查询成功"}'
            yield ""
            yield 'data: {"type":"end"}'
            yield ""

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return None

    def _fake_post(url, headers=None, json=None, timeout=None, stream=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        captured["stream"] = stream
        return _FakeResponse()

    provider._session.post = _fake_post

    events = list(provider.search_stream(query="查询快艇 B001", entity="TargetItem"))

    assert captured == {
        "url": "http://unit.test/api/projects/project_x/agents/agent_x/completions/stream",
        "json": {"messages": [{"question": "查询快艇 B001", "answer": ""}]},
        "timeout": 120,
        "stream": True,
    }
    assert events == [
        {"type": "start", "content": "开始执行本体探索"},
        {"type": "final", "content": "查询成功"},
        {"type": "done", "content": "查询流程结束"},
    ]


def test_mindsdb_provider_keeps_plain_text_sse_chunks_as_messages():
    provider = MindsDBOntologySearchProvider(_config())

    class _FakeResponse:
        status_code = 200
        text = ""

        def iter_lines(self, decode_unicode=True):
            del decode_unicode
            yield "data: plain text chunk"
            yield ""
            yield 'data: {"type":"data","content":"## 最终回答:\\n查询成功"}'
            yield ""
            yield 'data: {"type":"end"}'
            yield ""

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return None

    provider._session.post = lambda url, headers=None, json=None, timeout=None, stream=None: _FakeResponse()

    events = list(provider.search_stream(query="查询快艇 B001", entity="TargetItem"))

    assert events == [
        {"type": "intermediate", "content": "plain text chunk"},
        {"type": "final", "content": "查询成功"},
        {"type": "done", "content": "查询流程结束"},
    ]
