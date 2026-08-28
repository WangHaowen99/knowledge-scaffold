from types import SimpleNamespace

import pytest

from integration.mcp.ontology_binding import (
    MCPOntologyBindingError,
    bind_chat_arguments,
    resolve_effective_ontology_id,
    validate_runtime_ontology_availability,
    validate_token_ontology_binding,
)


def _ontology(*, enabled=True):
    return SimpleNamespace(
        id="knowledge_package_demo",
        enabled=enabled,
        active_version="1.0.0",
        versions=["1.0.0"],
    )


def _patch_ontology_runtime(monkeypatch, ontology):
    monkeypatch.setattr(
        "ontology.ontologies.get_ontology_registry",
        lambda **_kwargs: SimpleNamespace(get_ontology=lambda _ontology_id: ontology),
    )
    monkeypatch.setattr(
        "ontology.ontologies.get_runtime_ontology_manifest",
        lambda *_args, **_kwargs: ontology,
    )


def test_chat_token_can_bind_enabled_package_without_graph_data_source(monkeypatch):
    ontology = _ontology()
    _patch_ontology_runtime(monkeypatch, ontology)
    monkeypatch.setattr(
        "agent.tools.business_data.ontology_sources_store.build_ontology_business_data_summary",
        lambda *_args, **_kwargs: {"ready": False, "provider": ""},
    )

    binding = validate_token_ontology_binding(
        workspace_id="workspace_demo",
        ontology_id="knowledge_package_demo",
        scopes=["mcp:chat"],
    )

    assert binding.active_version == "1.0.0"
    assert binding.search_ready is False


def test_search_token_requires_ready_graph_data_source(monkeypatch):
    ontology = _ontology()
    _patch_ontology_runtime(monkeypatch, ontology)
    monkeypatch.setattr(
        "agent.tools.business_data.ontology_sources_store.build_ontology_business_data_summary",
        lambda *_args, **_kwargs: {"ready": True, "provider": "nl2sql_http"},
    )

    with pytest.raises(MCPOntologyBindingError) as exc_info:
        validate_token_ontology_binding(
            workspace_id="workspace_demo",
            ontology_id="knowledge_package_demo",
            scopes=["mcp:search"],
        )

    assert exc_info.value.code == "MCP_ONTOLOGY_SEARCH_NOT_READY"


def test_existing_package_with_unloadable_manifest_reports_invalid_active_version(monkeypatch, tmp_path):
    package_dir = tmp_path / "knowledge_package_demo"
    package_dir.mkdir()
    (package_dir / "manifest.yaml").write_text("synthetic: true\n", encoding="utf-8")
    monkeypatch.setattr(
        "ontology.ontologies.get_ontology_registry",
        lambda **_kwargs: SimpleNamespace(get_ontology=lambda _ontology_id: None),
    )
    monkeypatch.setattr(
        "workspace.workspaces.get_workspace_ontologies_root",
        lambda _workspace_id: tmp_path,
    )

    with pytest.raises(MCPOntologyBindingError) as exc_info:
        validate_token_ontology_binding(
            workspace_id="workspace_demo",
            ontology_id="knowledge_package_demo",
            scopes=["mcp:chat"],
        )

    assert exc_info.value.code == "MCP_ONTOLOGY_ACTIVE_VERSION_INVALID"


def test_runtime_rejects_disabled_bound_package(monkeypatch):
    ontology = _ontology(enabled=False)
    _patch_ontology_runtime(monkeypatch, ontology)

    with pytest.raises(MCPOntologyBindingError) as exc_info:
        validate_runtime_ontology_availability(
            workspace_id="workspace_demo",
            ontology_id="knowledge_package_demo",
        )

    assert exc_info.value.code == "MCP_ONTOLOGY_UNAVAILABLE"


def test_runtime_validates_requested_version_instead_of_current_active(monkeypatch):
    ontology = _ontology()
    ontology.active_version = "2.0.0"
    ontology.versions = ["1.0.0", "2.0.0"]
    loaded_versions = []
    monkeypatch.setattr(
        "ontology.ontologies.get_ontology_registry",
        lambda **_kwargs: SimpleNamespace(get_ontology=lambda _ontology_id: ontology),
    )
    monkeypatch.setattr(
        "ontology.ontologies.get_runtime_ontology_manifest",
        lambda _ontology_id, *, version, **_kwargs: (
            loaded_versions.append(version) or ontology
        ),
    )

    selected = validate_runtime_ontology_availability(
        workspace_id="workspace_demo",
        ontology_id="knowledge_package_demo",
        ontology_version="1.0.0",
    )

    assert selected == "1.0.0"
    assert loaded_versions == ["1.0.0"]


def test_effective_ontology_resolution_preserves_legacy_and_rejects_conflicts():
    assert resolve_effective_ontology_id(
        token_ontology_id="knowledge_package_demo",
        requested_ontology_id=None,
    ) == "knowledge_package_demo"
    assert resolve_effective_ontology_id(
        token_ontology_id="",
        requested_ontology_id="knowledge_package_demo",
    ) == "knowledge_package_demo"

    with pytest.raises(MCPOntologyBindingError) as conflict:
        resolve_effective_ontology_id(
            token_ontology_id="knowledge_package_demo",
            requested_ontology_id="knowledge_package_other",
        )
    with pytest.raises(MCPOntologyBindingError) as required:
        resolve_effective_ontology_id(
            token_ontology_id="",
            requested_ontology_id=None,
        )

    assert conflict.value.code == "MCP_ONTOLOGY_SCOPE_MISMATCH"
    assert required.value.code == "MCP_ONTOLOGY_REQUIRED"


def test_chat_arguments_are_copied_and_bound_without_mutating_input():
    original = {"mode": "synthetic"}

    bound = bind_chat_arguments(
        arguments=original,
        token_ontology_id="knowledge_package_demo",
    )

    assert original == {"mode": "synthetic"}
    assert bound == {
        "mode": "synthetic",
        "selected_ontology_id": "knowledge_package_demo",
    }
