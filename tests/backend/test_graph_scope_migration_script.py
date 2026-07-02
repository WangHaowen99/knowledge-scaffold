"""Tests for Neo4j graph scope migration helper."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


def _load_script_module():
    path = (
        Path(__file__).resolve().parents[3]
        / "examples"
        / "scripts"
        / "migrate_neo4j_graph_scope.py"
    )
    spec = importlib.util.spec_from_file_location("migrate_neo4j_graph_scope", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeRecord(dict):
    pass


class _FakeResult:
    def __init__(self, total):
        self.total = total

    def single(self):
        return _FakeRecord(total=self.total)


class _FakeSession:
    def __init__(self):
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, params):
        self.queries.append((query, params))
        if "SET" in query:
            return _FakeResult(2)
        return _FakeResult(3)


class _FakeDriver:
    def __init__(self):
        self.session_obj = _FakeSession()

    def session(self, database):
        self.database = database
        return self.session_obj


def test_migrate_scope_dry_run_counts_missing_scope_only():
    module = _load_script_module()
    driver = _FakeDriver()
    args = argparse.Namespace(
        database="neo4j",
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        workspace_property="d2o_workspace_id",
        ontology_property="d2o_ontology_id",
        legacy_scene_name="legacy-scene",
        legacy_workspace_name="",
        label="D2O_NODE",
        relationship_type="D2O_RELATION",
        apply=False,
        limit=0,
    )

    result = module.migrate_scope(driver, args)

    assert result["mode"] == "dry-run"
    assert result["missing_scope"] == {"nodes": 3, "relationships": 3}
    assert result["updated"] == {"nodes": 0, "relationships": 0}
    assert len(driver.session_obj.queries) == 2
    assert "scene_name = $legacy_scene_name" in driver.session_obj.queries[0][0]


def test_migrate_scope_apply_writes_workspace_and_ontology_scope():
    module = _load_script_module()
    driver = _FakeDriver()
    args = argparse.Namespace(
        database="neo4j",
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        workspace_property="d2o_workspace_id",
        ontology_property="d2o_ontology_id",
        legacy_scene_name="",
        legacy_workspace_name="legacy-workspace",
        label="D2O_NODE",
        relationship_type="D2O_RELATION",
        apply=True,
        limit=10,
    )

    result = module.migrate_scope(driver, args)

    assert result["mode"] == "apply"
    assert result["updated"] == {"nodes": 2, "relationships": 2}
    apply_query, apply_params = driver.session_obj.queries[2]
    assert "WITH n LIMIT 10 SET" in apply_query
    assert "n.`d2o_workspace_id` = $write_value_0" in apply_query
    assert "n.`d2o_ontology_id` = $write_value_1" in apply_query
    assert apply_params["legacy_workspace_name"] == "legacy-workspace"
    assert apply_params["write_value_0"] == "workspace-a"
    assert apply_params["write_value_1"] == "ontology-a"
