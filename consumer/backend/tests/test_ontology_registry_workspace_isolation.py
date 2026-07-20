"""Regression tests for workspace-scoped ontology discovery."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from agent.direct_execution.matcher import find_best_script_operation
from agent.core.shared.ontology_context import get_selected_ontology_context
from agent.core.shared.prompting import build_main_system_prompt
from agent.sub_agents.base import SubAgent
from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from agent.tools.business_data_search import handle_business_data_search
from agent.tools.ontology_context import handle_ontology_context
from ontology.ontology_catalog import get_ontology_manifest
from ontology.ontologies import (
    build_ontologies_section,
    build_selected_ontology_preload_context,
    build_selected_ontology_preload_records,
)
from workspace.workspaces import create_workspace, get_workspace_paths


def _write_ontology(
    workspace_id: str,
    *,
    ontology_id: str = "shared-ontology",
    description: str,
    operation_key: str = "run_shared_plan",
    parameter_name: str = "workspace_input",
) -> None:
    ontology_dir = get_workspace_paths(workspace_id).ontologies_dir / ontology_id
    version_dir = ontology_dir / "versions" / "1.0.0"
    scripts_dir = version_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (ontology_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                "id: {}".format(ontology_id),
                "name: {}".format(description),
                "description: {}".format(description),
                "version: 1.0.0",
                "active_version: 1.0.0",
                "versions:",
                "  - 1.0.0",
                "language: zh-CN",
            ]
        ),
        encoding="utf-8",
    )
    (version_dir / "index.md").write_text("# {}\n".format(description), encoding="utf-8")
    (scripts_dir / "run.py").write_text("print('ok')\n", encoding="utf-8")
    (scripts_dir / "run.md").write_text(
        "\n".join(
            [
                "---",
                "resource: script",
                "key: run",
                "title: {}".format(operation_key),
                "entrypoint: run.py",
                "bound_operation: {}".format(operation_key),
                "---",
                "",
                "| name | flag | type | required | description |",
                "| --- | --- | --- | --- | --- |",
                "| {} | --{} | string | true | Workspace input |".format(
                    parameter_name,
                    parameter_name.replace("_", "-"),
                ),
            ]
        ),
        encoding="utf-8",
    )


def _create_isolated_workspaces(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Workspace A", workspace_id="workspace-a", set_as_default=True)
    create_workspace("Workspace B", workspace_id="workspace-b")


def test_same_ontology_id_resolves_from_explicit_workspace(monkeypatch, tmp_path):
    _create_isolated_workspaces(monkeypatch, tmp_path)
    _write_ontology("workspace-a", description="Ontology from A", parameter_name="alpha")
    _write_ontology("workspace-b", description="Ontology from B", parameter_name="beta")

    ontology_a, error_a = get_ontology_manifest(
        "shared-ontology", workspace_id="workspace-a"
    )
    ontology_b, error_b = get_ontology_manifest(
        "shared-ontology", workspace_id="workspace-b"
    )

    assert error_a is None
    assert error_b is None
    assert ontology_a is not None and ontology_a.description == "Ontology from A"
    assert ontology_b is not None and ontology_b.description == "Ontology from B"
    assert ontology_a.ontology_dir != ontology_b.ontology_dir


def test_explicit_workspace_resolution_is_safe_under_concurrent_catalog_reads(
    monkeypatch,
    tmp_path,
):
    _create_isolated_workspaces(monkeypatch, tmp_path)
    _write_ontology("workspace-a", description="Ontology from A", parameter_name="alpha")
    _write_ontology("workspace-b", description="Ontology from B", parameter_name="beta")

    def resolve(workspace_id):
        ontology, error = get_ontology_manifest(
            "shared-ontology", workspace_id=workspace_id
        )
        assert error is None
        assert ontology is not None
        entry, operation_key, confidence = find_best_script_operation(
            user_request="run_shared_plan",
            ontology_id="shared-ontology",
            workspace_id=workspace_id,
            confidence_threshold=0.0,
        )
        assert operation_key == "run_shared_plan"
        assert entry is not None and confidence > 0
        return ontology.description, entry["parameters"][0]["name"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(resolve, "workspace-a")
        future_b = executor.submit(resolve, "workspace-b")

    assert future_a.result() == ("Ontology from A", "alpha")
    assert future_b.result() == ("Ontology from B", "beta")


def test_ontology_context_uses_tool_workspace_scope(monkeypatch, tmp_path):
    _create_isolated_workspaces(monkeypatch, tmp_path)
    _write_ontology("workspace-a", description="Ontology from A")
    _write_ontology("workspace-b", description="Ontology from B")

    result = handle_ontology_context(
        ontology_id="shared-ontology",
        resource="root",
        workspace_id="workspace-b",
    )

    assert "Ontology from B" in result
    assert "Ontology from A" not in result


def test_preload_and_prompt_cache_are_scoped_by_workspace(monkeypatch, tmp_path):
    _create_isolated_workspaces(monkeypatch, tmp_path)
    _write_ontology("workspace-a", description="Ontology from A")
    _write_ontology("workspace-b", description="Ontology from B")

    preload = build_selected_ontology_preload_context(
        "shared-ontology",
        workspace_id="workspace-b",
    )
    section = build_ontologies_section(
        selected_ontology_id="shared-ontology",
        workspace_id="workspace-b",
    )
    records = build_selected_ontology_preload_records(
        "shared-ontology",
        workspace_id="workspace-b",
    )

    assert preload is not None and "Ontology from B" in preload
    assert "Ontology from A" not in preload
    assert "Ontology from B" in section
    assert records
    assert all(record["path"].startswith("ontologies/shared-ontology/") for record in records)

    state = {
        "workspace_id": "workspace-a",
        "user_context": {"selected_ontology_id": "shared-ontology"},
    }
    assert "Ontology from A" in (get_selected_ontology_context(state) or "")
    state["workspace_id"] = "workspace-b"
    assert "Ontology from B" in (get_selected_ontology_context(state) or "")

    prompt = build_main_system_prompt(state)
    assert "Ontology from B" in prompt
    assert "Ontology from A" not in prompt

    subagent_prompt = SubAgent()._build_system_prompt(workspace_id="workspace-b")
    assert "Ontology from B" in subagent_prompt
    assert "Ontology from A" not in subagent_prompt


def test_business_data_search_validates_manifest_in_explicit_workspace(
    monkeypatch,
    tmp_path,
):
    _create_isolated_workspaces(monkeypatch, tmp_path)
    _write_ontology(
        "workspace-b",
        ontology_id="workspace-b-only",
        description="Ontology only in B",
    )

    monkeypatch.setattr(
        "agent.tools.business_data_search.resolve_business_data_source",
        lambda *, workspace_id, ontology_id: ResolvedBusinessDataSource(
            ontology_id=ontology_id,
            provider="test",
            config={"workspace_id": workspace_id},
        ),
    )

    def fake_query_stream(request, *, connection):
        assert request.workspace_id == "workspace-b"
        assert connection.config["workspace_id"] == "workspace-b"
        yield {"type": "final", "content": "workspace-b-result"}

    monkeypatch.setattr(
        "agent.tools.business_data_search.query_business_data_stream",
        fake_query_stream,
    )

    result = handle_business_data_search(
        ontology_id="workspace-b-only",
        entity="DemoObject",
        query="query workspace B",
        workspace_id="workspace-b",
    )

    assert result == "workspace-b-result"
