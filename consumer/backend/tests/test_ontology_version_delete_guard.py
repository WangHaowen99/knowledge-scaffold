from __future__ import annotations

from pathlib import Path

import yaml
from flask import Flask

from api.auth.auth import AuthManager
from api.routes.ontologies import ontologies_bp
from persistence.chat_store import ChatStore
from workspace.workspaces import create_workspace, get_workspace_paths


def _build_test_client(tmp_path):
    app = Flask(__name__)
    app.config["TESTING"] = True

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth_manager.authenticate("admin", "TestBootstrap#2026")
    assert user is not None
    if user.password_change_required:
        user = auth_manager.change_password("admin", "TestBootstrap#2026", "RootStrong123!")

    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(ontologies_bp)

    client = app.test_client()
    token = auth_manager.issue_token(user)
    return client, {"Authorization": "Bearer {}".format(token)}


def _create_demo_workspace(tmp_path, monkeypatch, workspace_id: str = "demo"):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    create_workspace("Demo Workspace", workspace_id=workspace_id, set_as_default=True)
    return get_workspace_paths(workspace_id)


def test_delete_referenced_ontology_version_returns_409(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    version1 = ontology_dir / "versions" / "1.0.0"
    version2 = ontology_dir / "versions" / "2.0.0"
    (version1 / "operations").mkdir(parents=True)
    (version2 / "operations").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology\n"
            "version: 2.0.0\n"
            "active_version: 2.0.0\n"
            "versions:\n"
            "  - 1.0.0\n"
            "  - 2.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (version1 / "operations" / "flow.md").write_text("v1\n", encoding="utf-8")
    (version2 / "operations" / "flow.md").write_text("v2\n", encoding="utf-8")

    chat_store = ChatStore(str(workspace_paths.users_runtime_dir / "alice" / "chats"))
    chat_store.create_session(
        session_id="sess-old-version",
        metadata={
            "selected_ontology_id": "demo-ontology",
            "selected_ontology_version": "1.0.0",
            "selected_ontology_name": "Demo Ontology",
        },
    )

    client, headers = _build_test_client(tmp_path)
    response = client.delete(
        "/agentic/ontologies/demo-ontology/versions/1.0.0",
        headers=headers,
    )

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["conflict_type"] == "session_reference"
    assert payload["ontology_id"] == "demo-ontology"
    assert payload["version"] == "1.0.0"
    assert payload["reference_count"] == 1
    assert payload["referenced_sessions"][0]["session_id"] == "sess-old-version"
    assert version1.exists()
    manifest = yaml.safe_load((ontology_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["versions"] == ["1.0.0", "2.0.0"]
