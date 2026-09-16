# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from types import SimpleNamespace

from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from agent.tools.business_data.service import BusinessDataSearchRequest
from agent.tools.business_data_search import handle_business_data_search


def test_request_positional_construction_keeps_runtime_context_slot():
    runtime_context = {"run_id": "run-legacy"}
    request = BusinessDataSearchRequest(
        "workspace-a",
        "ontology-a",
        "Part",
        "query",
        runtime_context,
    )

    assert request.workspace_id == "workspace-a"
    assert request.ontology_id == "ontology-a"
    assert request.entity == "Part"
    assert request.query == "query"
    assert request.provider_runtime_context == runtime_context
    assert request.ontology_version == ""


def test_handle_business_data_search_keeps_workspace_id_at_legacy_positional_slot(monkeypatch):
    captured = {"events": []}

    monkeypatch.setattr(
        "agent.tools.business_data_search.get_ontology_manifest",
        lambda ontology_id, *, version=None, workspace_id=None: (object(), None),
    )
    monkeypatch.setattr(
        "agent.tools.business_data_search.resolve_business_data_source",
        lambda *, workspace_id, ontology_id: ResolvedBusinessDataSource(
            ontology_id=ontology_id,
            mode="direct_provider",
            provider="nl2sql_http",
            config={},
        ),
    )

    def fake_query_business_data_stream(request, *, connection):
        captured["request"] = request
        captured["connection"] = connection
        yield {"type": "start", "content": "start"}
        yield {"type": "final", "content": "ok"}

    monkeypatch.setattr(
        "agent.tools.business_data_search.query_business_data_stream",
        fake_query_business_data_stream,
    )

    result = handle_business_data_search(
        "demo-ontology",
        "query",
        "Part",
        "demo-workspace",
        captured["events"].append,
        "tool-1",
        {"run_id": "run-1"},
    )

    assert result == "ok"
    assert captured["request"].workspace_id == "demo-workspace"
    assert captured["request"].provider_runtime_context == {"run_id": "run-1"}
    assert captured["request"].ontology_version == ""
