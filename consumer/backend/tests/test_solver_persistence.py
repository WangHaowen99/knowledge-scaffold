from __future__ import annotations

import json
import sqlite3
import threading

import pytest
from solver_coordination.candidate_service import create_candidate_set
from solver_coordination.candidate_store import CandidateSetNotFoundError, CandidateSetStore
from solver_coordination.run_ledger import SolverRunLedger
from solver_coordination.validation_store import ValidationRunStore, ValidationRunStoreError
from workspace.workspaces import create_workspace


def _validation_payload(run_id: str, marker: str) -> dict:
    candidate_digest = "sha256:solver-coordination.v1:" + marker * 64
    return {
        "validation_run_id": run_id,
        "source_run_id": "solve_{}".format(marker),
        "candidate_set_digest": "sha256:solver-coordination.v1:" + marker * 64,
        "candidate_set_ref": "candidate_set_" + marker * 64,
        "problem_digest": "sha256:solver-coordination.v1:" + marker * 64,
        "validator_revision": "validator-synthetic-v1",
        "result_count": 1,
        "checked_candidate_count": 1,
        "completed_count": 1,
        "error_count": 0,
        "batch_status": "validation_completed",
        "selection_status": "published",
        "feasible_candidate_ids": ["CANDIDATE_0"],
        "published_candidate_ids": ["CANDIDATE_0"],
        "all_candidate_results": [
            {
                "candidate_id": "CANDIDATE_0",
                "candidate_digest": candidate_digest,
                "status": "feasible",
                "knowledge_package_id": "package-synthetic",
                "knowledge_package_revision": "revision-synthetic",
                "fact_snapshot_digest": candidate_digest,
                "rules_revision": "rules-synthetic",
                "evidence_ref": "evidence-synthetic",
            }
        ],
    }


def test_run_ledger_uses_wal_and_survives_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Synthetic Workspace", workspace_id="ledger_demo", set_as_default=True)

    ledger = SolverRunLedger("ledger_demo")
    created = ledger.create_run(
        run_id="solve_synthetic_001",
        owner_id="synthetic_operator",
        run_type="solve",
        phase="candidate_ready",
        status="completed",
        problem_digest="sha256:v1:" + "1" * 64,
        candidate_set_digest="sha256:v1:" + "2" * 64,
    )
    assert created["run_id"] == "solve_synthetic_001"
    assert created["workspace_id"] == "ledger_demo"
    assert created["status"] == "completed"

    with sqlite3.connect(ledger.database_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"solve_runs", "solve_artifacts"}.issubset(tables)

    reopened = SolverRunLedger("ledger_demo")
    assert reopened.get_run("solve_synthetic_001", owner_id="synthetic_operator")["run_id"] == ("solve_synthetic_001")
    # A different owner must not learn that the run exists.
    assert reopened.get_run("solve_synthetic_001", owner_id="other_operator") is None

    updated = reopened.update_run(
        "solve_synthetic_001",
        owner_id="synthetic_operator",
        status="published",
        phase="complete",
    )
    assert updated["status"] == "published"
    assert updated["phase"] == "complete"


def test_run_ledger_artifact_is_owner_scoped_and_restartable(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Synthetic Workspace", workspace_id="artifact_demo", set_as_default=True)

    ledger = SolverRunLedger("artifact_demo")
    artifact = ledger.register_artifact(
        artifact_digest="sha256:v1:" + "a" * 64,
        artifact_type="candidate_set",
        artifact_ref="candidate_set_" + "a" * 64,
        artifact_path="candidate_sets/synthetic_operator/candidate_set_" + "a" * 64 + ".json",
        owner_id="synthetic_operator",
        source_run_id="solve_synthetic_002",
    )
    assert artifact["artifact_type"] == "candidate_set"
    assert ledger.get_artifact(artifact["artifact_digest"], owner_id="synthetic_operator")["artifact_ref"].startswith(
        "candidate_set_"
    )
    assert ledger.get_artifact(artifact["artifact_digest"], owner_id="other_operator") is None
    assert (
        SolverRunLedger("artifact_demo").get_artifact(artifact["artifact_digest"], owner_id="synthetic_operator")
        is not None
    )


def test_idempotency_key_isolated_by_owner_and_lock_registry_reclaims_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Synthetic Workspace", workspace_id="idem_demo", set_as_default=True)
    store = ValidationRunStore("idem_demo")
    key = "synthetic-idempotency-key"

    first = store.put(
        idempotency_key=key,
        owner_id="synthetic_operator",
        request_fingerprint="sha256:v1:" + "1" * 64,
        payload=_validation_payload("validation_run_synthetic_001", "1"),
    )
    # The same raw key is a separate namespace for another owner.
    second = store.put(
        idempotency_key=key,
        owner_id="other_operator",
        request_fingerprint="sha256:v1:" + "2" * 64,
        payload=_validation_payload("validation_run_synthetic_002", "2"),
    )
    assert first["validation_run_id"] != second["validation_run_id"]
    assert (
        store.get_idempotent(
            idempotency_key=key,
            owner_id="synthetic_operator",
            request_fingerprint="sha256:v1:" + "1" * 64,
        )["validation_run_id"]
        == "validation_run_synthetic_001"
    )
    assert (
        store.get_idempotent(
            idempotency_key=key,
            owner_id="other_operator",
            request_fingerprint="sha256:v1:" + "2" * 64,
        )["validation_run_id"]
        == "validation_run_synthetic_002"
    )
    # The durable result itself is owner-namespaced as well.
    first_path = store.owner_runs_root_for_test("synthetic_operator") / (
        "validation_run_synthetic_001.json"
    )
    second_path = store.owner_runs_root_for_test("other_operator") / (
        "validation_run_synthetic_002.json"
    )
    assert first_path.is_file()
    assert second_path.is_file()
    assert first_path.parent != second_path.parent

    # Exercise the registry concurrently and verify no stale per-key locks remain.
    errors: list[BaseException] = []

    def worker(owner: str) -> None:
        try:
            with store.idempotency_lock(workspace_id="idem_demo", owner_id=owner, idempotency_key="temporary-key"):
                pass
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=("owner_{}".format(i),)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert errors == []
    assert store.idempotency_lock_count == 0


def test_validation_replay_rejects_tampered_run_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Synthetic Workspace", workspace_id="replay_demo", set_as_default=True)
    store = ValidationRunStore("replay_demo")
    key = "replay-integrity-key"
    fingerprint = "sha256:v1:" + "3" * 64
    store.put(
        idempotency_key=key,
        owner_id="synthetic_operator",
        request_fingerprint=fingerprint,
        payload=_validation_payload("validation_run_replay_001", "4"),
    )
    run_path = store.run_path_for_test(
        "validation_run_replay_001",
        owner_id="synthetic_operator",
    )
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["selection_status"] = "not_publishable"
    run_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationRunStoreError, match="integrity|digest|schema"):
        store.get_idempotent(
            idempotency_key=key,
            owner_id="synthetic_operator",
            request_fingerprint=fingerprint,
        )


def test_validation_store_rejects_unversioned_problem_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Synthetic Workspace", workspace_id="problem_digest_demo", set_as_default=True)
    store = ValidationRunStore("problem_digest_demo")
    payload = _validation_payload("validation_run_problem_digest_001", "5")
    payload["problem_digest"] = "not-a-digest"

    with pytest.raises(ValidationRunStoreError, match="digest"):
        store.put(
            idempotency_key="problem-digest-key",
            owner_id="synthetic_operator",
            request_fingerprint="sha256:v1:" + "6" * 64,
            payload=payload,
        )


def test_candidate_artifact_reference_is_content_addressed_and_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Synthetic Workspace", workspace_id="candidate_demo", set_as_default=True)
    candidate_set = create_candidate_set(
        plan_payload={
            "items": [
                {
                    "item_order": 1,
                    "item_id": "item-synthetic-001",
                    "quantity": "1.0",
                    "duration": "1.0",
                }
            ],
            "option_domain": ["OPTION_0", "OPTION_1"],
            "max_transitions": 1,
        },
        knowledge_package_id="package-synthetic",
        knowledge_package_revision="revision-synthetic-1",
        source_run_id="solve_synthetic_003",
    )
    store = CandidateSetStore("candidate_demo")
    ref, payload = store.put(candidate_set, owner_id="synthetic_operator")
    assert ref == "candidate_set_" + candidate_set.candidate_set_digest.rsplit(":", 1)[-1]
    assert store.get(ref, owner_id="synthetic_operator").candidate_set_digest == candidate_set.candidate_set_digest
    with pytest.raises(CandidateSetNotFoundError):
        store.get(ref, owner_id="other_operator")
    owner_dir = (
        tmp_path
        / ".onto-runtime"
        / "workspaces"
        / "candidate_demo"
        / "runtime"
        / "solver_coordination"
        / "candidate_sets"
    )
    assert len(list(owner_dir.rglob(ref + ".json"))) == 1
    artifact = store.artifact_record_for_test(
        candidate_set.candidate_set_digest,
        owner_id="synthetic_operator",
    )
    assert artifact is not None
    assert artifact["artifact_ref"] == ref
