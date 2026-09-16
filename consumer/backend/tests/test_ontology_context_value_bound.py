# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Tests for value_bound field marking in ontology_context object loading."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent.tools.ontology_context import OntologyContextTool


def _write_ontology_package(tmp_path: Path, *, with_bindings: bool = True) -> Path:
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    (objects_dir / "index.md").write_text(
        "| object key | 中文名 |\n| --- | --- |\n| flight | 航班 |",
        encoding="utf-8",
    )
    (objects_dir / "flight.md").write_text(
        "---\n"
        "resource: object\n"
        "key: flight\n"
        "title: 航班\n"
        "---\n\n"
        "# 字段\n\n"
        "| 字段 key | 中文名 | 类型 |\n"
        "| --- | --- | --- |\n"
        "| FLT_NR | 航班号 | number |\n"
        "| LATEST_DEP_ARP_CD | 出发机场 | string |\n"
        "| AIRCRAFT_TYPE_CD | 机型 | string |\n",
        encoding="utf-8",
    )

    mappings_dir = tmp_path / "mappings"
    mappings_dir.mkdir(exist_ok=True)
    mapping_payload = {
        "schema_version": "0.1",
        "objects": {
            "flight": {
                "resource": "flight",
                "fields": {
                    "FLT_NR": {"source": "FLT_NR", "type": "number"},
                    "LATEST_DEP_ARP_CD": {
                        "source": "LATEST_DEP_ARP_CD",
                        "type": "string",
                        "value_binding_ref": "airport-code",
                    },
                    "AIRCRAFT_TYPE_CD": {
                        "source": "AIRCRAFT_TYPE_CD",
                        "type": "string",
                        "value_binding_ref": "aircraft-type",
                    },
                },
            }
        },
    }
    if with_bindings:
        mapping_payload["value_bindings"] = {
            "airport-code": {
                "resource": "机场中文代号",
                "value_field": "ARP_CD",
                "semantic_field": "机场名",
            },
            "aircraft-type": {
                "resource": "机型代码映射",
                "value_field": "AIRCRAFT_TYPE_CD",
                "semantic_field": "AIRCRAFT_TYPE_NAME_ZH",
            },
        }
    (mappings_dir / "object-data-mappings.json").write_text(
        json.dumps(mapping_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    (tmp_path / "manifest.yaml").write_text(
        "kind: ontology-package\n"
        "schema_version: '0.1'\n"
        "id: demo\n"
        "name: Demo\n"
        "description: Demo ontology\n"
        "version: '0.1'\n"
        "language: zh\n",
        encoding="utf-8",
    )
    return tmp_path


def _make_context(workspace_id="ws"):
    return SimpleNamespace(
        workspace_id=workspace_id,
        run_id="r1",
        session_id="s1",
        turn_id=1,
        caller="main",
        agent_iteration=0,
        model_profile_id=None,
        observer=None,
        project_root=None,
        read_documents=None,
        selected_ontology_id="demo",
        selected_ontology_version="",
    )


def test_object_context_appends_value_bound_fields(tmp_path, monkeypatch):
    ontology_dir = _write_ontology_package(tmp_path, with_bindings=True)
    monkeypatch.setattr(
        "agent.tools.ontology_context.get_ontology_manifest",
        lambda ontology_id, **kwargs: (
            SimpleNamespace(
                id="demo",
                ontology_dir=str(ontology_dir),
                manifest_path=str(ontology_dir / "manifest.yaml"),
                version="0.1",
                enabled=True,
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        "agent.tools.ontology_context.build_near_point_block",
        lambda **kwargs: ("", []),
    )

    tool = OntologyContextTool()
    outcome = tool.execute(
        args={"ontology_id": "demo", "resource": "object", "key": "flight"},
        context=_make_context(),
    )
    content = outcome.content
    runtime_context = content.split("runtime_field_context:", 1)[1]
    assert "- field_key: LATEST_DEP_ARP_CD; value_bound: true" in runtime_context
    assert "- field_key: AIRCRAFT_TYPE_CD; value_bound: true" in runtime_context
    assert "field_key: FLT_NR; value_bound: true" not in runtime_context


def test_object_context_no_value_bound_without_bindings(tmp_path, monkeypatch):
    ontology_dir = _write_ontology_package(tmp_path, with_bindings=False)
    monkeypatch.setattr(
        "agent.tools.ontology_context.get_ontology_manifest",
        lambda ontology_id, **kwargs: (
            SimpleNamespace(
                id="demo",
                ontology_dir=str(ontology_dir),
                manifest_path=str(ontology_dir / "manifest.yaml"),
                version="0.1",
                enabled=True,
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        "agent.tools.ontology_context.build_near_point_block",
        lambda **kwargs: ("", []),
    )

    tool = OntologyContextTool()
    outcome = tool.execute(
        args={"ontology_id": "demo", "resource": "object", "key": "flight"},
        context=_make_context(),
    )
    assert "runtime_field_context:" not in outcome.content


def test_root_context_appends_value_domain_hint_with_bindings(tmp_path, monkeypatch):
    ontology_dir = _write_ontology_package(tmp_path, with_bindings=True)
    monkeypatch.setattr(
        "agent.tools.ontology_context.get_ontology_manifest",
        lambda ontology_id, **kwargs: (
            SimpleNamespace(
                id="demo",
                name="Demo",
                description="Demo ontology",
                ontology_dir=str(ontology_dir),
                manifest_path=str(ontology_dir / "manifest.yaml"),
                version="0.1",
                enabled=True,
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        "agent.tools.ontology_context.build_near_point_block",
        lambda **kwargs: ("", []),
    )

    tool = OntologyContextTool()
    outcome = tool.execute(
        args={"ontology_id": "demo", "resource": "root"},
        context=_make_context(),
    )
    assert "value_domain_hint:" in outcome.content
    assert "value_bound: true" in outcome.content
    assert "lookup_values" in outcome.content
    assert "看似常识" in outcome.content
    # The hint must render near the top, before any catalog block, so it is
    # not diluted by the (long) catalog listings.
    assert outcome.content.index("value_domain_hint:") < outcome.content.index("operations_catalog:")


def test_root_context_no_value_domain_hint_without_bindings(tmp_path, monkeypatch):
    ontology_dir = _write_ontology_package(tmp_path, with_bindings=False)
    monkeypatch.setattr(
        "agent.tools.ontology_context.get_ontology_manifest",
        lambda ontology_id, **kwargs: (
            SimpleNamespace(
                id="demo",
                name="Demo",
                description="Demo ontology",
                ontology_dir=str(ontology_dir),
                manifest_path=str(ontology_dir / "manifest.yaml"),
                version="0.1",
                enabled=True,
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        "agent.tools.ontology_context.build_near_point_block",
        lambda **kwargs: ("", []),
    )

    tool = OntologyContextTool()
    outcome = tool.execute(
        args={"ontology_id": "demo", "resource": "root"},
        context=_make_context(),
    )
    assert "value_domain_hint:" not in outcome.content


def test_object_context_no_value_bound_without_mapping(tmp_path, monkeypatch):
    ontology_dir = tmp_path
    objects_dir = ontology_dir / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    (objects_dir / "index.md").write_text(
        "| object key | 中文名 |\n| --- | --- |\n| flight | 航班 |",
        encoding="utf-8",
    )
    (objects_dir / "flight.md").write_text(
        "---\nresource: object\nkey: flight\ntitle: 航班\n---\n\n# 字段\n\n| 字段 key | 中文名 |\n| --- | --- |\n|"
        " FLT_NR | 航班号 |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent.tools.ontology_context.get_ontology_manifest",
        lambda ontology_id, **kwargs: (
            SimpleNamespace(
                id="demo",
                ontology_dir=str(ontology_dir),
                manifest_path=str(ontology_dir / "manifest.yaml"),
                version="0.1",
                enabled=True,
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        "agent.tools.ontology_context.build_near_point_block",
        lambda **kwargs: ("", []),
    )

    tool = OntologyContextTool()
    outcome = tool.execute(
        args={"ontology_id": "demo", "resource": "object", "key": "flight"},
        context=_make_context(),
    )
    assert "runtime_field_context:" not in outcome.content
