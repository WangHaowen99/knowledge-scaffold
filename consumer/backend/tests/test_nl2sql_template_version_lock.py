# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

import json

from agent.tools.business_data.adapters.nl2sql_template_search import (
    NL2SQLTemplateSearchAdapter,
    _DefaultMatcher,
)
from backend_synthetic_credentials import synthetic_secret
from shared_capabilities.business_data import AuthSpec, EffectiveConnection
from internal_access import internal


def _provider(*, workspace_root: str, ontology_id: str, ontology_version: str = ""):
    connection = EffectiveConnection(
        version=1,
        provider="nl2sql_http",
        endpoint="https://nl2sql.example.net",
        auth=AuthSpec(type="bearer", token=synthetic_secret("nl2sql_template_version_lock-credential-01")),
        options={"scene": "scene-x", "timeout_seconds": 88, "max_retries": 2},
    )
    return NL2SQLTemplateSearchAdapter(
        connection,
        workspace_root=workspace_root,
        ontology_id=ontology_id,
        ontology_version=ontology_version,
    )


def test_default_matcher_uses_locked_version_instead_of_current_active(tmp_path):
    ontology_root = tmp_path / "my-ontology"
    version1_dir = ontology_root / "versions" / "1.0.0" / "scripts"
    version2_dir = ontology_root / "versions" / "2.0.0" / "scripts"
    version1_dir.mkdir(parents=True)
    version2_dir.mkdir(parents=True)
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
    (version1_dir / "query_templates.json").write_text(
        json.dumps({"query": "SELECT old_version"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (version2_dir / "query_templates.json").write_text(
        json.dumps({"query": "SELECT active_version"}, ensure_ascii=False),
        encoding="utf-8",
    )

    matcher = _DefaultMatcher(str(tmp_path), "my-ontology", "1.0.0")
    sql, metadata = matcher("query")
    assert sql == "SELECT old_version"
    assert metadata["from_template"] == "query"


def test_adapter_default_matcher_uses_locked_version_templates(tmp_path):
    ontology_root = tmp_path / "test-ontology"
    version1_dir = ontology_root / "versions" / "1.0.0" / "scripts"
    version2_dir = ontology_root / "versions" / "2.0.0" / "scripts"
    version1_dir.mkdir(parents=True)
    version2_dir.mkdir(parents=True)
    (ontology_root / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                "id: test-ontology",
                "name: Test Ontology",
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
    (version1_dir / "query_templates.json").write_text(
        json.dumps({"query": "SELECT version_one"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (version2_dir / "query_templates.json").write_text(
        json.dumps({"query": "SELECT version_two"}, ensure_ascii=False),
        encoding="utf-8",
    )

    provider = _provider(
        workspace_root=str(tmp_path),
        ontology_id="test-ontology",
        ontology_version="1.0.0",
    )

    captured = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def json(self):
            return {"success": True, "data": [{"id": 1}], "row_count": 1}

    def _fake_post(url, headers=None, timeout=None, **request_kwargs):
        captured["url"] = url
        captured["json"] = request_kwargs.get("json")
        return _FakeResponse()

    internal(provider).session.post = _fake_post

    list(provider.search_stream(query="query", entity="TargetItem"))
    assert captured["url"] == "https://nl2sql.example.net/api/v1/sql"
    assert captured["json"]["sql"] == "SELECT version_one"
