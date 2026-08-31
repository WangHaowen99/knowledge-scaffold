from __future__ import annotations

import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from api.auth import user_deletion
from api.auth.user_deletion import UserDeletionPendingError, identity_reference_guard
from api.workspace_acl_service import (
    WorkspaceIdentityDirectoryUnavailableError,
    WorkspaceIdentityNotFoundError,
    ensure_user_personal_workspace,
    replace_workspace_members,
    update_workspace_authorized_with_validated_owner,
    validate_workspace_member,
)
from workspace.workspaces import (
    WorkspaceOwnershipConflictError,
    create_workspace,
    get_workspace,
    list_workspaces,
    remove_user_from_workspace_members,
)


class _DirectoryBackend:
    def __init__(self, usernames):
        self._lock = threading.Lock()
        self._usernames = set(usernames)
        self.list_calls = 0
        self.list_started = threading.Event()
        self.allow_list = threading.Event()
        self.block_list = False
        self.delete_called = threading.Event()

    def list_users(self):
        with self._lock:
            self.list_calls += 1
            usernames = sorted(self._usernames)
        self.list_started.set()
        if self.block_list and not self.allow_list.wait(2):
            raise TimeoutError("test did not release identity lookup")
        return [{"username": username, "role": "user"} for username in usernames]

    def get_user(self, username):
        with self._lock:
            if username not in self._usernames:
                return None
        return SimpleNamespace(username=username, role="user")

    def delete_user(self, username):
        self.delete_called.set()
        with self._lock:
            if username not in self._usernames:
                raise ValueError("用户不存在")
            self._usernames.remove(username)


@pytest.fixture()
def acl_env(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    create_workspace("Private", workspace_id="private", owner="admin")
    return SimpleNamespace(
        runtime_root=runtime_root,
        admin=SimpleNamespace(username="admin", role="admin"),
    )


def test_batch_member_validation_reads_identity_directory_once(acl_env):
    backend = _DirectoryBackend({"admin", "alice", "bob"})

    updated = replace_workspace_members(
        backend,
        "private",
        acl_env.admin,
        ["alice", "bob"],
    )

    assert updated["members"] == ["alice", "bob"]
    assert backend.list_calls == 1


@pytest.mark.parametrize(
    "entries",
    [
        [{"username": 123}],
        [{"username": " alice "}],
        [{"username": ""}],
        [{"username": "alice"}, {"username": "alice"}],
    ],
)
def test_identity_directory_rejects_noncanonical_usernames(acl_env, entries):
    backend = SimpleNamespace(list_users=lambda: entries)

    with pytest.raises(WorkspaceIdentityDirectoryUnavailableError):
        validate_workspace_member(backend, "alice")


def test_pending_user_deletion_cannot_be_reintroduced_as_member(acl_env):
    operation_id = "a" * 32
    reconciliation_path = (
        acl_env.runtime_root / "system" / "user-deletion-reconciliation.json"
    )
    reconciliation_path.parent.mkdir(parents=True, exist_ok=True)
    reconciliation_path.write_text(
        json.dumps({
            "version": 1,
            "records": [{
                "id": operation_id,
                "username": "alice",
                "status": "cleanup_pending",
                "identity_deleted": True,
                "created_at": "2026-08-04T00:00:00+00:00",
                "updated_at": "2026-08-04T00:00:00+00:00",
                "attempts": 1,
                "completed_steps": [],
                "errors": {"workspace_members": "pending"},
            }],
        }),
        encoding="utf-8",
    )
    backend = _DirectoryBackend({"admin", "alice"})

    with pytest.raises(UserDeletionPendingError) as exc_info:
        replace_workspace_members(
            backend,
            "private",
            acl_env.admin,
            ["alice"],
        )

    assert exc_info.value.reconciliation_id == operation_id
    assert backend.list_calls == 0
    assert get_workspace("private")["members"] == []


def test_member_write_and_user_delete_cannot_leave_orphan_reference(
    acl_env,
    monkeypatch,
):
    backend = _DirectoryBackend({"admin", "alice"})
    backend.block_list = True
    results = {}
    errors = []

    def cleanup_only_workspace_members(username, *, operation_id):
        remove_user_from_workspace_members(username)
        return ["workspace_members"], {}

    monkeypatch.setattr(
        user_deletion,
        "_cleanup_deleted_user_artifacts",
        cleanup_only_workspace_members,
    )

    def add_member():
        try:
            results["add"] = replace_workspace_members(
                backend,
                "private",
                acl_env.admin,
                ["alice"],
            )
        except Exception as exc:
            errors.append(exc)

    def delete_user():
        try:
            results["delete"] = user_deletion.delete_user_with_cleanup(
                backend,
                "alice",
            )
        except Exception as exc:
            errors.append(exc)

    add_thread = threading.Thread(target=add_member)
    delete_thread = threading.Thread(target=delete_user)
    add_thread.start()
    assert backend.list_started.wait(2)
    delete_thread.start()
    assert not backend.delete_called.wait(0.1)

    backend.allow_list.set()
    add_thread.join(2)
    delete_thread.join(2)

    assert not add_thread.is_alive()
    assert not delete_thread.is_alive()
    assert errors == []
    assert results["add"]["members"] == ["alice"]
    assert results["delete"].pending_reconciliation is False
    assert backend.get_user("alice") is None
    assert get_workspace("private")["members"] == []


def test_owner_write_and_user_delete_preserve_identity_invariant(
    acl_env,
):
    backend = _DirectoryBackend({"admin", "alice"})
    backend.block_list = True
    results = {}
    errors = []

    def transfer_owner():
        try:
            results["owner"] = update_workspace_authorized_with_validated_owner(
                backend,
                "private",
                acl_env.admin,
                owner="alice",
            )
        except Exception as exc:
            errors.append(exc)

    def delete_user():
        try:
            results["delete"] = user_deletion.delete_user_with_cleanup(
                backend,
                "alice",
            )
        except Exception as exc:
            errors.append(exc)

    owner_thread = threading.Thread(target=transfer_owner)
    delete_thread = threading.Thread(target=delete_user)
    owner_thread.start()
    assert backend.list_started.wait(2)
    delete_thread.start()
    assert not backend.delete_called.wait(0.1)

    backend.allow_list.set()
    owner_thread.join(2)
    delete_thread.join(2)

    assert not owner_thread.is_alive()
    assert not delete_thread.is_alive()
    assert results["owner"]["owner"] == "alice"
    assert len(errors) == 1
    assert isinstance(errors[0], WorkspaceOwnershipConflictError)
    assert backend.get_user("alice") is not None
    assert get_workspace("private")["owner"] == "alice"


def test_identity_reference_guard_uses_canonical_order_for_reverse_batches():
    def guarded(usernames):
        with identity_reference_guard(usernames) as normalized:
            return normalized

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(guarded, ["bob", "alice"])
        second = executor.submit(guarded, ["alice", "bob"])

        assert first.result(timeout=2) == ("alice", "bob")
        assert second.result(timeout=2) == ("alice", "bob")


def test_first_personal_workspace_is_owned_empty_and_not_global_default(acl_env):
    backend = _DirectoryBackend({"admin", "alice"})
    user = SimpleNamespace(username="alice", role="user")

    personal, workspaces = ensure_user_personal_workspace(backend, user)

    assert personal["id"] == "home-2bd806c97f0e00af1a1f"
    assert personal["name"] == "默认工作空间"
    assert personal["description"] == "用户个人默认工作空间"
    assert personal["owner"] == "alice"
    assert personal["members"] == []
    assert personal["is_default"] is False
    assert [item["id"] for item in workspaces].count(personal["id"]) == 1
    assert next(item for item in list_workspaces() if item["id"] == "private")[
        "is_default"
    ] is True


def test_personal_workspace_creation_and_user_delete_preserve_identity_invariant(
    acl_env,
    monkeypatch,
):
    backend = _DirectoryBackend({"admin", "alice"})
    user = SimpleNamespace(username="alice", role="user")
    identity_deleted = threading.Event()
    allow_delete_to_finish = threading.Event()
    ensure_guard_entered = threading.Event()
    original_delete = backend.delete_user
    original_guard = identity_reference_guard
    results = {}
    errors = []

    def blocking_delete(username):
        original_delete(username)
        identity_deleted.set()
        if not allow_delete_to_finish.wait(2):
            raise TimeoutError("test did not release deletion")

    monkeypatch.setattr(backend, "delete_user", blocking_delete)

    def observed_guard(usernames):
        ensure_guard_entered.set()
        return original_guard(usernames)

    monkeypatch.setattr(
        "api.workspace_acl_service.identity_reference_guard",
        observed_guard,
    )

    def delete_user():
        try:
            results["delete"] = user_deletion.delete_user_with_cleanup(
                backend,
                "alice",
            )
        except Exception as exc:
            errors.append(exc)

    def ensure_workspace():
        try:
            results["ensure"] = ensure_user_personal_workspace(backend, user)
        except Exception as exc:
            errors.append(exc)

    delete_thread = threading.Thread(target=delete_user)
    ensure_thread = threading.Thread(target=ensure_workspace)
    delete_thread.start()
    assert identity_deleted.wait(2)
    ensure_thread.start()
    assert ensure_guard_entered.wait(2)
    assert not backend.list_started.is_set()

    allow_delete_to_finish.set()
    delete_thread.join(2)
    ensure_thread.join(2)

    assert not delete_thread.is_alive()
    assert not ensure_thread.is_alive()
    assert results["delete"].pending_reconciliation is False
    assert len(errors) == 1
    assert isinstance(errors[0], WorkspaceIdentityNotFoundError)
    assert not any(item.get("owner") == "alice" for item in list_workspaces())


def test_reserved_id_collision_uses_a_new_personal_workspace_id(acl_env, monkeypatch):
    backend = _DirectoryBackend({"admin", "alice", "mallory"})
    user = SimpleNamespace(username="alice", role="user")
    reserved_id = "home-2bd806c97f0e00af1a1f"
    create_workspace(
        "Legacy collision",
        workspace_id=reserved_id,
        owner="mallory",
        allow_reserved_id=True,
    )
    original_token_hex = secrets.token_hex
    monkeypatch.setattr(
        "api.workspace_acl_service.secrets.token_hex",
        lambda size: "cafebabe" if size == 4 else original_token_hex(size),
    )

    personal, workspaces = ensure_user_personal_workspace(backend, user)

    assert personal["owner"] == "alice"
    assert personal["id"] == reserved_id + "-cafebabe"
    assert personal["id"] in {item["id"] for item in workspaces}


def test_existing_owned_workspace_uses_earliest_created_at_then_id(
    acl_env,
    monkeypatch,
):
    timestamps = iter(
        [
            "2026-01-02T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ]
    )
    monkeypatch.setattr("workspace.workspaces._utc_now", lambda: next(timestamps))
    create_workspace("Later", workspace_id="alice-later", owner="alice")
    create_workspace("Earliest B", workspace_id="alice-earliest-b", owner="alice")
    create_workspace("Earliest A", workspace_id="alice-earliest-a", owner="alice")
    backend = SimpleNamespace(
        list_users=lambda: (_ for _ in ()).throw(ConnectionError("offline")),
    )
    user = SimpleNamespace(username="alice", role="user")

    personal, workspaces = ensure_user_personal_workspace(backend, user)

    assert personal["id"] == "alice-earliest-a"
    assert personal["id"] in {item["id"] for item in workspaces}


def test_legacy_generated_personal_workspace_name_is_migrated(acl_env):
    create_workspace(
        "alice 的工作空间",
        description="用户个人默认工作空间",
        workspace_id="home-2bd806c97f0e00af1a1f",
        owner="alice",
        allow_reserved_id=True,
    )
    backend = SimpleNamespace(
        list_users=lambda: (_ for _ in ()).throw(ConnectionError("offline")),
    )
    user = SimpleNamespace(username="alice", role="user")

    personal, workspaces = ensure_user_personal_workspace(backend, user)

    assert personal["name"] == "默认工作空间"
    listed = next(item for item in workspaces if item["id"] == personal["id"])
    assert listed["name"] == "默认工作空间"


def test_first_personal_workspace_creation_fails_closed_without_identity_directory(
    acl_env,
):
    backend = SimpleNamespace(
        list_users=lambda: (_ for _ in ()).throw(ConnectionError("offline")),
    )
    user = SimpleNamespace(username="alice", role="user")

    with pytest.raises(WorkspaceIdentityDirectoryUnavailableError):
        ensure_user_personal_workspace(backend, user)

    assert not any(item.get("owner") == "alice" for item in list_workspaces())
