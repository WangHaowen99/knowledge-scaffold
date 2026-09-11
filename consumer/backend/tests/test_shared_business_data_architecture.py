from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
PROVIDER_ROOT = BACKEND_ROOT / "shared_capabilities" / "business_data"


def test_provider_core_has_no_control_plane_dependencies():
    forbidden_roots = {
        "agent",
        "api",
        "config",
        "integration",
        "ontology",
        "persistence",
        "workspace",
        "flask",
    }
    violations = []
    for path in PROVIDER_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [str(node.module or "")]
            for name in names:
                if name.split(".", 1)[0].lower() in forbidden_roots:
                    violations.append("{}:{}".format(path, name))
    assert violations == []


def test_agent_uses_adapters_while_retaining_target_graph_providers():
    agent_business_data = BACKEND_ROOT / "agent/tools/business_data"
    assert (agent_business_data / "adapters").is_dir()
    assert (agent_business_data / "provider_registry.py").is_file()
    assert (agent_business_data / "providers/graph").is_dir()


def test_legacy_sandbox_sdk_is_deleted():
    assert not any((REPO_ROOT / "sdk/sandbox-python-sdk").rglob("*.py"))


def test_shared_capability_copy_allowlist_contains_only_required_roots():
    build_script = (REPO_ROOT / "deploy/build_jiuwenbox.sh").read_text(encoding="utf-8")
    assert "backend/shared_capabilities/business_data" in build_script
    assert "backend/shared_capabilities/ontology_script_sdk" in build_script
    assert 'cp -R "${PROJECT_ROOT}/backend"' not in build_script
