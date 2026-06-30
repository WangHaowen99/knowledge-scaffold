"""Tests for skill routes against the onto-runtime layout."""

from flask import Flask

from agentic.routes.skills import skills_bp
from agentic.services.auth import AuthManager
from agentic.workspaces import create_workspace, get_workspace_paths


def _build_test_client(tmp_path, username: str = "admin", password: str = "admin123"):
    app = Flask(__name__)
    app.config["TESTING"] = True

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth_manager.authenticate(username, password)
    assert user is not None
    if user.password_change_required:
        user = auth_manager.change_password(username, password, "RootStrong123!")

    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(skills_bp)

    client = app.test_client()
    token = auth_manager.issue_token(user)
    headers = {"Authorization": "Bearer {}".format(token)}
    return client, headers


def _create_demo_workspace(tmp_path, monkeypatch, workspace_id: str = "demo"):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    create_workspace("Demo Workspace", workspace_id=workspace_id, set_as_default=True)
    return get_workspace_paths(workspace_id)


def test_list_skills_uses_onto_runtime_root_from_env(tmp_path, monkeypatch):
    from agentic.utils import skills as skills_module

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    skill_dir = workspace_paths.skills_dir / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: demo-skill\n"
            "description: Demo reusable skill\n"
            "---\n\n"
            "# Demo Skill\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(skills_module, "_global_registry", None)

    client, headers = _build_test_client(tmp_path)
    response = client.get("/agentic/skills", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert payload["skills"] == [
        {
            "allowed_tools": None,
            "description": "Demo reusable skill",
            "file_path": str(skill_dir / "SKILL.md"),
            "name": "demo-skill",
        }
    ]


def test_get_skill_reads_from_onto_runtime_root_env(tmp_path, monkeypatch):
    from agentic.utils import skills as skills_module

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    skill_dir = workspace_paths.skills_dir / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: demo-skill\n"
            "description: Demo reusable skill\n"
            "---\n\n"
            "# Demo Skill\n"
            "Use this skill in tests.\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(skills_module, "_global_registry", None)

    client, headers = _build_test_client(tmp_path)
    response = client.get("/agentic/skills/demo-skill", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert payload["name"] == "demo-skill"
    assert payload["file_path"] == str(skill_dir / "SKILL.md")
    assert payload["content"] == "# Demo Skill\nUse this skill in tests."


def test_delete_skill_rejects_traversal_name_without_deleting_workspace(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch, workspace_id="delete-demo")
    skill_dir = workspace_paths.skills_dir / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: demo-skill\n"
            "description: Demo reusable skill\n"
            "---\n\n"
            "# Demo Skill\n"
        ),
        encoding="utf-8",
    )

    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")
    response = client.delete("/agentic/skills/%2E%2E", headers=headers)

    assert response.status_code == 400
    assert workspace_paths.workspace_dir.exists()
    assert skill_dir.exists()
