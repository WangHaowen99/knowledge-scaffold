"""Provider selection tests for ontology_search."""

from __future__ import annotations

import pytest

from agentic.ontology_search.service import (
    OntologySearchService,
    build_ontology_search_config,
    query_ontology_stream,
)


def _config(provider: str | None = None) -> dict:
    payload = {
        "ontology_search": {
            "provider": provider or "",
            "timeout_seconds": 120,
            "max_retries": 3,
            "mindsdb": {
                "url": "http://mindsdb.test",
                "login": "mindsdb",
                "password": "",
                "default_database": "",
                "default_agent_name": "agent_x",
                "project_name": "project_x",
                "requires_auth": False,
                "timeout_seconds": 120,
                "max_retries": 3,
            },
            "nl2sql_http": {
                "url": "http://nl2sql.test",
                "token": "token-x",
                "scene": "scene-x",
                "timeout_seconds": 120,
                "max_retries": 3,
            },
        },
    }
    if provider is None:
        payload["ontology_search"].pop("provider")
    return payload


def test_service_requires_explicit_provider():
    with pytest.raises(ValueError, match="ontology_search.provider is not configured"):
        OntologySearchService(_config(provider=None))


def test_service_selects_nl2sql_http_provider(monkeypatch):
    captured = {}

    class _FakeProvider:
        name = "nl2sql_http"

        def __init__(self, config):
            captured["url"] = config["ontology_search"]["nl2sql_http"]["url"]

        def search_stream(self, *, query, entity):
            captured["query"] = query
            captured["entity"] = entity
            yield {"type": "final", "content": "nl2sql-result"}

    monkeypatch.setattr(
        "agentic.ontology_search.service.PROVIDER_FACTORIES",
        {"nl2sql_http": _FakeProvider},
    )

    events = list(OntologySearchService(_config("nl2sql_http")).search_stream(query="query", entity="TargetItem"))

    assert captured == {
        "url": "http://nl2sql.test",
        "query": "query",
        "entity": "TargetItem",
    }
    assert events == [{"type": "final", "content": "nl2sql-result"}]


def test_query_ontology_stream_uses_system_config_provider(monkeypatch):
    monkeypatch.setattr(
        "agentic.ontology_search.service.load_system_config",
        lambda: _config("nl2sql_http"),
    )

    class _FakeProvider:
        name = "nl2sql_http"

        def __init__(self, config):
            self.config = config

        def search_stream(self, *, query, entity):
            yield {"type": "start", "content": "{}:{}".format(entity, query)}

    monkeypatch.setattr(
        "agentic.ontology_search.service.PROVIDER_FACTORIES",
        {"nl2sql_http": _FakeProvider},
    )

    events = list(query_ontology_stream("query", entity="TargetItem", reuse_client=False))

    assert events == [{"type": "start", "content": "TargetItem:query"}]


def test_build_config_applies_provider_url_override_from_registry(monkeypatch):
    monkeypatch.setattr(
        "agentic.ontology_search.service.load_system_config",
        lambda: _config("nl2sql_http"),
    )

    config = build_ontology_search_config(
        api_url="http://override.test/",
        timeout=456,
    )

    assert config["ontology_search"]["provider"] == "nl2sql_http"
    assert config["ontology_search"]["nl2sql_http"]["url"] == "http://override.test"
    assert config["ontology_search"]["nl2sql_http"]["timeout_seconds"] == 456
