from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from shared_capabilities.business_data import ProviderResult


REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_SCRIPT = (
    REPO_ROOT
    / "examples"
    / "example-domain-knowledge"
    / "ontology"
    / "scripts"
    / "get_source_item_versions_by_target_item.py"
)
EXAMPLE_SCRIPT = (
    REPO_ROOT
    / "examples"
    / "refinery-planning-case"
    / "scripts"
    / "evaluate_example_plan.py"
)


def _load_script(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_example_script_uses_shared_sdk_provider_result():
    module = _load_script(EXAMPLE_SCRIPT, "example_provider_example")

    class FakeDataProviderClient:
        def query_sql(self, sql: str) -> ProviderResult:
            assert "FROM ontology.example_db.`SourceItemVersion`" in sql
            return ProviderResult(
                success=True,
                data={
                    "column_names": ["source_item_version_id", "source_item_code"],
                    "data": [["EV-1", "EP-1"]],
                },
            )

    rows = module._get_source_item_version_rows(FakeDataProviderClient(), "A001")

    assert rows == [{"source_item_version_id": "EV-1", "source_item_code": "EP-1"}]


def test_example_script_surfaces_shared_provider_failure():
    module = _load_script(EXAMPLE_SCRIPT, "example_provider_failure_example")

    class FakeDataProviderClient:
        def query_sql(self, _sql: str) -> ProviderResult:
            return ProviderResult(success=False, error="权限拒绝")

    try:
        module._query(FakeDataProviderClient(), "SELECT 1")
    except RuntimeError as exc:
        assert str(exc) == "权限拒绝"
    else:
        raise AssertionError("provider failure should raise RuntimeError")


def test_example_scripts_read_versioned_shared_provider_envelope(monkeypatch):
    module = _load_script(EXAMPLE_SCRIPT, "example_provider_example")
    monkeypatch.setenv(
        module.SCRIPT_CONNECTION_ENV,
        json.dumps(
            {
                "version": 1,
                "provider": "neo4j",
                "endpoint": "",
                "auth": {"type": "none"},
                "options": {
                    "uri": "bolt://neo4j.example:7687",
                    "username": "neo4j",
                    "password": "secret",
                    "database": "example",
                },
            }
        ),
    )

    assert module.load_script_business_data_connection() == {
        "provider": "neo4j",
        "config": {
            "uri": "bolt://neo4j.example:7687",
            "username": "neo4j",
            "password": "secret",
            "database": "example",
        },
    }
