import json
import os
import sqlite3
from pathlib import Path

import pytest

from ontology.mutations import (
    MutationOutboxDispatcher,
    MutationStore,
    OntologyMutationConflictError,
    OntologyMutationError,
    OntologyMutationCoordinator,
    PreparedOntologyMutation,
)


def _atomic_file_mutation(target: Path, staged: Path, backup: Path):
    def commit():
        os.replace(target, backup)
        os.replace(staged, target)

    def rollback():
        if target.exists():
            target.unlink()
        if backup.exists():
            os.replace(backup, target)

    def finalize():
        backup.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)

    return commit, rollback, finalize


def _write_pointer(runtime_root: Path):
    path = runtime_root / "ontology-evidence" / "ontologies" / "onto-a" / "active.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "contract": "OntologyEvidenceActivePointer/v1",
                "workspace_id": "workspace-a",
                "ontology_id": "onto-a",
                "ontology_version": "1.0.0",
                "mutation_revision": 0,
                "active_generation": "a" * 64,
                "target_generation": "a" * 64,
                "status": "ready",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_coordinator_commits_revision_outbox_and_revokes_pointer(tmp_path):
    runtime_root = tmp_path / "runtime"
    pointer_path = _write_pointer(runtime_root)
    target = tmp_path / "objects" / "part.md"
    target.parent.mkdir()
    target.write_text("before", encoding="utf-8")
    staged = target.with_suffix(".staged")
    staged.write_text("after", encoding="utf-8")
    backup = target.with_suffix(".backup")
    commit, rollback, finalize = _atomic_file_mutation(target, staged, backup)

    coordinator = OntologyMutationCoordinator(runtime_root)
    result = coordinator.commit(
        PreparedOntologyMutation(
            workspace_id="workspace-a",
            ontology_id="onto-a",
            mutation_type="ONTOLOGY_FILE_CHANGED",
            before_version="1.0.0",
            after_version="1.0.0",
            before_enabled=True,
            after_enabled=True,
            commit=commit,
            rollback=rollback,
            finalize=finalize,
            digest_hints={
                "algorithm": "sha256",
                "digest_basis": "raw_file_bytes_v1",
                "scope_version": "1.0.0",
                "completeness": "partial",
                "base_mutation_revision": 0,
                "semantic_manifest_digest": None,
                "entries": {"objects/part.md": {"sha256": "a" * 64, "size_bytes": 5}},
            },
            trigger="api",
        )
    )

    assert target.read_text(encoding="utf-8") == "after"
    assert result.mutation_revision == 1
    state = coordinator.store.get_state(workspace_id="workspace-a", ontology_id="onto-a")
    assert state["mutation_revision"] == 1
    assert state["last_event_id"] == result.event_id
    outbox = coordinator.store.list_outbox()
    assert len(outbox) == 1
    assert outbox[0]["status"] == "pending"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert pointer["mutation_revision"] == 1
    assert pointer["status"] == "epoch_pending"
    assert pointer["active_generation"] is None

    delivered = []
    dispatcher = MutationOutboxDispatcher(coordinator.store)
    assert dispatcher.dispatch_once(delivered.append) == {
        "claimed": 1,
        "dispatched": 1,
        "retried": 0,
        "dead_letter": 0,
    }
    assert delivered[0]["event_id"] == result.event_id
    assert dispatcher.dispatch_once(delivered.append)["claimed"] == 0
    assert coordinator.store.list_outbox()[0]["status"] == "dispatched"


def test_invalid_event_rolls_back_filesystem_and_store_but_keeps_pointer_revoked(tmp_path):
    runtime_root = tmp_path / "runtime"
    pointer_path = _write_pointer(runtime_root)
    target = tmp_path / "part.md"
    target.write_text("before", encoding="utf-8")
    staged = tmp_path / "part.staged"
    staged.write_text("after", encoding="utf-8")
    backup = tmp_path / "part.backup"
    commit, rollback, finalize = _atomic_file_mutation(target, staged, backup)
    coordinator = OntologyMutationCoordinator(runtime_root)

    with pytest.raises(Exception):
        coordinator.commit(
            PreparedOntologyMutation(
                workspace_id="workspace-a",
                ontology_id="onto-a",
                mutation_type="NOT_A_MUTATION",
                before_version="1.0.0",
                after_version="1.0.0",
                before_enabled=True,
                after_enabled=True,
                commit=commit,
                rollback=rollback,
                finalize=finalize,
            )
        )

    assert target.read_text(encoding="utf-8") == "before"
    assert coordinator.store.get_state(workspace_id="workspace-a", ontology_id="onto-a") is None
    assert coordinator.store.list_outbox() == []
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert pointer["mutation_revision"] == 1
    assert pointer["status"] == "epoch_pending"


def test_dispatcher_retries_transient_handler_failure(tmp_path):
    runtime_root = tmp_path / "runtime"
    coordinator = OntologyMutationCoordinator(runtime_root)
    result = coordinator.commit(
        PreparedOntologyMutation(
            workspace_id="workspace-a",
            ontology_id="onto-a",
            mutation_type="ONTOLOGY_VISIBILITY_CHANGED",
            before_version="1.0.0",
            after_version="1.0.0",
            before_enabled=True,
            after_enabled=False,
            commit=lambda: None,
            rollback=lambda: None,
        )
    )
    dispatcher = MutationOutboxDispatcher(coordinator.store, max_attempts=2)

    def fail(_event):
        raise RuntimeError("transient")

    stats = dispatcher.dispatch_once(fail)
    assert stats["retried"] == 1
    row = coordinator.store.list_outbox()[0]
    assert row["event_id"] == result.event_id
    assert row["status"] == "pending"
    assert row["error_code"] == "DELIVERY_FAILED"


def test_filesystem_commit_does_not_hold_the_sqlite_write_lock(tmp_path):
    runtime_root = tmp_path / "runtime"
    coordinator = OntologyMutationCoordinator(runtime_root)
    target = tmp_path / "part.md"
    target.write_text("before", encoding="utf-8")

    def commit():
        # A different ontology can acquire and commit the workspace SQLite
        # writer while this mutation is in its filesystem phase.
        MutationStore(runtime_root / "ontology-mutations.sqlite3").ensure_state(
            workspace_id="workspace-a",
            ontology_id="onto-b",
            active_version="1.0.0",
            enabled=True,
        )
        target.write_text("after", encoding="utf-8")

    coordinator.commit(
        PreparedOntologyMutation(
            workspace_id="workspace-a",
            ontology_id="onto-a",
            mutation_type="ONTOLOGY_FILE_CHANGED",
            before_version="1.0.0",
            after_version="1.0.0",
            before_enabled=True,
            after_enabled=True,
            commit=commit,
            rollback=lambda: target.write_text("before", encoding="utf-8"),
        )
    )

    assert target.read_text(encoding="utf-8") == "after"
    other = coordinator.store.get_state(workspace_id="workspace-a", ontology_id="onto-b")
    assert other is not None and other["mutation_revision"] == 0


def test_stale_prepared_revision_is_rejected_and_staged_artifacts_are_cleaned(tmp_path):
    runtime_root = tmp_path / "runtime"
    coordinator = OntologyMutationCoordinator(runtime_root)
    target = tmp_path / "part.md"
    target.write_text("before", encoding="utf-8")
    first_staged = tmp_path / "part.first.staged"
    first_staged.write_text("first", encoding="utf-8")
    second_staged = tmp_path / "part.second.staged"
    second_staged.write_text("second", encoding="utf-8")
    first = _atomic_file_mutation(target, first_staged, tmp_path / "part.first.backup")
    second = _atomic_file_mutation(target, second_staged, tmp_path / "part.second.backup")

    coordinator.commit(
        PreparedOntologyMutation(
            workspace_id="workspace-a",
            ontology_id="onto-a",
            mutation_type="ONTOLOGY_FILE_CHANGED",
            before_version="1.0.0",
            after_version="1.0.0",
            before_enabled=True,
            after_enabled=True,
            commit=first[0],
            rollback=first[1],
            finalize=first[2],
            base_mutation_revision=0,
        )
    )

    with pytest.raises(OntologyMutationConflictError) as exc_info:
        coordinator.commit(
            PreparedOntologyMutation(
                workspace_id="workspace-a",
                ontology_id="onto-a",
                mutation_type="ONTOLOGY_FILE_CHANGED",
                before_version="1.0.0",
                after_version="1.0.0",
                before_enabled=True,
                after_enabled=True,
                commit=second[0],
                rollback=second[1],
                finalize=second[2],
                base_mutation_revision=0,
            )
        )

    assert exc_info.value.code == "MUTATION_REVISION_CONFLICT"
    assert target.read_text(encoding="utf-8") == "first"
    assert not second_staged.exists()
    state = coordinator.store.get_state(workspace_id="workspace-a", ontology_id="onto-a")
    assert state is not None and state["mutation_revision"] == 1


def test_precondition_conflict_does_not_revoke_pointer_or_commit_filesystem(tmp_path):
    runtime_root = tmp_path / "runtime"
    pointer_path = _write_pointer(runtime_root)
    target = tmp_path / "part.md"
    target.write_text("external", encoding="utf-8")
    staged = tmp_path / "part.staged"
    staged.write_text("prepared", encoding="utf-8")
    commit, rollback, finalize = _atomic_file_mutation(target, staged, tmp_path / "part.backup")

    def reject_changed_file():
        raise OntologyMutationConflictError(
            "MUTATION_CONTENT_CONFLICT",
            "ontology file changed after the mutation was prepared",
        )

    with pytest.raises(OntologyMutationConflictError) as exc_info:
        OntologyMutationCoordinator(runtime_root).commit(
            PreparedOntologyMutation(
                workspace_id="workspace-a",
                ontology_id="onto-a",
                mutation_type="ONTOLOGY_FILE_CHANGED",
                before_version="1.0.0",
                after_version="1.0.0",
                before_enabled=True,
                after_enabled=True,
                commit=commit,
                rollback=rollback,
                finalize=finalize,
                precondition=reject_changed_file,
                base_mutation_revision=0,
            )
        )

    assert exc_info.value.code == "MUTATION_CONTENT_CONFLICT"
    assert target.read_text(encoding="utf-8") == "external"
    assert not staged.exists()
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert pointer["status"] == "ready"
    assert pointer["mutation_revision"] == 0


def test_final_revision_cas_conflict_rolls_back_filesystem(tmp_path):
    runtime_root = tmp_path / "runtime"
    coordinator = OntologyMutationCoordinator(runtime_root)
    target = tmp_path / "part.md"
    target.write_text("before", encoding="utf-8")

    def commit():
        target.write_text("after", encoding="utf-8")
        store = MutationStore(runtime_root / "ontology-mutations.sqlite3")
        store.ensure_state(
            workspace_id="workspace-a",
            ontology_id="onto-a",
            active_version="1.0.0",
            enabled=True,
        )
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                """
                UPDATE mutation_state SET mutation_revision=1
                WHERE workspace_id='workspace-a' AND ontology_id='onto-a'
                """
            )

    with pytest.raises(OntologyMutationConflictError) as exc_info:
        coordinator.commit(
            PreparedOntologyMutation(
                workspace_id="workspace-a",
                ontology_id="onto-a",
                mutation_type="ONTOLOGY_FILE_CHANGED",
                before_version="1.0.0",
                after_version="1.0.0",
                before_enabled=True,
                after_enabled=True,
                commit=commit,
                rollback=lambda: target.write_text("before", encoding="utf-8"),
            )
        )

    assert exc_info.value.code == "MUTATION_REVISION_CONFLICT"
    assert target.read_text(encoding="utf-8") == "before"
    assert coordinator.store.list_outbox() == []


def test_failed_rollback_returns_uncertain_state_error(tmp_path):
    coordinator = OntologyMutationCoordinator(tmp_path / "runtime")

    def commit():
        raise OSError("filesystem commit failed")

    def rollback():
        raise OSError("rollback failed")

    with pytest.raises(OntologyMutationError) as exc_info:
        coordinator.commit(
            PreparedOntologyMutation(
                workspace_id="workspace-a",
                ontology_id="onto-a",
                mutation_type="ONTOLOGY_FILE_CHANGED",
                before_version="1.0.0",
                after_version="1.0.0",
                before_enabled=True,
                after_enabled=True,
                commit=commit,
                rollback=rollback,
            )
        )

    assert exc_info.value.code == "MUTATION_COMMIT_STATE_UNCERTAIN"
    assert coordinator.store.list_outbox() == []
