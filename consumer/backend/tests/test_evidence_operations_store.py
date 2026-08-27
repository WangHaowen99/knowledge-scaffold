"""Evidence operation persistence and idempotency contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from persistence.evidence_operations import EvidenceOperationsStore


def _create_build_job(
    store: EvidenceOperationsStore,
    *,
    job_id: str,
    request_hash: str,
) -> dict:
    return store.create_or_get_build_job(
        job_id=job_id,
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_epoch="a" * 64,
        target_generation="b" * 64,
        mutation_revision=1,
        semantic="inherit",
        reason="idempotency ttl test",
        requested_by="tester",
        idempotency_key="rebuild-key",
        request_hash=request_hash,
    )


def _expire_key(
    store: EvidenceOperationsStore,
    *,
    operation: str,
    idempotency_key: str,
) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE idempotency_keys SET expires_at='2000-01-01T00:00:00+00:00'
            WHERE workspace_id='workspace-a' AND operation=? AND idempotency_key=?
            """,
            (operation, idempotency_key),
        )


def test_expired_rebuild_idempotency_key_can_be_reused_for_a_new_request(tmp_path: Path):
    store = EvidenceOperationsStore(tmp_path / "operations.sqlite3")
    first = _create_build_job(store, job_id="job-1", request_hash="hash-1")
    _expire_key(store, operation="rebuild", idempotency_key="rebuild-key")

    second = _create_build_job(store, job_id="job-2", request_hash="hash-2")

    assert first["duplicate"] is False
    assert second["duplicate"] is False
    assert second["job_id"] == "job-2"


def test_expired_cleanup_idempotency_key_can_be_reserved_again(tmp_path: Path):
    store = EvidenceOperationsStore(tmp_path / "operations.sqlite3")
    reserved, _ = store.reserve_result(
        workspace_id="workspace-a",
        operation="runtime_cleanup",
        idempotency_key="cleanup-key",
        request_hash="hash-1",
        result_ref="cleanup-1",
    )
    assert reserved is True
    _expire_key(store, operation="runtime_cleanup", idempotency_key="cleanup-key")

    reserved_again, previous = store.reserve_result(
        workspace_id="workspace-a",
        operation="runtime_cleanup",
        idempotency_key="cleanup-key",
        request_hash="hash-2",
        result_ref="cleanup-2",
    )

    assert reserved_again is True
    assert previous is None
