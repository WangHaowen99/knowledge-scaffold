"""Tests for the NL2SQL Template search adapter."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from agent.tools.business_data.adapters import (
    nl2sql_template_search as nl2sql_template_module,
)
from agent.tools.business_data.adapters.nl2sql_template_search import (
    NL2SQLTemplateSearchAdapter,
    _DefaultMatcher,
)
from shared_capabilities.business_data import AuthSpec, EffectiveConnection


def _provider(**overrides) -> NL2SQLTemplateSearchAdapter:
    config = {
        "url": "http://nl2sql.test/",
        "token": "token-x",
        "scene": "scene-x",
        "timeout_seconds": 88,
        "max_retries": 2,
        "ontology_id": "",
        "_workspace_root": "",
        **overrides,
    }
    workspace_root = str(config.pop("_workspace_root"))
    workspace_id = str(config.pop("workspace_id", ""))
    ontology_id = str(config.pop("ontology_id"))
    ontology_version = str(config.pop("ontology_version", ""))
    endpoint = str(config.pop("url")).rstrip("/")
    token = str(config.pop("token"))
    connection = EffectiveConnection(
        version=1,
        provider="nl2sql_http",
        endpoint=endpoint,
        auth=AuthSpec(type="bearer", token=token),
        options=config,
    )
    return NL2SQLTemplateSearchAdapter(
        connection,
        workspace_root=workspace_root,
        workspace_id=workspace_id,
        ontology_id=ontology_id,
        ontology_version=ontology_version,
    )


# ---------------------------------------------------------------------------
# Default matcher — exact match, template hit → SQL direct
# ---------------------------------------------------------------------------

def test_default_matcher_hits_template_and_executes_sql(tmp_path):
    """Built-in matcher: exact key match → SQL execution (not NL2SQL)."""
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    templates_file = scripts_dir / "query_templates.json"
    templates_file.write_text(
        json.dumps({"查询零件": "SELECT * FROM Part"}, ensure_ascii=False),
        encoding="utf-8",
    )

    provider = _provider(
        _workspace_root=str(tmp_path),
        ontology_id="test-ontology",
    )

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "sql": "SELECT * FROM Part", "data": [{"id": 1}], "row_count": 1}

    def _fake_post(url, headers=None, timeout=None, **request_kwargs):
        captured["url"] = url
        captured["json"] = request_kwargs.get("json")
        captured["timeout"] = timeout
        return _FakeResponse()

    provider._session.post = _fake_post

    events = list(provider.search_stream(query="查询零件", entity="TargetItem"))

    # Should call /api/v1/sql (not /api/v1/query)
    assert captured["url"] == "http://nl2sql.test/api/v1/sql"
    assert "SELECT * FROM Part" in captured["json"]["sql"]

    assert events[0] == {"type": "start", "content": "开始查询本体数据"}
    assert events[1]["type"] == "intermediate"
    assert "模板命中" in events[1]["content"]
    assert events[1]["public_summary"] == "模板命中，正在执行预定义查询"
    assert "SELECT" not in events[1]["public_summary"]
    assert events[2]["type"] == "final"
    payload = json.loads(events[2]["content"])
    assert payload["success"] is True
    assert events[3] == {"type": "done", "content": "查询流程结束"}


# ---------------------------------------------------------------------------
# Default matcher — no match → NL2SQL fallback
# ---------------------------------------------------------------------------

def test_default_matcher_falls_back_to_nl2sql(tmp_path):
    """Built-in matcher: no template match → NL2SQL query fallback."""
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    templates_file = scripts_dir / "query_templates.json"
    templates_file.write_text("{}", encoding="utf-8")

    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "data": [], "row_count": 0}

    def _fake_post(url, headers=None, timeout=None, **request_kwargs):
        captured["url"] = url
        captured["json"] = request_kwargs.get("json")
        return _FakeResponse()

    provider._session.post = _fake_post

    events = list(provider.search_stream(query="查某个不存在的东西", entity=""))

    # Should call /api/v1/query (NL2SQL)
    assert captured["url"] == "http://nl2sql.test/api/v1/query"
    assert "模板未命中" in events[1]["content"]
    assert events[1]["public_summary"] == "模板未命中，改用自然语言查询"


# ---------------------------------------------------------------------------
# Default matcher — rich template format with dedupe + group_by
# ---------------------------------------------------------------------------

def test_default_matcher_with_rich_template_dedupes(tmp_path):
    """Built-in matcher: rich template format triggers dedupe post-processing."""
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    templates_file = scripts_dir / "query_templates.json"
    templates_file.write_text(
        json.dumps(
            {
                "查询零件": {
                    "sql": "SELECT * FROM Part WHERE id IN ({IDS}) ORDER BY updateTime DESC",
                    "dedupe_key": "id",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "success": True,
                "data": [
                    {"id": "P1", "value": "b", "updateTime": "2024-01-02"},
                    {"id": "P1", "value": "a", "updateTime": "2024-01-01"},
                    {"id": "P2", "value": "c", "updateTime": "2024-01-01"},
                ],
                "row_count": 3,
            }

    provider._session.post = lambda *a, **kw: _FakeResponse()

    events = list(provider.search_stream(query="查询零件", entity=""))

    final = json.loads(events[2]["content"])
    # Deduped: P1 keeps the first (updateTime 2024-01-02, SQL already sorted DESC), P2 unchanged
    assert len(final["data"]) == 2
    p1_rows = [r for r in final["data"] if r["id"] == "P1"]
    assert len(p1_rows) == 1
    assert p1_rows[0]["updateTime"] == "2024-01-02"


# ---------------------------------------------------------------------------
# External matcher discovery
# ---------------------------------------------------------------------------

def test_external_matcher_discovered_and_called(tmp_path):
    """If sql_matcher.py exists in ontology_dir/scripts/, it is loaded and called."""
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)

    # Write a minimal sql_matcher.py
    matcher_code = textwrap.dedent("""\
        def matcher(query):
            if "零件" in query:
                return "SELECT * FROM Part", {"from_template": "零件模板"}
            return None, None
    """)
    (scripts_dir / "sql_matcher.py").write_text(matcher_code, encoding="utf-8")

    provider = _provider(
        _workspace_root=str(tmp_path),
        ontology_id="test-ontology",
    )

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "data": [{"id": 1}], "row_count": 1}

    def _fake_post(url, headers=None, timeout=None, **request_kwargs):
        captured["json"] = request_kwargs.get("json")
        return _FakeResponse()

    provider._session.post = _fake_post

    events = list(provider.search_stream(query="查询零件信息", entity=""))

    # Should hit the external matcher and execute the returned SQL
    assert captured["json"]["sql"] == "SELECT * FROM Part"
    assert "模板命中" in events[1]["content"]


def test_external_matcher_load_emits_structured_legacy_audit(tmp_path, monkeypatch):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    matcher_path = scripts_dir / "sql_matcher.py"
    matcher_path.write_text(
        "def matcher(query):\n    return None, None\n",
        encoding="utf-8",
    )
    audit_events = []
    monkeypatch.setattr(
        nl2sql_template_module.logger,
        "warning",
        lambda *args, **kwargs: audit_events.append(kwargs["extra"]),
    )

    _provider(
        _workspace_root=str(tmp_path),
        workspace_id="workspace-a",
        ontology_id="test-ontology",
    )

    assert audit_events == [
        {
            "security_event": "legacy_sql_matcher_load",
            "execution_decision": "backend_dynamic_import",
            "legacy_capability": "sql_matcher.py",
            "provider": "nl2sql_template",
            "load_status": "loaded",
            "workspace_id": "workspace-a",
            "ontology_id": "test-ontology",
            "ontology_version": "",
            "matcher_path": str(matcher_path),
            "error_type": "",
        }
    ]


def test_external_matcher_load_failure_is_audited(tmp_path, monkeypatch):
    scripts_dir = tmp_path / "test-ontology" / "scripts"
    scripts_dir.mkdir(parents=True)
    matcher_path = scripts_dir / "sql_matcher.py"
    matcher_path.write_text("NOT_A_MATCHER = True\n", encoding="utf-8")
    audit_events = []
    monkeypatch.setattr(
        nl2sql_template_module.logger,
        "warning",
        lambda *args, **kwargs: audit_events.append(kwargs["extra"]),
    )

    with pytest.raises(AttributeError, match="must define a callable"):
        _provider(
            _workspace_root=str(tmp_path),
            workspace_id="workspace-a",
            ontology_id="test-ontology",
        )

    assert audit_events[0]["load_status"] == "failed"
    assert audit_events[0]["error_type"] == "AttributeError"
    assert audit_events[0]["matcher_path"] == str(matcher_path)


def test_external_matcher_no_match_falls_back_to_nl2sql(tmp_path):
    """External matcher returns (None, None) → provider falls back to NL2SQL."""
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)

    matcher_code = textwrap.dedent("""\
        def matcher(query):
            return None, None
    """)
    (scripts_dir / "sql_matcher.py").write_text(matcher_code, encoding="utf-8")

    provider = _provider(
        _workspace_root=str(tmp_path),
        ontology_id="test-ontology",
    )

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "data": [], "row_count": 0}

    def _fake_post(url, headers=None, timeout=None, **_request_kwargs):
        captured["url"] = url
        return _FakeResponse()

    provider._session.post = _fake_post

    events = list(provider.search_stream(query="随便查", entity=""))

    assert captured["url"] == "http://nl2sql.test/api/v1/query"
    assert "模板未命中" in events[1]["content"]


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------

def test_template_matcher_returns_error_for_http_failure(tmp_path):
    """SQL execution returns HTTP error → final event contains error message."""
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    templates_file = scripts_dir / "query_templates.json"
    templates_file.write_text(
        json.dumps({"查询零件": "SELECT * FROM Part"}, ensure_ascii=False),
        encoding="utf-8",
    )

    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

    class _FakeResponse:
        status_code = 500
        text = "server failed"

        def json(self):
            raise ValueError("not json")

    provider._session.post = lambda *a, **kw: _FakeResponse()

    events = list(provider.search_stream(query="查询零件", entity=""))

    final = json.loads(events[2]["content"])
    assert final["success"] is False
    assert "HTTP 500" in final["message"]


# ---------------------------------------------------------------------------
# Dedupe post-processing
# ---------------------------------------------------------------------------

def test_dedupe_result_keeps_first_per_key():
    """_dedupe_result keeps only the first row per dedupe_key (SQL must pre-sort)."""
    result = {
        "success": True,
        "data": [
            {"id": "A", "val": 2, "updateTime": "2024-01-03"},
            {"id": "A", "val": 0, "updateTime": "2024-01-02"},
            {"id": "B", "val": 3, "updateTime": "2024-01-02"},
            {"id": "A", "val": 1, "updateTime": "2024-01-01"},
        ],
    }

    deduped = NL2SQLTemplateSearchAdapter._dedupe_result(result, dedupe_key="id")

    assert len(deduped["data"]) == 2
    row_a = next(r for r in deduped["data"] if r["id"] == "A")
    assert row_a["val"] == 2  # first occurrence (SQL sorted DESC by updateTime)
    row_b = next(r for r in deduped["data"] if r["id"] == "B")
    assert row_b["val"] == 3


def test_dedupe_result_empty_data_is_noop():
    """_dedupe_result with empty data returns the result unchanged."""
    result = {"success": True, "data": []}
    deduped = NL2SQLTemplateSearchAdapter._dedupe_result(result, dedupe_key="id")
    assert len(deduped["data"]) == 0


# ---------------------------------------------------------------------------
# Default matcher — simple string template format
# ---------------------------------------------------------------------------

def test_default_matcher_simple_string_format(tmp_path):
    """Built-in matcher: simple string format (key → SQL string)."""
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    templates_file = scripts_dir / "query_templates.json"
    templates_file.write_text(
        json.dumps({"查询零件": "SELECT * FROM Part"}, ensure_ascii=False),
        encoding="utf-8",
    )

    matcher = _DefaultMatcher(str(tmp_path), "test-ontology")

    sql, metadata = matcher("查询零件")
    assert sql == "SELECT * FROM Part"
    assert metadata["from_template"] == "查询零件"
    assert "dedupe_key" not in metadata

    sql_none, meta_none = matcher("不存在")
    assert sql_none is None
    assert meta_none is None


def test_default_matcher_rich_dict_format(tmp_path):
    """Built-in matcher: rich dict format (key → {sql, dedupe_key})."""
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    templates_file = scripts_dir / "query_templates.json"
    templates_file.write_text(
        json.dumps(
            {
                "查询零件": {
                    "sql": "SELECT * FROM Part ORDER BY updateTime DESC",
                    "dedupe_key": "id",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    matcher = _DefaultMatcher(str(tmp_path), "test-ontology")

    sql, metadata = matcher("查询零件")
    assert sql == "SELECT * FROM Part ORDER BY updateTime DESC"
    assert metadata["dedupe_key"] == "id"


# ---------------------------------------------------------------------------
# Template path resolution (fixed: scripts/query_templates.json)
# ---------------------------------------------------------------------------

def test_default_matcher_resolves_template_path(tmp_path):
    """_DefaultMatcher loads scripts/query_templates.json under workspace_root/ontology_id."""
    ontology_dir = tmp_path / "my-ontology"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)

    templates_file = scripts_dir / "query_templates.json"
    templates_file.write_text(
        json.dumps({"查询零件": "SELECT 1"}, ensure_ascii=False),
        encoding="utf-8",
    )

    matcher = _DefaultMatcher(str(tmp_path), "my-ontology")
    sql, metadata = matcher("查询零件")
    assert sql == "SELECT 1"


def test_default_matcher_resolves_versioned_scripts_dir(tmp_path):
    """_DefaultMatcher loads scripts from the active ontology version directory."""
    ontology_root = tmp_path / "my-ontology"
    version_dir = ontology_root / "versions" / "2.0.0"
    scripts_dir = version_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (ontology_root / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                "id: my-ontology",
                "name: My Ontology",
                "description: Demo",
                "version: 2.0.0",
                "active_version: 2.0.0",
                "versions:",
                "  - 1.0.0",
                "  - 2.0.0",
                "language: zh-CN",
            ]
        ),
        encoding="utf-8",
    )
    (scripts_dir / "query_templates.json").write_text(
        json.dumps({"查询零件": "SELECT 2"}, ensure_ascii=False),
        encoding="utf-8",
    )

    matcher = _DefaultMatcher(str(tmp_path), "my-ontology")
    sql, metadata = matcher("查询零件")
    assert sql == "SELECT 2"


def test_external_matcher_uses_versioned_scripts_dir(tmp_path):
    """External sql_matcher.py is discovered under the active ontology version directory."""
    ontology_root = tmp_path / "test-ontology"
    version_dir = ontology_root / "versions" / "1.0.0"
    scripts_dir = version_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (ontology_root / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                "id: test-ontology",
                "name: Test Ontology",
                "description: Demo",
                "version: 1.0.0",
                "active_version: 1.0.0",
                "versions:",
                "  - 1.0.0",
                "language: zh-CN",
            ]
        ),
        encoding="utf-8",
    )

    matcher_code = textwrap.dedent("""\
        def matcher(query):
            if "零件" in query:
                return "SELECT * FROM VersionedPart", {"from_template": "零件模板"}
            return None, None
    """)
    (scripts_dir / "sql_matcher.py").write_text(matcher_code, encoding="utf-8")

    provider = _provider(
        _workspace_root=str(tmp_path),
        ontology_id="test-ontology",
    )

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "data": [{"id": 1}], "row_count": 1}

    def _fake_post(url, headers=None, timeout=None, **request_kwargs):
        captured["json"] = request_kwargs.get("json")
        return _FakeResponse()

    provider._session.post = _fake_post

    events = list(provider.search_stream(query="查询零件信息", entity=""))

    assert captured["json"]["sql"] == "SELECT * FROM VersionedPart"
    assert "模板命中" in events[1]["content"]
