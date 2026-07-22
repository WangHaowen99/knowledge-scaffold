"""Tests for the NL2SQL Template search adapter."""

from __future__ import annotations

import json
import logging
from pathlib import Path

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
    ontology_id = str(config.pop("ontology_id"))
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
        ontology_id=ontology_id,
    )


def _write_templates(scripts_dir: Path, payload: dict[str, object]) -> None:
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "query_templates.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_matcher(scripts_dir: Path, payload: dict[str, object]) -> None:
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "sql_matcher.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def test_default_matcher_hits_template_and_executes_sql(tmp_path):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_templates(scripts_dir, {"查询零件": "SELECT * FROM Part"})

    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "sql": "SELECT * FROM Part", "data": [{"id": 1}], "row_count": 1}

    def _fake_post(url, headers=None, timeout=None, **request_kwargs):
        captured["url"] = url
        captured["json"] = request_kwargs.get("json")
        return _FakeResponse()

    provider._session.post = _fake_post

    events = list(provider.search_stream(query="查询零件", entity="TargetItem"))

    assert captured["url"] == "http://nl2sql.test/api/v1/sql"
    assert "SELECT * FROM Part" in captured["json"]["sql"]
    assert events[0] == {"type": "start", "content": "开始查询本体数据"}
    assert "模板命中" in events[1]["content"]
    assert json.loads(events[2]["content"])["success"] is True
    assert events[3] == {"type": "done", "content": "查询流程结束"}


def test_default_matcher_falls_back_to_nl2sql(tmp_path):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_templates(scripts_dir, {})

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

    assert captured["url"] == "http://nl2sql.test/api/v1/query"
    assert "模板未命中" in events[1]["content"]


def test_default_matcher_with_rich_template_dedupes(tmp_path):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_templates(
        scripts_dir,
        {
            "查询零件": {
                "sql": "SELECT * FROM Part WHERE id IN ({IDS}) ORDER BY updateTime DESC",
                "dedupe_key": "id",
            },
        },
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
    assert len(final["data"]) == 2
    p1_rows = [r for r in final["data"] if r["id"] == "P1"]
    assert len(p1_rows) == 1
    assert p1_rows[0]["updateTime"] == "2024-01-02"


def test_declarative_matcher_discovered_and_called(tmp_path):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_matcher(
        scripts_dir,
        {
            "version": 1,
            "rules": [
                {
                    "match": {"type": "contains", "value": "零件"},
                    "sql": "SELECT * FROM Part",
                    "metadata": {"from_template": "零件模板"},
                }
            ],
        },
    )

    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

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

    assert captured["json"]["sql"] == "SELECT * FROM Part"
    assert "模板命中" in events[1]["content"]


def test_declarative_matcher_no_match_falls_back_to_nl2sql(tmp_path):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_matcher(
        scripts_dir,
        {
            "version": 1,
            "rules": [
                {
                    "match": {"type": "contains", "value": "零件"},
                    "sql": "SELECT * FROM Part",
                }
            ],
        },
    )

    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

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


def test_declarative_then_default_matcher_precedence(tmp_path):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_templates(scripts_dir, {"查询零件": "SELECT * FROM ExactTemplate"})
    _write_matcher(
        scripts_dir,
        {
            "version": 1,
            "rules": [
                {
                    "match": {"type": "contains", "value": "供应商"},
                    "sql": "SELECT * FROM Supplier",
                }
            ],
        },
    )

    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "data": [{"id": 1}], "row_count": 1}

    def _fake_post(url, headers=None, timeout=None, **request_kwargs):
        captured["url"] = url
        captured["json"] = request_kwargs.get("json")
        return _FakeResponse()

    provider._session.post = _fake_post

    events = list(provider.search_stream(query="查询零件", entity=""))

    assert captured["url"] == "http://nl2sql.test/api/v1/sql"
    assert captured["json"]["sql"] == "SELECT * FROM ExactTemplate"
    assert "模板命中" in events[1]["content"]


def test_declarative_matcher_rejects_sql_template_and_falls_back(tmp_path, caplog):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_matcher(
        scripts_dir,
        {
            "version": 1,
            "rules": [
                {
                    "match": {"type": "contains", "value": "零件"},
                    "sql_template": "SELECT * FROM Part WHERE name = '{query}'",
                }
            ],
        },
    )

    caplog.set_level(logging.WARNING)
    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "data": [], "row_count": 0}

    def _fake_post(url, headers=None, timeout=None, **request_kwargs):
        captured["url"] = url
        return _FakeResponse()

    provider._session.post = _fake_post
    events = list(provider.search_stream(query="查询零件", entity=""))

    assert captured["url"] == "http://nl2sql.test/api/v1/query"
    assert "模板未命中" in events[1]["content"]
    assert "does not support sql_template" in caplog.text


def test_declarative_matcher_rejects_regex_rules_and_falls_back(tmp_path, caplog):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_matcher(
        scripts_dir,
        {
            "version": 1,
            "rules": [
                {
                    "match": {"type": "regex", "value": "^(a+)+$"},
                    "sql": "SELECT * FROM Part",
                }
            ],
        },
    )

    caplog.set_level(logging.WARNING)
    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "data": [], "row_count": 0}

    def _fake_post(url, headers=None, timeout=None, **request_kwargs):
        captured["url"] = url
        return _FakeResponse()

    provider._session.post = _fake_post
    events = list(provider.search_stream(query="aaaaaaaaaaaaaaaaaaaaab", entity=""))

    assert captured["url"] == "http://nl2sql.test/api/v1/query"
    assert "模板未命中" in events[1]["content"]
    assert "supports only exact and contains match types" in caplog.text


def test_declarative_matcher_rejects_non_integer_version(tmp_path):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_matcher(
        scripts_dir,
        {
            "version": {"major": 1},
            "rules": [{"match": {"type": "exact", "value": "查询零件"}, "sql": "SELECT 1"}],
        },
    )

    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "data": [], "row_count": 0}

    def _fake_post(url, headers=None, timeout=None, **request_kwargs):
        captured["url"] = url
        return _FakeResponse()

    provider._session.post = _fake_post
    events = list(provider.search_stream(query="查询零件", entity=""))

    assert captured["url"] == "http://nl2sql.test/api/v1/query"
    assert "模板未命中" in events[1]["content"]


def test_declarative_matcher_rejects_non_boolean_ignore_case_and_falls_back(tmp_path, caplog):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_matcher(
        scripts_dir,
        {
            "version": 1,
            "rules": [
                {
                    "match": {"type": "contains", "value": "零件", "ignore_case": "false"},
                    "sql": "SELECT * FROM Part",
                }
            ],
        },
    )

    caplog.set_level(logging.WARNING)
    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "data": [], "row_count": 0}

    def _fake_post(url, headers=None, timeout=None, **request_kwargs):
        captured["url"] = url
        return _FakeResponse()

    provider._session.post = _fake_post
    events = list(provider.search_stream(query="查询零件", entity=""))

    assert captured["url"] == "http://nl2sql.test/api/v1/query"
    assert "ignore_case must be a boolean" in caplog.text
    assert "模板未命中" in events[1]["content"]


def test_declarative_matcher_rejects_non_string_sql_and_falls_back(tmp_path, caplog):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_matcher(
        scripts_dir,
        {
            "version": 1,
            "rules": [
                {
                    "match": {"type": "contains", "value": "零件"},
                    "sql": ["SELECT 1"],
                }
            ],
        },
    )

    caplog.set_level(logging.WARNING)
    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "data": [], "row_count": 0}

    def _fake_post(url, headers=None, timeout=None, **request_kwargs):
        captured["url"] = url
        return _FakeResponse()

    provider._session.post = _fake_post
    events = list(provider.search_stream(query="查询零件", entity=""))

    assert captured["url"] == "http://nl2sql.test/api/v1/query"
    assert "sql must be a string" in caplog.text
    assert "模板未命中" in events[1]["content"]


def test_declarative_matcher_rejects_non_string_reserved_metadata_and_falls_back(tmp_path, caplog):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_matcher(
        scripts_dir,
        {
            "version": 1,
            "rules": [
                {
                    "match": {"type": "contains", "value": "零件"},
                    "sql": "SELECT * FROM Part",
                    "metadata": {"dedupe_key": ["id"]},
                }
            ],
        },
    )

    caplog.set_level(logging.WARNING)
    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "data": [], "row_count": 0}

    def _fake_post(url, headers=None, timeout=None, **request_kwargs):
        captured["url"] = url
        return _FakeResponse()

    provider._session.post = _fake_post
    events = list(provider.search_stream(query="查询零件", entity=""))

    assert captured["url"] == "http://nl2sql.test/api/v1/query"
    assert "metadata.dedupe_key must be a string" in caplog.text
    assert "模板未命中" in events[1]["content"]


def test_declarative_matcher_allows_non_scalar_metadata_values(tmp_path):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_matcher(
        scripts_dir,
        {
            "version": 1,
            "rules": [
                {
                    "match": {"type": "contains", "value": "零件"},
                    "sql": "SELECT * FROM Part",
                    "metadata": {
                        "from_template": "零件模板",
                        "tags": ["a", "b"],
                        "extra": {"source": "matcher"},
                    },
                }
            ],
        },
    )

    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True, "data": [{"id": 1}], "row_count": 1}

    provider._session.post = lambda *a, **kw: _FakeResponse()

    events = list(provider.search_stream(query="查询零件信息", entity=""))

    final = json.loads(events[2]["content"])
    assert final["success"] is True
    assert final["from_template"] == "零件模板"


def test_legacy_python_matcher_is_blocked_and_not_executed(tmp_path, caplog):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    marker_path = tmp_path / "legacy-match-ran.txt"
    (scripts_dir / "sql_matcher.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "Path(r'{}').write_text('executed', encoding='utf-8')".format(marker_path),
                "def matcher(query):",
                "    return 'SELECT 1', {'from_template': 'legacy'}",
            ]
        ),
        encoding="utf-8",
    )

    caplog.set_level(logging.WARNING)
    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

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
    events = list(provider.search_stream(query="不会命中", entity=""))

    assert not marker_path.exists()
    assert captured["url"] == "http://nl2sql.test/api/v1/query"
    assert events[1]["content"] == "模板未命中，使用自然语言查询"
    assert "UNTRUSTED_MATCHER_BLOCKED" in caplog.text
    assert any(
        getattr(record, "security_event", "") == "untrusted_matcher_blocked"
        for record in caplog.records
    )


def test_template_matcher_returns_error_for_http_failure(tmp_path):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_templates(scripts_dir, {"查询零件": "SELECT * FROM Part"})

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


def test_dedupe_result_keeps_first_per_key():
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
    assert row_a["val"] == 2
    row_b = next(r for r in deduped["data"] if r["id"] == "B")
    assert row_b["val"] == 3


def test_dedupe_result_empty_data_is_noop():
    result = {"success": True, "data": []}
    deduped = NL2SQLTemplateSearchAdapter._dedupe_result(result, dedupe_key="id")
    assert len(deduped["data"]) == 0


def test_default_matcher_simple_string_format(tmp_path):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_templates(scripts_dir, {"查询零件": "SELECT * FROM Part"})

    matcher = _DefaultMatcher(str(tmp_path), "test-ontology")

    sql, metadata = matcher("查询零件")
    assert sql == "SELECT * FROM Part"
    assert metadata["from_template"] == "查询零件"
    assert "dedupe_key" not in metadata

    sql_none, meta_none = matcher("不存在")
    assert sql_none is None
    assert meta_none is None


def test_default_matcher_rich_dict_format(tmp_path):
    ontology_dir = tmp_path / "test-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_templates(
        scripts_dir,
        {
            "查询零件": {
                "sql": "SELECT * FROM Part ORDER BY updateTime DESC",
                "dedupe_key": "id",
            },
        },
    )

    matcher = _DefaultMatcher(str(tmp_path), "test-ontology")

    sql, metadata = matcher("查询零件")
    assert sql == "SELECT * FROM Part ORDER BY updateTime DESC"
    assert metadata["dedupe_key"] == "id"


def test_default_matcher_resolves_template_path(tmp_path):
    ontology_dir = tmp_path / "my-ontology"
    scripts_dir = ontology_dir / "scripts"
    _write_templates(scripts_dir, {"查询零件": "SELECT 1"})

    matcher = _DefaultMatcher(str(tmp_path), "my-ontology")
    sql, metadata = matcher("查询零件")
    assert sql == "SELECT 1"
    assert metadata["from_template"] == "查询零件"


def test_default_matcher_resolves_versioned_scripts_dir(tmp_path):
    ontology_root = tmp_path / "my-ontology"
    version_dir = ontology_root / "versions" / "2.0.0"
    scripts_dir = version_dir / "scripts"
    ontology_root.mkdir(parents=True)
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
    _write_templates(scripts_dir, {"查询零件": "SELECT 2"})

    matcher = _DefaultMatcher(str(tmp_path), "my-ontology")
    sql, metadata = matcher("查询零件")
    assert sql == "SELECT 2"
    assert metadata["from_template"] == "查询零件"


def test_declarative_matcher_uses_versioned_scripts_dir(tmp_path):
    ontology_root = tmp_path / "test-ontology"
    version_dir = ontology_root / "versions" / "1.0.0"
    scripts_dir = version_dir / "scripts"
    ontology_root.mkdir(parents=True)
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
    _write_matcher(
        scripts_dir,
        {
            "version": 1,
            "rules": [
                {
                    "match": {"type": "contains", "value": "零件"},
                    "sql": "SELECT * FROM VersionedPart",
                    "metadata": {"from_template": "零件模板"},
                }
            ],
        },
    )

    provider = _provider(_workspace_root=str(tmp_path), ontology_id="test-ontology")

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
