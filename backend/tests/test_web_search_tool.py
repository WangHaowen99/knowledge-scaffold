"""Tests for the public web_search tool."""

from __future__ import annotations

import json

from agentic.tools.web_search import handle_web_search


def _configure_web_search(tmp_path, monkeypatch, payload):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(json.dumps({"web_search": payload}), encoding="utf-8")
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)


def test_web_search_returns_normalized_results(tmp_path, monkeypatch):
    _configure_web_search(
        tmp_path,
        monkeypatch,
        {
            "enabled": True,
            "provider": "tavily",
            "base_url": "https://tavily.test",
            "api_key": "tvly-test",
            "timeout_seconds": 5,
            "default_max_results": 2,
            "max_results_limit": 5,
            "search_depth": "basic",
            "blocked_domains": "blocked.example.com",
        },
    )

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "query": "latest ontology systems",
                "request_id": "req-1",
                "response_time": "0.3",
                "results": [
                    {
                        "title": "Ontology Systems",
                        "url": "https://docs.example.com/ontology",
                        "content": "A concise result snippet.",
                        "score": 0.9,
                        "published_date": "2026-06-01",
                    }
                ],
            }

    class _FakeSession:
        def post(self, url, headers=None, json=None, timeout=None):
            captured.update(
                {
                    "url": url,
                    "headers": headers,
                    "json": json,
                    "timeout": timeout,
                }
            )
            return _FakeResponse()

        def close(self):
            return None

    monkeypatch.setattr(
        "agentic.web_search.providers.tavily_provider.requests.Session",
        lambda: _FakeSession(),
    )

    content = handle_web_search(
        "latest ontology systems",
        max_results=2,
        freshness="week",
        include_domains=["docs.example.com"],
    )
    payload = json.loads(content)

    assert payload["ok"] is True
    assert payload["provider"] == "tavily"
    assert payload["results"][0]["domain"] == "docs.example.com"
    assert payload["results"][0]["published_at"] == "2026-06-01"
    assert captured["url"] == "https://tavily.test/search"
    assert captured["headers"]["Authorization"] == "Bearer tvly-test"
    assert captured["json"]["query"] == "latest ontology systems"
    assert captured["json"]["max_results"] == 2
    assert captured["json"]["time_range"] == "week"
    assert captured["json"]["include_domains"] == ["docs.example.com"]
    assert captured["json"]["exclude_domains"] == ["blocked.example.com"]
    assert captured["timeout"] == 5


def test_web_search_disabled_returns_structured_error(tmp_path, monkeypatch):
    _configure_web_search(
        tmp_path,
        monkeypatch,
        {
            "enabled": False,
            "provider": "tavily",
        },
    )

    payload = json.loads(handle_web_search("latest ontology systems"))

    assert payload["ok"] is False
    assert payload["status"] == "error"
    assert "disabled" in payload["error"]


def test_web_search_rejects_result_count_above_configured_limit(tmp_path, monkeypatch):
    _configure_web_search(
        tmp_path,
        monkeypatch,
        {
            "enabled": True,
            "provider": "tavily",
            "api_key": "tvly-test",
            "default_max_results": 2,
            "max_results_limit": 3,
        },
    )

    payload = json.loads(handle_web_search("latest ontology systems", max_results=4))

    assert payload["ok"] is False
    assert "max_results must be <= 3" in payload["error"]


def test_web_search_rejects_url_like_domain_filters(tmp_path, monkeypatch):
    _configure_web_search(
        tmp_path,
        monkeypatch,
        {
            "enabled": True,
            "provider": "tavily",
            "api_key": "tvly-test",
        },
    )

    payload = json.loads(
        handle_web_search(
            "latest ontology systems",
            include_domains=["https://docs.example.com/path"],
        )
    )

    assert payload["ok"] is False
    assert "invalid domain" in payload["error"]
