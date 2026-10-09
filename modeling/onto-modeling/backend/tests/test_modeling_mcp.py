import pytest

from types import SimpleNamespace

from app.services import modeling_mcp


def test_sync_members_maps_onto_roles_and_forwards_bearer(monkeypatch):
    calls = []

    def fake_request(method, path, token, body=None, extra_headers=None):
        calls.append((method, path, token, body, extra_headers))
        return {"members": body["members"]}

    monkeypatch.setattr(modeling_mcp, "_request", fake_request)
    monkeypatch.setattr(modeling_mcp.config, "MODELING_URL", "https://modeling")
    request = SimpleNamespace(headers={"authorization": "Bearer admin-token"})
    result = modeling_mcp.sync_members(
        {
            "id": "onto-1",
            "modeling_project_id": "modeling-1",
            "members": {"alice": "editor", "bob": "viewer"},
        },
        request,
    )

    assert result["members"] == [
        {"username": "alice", "role": "modeler"},
        {"username": "bob", "role": "viewer"},
    ]
    assert calls == [
        (
            "PUT",
            "/api/projects/modeling-1/members/sync",
            "Bearer admin-token",
            {"members": result["members"]},
            None,
        )
    ]


def test_admin_member_sync_failure_rolls_back_local_members(monkeypatch):
    pytest.importorskip('fastapi')
    from fastapi.responses import JSONResponse
    from app.routers import projects
    previous = {
        "id": "onto-1",
        "owner": "owner",
        "members": {"old": "viewer"},
        "modeling_project_id": "modeling-1",
    }
    updated = {**previous, "members": {"new": "editor"}}
    rollbacks = []

    monkeypatch.setattr(projects, "ensure_owner", lambda project_id, request: None)
    monkeypatch.setattr(projects.workspaces, "get_project", lambda project_id: previous)
    monkeypatch.setattr(projects.workspaces, "set_members", lambda project_id, members: updated)
    monkeypatch.setattr(
        projects.workspaces,
        "_update_meta",
        lambda project_id, **fields: rollbacks.append((project_id, fields)),
    )

    def fail_sync(meta, request):
        raise modeling_mcp.ModelingMcpError("同步失败", 502)

    monkeypatch.setattr(modeling_mcp, "sync_members", fail_sync)
    request = SimpleNamespace(state=SimpleNamespace(user={"sub": "admin", "role": "admin"}))

    response = projects.put_members(
        "onto-1",
        projects.MembersBody(members={"new": "editor"}),
        request,
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 502
    assert rollbacks == [("onto-1", {"members": {"old": "viewer"}})]
