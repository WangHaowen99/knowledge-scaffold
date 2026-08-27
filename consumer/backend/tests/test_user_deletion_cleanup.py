from __future__ import annotations

from fastapi.testclient import TestClient

from api.testing import TestApplication

import json
import threading
from types import SimpleNamespace

import pytest

from integration.mcp.config import MCPRuntimeConfig
from integration.mcp.token_service import (
    MCPTokenAuthError,
    create_mcp_token,
    verify_mcp_token,
)
from api.auth import user_deletion
from auth_test_backend import AuthManager
from api.auth.auth import AuthUser
from api.routes.auth import auth_router
from utils.identifiers import safe_user_storage_key
from workspace.workspaces import (
    create_workspace,
    get_workspace,
    get_workspace_paths,
    update_workspace,
    user_can_access_workspace,
)


STRONG_PASSWORD = "Zq7#Wmx2Lp8!"


def _mcp_config(path):
    return MCPRuntimeConfig(
        server_host="127.0.0.1",
        server_port=7488,
        public_base_url="",
        agentic_base_url="",
        token_secret="test-secret-at-least-32-characters-long",
        token_algorithm="HS256",
        token_issuer="meta-wise",
        token_audience="meta-wise-mcp",
        token_ttl_seconds=3600,
        token_max_ttl_seconds=86400,
        http_timeout_seconds=30,
        transport="streamable-http",
        token_store_path=path,
    )


@pytest.fixture()
def deletion_env(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    memory_root = runtime_root / "memory"
    token_store_path = runtime_root / "mcp-tokens.json"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("MEMORY_WORKSPACE_ROOT", str(memory_root))
    monkeypatch.setenv("MCP_TOKEN_STORE_PATH", str(token_store_path))
    monkeypatch.setenv("MCP_TOKEN_SECRET", "test-mcp-secret")
    monkeypatch.setenv("MCP_TOKEN_ALGORITHM", "HS256")
    monkeypatch.setenv("MCP_TOKEN_ISSUER", "meta-wise")
    monkeypatch.setenv("MCP_TOKEN_AUDIENCE", "meta-wise-mcp")

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    auth_manager.change_password("admin", "TestBootstrap#2026", "RootStrong#2026")
    login = auth_manager.login("admin", "RootStrong#2026")
    assert login is not None

    app = TestApplication()
    app.state.services["TESTING"] = True
    app.state.services["AUTH_MANAGER"] = auth_manager
    app.state.services["APP_CONFIG"] = SimpleNamespace(auth_server_url="")
    app.include_router(auth_router)

    create_workspace(
        "Shared",
        workspace_id="shared",
        owner=None,
        set_as_default=True,
    )
    return SimpleNamespace(
        runtime_root=runtime_root,
        memory_root=memory_root,
        token_store_path=token_store_path,
        auth_manager=auth_manager,
        client=TestClient(app),
        headers={"Authorization": "Bearer " + login.access_token},
    )


def test_delete_workspace_owner_requires_transfer(deletion_env):
    deletion_env.auth_manager.create_user("alice", STRONG_PASSWORD)
    create_workspace("Alice", workspace_id="alice-space", owner="alice")

    response = deletion_env.client.delete(
        "/auth/users/alice",
        headers=deletion_env.headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "WORKSPACE_OWNER_TRANSFER_REQUIRED"
    assert response.json()["workspace_ids"] == ["alice-space"]
    assert deletion_env.auth_manager.get_user("alice") is not None


def test_delete_missing_user_does_not_reserve_username(deletion_env):
    response = deletion_env.client.delete(
        "/auth/users/missing-user",
        headers=deletion_env.headers,
    )

    assert response.status_code == 400
    assert response.json() == {"error": "用户 missing-user 不存在"}
    assert user_deletion.list_user_deletion_reconciliations() == []


def test_delete_member_archives_data_revokes_mcp_and_prevents_reuse_inheritance(
    deletion_env,
):
    deletion_env.auth_manager.create_user("alice", STRONG_PASSWORD)
    create_workspace("Private", workspace_id="private", owner="admin")
    update_workspace("private", members=["alice"])

    user_key = safe_user_storage_key("alice")
    runtime_user_dir = get_workspace_paths("private").users_runtime_dir / user_key
    (runtime_user_dir / "chats").mkdir(parents=True)
    (runtime_user_dir / "chats" / "old-session.json").write_text(
        "old session",
        encoding="utf-8",
    )
    memory_user_dir = deletion_env.memory_root / user_key
    memory_user_dir.mkdir(parents=True)
    (memory_user_dir / "MEMORY.md").write_text("old memory", encoding="utf-8")

    mcp_config = _mcp_config(deletion_env.token_store_path)
    mcp_token = create_mcp_token(
        username="alice",
        workspace_id="private",
        scopes=["mcp:search"],
        expires_in=3600,
        config=mcp_config,
    )["token"]

    response = deletion_env.client.delete(
        "/auth/users/alice",
        headers=deletion_env.headers,
    )

    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert deletion_env.auth_manager.get_user("alice") is None
    assert get_workspace("private")["members"] == []
    assert not runtime_user_dir.exists()
    assert not memory_user_dir.exists()
    with pytest.raises(MCPTokenAuthError):
        verify_mcp_token(mcp_token, config=mcp_config)

    manifests = list(
        (deletion_env.runtime_root / "system" / "deleted-user-data").glob(
            "*/manifest.json"
        )
    )
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text(encoding="utf-8"))["username"] == "alice"
    assert user_deletion.list_user_deletion_reconciliations() == []

    deletion_env.auth_manager.create_user("alice", STRONG_PASSWORD)
    workspace = get_workspace("private")
    assert "alice" not in workspace["members"]
    assert not user_can_access_workspace(
        AuthUser(username="alice", role="user"),
        workspace,
        False,
    )
    assert not runtime_user_dir.exists()
    assert not memory_user_dir.exists()


def test_cleanup_failure_returns_202_and_records_reconciliation(
    deletion_env,
    monkeypatch,
):
    deletion_env.auth_manager.create_user("alice", STRONG_PASSWORD)
    create_workspace("Private", workspace_id="private", owner="admin")
    update_workspace("private", members=["alice"])

    def fail_members(_username):
        raise OSError("simulated member cleanup failure")

    original_remove_members = user_deletion.remove_user_from_workspace_members
    monkeypatch.setattr(user_deletion, "remove_user_from_workspace_members", fail_members)
    response = deletion_env.client.delete(
        "/auth/users/alice",
        headers=deletion_env.headers,
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["success"] is True
    assert payload["reconciliation_required"] is True
    assert payload["reconciliation_id"]
    assert deletion_env.auth_manager.get_user("alice") is None

    records = user_deletion.list_user_deletion_reconciliations()
    assert len(records) == 1
    assert records[0]["id"] == payload["reconciliation_id"]
    assert records[0]["username"] == "alice"
    assert records[0]["status"] == "cleanup_pending"
    assert "workspace_members" in records[0]["errors"]

    blocked_admin_create = deletion_env.client.post(
        "/auth/users",
        headers=deletion_env.headers,
        json={"username": "alice", "password": STRONG_PASSWORD},
    )
    assert blocked_admin_create.status_code == 409
    assert blocked_admin_create.json()["code"] == "USER_DELETION_RECONCILIATION_PENDING"
    blocked_open_register = deletion_env.client.post(
        "/auth/register",
        json={"username": "alice", "password": STRONG_PASSWORD},
    )
    assert blocked_open_register.status_code == 409

    with pytest.raises(ValueError, match="用户名已被重新创建"):
        user_deletion.retry_user_deletion_reconciliation(
            payload["reconciliation_id"],
            identity_exists=lambda _username: True,
        )

    monkeypatch.setattr(
        user_deletion,
        "remove_user_from_workspace_members",
        original_remove_members,
    )
    retried = user_deletion.retry_user_deletion_reconciliation(
        payload["reconciliation_id"],
        identity_exists=lambda _username: False,
    )
    assert retried.pending_reconciliation is False
    assert user_deletion.list_user_deletion_reconciliations() == []
    assert get_workspace("private")["members"] == []


def test_deletion_intent_is_durable_before_identity_delete_and_blocks_reuse(
    deletion_env,
    monkeypatch,
):
    deletion_env.auth_manager.create_user("alice", STRONG_PASSWORD)
    observed_records = []

    def fail_delete(username):
        observed_records.extend(
            user_deletion.list_user_deletion_reconciliations()
        )
        raise OSError("simulated uncertain identity deletion")

    monkeypatch.setattr(deletion_env.auth_manager, "delete_user", fail_delete)

    response = deletion_env.client.delete(
        "/auth/users/alice",
        headers=deletion_env.headers,
    )

    assert response.status_code == 500
    payload = response.json()
    assert payload["code"] == "USER_DELETION_RECONCILIATION_FAILED"
    assert payload["reconciliation_required"] is True
    assert payload["reconciliation_id"]
    assert observed_records[0]["status"] == "deleting_identity"
    records = user_deletion.list_user_deletion_reconciliations()
    assert records[0]["status"] == "identity_delete_unconfirmed"
    assert records[0]["identity_deleted"] is False
    assert deletion_env.auth_manager.get_user("alice") is not None

    admin_create = deletion_env.client.post(
        "/auth/users",
        headers=deletion_env.headers,
        json={"username": "alice", "password": STRONG_PASSWORD},
    )
    assert admin_create.status_code == 409
    assert admin_create.json()["code"] == "USER_DELETION_RECONCILIATION_PENDING"

    open_register = deletion_env.client.post(
        "/auth/register",
        json={"username": " alice ", "password": STRONG_PASSWORD},
    )
    assert open_register.status_code == 409
    assert open_register.json()["reconciliation_id"] == payload["reconciliation_id"]


def test_creation_waits_until_delete_cleanup_finishes(deletion_env, monkeypatch):
    deletion_env.auth_manager.create_user("alice", STRONG_PASSWORD)
    create_workspace("Private", workspace_id="private", owner="admin")
    update_workspace("private", members=["alice"])

    identity_deleted = threading.Event()
    allow_cleanup = threading.Event()
    creation_started = threading.Event()
    creation_executed = threading.Event()
    original_delete = deletion_env.auth_manager.delete_user
    results = {}
    errors = []

    def blocking_delete(username):
        original_delete(username)
        identity_deleted.set()
        if not allow_cleanup.wait(2):
            raise TimeoutError("test did not release deletion")

    monkeypatch.setattr(deletion_env.auth_manager, "delete_user", blocking_delete)

    def run_delete():
        try:
            results["delete"] = user_deletion.delete_user_with_cleanup(
                deletion_env.auth_manager,
                "alice",
            )
        except Exception as exc:  # surfaced by the main test thread
            errors.append(exc)

    def run_create():
        creation_started.set()

        def create_identity():
            creation_executed.set()
            return deletion_env.auth_manager.create_user("alice", STRONG_PASSWORD)

        try:
            results["create"] = user_deletion.create_identity_if_reusable(
                "alice",
                create_identity,
            )
        except Exception as exc:  # surfaced by the main test thread
            errors.append(exc)

    delete_thread = threading.Thread(target=run_delete)
    create_thread = threading.Thread(target=run_create)
    delete_thread.start()
    assert identity_deleted.wait(2)
    create_thread.start()
    assert creation_started.wait(2)
    assert not creation_executed.wait(0.1)

    allow_cleanup.set()
    delete_thread.join(2)
    create_thread.join(2)

    assert not delete_thread.is_alive()
    assert not create_thread.is_alive()
    assert errors == []
    assert results["delete"].pending_reconciliation is False
    assert results["create"].username == "alice"
    assert get_workspace("private")["members"] == []
    assert user_deletion.list_user_deletion_reconciliations() == []


def test_retry_holds_reuse_gate_for_entire_cleanup(deletion_env, monkeypatch):
    deletion_env.auth_manager.create_user("alice", STRONG_PASSWORD)

    original_remove_members = user_deletion.remove_user_from_workspace_members

    def fail_members(_username):
        raise OSError("simulated member cleanup failure")

    monkeypatch.setattr(user_deletion, "remove_user_from_workspace_members", fail_members)
    response = deletion_env.client.delete(
        "/auth/users/alice",
        headers=deletion_env.headers,
    )
    record_id = response.json()["reconciliation_id"]
    monkeypatch.setattr(
        user_deletion,
        "remove_user_from_workspace_members",
        original_remove_members,
    )

    retry_checked_identity = threading.Event()
    allow_retry = threading.Event()
    creation_started = threading.Event()
    creation_executed = threading.Event()
    results = {}
    errors = []

    def identity_exists(_username):
        retry_checked_identity.set()
        if not allow_retry.wait(2):
            raise TimeoutError("test did not release retry")
        return False

    def run_retry():
        try:
            results["retry"] = user_deletion.retry_user_deletion_reconciliation(
                record_id,
                identity_exists=identity_exists,
            )
        except Exception as exc:  # surfaced by the main test thread
            errors.append(exc)

    def run_create():
        creation_started.set()

        def create_identity():
            creation_executed.set()
            return deletion_env.auth_manager.create_user("alice", STRONG_PASSWORD)

        try:
            results["create"] = user_deletion.create_identity_if_reusable(
                "alice",
                create_identity,
            )
        except Exception as exc:  # surfaced by the main test thread
            errors.append(exc)

    retry_thread = threading.Thread(target=run_retry)
    create_thread = threading.Thread(target=run_create)
    retry_thread.start()
    assert retry_checked_identity.wait(2)
    create_thread.start()
    assert creation_started.wait(2)
    assert not creation_executed.wait(0.1)

    allow_retry.set()
    retry_thread.join(2)
    create_thread.join(2)

    assert not retry_thread.is_alive()
    assert not create_thread.is_alive()
    assert errors == []
    assert results["retry"].pending_reconciliation is False
    assert results["create"].username == "alice"
    assert user_deletion.list_user_deletion_reconciliations() == []


def test_concurrent_retry_rechecks_record_after_waiting(deletion_env, monkeypatch):
    deletion_env.auth_manager.create_user("alice", STRONG_PASSWORD)
    original_remove_members = user_deletion.remove_user_from_workspace_members

    def fail_members(_username):
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(
        user_deletion,
        "remove_user_from_workspace_members",
        fail_members,
    )
    response = deletion_env.client.delete(
        "/auth/users/alice",
        headers=deletion_env.headers,
    )
    record_id = response.json()["reconciliation_id"]
    monkeypatch.setattr(
        user_deletion,
        "remove_user_from_workspace_members",
        original_remove_members,
    )

    first_retry_entered = threading.Event()
    allow_first_retry = threading.Event()
    second_retry_started = threading.Event()
    results = []
    errors = []

    def first_identity_exists(_username):
        first_retry_entered.set()
        if not allow_first_retry.wait(2):
            raise TimeoutError("test did not release first retry")
        return False

    def run_retry(identity_exists, started=None):
        if started is not None:
            started.set()
        try:
            results.append(
                user_deletion.retry_user_deletion_reconciliation(
                    record_id,
                    identity_exists=identity_exists,
                )
            )
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=run_retry, args=(first_identity_exists,))
    second = threading.Thread(
        target=run_retry,
        args=(lambda _username: False, second_retry_started),
    )
    first.start()
    assert first_retry_entered.wait(2)
    second.start()
    assert second_retry_started.wait(2)
    allow_first_retry.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(results) == 1
    assert results[0].pending_reconciliation is False
    assert len(errors) == 1
    assert isinstance(errors[0], KeyError)
    assert user_deletion.list_user_deletion_reconciliations() == []
