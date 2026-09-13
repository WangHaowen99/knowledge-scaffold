"""Tests for ontology manifest loading and prompt section construction."""

from pathlib import Path

from config import AppConfig
from workspace.workspaces import create_workspace, get_workspace_paths


EXAMPLE_ONTOLOGY_ID = "example-domain-knowledge-agent"
EXAMPLE_ONTOLOGY_DESCRIPTION = "Example domain ontology for tests"
REPO_ROOT = Path(__file__).resolve().parents[2]
ONTO_RUNTIME_ROOT = REPO_ROOT / ".onto-runtime"


def _create_demo_workspace(tmp_path, monkeypatch, workspace_id: str = "demo"):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    create_workspace("Demo Workspace", workspace_id=workspace_id, set_as_default=True)
    return get_workspace_paths(workspace_id)


def _write_example_manifest(workspace_paths):
    ontology_dir = workspace_paths.ontologies_dir / EXAMPLE_ONTOLOGY_ID
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            f"id: {EXAMPLE_ONTOLOGY_ID}\n"
            f"name: {EXAMPLE_ONTOLOGY_ID}\n"
            f"description: {EXAMPLE_ONTOLOGY_DESCRIPTION}\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
            "enabled: true\n"
            "scope: []\n"
        ),
        encoding="utf-8",
    )
    return ontology_dir


def test_app_config_defaults_to_onto_runtime_root(monkeypatch):
    monkeypatch.delenv("PROJECT_ROOT", raising=False)

    config = AppConfig.from_env()

    assert config.project_root == str(ONTO_RUNTIME_ROOT)
    assert not hasattr(config, "ontologies_root")
    assert not hasattr(config, "skills_root")


def test_ontology_registry_loads_manifest_and_builds_section(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module
    from ontology.ontologies import build_ontologies_section, get_ontology_registry

    _write_example_manifest(_create_demo_workspace(tmp_path, monkeypatch, workspace_id="example"))
    monkeypatch.setattr(ontologies_module, "_global_registry", None)
    registry = get_ontology_registry()
    ontology = registry.get_ontology(EXAMPLE_ONTOLOGY_ID)

    assert ontology is not None
    assert ontology.id == EXAMPLE_ONTOLOGY_ID
    assert ontology.manifest_path.endswith("manifest.yaml")

    section = build_ontologies_section()
    assert section.startswith("<Available-Ontologies>")
    assert section.endswith("</Available-Ontologies>")
    assert EXAMPLE_ONTOLOGY_ID in section
    assert EXAMPLE_ONTOLOGY_DESCRIPTION in section


def test_ontology_registry_uses_project_root_env(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module
    from ontology.ontologies import build_ontologies_section, get_ontology_registry

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for tests\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(ontologies_module, "_global_registry", None)

    registry = get_ontology_registry()
    ontology = registry.get_ontology("demo-ontology")

    assert ontology is not None
    assert ontology.manifest_path == str(ontology_dir / "manifest.yaml")
    section = build_ontologies_section()
    assert section.startswith("<Available-Ontologies>")
    assert section.endswith("</Available-Ontologies>")
    assert "- demo-ontology: Demo ontology for tests" in section


def test_disabled_ontology_is_not_exposed_to_model_context(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module
    from ontology.ontologies import (
        build_ontologies_section,
        build_selected_ontology_preload_context,
        get_ontology_registry,
    )

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    enabled_dir = workspace_paths.ontologies_dir / "enabled-ontology"
    disabled_dir = workspace_paths.ontologies_dir / "disabled-ontology"
    enabled_dir.mkdir(parents=True)
    disabled_dir.mkdir(parents=True)
    (enabled_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: enabled-ontology\n"
            "name: Enabled Ontology\n"
            "description: Enabled ontology for tests\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
            "enabled: true\n"
        ),
        encoding="utf-8",
    )
    (disabled_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: disabled-ontology\n"
            "name: Disabled Ontology\n"
            "description: Disabled ontology for tests\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
            "enabled: false\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(ontologies_module, "_global_registry", None)

    registry = get_ontology_registry()
    assert registry.get_ontology("disabled-ontology").enabled is False
    assert registry.list_enabled_ontologies() == ["enabled-ontology"]

    section = build_ontologies_section()
    assert "enabled-ontology" in section
    assert "disabled-ontology" not in section

    selected_section = build_ontologies_section(selected_ontology_id="disabled-ontology")
    assert "disabled-ontology" not in selected_section
    assert build_selected_ontology_preload_context("disabled-ontology") is None


def test_get_sole_enabled_ontology_id_variants(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module
    from ontology.ontologies import get_sole_enabled_ontology_id

    def _write_ontology(ontologies_dir, ontology_id, *, enabled=True):
        ontology_dir = ontologies_dir / ontology_id
        ontology_dir.mkdir(parents=True, exist_ok=True)
        (ontology_dir / "manifest.yaml").write_text(
            (
                "kind: ontology-package\n"
                "schema_version: 0.1\n"
                "id: {id}\n"
                "name: {id}\n"
                "description: {id}\n"
                "version: 1.0.0\n"
                "language: zh-CN\n"
                "enabled: {enabled}\n"
            ).format(id=ontology_id, enabled="true" if enabled else "false"),
            encoding="utf-8",
        )

    # Sole enabled ontology (a disabled sibling does not count) → its id.
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    _write_ontology(workspace_paths.ontologies_dir, "only-enabled", enabled=True)
    _write_ontology(workspace_paths.ontologies_dir, "extra-disabled", enabled=False)
    monkeypatch.setattr(ontologies_module, "_global_registry", None)
    assert get_sole_enabled_ontology_id() == "only-enabled"

    # A second enabled ontology → None (no unambiguous choice).
    _write_ontology(workspace_paths.ontologies_dir, "second-enabled", enabled=True)
    monkeypatch.setattr(ontologies_module, "_global_registry", None)
    assert get_sole_enabled_ontology_id() is None


def test_get_active_ontology_version_variants(tmp_path, monkeypatch):
    """Revision-bound capabilities need a concrete version for a package the
    session never pinned; only enabled packages may supply one."""
    import ontology.ontologies as ontologies_module
    from ontology.ontologies import get_active_ontology_version

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    _write_example_manifest(workspace_paths)
    disabled_dir = workspace_paths.ontologies_dir / "disabled-ontology"
    disabled_dir.mkdir(parents=True)
    (disabled_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: disabled-ontology\n"
            "name: disabled-ontology\n"
            "description: disabled-ontology\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
            "enabled: false\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ontologies_module, "_global_registry", None)

    assert get_active_ontology_version(EXAMPLE_ONTOLOGY_ID) == "1.0.0"
    assert get_active_ontology_version("disabled-ontology") is None
    assert get_active_ontology_version("missing-ontology") is None
    assert get_active_ontology_version("") is None


def test_build_ontologies_section_keeps_empty_block_for_empty_workspace(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module
    from ontology.ontologies import build_ontologies_section, get_ontology_registry

    _create_demo_workspace(tmp_path, monkeypatch, workspace_id="empty")
    monkeypatch.setattr(ontologies_module, "_global_registry", None)

    registry = get_ontology_registry()
    section = build_ontologies_section()

    assert registry.ontologies == {}
    assert section == "<Available-Ontologies>\n</Available-Ontologies>"


def test_runtime_loader_rejects_legacy_manifest(tmp_path):
    from ontology.ontologies import OntologyLoader

    ontology_dir = tmp_path / "legacy"
    ontology_dir.mkdir()
    (ontology_dir / "manifest.yaml").write_text(
        "kind: ontology\nid: legacy\nname: Legacy\ndescription: Legacy package\n"
        "version: 1.0.0\nlanguage: zh-CN\n",
        encoding="utf-8",
    )
    (ontology_dir / "index.md").write_text("# Legacy\n", encoding="utf-8")

    assert OntologyLoader.load_ontology(ontology_dir) is None


def test_runtime_loader_rejects_installed_version_missing_from_root_manifest(
    tmp_path,
):
    from ontology.ontologies import OntologyLoader

    ontology_dir = tmp_path / "versioned"
    for version in ("1.0.0", "2.0.0"):
        version_dir = ontology_dir / "versions" / version
        version_dir.mkdir(parents=True)
        (version_dir / "manifest.yaml").write_text(
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: versioned\n"
            "name: Versioned\n"
            "description: Versioned package\n"
            f"version: {version}\n"
            "language: zh-CN\n",
            encoding="utf-8",
        )
    (ontology_dir / "manifest.yaml").write_text(
        "kind: ontology-package\n"
        "schema_version: 0.1\n"
        "id: versioned\n"
        "name: Versioned\n"
        "description: Versioned package\n"
        "version: 2.0.0\n"
        "active_version: 2.0.0\n"
        "versions: [2.0.0]\n"
        "language: zh-CN\n",
        encoding="utf-8",
    )

    assert OntologyLoader.load_ontology(ontology_dir, version="2.0.0") is not None
    assert OntologyLoader.load_ontology(ontology_dir, version="1.0.0") is None


def test_runtime_manifest_lookup_rejects_unsafe_or_mismatched_ontology_id(
    tmp_path,
    monkeypatch,
):
    from ontology.ontologies import get_runtime_ontology_manifest

    workspace_paths = _create_demo_workspace(
        tmp_path,
        monkeypatch,
        workspace_id="runtime-lookup",
    )

    def write_manifest(root, ontology_id):
        root.mkdir(parents=True)
        (root / "manifest.yaml").write_text(
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            f"id: {ontology_id}\n"
            f"name: {ontology_id}\n"
            "description: Runtime lookup fixture\n"
            "version: 1.0.0\n"
            "language: zh-CN\n",
            encoding="utf-8",
        )

    write_manifest(workspace_paths.ontologies_dir.parent / "outside", "outside")
    write_manifest(workspace_paths.ontologies_dir / "requested", "different")

    assert (
        get_runtime_ontology_manifest(
            "../outside",
            workspace_id="runtime-lookup",
        )
        is None
    )
    assert (
        get_runtime_ontology_manifest(
            "requested",
            workspace_id="runtime-lookup",
        )
        is None
    )


def test_build_ontologies_section_honors_selected_ontology(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module
    from ontology.ontologies import build_ontologies_section

    _write_example_manifest(_create_demo_workspace(tmp_path, monkeypatch, workspace_id="selected-example"))
    monkeypatch.setattr(ontologies_module, "_global_registry", None)

    section = build_ontologies_section(
        selected_ontology_id=EXAMPLE_ONTOLOGY_ID,
    )

    assert "The current user turn has an explicitly selected ontology" not in section
    assert EXAMPLE_ONTOLOGY_ID in section


def test_build_selected_ontology_preload_context_includes_root_docs(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module
    from ontology.ontologies import (
        SELECTED_ONTOLOGY_CONTEXT_END,
        SELECTED_ONTOLOGY_CONTEXT_START,
        build_selected_ontology_preload_context,
    )

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch, workspace_id="preload")
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    (ontology_dir / "operations").mkdir(parents=True)
    (ontology_dir / "objects").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for preload tests\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "operations" / "index.md").write_text("- demo-op", encoding="utf-8")
    (ontology_dir / "objects" / "index.md").write_text("- demo-object", encoding="utf-8")

    monkeypatch.setattr(ontologies_module, "_global_registry", None)

    context = build_selected_ontology_preload_context(
        selected_ontology_id="demo-ontology",
    )

    assert context is not None
    assert context.startswith(SELECTED_ONTOLOGY_CONTEXT_START)
    assert context.endswith(SELECTED_ONTOLOGY_CONTEXT_END)
    assert "Do not call `ontology_context(resource=\"root\")`" not in context
    assert "id: demo-ontology" in context
    assert "ontology: Demo Ontology - Demo ontology for preload tests" in context
    assert "operations_catalog:\n- demo-op" in context
    assert "objects_catalog:\n- demo-object" in context
    assert "script_catalog:\n- none" in context
    assert "```json" not in context
    assert '"operations_catalog"' not in context
    assert "- demo-op" in context
    assert "- demo-object" in context


def test_build_selected_ontology_preload_context_formats_catalogs_compactly(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module
    from ontology.ontologies import build_selected_ontology_preload_context

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch, workspace_id="compact-preload")
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    (ontology_dir / "operations").mkdir(parents=True)
    (ontology_dir / "objects").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for compact preload tests\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "operations" / "index.md").write_text(
        "| 名称 | 标签 | 业务描述 |\n"
        "| --- | --- | --- |\n"
        "| demo-op | 工具流程 | compact operation |\n",
        encoding="utf-8",
    )
    (ontology_dir / "objects" / "index.md").write_text(
        "| 中文名称 | 别名 | 业务定义 |\n"
        "| --- | --- | --- |\n"
        "| DemoObject | demo | compact object |\n",
        encoding="utf-8",
    )
    (ontology_dir / "scripts").mkdir(parents=True)
    (ontology_dir / "scripts" / "index.md").write_text("# 脚本目录\n", encoding="utf-8")
    (ontology_dir / "scripts" / "demo.md").write_text(
        "---\nresource: script\nkey: demo\ntitle: demo-op\nbound_operation: demo-op\nentrypoint: demo.py\n---\n\n"
        "| 参数 key | CLI 参数 | 类型 | 必填 | 说明 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| 编码 | --code | string | 是 | 编码 |\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(ontologies_module, "_global_registry", None)

    context = build_selected_ontology_preload_context(
        selected_ontology_id="demo-ontology",
    )

    assert context is not None
    assert (
        "operations_catalog:\n"
        "columns: 名称 | 标签 | 业务描述\n"
        "- demo-op | 工具流程 | compact operation"
    ) in context
    assert (
        "objects_catalog:\n"
        "columns: 中文名称 | 别名 | 业务定义\n"
        "- DemoObject | demo | compact object"
    ) in context
    assert (
        "script_catalog:\n"
        "parameter format: name=description required|optional type; type[] means pass an array value.\n"
        "- demo-op (parameters: 编码 required string)"
    ) in context
    assert '"名称": "demo-op"' not in context
    assert '"operation_key": "demo-op"' not in context


def test_root_catalog_derives_coordinator_metadata_without_mutating_package(tmp_path):
    from ontology.ontology_catalog import _annotate_coordinating_operations

    ontology_dir = tmp_path / "demo-ontology"
    operations_dir = ontology_dir / "operations"
    operations_dir.mkdir(parents=True)
    catalog = [
        {"operation key": "summary", "输出": "combined result"},
        {"operation key": "stage-a", "输出": "first result"},
        {"operation key": "stage-b", "输出": "second result"},
    ]
    coordinator_source = (
        "---\nresource: operation\nkey: summary\n---\n\n"
        "Run [[stage-a]] and then [[stage-b]].\n"
    )
    (operations_dir / "summary.md").write_text(coordinator_source, encoding="utf-8")
    (operations_dir / "stage-a.md").write_text(
        "---\nresource: operation\nkey: stage-a\n---\n",
        encoding="utf-8",
    )
    (operations_dir / "stage-b.md").write_text(
        "---\nresource: operation\nkey: stage-b\n---\n",
        encoding="utf-8",
    )

    annotated = _annotate_coordinating_operations(catalog, ontology_dir=ontology_dir)

    assert annotated[0]["execution_role"] == "coordinator; load before referenced stages"
    assert annotated[0]["referenced_operation_count"] == 2
    assert annotated[1] == catalog[1]
    assert annotated[2] == catalog[2]
    assert (operations_dir / "summary.md").read_text(encoding="utf-8") == coordinator_source


def test_root_catalog_does_not_infer_coordinator_from_plain_text_mentions(tmp_path):
    from ontology.ontology_catalog import _annotate_coordinating_operations

    ontology_dir = tmp_path / "demo-ontology"
    operations_dir = ontology_dir / "operations"
    operations_dir.mkdir(parents=True)
    catalog = [
        {"operation key": "summary", "输出": "combined result"},
        {"operation key": "stage-a", "输出": "first result"},
        {"operation key": "stage-b", "输出": "second result"},
    ]
    (operations_dir / "summary.md").write_text(
        "---\nresource: operation\nkey: summary\n---\n\n"
        "This leaf compares the words stage-a and stage-b, but invokes neither.\n",
        encoding="utf-8",
    )
    for key in ("stage-a", "stage-b"):
        (operations_dir / "{}.md".format(key)).write_text(
            "---\nresource: operation\nkey: {}\n---\n".format(key),
            encoding="utf-8",
        )

    annotated = _annotate_coordinating_operations(catalog, ontology_dir=ontology_dir)

    assert annotated == catalog


def test_script_catalog_keeps_unbound_coordinator_helper_private(tmp_path):
    from ontology.ontology_catalog import load_script_catalog, public_script_catalog

    ontology_dir = tmp_path / "demo-ontology"
    operations_dir = ontology_dir / "operations"
    scripts_dir = ontology_dir / "scripts"
    operations_dir.mkdir(parents=True)
    scripts_dir.mkdir(parents=True)
    (operations_dir / "index.md").write_text(
        "| operation key | output |\n"
        "| --- | --- |\n"
        "| summary | combined |\n"
        "| stage-a | first |\n"
        "| stage-b | second |\n",
        encoding="utf-8",
    )
    coordinator_source = (
        "---\nresource: operation\nkey: summary\n---\n\n"
        "# 输出\n\n"
        "| output key | description |\n"
        "| --- | --- |\n"
        "| combined_result | combined rows |\n"
        "| recommended_plan | final recommendation |\n\n"
        "# 流程\n\n"
        "Run [[stage-a]] and [[stage-b]]; monthly.py is an intermediate helper.\n"
    )
    (operations_dir / "summary.md").write_text(coordinator_source, encoding="utf-8")
    (operations_dir / "stage-a.md").write_text(
        "---\nresource: operation\nkey: stage-a\n---\n",
        encoding="utf-8",
    )
    (operations_dir / "stage-b.md").write_text(
        "---\nresource: operation\nkey: stage-b\n---\n",
        encoding="utf-8",
    )
    descriptor_source = (
        "---\nresource: script\nkey: summary-helper\n"
        "title: Summary helper\nentrypoint: summary.py\n---\n\n"
        "| 参数 key | CLI 参数 | 类型 | 必填 | 说明 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| records | --records | object | 是 | current records |\n"
    )
    (scripts_dir / "summary-helper.md").write_text(descriptor_source, encoding="utf-8")
    (scripts_dir / "summary.py").write_text(
        "print({'combined_result': [], 'recommended_plan': None})\n",
        encoding="utf-8",
    )
    (scripts_dir / "monthly-helper.md").write_text(
        "---\nresource: script\nkey: monthly-helper\n"
        "title: Monthly helper\nentrypoint: monthly.py\n---\n",
        encoding="utf-8",
    )
    (scripts_dir / "monthly.py").write_text(
        "print({'combined_result': []})\n",
        encoding="utf-8",
    )

    catalog = load_script_catalog(ontology_dir)
    original = next(entry for entry in catalog if entry["script_key"] == "summary-helper")
    public = public_script_catalog(catalog)

    assert original["_is_bound_operation"] is False
    assert original.get("_is_derived_coordinator_binding") is None
    assert all(entry.get("operation_key") != "summary" for entry in catalog)
    assert public == []
    assert (operations_dir / "summary.md").read_text(encoding="utf-8") == coordinator_source
    assert (scripts_dir / "summary-helper.md").read_text(encoding="utf-8") == descriptor_source


def test_registry_preload_uses_active_ontology_version(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module
    from ontology.ontologies import build_selected_ontology_preload_context, get_ontology_registry

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch, workspace_id="versioned-preload")
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    version1 = ontology_dir / "versions" / "1.0.0"
    version2 = ontology_dir / "versions" / "2.0.0"
    version1.mkdir(parents=True)
    version2.mkdir(parents=True)
    for version_dir in (version1, version2):
        (version_dir / "operations").mkdir()
        (version_dir / "objects").mkdir()
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for active version tests\n"
            "version: 2.0.0\n"
            "active_version: 2.0.0\n"
            "versions:\n"
            "  - 1.0.0\n"
            "  - 2.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (version1 / "operations" / "index.md").write_text("- old-op\n", encoding="utf-8")
    (version1 / "objects" / "index.md").write_text("- old-object\n", encoding="utf-8")
    (version2 / "operations" / "index.md").write_text("- active-op\n", encoding="utf-8")
    (version2 / "objects" / "index.md").write_text("- active-object\n", encoding="utf-8")

    monkeypatch.setattr(ontologies_module, "_global_registry", None)
    ontology = get_ontology_registry().get_ontology("demo-ontology")
    assert ontology.version == "2.0.0"
    assert Path(ontology.ontology_dir) == version2

    context = build_selected_ontology_preload_context("demo-ontology")
    assert "active-op" in context
    assert "active-object" in context
    assert "old-op" not in context


def test_v01_preload_records_include_active_version_indexes(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module
    from ontology.ontologies import (
        build_selected_ontology_preload_context,
        build_selected_ontology_preload_records,
        get_ontology_registry,
    )

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch, workspace_id="v01-versioned-preload")
    ontology_dir = workspace_paths.ontologies_dir / "demo-v01"
    version1 = ontology_dir / "versions" / "1.0.0"
    version2 = ontology_dir / "versions" / "2.0.0"
    for version_dir in (version1, version2):
        for directory in ("objects", "operations", "rules", "metrics", "scripts"):
            (version_dir / directory).mkdir(parents=True, exist_ok=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-v01\n"
            "name: Demo v0.1\n"
            "description: Versioned v0.1 ontology\n"
            "version: 2.0.0\n"
            "active_version: 2.0.0\n"
            "versions:\n"
            "  - 1.0.0\n"
            "  - 2.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    for version_dir, marker in ((version1, "old"), (version2, "active")):
        (version_dir / "index.md").write_text("# {} root\n".format(marker), encoding="utf-8")
        for directory in ("objects", "operations", "rules", "metrics", "scripts"):
            (version_dir / directory / "index.md").write_text(
                "| key | title |\n| --- | --- |\n| {}-{} | {} |\n".format(marker, directory, marker),
                encoding="utf-8",
            )

    monkeypatch.setattr(ontologies_module, "_global_registry", None)
    ontology = get_ontology_registry().get_ontology("demo-v01")
    assert ontology is not None
    assert ontology.kind == "ontology-package"
    assert ontology.active_version == "2.0.0"
    assert Path(ontology.ontology_dir) == version2

    context = build_selected_ontology_preload_context("demo-v01")
    assert "active root" in context
    assert "active-operations" in context
    assert "old root" not in context

    records = build_selected_ontology_preload_records("demo-v01", turn_id=3)
    record_paths = {record["path"] for record in records}
    assert any(
        path.endswith("ontologies/demo-v01/versions/2.0.0/manifest.yaml")
        for path in record_paths
    )
    for relative_path in (
        "index.md",
        "objects/index.md",
        "operations/index.md",
        "rules/index.md",
        "metrics/index.md",
        "scripts/index.md",
    ):
        assert any(path.endswith("ontologies/demo-v01/versions/2.0.0/{}".format(relative_path)) for path in record_paths)
    assert not any("/versions/1.0.0/" in path for path in record_paths)
    assert {record["ontology_version"] for record in records} == {"2.0.0"}
    assert {record["doc_type"] for record in records} >= {
        "manifest",
        "ontology_index",
        "objects_index",
        "operations_index",
        "rules_index",
        "metrics_index",
        "scripts_index",
    }
