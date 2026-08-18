"""Workspace ACL checks for run, task, memory, and ACP entry points."""

import asyncio
from types import SimpleNamespace

from flask import Flask, g
import pytest
from werkzeug.exceptions import HTTPException

from api.auth.auth import AuthUser
from api.protocols.acp import server as acp_server
from api.protocols.acp.config import ACPConfig
from api.routes import a2a, memories, workflows
from agent.runtime.contracts import RunScope, RunSnapshot, RunStatus as AsyncRunStatus
from agent.runtime.store import utc_now_iso
from persistence.run_store import RunStore
from workspace.workspaces import (
    create_workspace,
    set_default_workspace,
    update_workspace,
)


@pytest.fixture()
def workspace_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace(
        "Shared",
        workspace_id="shared",
        owner=None,
        set_as_default=True,
    )
    create_workspace("Private", workspace_id="private", owner="alice")
    update_workspace("private", members=["bob"])
    return Flask(__name__)


def _assert_forbidden(exc_info) -> None:
    assert isinstance(exc_info.value, HTTPException)
    assert exc_info.value.response.status_code == 403


def _async_snapshot(run_id, username, workspace_id, session_id):
    from agent.runtime.contracts import RuntimeInvocationContext

    now = utc_now_iso()
    return RunSnapshot(
        run_id=run_id,
        session_id=session_id,
        turn_id=1,
        status=AsyncRunStatus.QUEUED,
        created_at=now,
        updated_at=now,
        last_seq=0,
        scope=RunScope(username, workspace_id, ("agent:read",)),
        context=RuntimeInvocationContext(
            run_id=run_id,
            session_id=session_id,
            turn_id=1,
            workspace_id=workspace_id,
            username=username,
            query="synthetic question",
        ),
    )


def test_workflow_run_requires_matching_request_workspace_and_current_membership(
    workspace_env,
    monkeypatch,
):
    snapshot = _async_snapshot("run-private", "bob", "private", "session-private")

    def lookup(run_id, username, workspace_id):
        return snapshot if (
            run_id == snapshot.run_id
            and username == snapshot.scope.username
            and workspace_id == snapshot.scope.workspace_id
        ) else None

    monkeypatch.setattr(workflows, "_async_snapshot", lookup)

    with workspace_env.test_request_context(headers={"X-Workspace-Id": "private"}):
        g.current_user = AuthUser(username="bob", role="user")
        assert workflows._async_snapshot("run-private", "bob", "private") is snapshot

    with workspace_env.test_request_context(headers={"X-Workspace-Id": "shared"}):
        g.current_user = AuthUser(username="bob", role="user")
        assert workflows._async_snapshot("run-private", "bob", "shared") is None

    update_workspace("private", members=[])
    with workspace_env.test_request_context(headers={"X-Workspace-Id": "private"}):
        g.current_user = AuthUser(username="bob", role="user")
        with pytest.raises(HTTPException) as exc_info:
            workflows._get_request_workspace_id()
        _assert_forbidden(exc_info)


def test_memory_routes_resolve_workspace_before_accessing_memory(workspace_env):
    update_workspace("private", members=[])

    with workspace_env.test_request_context(headers={"X-Workspace-Id": "private"}):
        g.current_user = AuthUser(username="bob", role="user")
        with pytest.raises(HTTPException) as exc_info:
            memories._enforce_request_workspace_access()
        _assert_forbidden(exc_info)


def test_model_profiles_does_not_swallow_workspace_forbidden(
    workspace_env,
    monkeypatch,
):
    update_workspace("private", members=[])
    monkeypatch.setattr(
        workflows,
        "get_public_llm_profiles",
        lambda **_kwargs: pytest.fail("ACL failure must not fall back to global profiles"),
    )

    with workspace_env.test_request_context(headers={"X-Workspace-Id": "private"}):
        g.current_user = AuthUser(username="bob", role="user")
        with pytest.raises(HTTPException) as exc_info:
            workflows.list_model_profiles.__wrapped__()
        _assert_forbidden(exc_info)


def test_model_profiles_does_not_fall_back_when_workspace_resolution_is_invalid(
    workspace_env,
    monkeypatch,
):
    def invalid_workspace():
        raise ValueError("invalid workspace header")

    monkeypatch.setattr(workflows, "_get_request_workspace_id", invalid_workspace)
    monkeypatch.setattr(
        workflows,
        "get_public_llm_profiles",
        lambda **_kwargs: pytest.fail("invalid workspace must not fall back to global profiles"),
    )

    with workspace_env.test_request_context():
        response, status_code = workflows.list_model_profiles.__wrapped__()

    assert status_code == 400
    assert response.get_json() == {"detail": "invalid workspace header"}


def test_a2a_task_access_is_fail_closed_and_rechecks_membership(workspace_env):
    snapshot = _async_snapshot("task-private", "bob", "private", "session-private")

    class Manager:
        async def get_run(self, task_id, scope):
            if task_id == snapshot.run_id and scope.owns(snapshot):
                return snapshot
            return None

    service = SimpleNamespace(
        async_manager=Manager(),
        async_call=asyncio.run,
        user_id="bob",
        workspace_id="private",
    )

    with workspace_env.test_request_context(headers={"X-Workspace-Id": "private"}):
        g.current_user = AuthUser(username="bob", role="user")
        assert a2a._task_access_matches(
            service,
            "task-private",
            session_id="session-private",
        ) is True
        assert a2a._task_access_matches(
            service,
            "task-private",
            session_id="other-session",
        ) is False
        assert a2a._task_access_matches(
            SimpleNamespace(user_id="bob", workspace_id="private"),
            "task-private",
        ) is False

    update_workspace("private", members=[])
    with workspace_env.test_request_context(headers={"X-Workspace-Id": "private"}):
        g.current_user = AuthUser(username="bob", role="user")
        with pytest.raises(HTTPException) as exc_info:
            a2a._get_request_workspace_id()
        _assert_forbidden(exc_info)


def test_acp_anonymous_access_is_limited_to_public_or_default_workspace(workspace_env):
    with workspace_env.test_request_context(headers={"X-Workspace-Id": "shared"}):
        assert acp_server._resolve_acp_request_workspace_id() == "shared"

    with workspace_env.test_request_context(headers={"X-Workspace-Id": "private"}):
        with pytest.raises(HTTPException) as exc_info:
            acp_server._resolve_acp_request_workspace_id()
        _assert_forbidden(exc_info)

    set_default_workspace("private")
    with workspace_env.test_request_context(headers={"X-Workspace-Id": "private"}):
        assert acp_server._resolve_acp_request_workspace_id() == "private"


def test_acp_agent_detail_applies_conditional_auth(workspace_env, monkeypatch):
    config = ACPConfig()
    monkeypatch.setattr(acp_server, "get_config", lambda: config)
    workspace_env.register_blueprint(acp_server.acp_bp)
    client = workspace_env.test_client()

    config.acp.authentication.type = "none"
    assert client.get(
        "/acp/agent",
        headers={"X-Workspace-Id": "private"},
    ).status_code == 403
    assert client.get(
        "/acp/agent",
        headers={"X-Workspace-Id": "shared"},
    ).status_code == 200

    config.acp.authentication.type = "jwt"
    workspace_env.config["AUTH_MANAGER"] = SimpleNamespace(
        verify_token=lambda _token: AuthUser(username="alice", role="user"),
    )
    assert client.get(
        "/acp/agent",
        headers={
            "Authorization": "Bearer test-token",
            "X-Workspace-Id": "private",
        },
    ).status_code == 200


def test_acp_persisted_run_rechecks_owner_workspace_and_request_scope(
    workspace_env,
    monkeypatch,
):
    run_store = RunStore()
    run_store.create_run(
        run_id="acp-private",
        user_request="hello",
        metadata={
            "source": "acp",
            "acp_user_id": "bob",
            "acp_workspace_id": "private",
            "acp_session_id": "session-private",
            "acp_status": "pending",
        },
    )
    monkeypatch.setattr(
        acp_server,
        "_orchestrator",
        SimpleNamespace(run_store=run_store),
    )
    with acp_server._ACP_REGISTRY_LOCK:
        acp_server._ACP_RUN_REGISTRY.clear()

    with workspace_env.test_request_context(headers={"X-Workspace-Id": "private"}):
        g.current_user = AuthUser(username="bob", role="user")
        assert acp_server._ensure_acp_run_owner("acp-private") is True

    with workspace_env.test_request_context(headers={"X-Workspace-Id": "shared"}):
        g.current_user = AuthUser(username="bob", role="user")
        assert acp_server._ensure_acp_run_owner("acp-private") is False

    update_workspace("private", members=[])
    registry_lookups = 0
    original_ensure_registry_entry = acp_server._ensure_registry_entry

    def counting_ensure_registry_entry(run_id):
        nonlocal registry_lookups
        registry_lookups += 1
        return original_ensure_registry_entry(run_id)

    monkeypatch.setattr(
        acp_server,
        "_ensure_registry_entry",
        counting_ensure_registry_entry,
    )
    with workspace_env.test_request_context(headers={"X-Workspace-Id": "private"}):
        g.current_user = AuthUser(username="bob", role="user")
        with pytest.raises(HTTPException) as exc_info:
            acp_server._ensure_acp_run_owner("acp-private")
        _assert_forbidden(exc_info)
    assert registry_lookups == 0

    with workspace_env.test_request_context(headers={"X-Workspace-Id": "shared"}):
        assert acp_server._ensure_acp_run_owner("acp-private") is False
    assert registry_lookups == 1


def test_acp_anonymous_cannot_resume_authenticated_run(workspace_env, monkeypatch):
    run_store = RunStore()
    for run_id, user_id in (
        ("acp-authenticated", "bob"),
        ("acp-anonymous", None),
    ):
        run_store.create_run(
            run_id=run_id,
            user_request="hello",
            metadata={
                "source": "acp",
                "acp_user_id": user_id,
                "acp_workspace_id": "shared",
                "acp_session_id": f"session-{run_id}",
                "acp_status": "pending",
            },
        )
    monkeypatch.setattr(
        acp_server,
        "_orchestrator",
        SimpleNamespace(run_store=run_store),
    )
    with acp_server._ACP_REGISTRY_LOCK:
        acp_server._ACP_RUN_REGISTRY.clear()

    with workspace_env.test_request_context(headers={"X-Workspace-Id": "shared"}):
        assert acp_server._ensure_acp_run_owner("acp-authenticated") is False
        assert acp_server._ensure_acp_run_owner("acp-anonymous") is True


def test_acp_run_list_is_scoped_by_user_and_request_workspace(workspace_env, monkeypatch):
    run_store = RunStore()
    for run_id, user_id, workspace_id in (
        ("bob-shared", "bob", "shared"),
        ("bob-private", "bob", "private"),
        ("alice-shared", "alice", "shared"),
        ("anonymous-shared", None, "shared"),
    ):
        run_store.create_run(
            run_id=run_id,
            user_request="hello",
            metadata={
                "source": "acp",
                "acp_user_id": user_id,
                "acp_workspace_id": workspace_id,
                "acp_session_id": f"session-{run_id}",
                "acp_status": "pending",
            },
        )
    monkeypatch.setattr(
        acp_server,
        "_orchestrator",
        SimpleNamespace(run_store=run_store),
    )
    with acp_server._ACP_REGISTRY_LOCK:
        acp_server._ACP_RUN_REGISTRY.clear()
    for run_id in ("bob-shared", "bob-private", "alice-shared", "anonymous-shared"):
        assert acp_server._ensure_registry_entry(run_id) is not None

    class Manager:
        async def list_runs(self, scope):
            return []

    monkeypatch.setattr(
        acp_server,
        "_get_async_runtime_bridge",
        lambda: (Manager(), asyncio.run),
    )

    with workspace_env.test_request_context(headers={"X-Workspace-Id": "shared"}):
        g.current_user = AuthUser(username="bob", role="user")
        response = acp_server.list_runs.__wrapped__()
        assert {item["run_id"] for item in response.get_json()["runs"]} == {"bob-shared"}

    with workspace_env.test_request_context(headers={"X-Workspace-Id": "shared"}):
        response = acp_server.list_runs.__wrapped__()
        assert {item["run_id"] for item in response.get_json()["runs"]} == {
            "anonymous-shared",
        }
