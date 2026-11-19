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



def test_retired_core_allows_member_update_without_remote_sync(monkeypatch):
    pytest.importorskip('fastapi')
    from contextlib import nullcontext
    from app.routers import projects
    from app.services import hub_members, hub_packages

    previous = {"id": "onto-1", "owner": "owner", "members": {"old": "viewer"},
                "modeling_project_id": "retired-project"}
    updated = {**previous, "members": {"new": "editor"}}
    monkeypatch.setattr(modeling_mcp.config, "MODELING_CORE_RETIRED", True)
    monkeypatch.setattr(projects, "ensure_owner", lambda *_args: None)
    monkeypatch.setattr(projects.workspaces, "get_project", lambda *_args: previous)
    monkeypatch.setattr(projects.workspaces, "set_members", lambda *_args: updated)
    monkeypatch.setattr(modeling_mcp, "sync_members", lambda *_args: pytest.fail("retired Core called"))
    monkeypatch.setattr(hub_packages, "association", lambda *_args: None)
    monkeypatch.setattr(hub_members, "writable", lambda *_args: None)
    monkeypatch.setattr(hub_members, "sync", lambda *_args, **_kwargs: {"synced": True})
    monkeypatch.setattr(projects.knowledge_hub, "lock", lambda *_args: nullcontext())
    request = SimpleNamespace(state=SimpleNamespace(user={"sub": "owner", "role": "admin"}),
                              headers={}, query_params={})
    response = projects.put_members("onto-1", projects.MembersBody(members={"new": "editor"}), request)
    assert response["members"] == {"new": "editor"}


def test_retired_core_disables_legacy_endpoints_even_with_saved_urls(monkeypatch):
    from app.services import modeling_eval
    monkeypatch.setattr(modeling_mcp.config, "MODELING_URL", "https://core.example.com")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_ASSOCIATION_URL", "https://core.example.com")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_ASSOCIATION_TOKEN", "synthetic")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_CORE_RETIRED", True)
    assert modeling_mcp.configured() is False
    assert modeling_mcp.association_configured() is False
    assert modeling_eval.enabled() is False


def test_retired_core_preserves_cleanup_queue(monkeypatch, tmp_path):
    from app.services import modeling_cleanup
    monkeypatch.setattr(modeling_cleanup.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(modeling_cleanup.config, "MODELING_CORE_RETIRED", True)
    modeling_cleanup.enqueue({"id": "synthetic-project", "modeling_project_id": "retired-project"},
                             reason="synthetic", operation="project_delete")
    assert modeling_cleanup.retry_due() == 0
    assert modeling_cleanup.list_tasks()[0]["status"] == "pending"


def test_ensure_project_association_persists_before_sync_and_retries_sync(monkeypatch):
    request = SimpleNamespace(headers={})
    meta = {"id": "onto-1", "name": "Synthetic project", "members": {}}
    persisted = []
    monkeypatch.setattr(modeling_mcp, "configured", lambda: True)
    monkeypatch.setattr(
        modeling_mcp,
        "provision_project",
        lambda current, req: modeling_mcp.ProvisionResult(
            {**current, "modeling_project_id": "modeling-1"}, "modeling-1", True
        ),
    )

    def persist(project_id, **fields):
        persisted.append((project_id, fields))
        return {**meta, **fields}

    calls = []

    def sync(current, req):
        calls.append(current["modeling_project_id"])
        if len(calls) == 1:
            raise modeling_mcp.ModelingMcpError("暂时不可达", 502)
        return {"members": []}

    monkeypatch.setattr(modeling_mcp, "sync_members", sync)
    with pytest.raises(modeling_mcp.ModelingMcpError):
        modeling_mcp.ensure_project_association(meta, request, persist)

    assert persisted == [("onto-1", {"modeling_project_id": "modeling-1"})]
    assert modeling_mcp.ensure_project_association(meta, request, persist) == {
        "linked": True,
        "configured": True,
        "modeling_project_id": "modeling-1",
    }
    assert calls == ["modeling-1", "modeling-1"]


def test_mcp_lazy_association_is_limited_to_owner_and_admin(monkeypatch):
    pytest.importorskip("fastapi")
    from app.routers import projects

    calls = []
    monkeypatch.setattr(
        projects.modeling_mcp,
        "ensure_project_association",
        lambda meta, request, persist: calls.append(meta["id"]),
    )
    monkeypatch.setattr(
        projects.workspaces,
        "get_project",
        lambda project_id: {"id": project_id, "modeling_project_id": "modeling-1"},
    )
    meta = {"id": "onto-1", "owner": "owner"}
    reader = SimpleNamespace(state=SimpleNamespace(user={"sub": "reader", "role": "user"}))
    owner = SimpleNamespace(state=SimpleNamespace(user={"sub": "owner", "role": "user"}))

    assert projects.ensure_mcp_association(meta, reader) is meta
    projects.ensure_mcp_association(meta, owner)
    assert calls == ["onto-1"]


def test_import_association_uses_dedicated_service_header(monkeypatch):
    calls = []

    def fake_request(method, path, token, body=None, extra_headers=None, base_url=None):
        calls.append((method, path, token, body, extra_headers, base_url))
        return {"project": {"id": "modeling-1"}, "created": True}

    monkeypatch.setattr(modeling_mcp, "_request", fake_request)
    monkeypatch.setattr(modeling_mcp.config, "MODELING_URL", "")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_ASSOCIATION_URL", "https://modeling")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_ASSOCIATION_TOKEN", "service-token")
    persisted = []
    result = modeling_mcp.ensure_project_association(
        {"id": "onto-1", "name": "Synthetic project", "members": {}},
        SimpleNamespace(headers={"authorization": "Bearer user-token"}),
        lambda project_id, **fields: persisted.append((project_id, fields)) or {
            "id": project_id,
            "modeling_project_id": fields["modeling_project_id"],
            "members": {},
        },
    )

    assert result["linked"] is True
    assert persisted == [("onto-1", {"modeling_project_id": "modeling-1"})]
    assert calls[0][2] == ""
    assert calls[0][4] == {
        "X-Modeling-Service-Token": "service-token",
        "X-Onto-Project-Id": "onto:onto-1",
    }
    assert calls[0][5] == "https://modeling"


def test_dedicated_members_and_cleanup_use_same_service_and_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(modeling_mcp.config, "MODELING_URL", "")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_ASSOCIATION_URL", "https://service.example.com")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_ASSOCIATION_TOKEN", "synthetic-service-token")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_CLEANUP_TOKEN", "")

    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(modeling_mcp, "_request", capture)
    meta = {"id": "synthetic-project", "modeling_project_id": "synthetic-modeling",
            "owner": "operator_a", "members": {"operator_b": "viewer"}}
    request = SimpleNamespace(headers={})
    modeling_mcp.sync_members(meta, request)
    modeling_mcp.delete_project(meta, request, operation="project_delete")
    # Persisted retries have no user session or owner metadata.
    modeling_mcp.delete_project({"id": meta["id"], "modeling_project_id": meta["modeling_project_id"]},
                                cleanup=True, operation="project_delete")
    assert [args[0] for args, _ in calls] == ["PUT", "DELETE", "DELETE"]
    for args, kwargs in calls:
        assert args[2] == ""
        assert kwargs["base_url"] == "https://service.example.com"
        assert kwargs["extra_headers"]["X-Onto-Project-Id"] == "onto:synthetic-project"
    assert calls[0][0][3] == {"members": [{"username": "operator_b", "role": "viewer"}]}
    assert calls[2][0][3]["external_id"] == "onto:synthetic-project"
    assert calls[2][0][3]["cleanup_operation"] == "project_delete"
    assert "X-Onto-Username" not in calls[2][1]["extra_headers"]


def test_global_configuration_keeps_legacy_user_auth(monkeypatch):
    monkeypatch.setattr(modeling_mcp.config, "MODELING_URL", "https://legacy.example.com")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_ASSOCIATION_URL", "https://service.example.com")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_ASSOCIATION_TOKEN", "synthetic-service-token")
    calls = []
    monkeypatch.setattr(modeling_mcp, "_request", lambda *a, **k: calls.append((a, k)) or {})
    meta = {"id": "synthetic-project", "modeling_project_id": "synthetic-modeling"}
    request = SimpleNamespace(headers={"authorization": "Bearer synthetic-user-token"})
    modeling_mcp.sync_members(meta, request)
    modeling_mcp.status(meta, request)
    modeling_mcp.delete_project(meta, request)
    for args, kwargs in calls:
        assert args[2] == "Bearer synthetic-user-token"
        assert "base_url" not in kwargs


def test_cleanup_queue_can_retry_dedicated_association(monkeypatch, tmp_path):
    from app.services import modeling_cleanup
    monkeypatch.setattr(modeling_cleanup.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(modeling_mcp.config, "MODELING_URL", "")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_ASSOCIATION_URL", "https://service.example.com")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_ASSOCIATION_TOKEN", "synthetic-service-token")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_CLEANUP_TOKEN", "")
    calls = []
    monkeypatch.setattr(modeling_mcp, "_request", lambda *a, **k: calls.append((a, k)) or {})
    modeling_cleanup.enqueue({"id": "synthetic-project", "modeling_project_id": "synthetic-modeling"},
                             reason="Synthetic outage", operation="project_delete")
    assert modeling_cleanup.retry_due() == 1
    assert modeling_cleanup.list_tasks()[0]["status"] == "succeeded"
    assert calls[0][1]["extra_headers"]["X-Onto-Project-Id"] == "onto:synthetic-project"


@pytest.mark.parametrize("role", ["admin", "user"])
def test_management_delegates_only_authenticated_admin_role(monkeypatch, role):
    monkeypatch.setattr(modeling_mcp.config, "MODELING_URL", "")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_ASSOCIATION_URL", "https://service.example.com")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_ASSOCIATION_TOKEN", "synthetic-service-token")
    calls = []
    monkeypatch.setattr(modeling_mcp, "_request", lambda *a, **k: calls.append((a, k)) or {})
    request = SimpleNamespace(state=SimpleNamespace(user={"sub": "operator_a", "role": role}),
                              headers={"X-Onto-Role": "admin"})
    meta = {"id": "synthetic-project", "modeling_project_id": "synthetic-modeling"}
    modeling_mcp.status(meta, request)
    modeling_mcp.tokens(meta, request)
    modeling_mcp.issue_token(meta, request, {"name": "Synthetic token"})
    modeling_mcp.revoke_token(meta, request, "synthetic-token-id")
    for args, kwargs in calls:
        headers = kwargs["extra_headers"]
        assert headers["X-Onto-Username"] == "operator_a"
        assert headers["X-Onto-Project-Id"] == "onto:synthetic-project"
        assert headers.get("X-Onto-Role") == ("admin" if role == "admin" else None)


@pytest.mark.parametrize("recover", [False, True])
def test_global_admin_association_preserves_owner_and_project_scope(monkeypatch, recover):
    monkeypatch.setattr(modeling_mcp.config, "MODELING_URL", "https://modeling.example.com")
    calls = []
    meta = {"id": "synthetic-project", "name": "Synthetic project",
            "owner": "operator_a", "members": {}}
    request = SimpleNamespace(
        headers={"authorization": "Bearer synthetic-admin-token"},
        state=SimpleNamespace(user={"sub": "operator_admin", "role": "admin"}),
    )

    def capture(method, path, token, body=None, extra_headers=None):
        calls.append((method, path, token, body, extra_headers))
        if recover and len(calls) == 1:
            raise modeling_mcp.ModelingMcpError("Synthetic lost response", 502)
        return {"project": {"id": "synthetic-modeling"}, "created": True}

    monkeypatch.setattr(modeling_mcp, "_request", capture)
    persisted = []
    result = modeling_mcp.ensure_project_association(
        meta, request, lambda project_id, **fields:
        persisted.append(fields) or {**meta, **fields},
    )
    assert result["linked"] is True
    assert calls[0][3]["onto_owner"] == "operator_a"
    assert persisted == [{"modeling_project_id": "synthetic-modeling"}]
    modeling_mcp.status(meta, request)
    modeling_mcp.tokens(meta, request)
    modeling_mcp.issue_token(meta, request, {"name": "Synthetic token"})
    modeling_mcp.revoke_token(meta, request, "synthetic-token")
    for method, path, token, body, headers in calls:
        assert token == "Bearer synthetic-admin-token"
        if not path.endswith("/members/sync"):
            assert headers == {"X-Onto-Project-Id": "onto:synthetic-project"}
    if recover:
        assert calls[1][0] == "GET"
        assert "/by-external-id?" in calls[1][1]
