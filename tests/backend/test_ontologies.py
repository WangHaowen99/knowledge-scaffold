"""Tests for ontology manifest loading and prompt section construction."""

from pathlib import Path

from agentic.config import AppConfig
from agentic.workspaces import create_workspace, get_default_workspace_id, get_workspace_paths


REPO_ROOT = Path(__file__).resolve().parents[3]
ONTO_RUNTIME_ROOT = REPO_ROOT / ".onto-runtime"


def _create_demo_workspace(tmp_path, monkeypatch, workspace_id: str = "demo"):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    create_workspace("Demo Workspace", workspace_id=workspace_id, set_as_default=True)
    return get_workspace_paths(workspace_id)


def test_app_config_defaults_to_onto_runtime_root(monkeypatch):
    monkeypatch.delenv("PROJECT_ROOT", raising=False)

    config = AppConfig.from_env()

    assert config.project_root == str(ONTO_RUNTIME_ROOT)
    assert not hasattr(config, "ontologies_root")
    assert not hasattr(config, "skills_root")


def test_ontology_registry_loads_manifest_and_builds_section(monkeypatch):
    from agentic.utils import ontologies as ontologies_module
    from agentic.utils.ontologies import build_ontologies_section, get_ontology_registry

    monkeypatch.setattr(ontologies_module, "_global_registry", None)
    registry = get_ontology_registry()
    ontology = registry.get_ontology("example-domain-operations-manual")

    assert ontology is not None
    assert ontology.id == "example-domain-operations-manual"
    assert ontology.manifest_path.endswith("manifest.yaml")

    section = build_ontologies_section()
    assert section.startswith("<Available-Ontologies>")
    assert section.endswith("</Available-Ontologies>")
    assert "example-domain-operations-manual" in section
    assert "面向示例企业内部流程执行的领域本体" in section


def test_ontology_registry_uses_project_root_env(tmp_path, monkeypatch):
    from agentic.utils import ontologies as ontologies_module
    from agentic.utils.ontologies import build_ontologies_section, get_ontology_registry

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology\n"
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
    from agentic.utils import ontologies as ontologies_module
    from agentic.utils.ontologies import (
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
            "kind: ontology\n"
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
            "kind: ontology\n"
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


def test_build_ontologies_section_keeps_empty_block_for_empty_workspace(tmp_path, monkeypatch):
    from agentic.utils import ontologies as ontologies_module
    from agentic.utils.ontologies import build_ontologies_section, get_ontology_registry

    _create_demo_workspace(tmp_path, monkeypatch, workspace_id="empty")
    monkeypatch.setattr(ontologies_module, "_global_registry", None)

    registry = get_ontology_registry()
    section = build_ontologies_section()

    assert registry.ontologies == {}
    assert section == "<Available-Ontologies>\n</Available-Ontologies>"


def test_example_ontology_manifest_contains_expected_metadata():
    import yaml

    default_workspace_id = get_default_workspace_id()
    assert default_workspace_id is not None
    manifest_path = (
        ONTO_RUNTIME_ROOT
        / "workspaces"
        / default_workspace_id
        / "ontologies"
        / "example-domain-operations-manual"
        / "manifest.yaml"
    )

    content = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    assert content["kind"] == "ontology"
    assert content["id"] == "example-domain-operations-manual"
    assert content["language"] == "zh-CN"
    assert "scope" in content


def test_build_ontologies_section_honors_selected_ontology(monkeypatch):
    from agentic.utils import ontologies as ontologies_module
    from agentic.utils.ontologies import build_ontologies_section

    monkeypatch.setattr(ontologies_module, "_global_registry", None)

    section = build_ontologies_section(
        selected_ontology_id="example-domain-operations-manual",
    )

    assert "The current user turn has an explicitly selected ontology" not in section
    assert "example-domain-operations-manual" in section


def test_build_selected_ontology_preload_context_includes_root_docs(tmp_path, monkeypatch):
    from agentic.utils import ontologies as ontologies_module
    from agentic.utils.ontologies import (
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
            "kind: ontology\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for preload tests\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "operations-list.md").write_text("- demo-op", encoding="utf-8")
    (ontology_dir / "objects-list.md").write_text("- demo-object", encoding="utf-8")

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
    from agentic.utils import ontologies as ontologies_module
    from agentic.utils.ontologies import build_selected_ontology_preload_context

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch, workspace_id="compact-preload")
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    (ontology_dir / "operations").mkdir(parents=True)
    (ontology_dir / "objects").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for compact preload tests\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "operations-list.md").write_text(
        "| 名称 | 标签 | 业务描述 |\n"
        "| --- | --- | --- |\n"
        "| demo-op | 工具流程 | compact operation |\n",
        encoding="utf-8",
    )
    (ontology_dir / "objects-list.md").write_text(
        "| 中文名称 | 别名 | 业务定义 |\n"
        "| --- | --- | --- |\n"
        "| DemoObject | demo | compact object |\n",
        encoding="utf-8",
    )
    (ontology_dir / "scripts-list.md").write_text(
        "- `demo-op` -> `scripts/demo.py`\n"
        "  `args`: `[\"--code\", \"<编码>\"]`\n",
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


def test_registry_preload_uses_active_ontology_version(tmp_path, monkeypatch):
    from agentic.utils import ontologies as ontologies_module
    from agentic.utils.ontologies import build_selected_ontology_preload_context, get_ontology_registry

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch, workspace_id="versioned-preload")
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    version1 = ontology_dir / "versions" / "1.0.0"
    version2 = ontology_dir / "versions" / "2.0.0"
    version1.mkdir(parents=True)
    version2.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology\n"
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
    (version1 / "operations-list.md").write_text("- old-op\n", encoding="utf-8")
    (version1 / "objects-list.md").write_text("- old-object\n", encoding="utf-8")
    (version2 / "operations-list.md").write_text("- active-op\n", encoding="utf-8")
    (version2 / "objects-list.md").write_text("- active-object\n", encoding="utf-8")

    monkeypatch.setattr(ontologies_module, "_global_registry", None)
    ontology = get_ontology_registry().get_ontology("demo-ontology")
    assert ontology.version == "2.0.0"
    assert Path(ontology.ontology_dir) == version2

    context = build_selected_ontology_preload_context("demo-ontology")
    assert "active-op" in context
    assert "active-object" in context
    assert "old-op" not in context
