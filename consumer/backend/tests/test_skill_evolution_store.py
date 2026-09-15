# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Tests for the skill-evolution filesystem store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent.skill_evolution.models import TaskRecord
from agent.skill_evolution.store import SkillEvolutionStore
from internal_access import internal


def _make_record(task_id="t1", workspace_id="ws1", ontology_id="onto1", status="submitted"):
    return TaskRecord(
        task_id=task_id,
        workspace_id=workspace_id,
        ontology_id=ontology_id,
        session_id="sess1",
        triggered_by="alice",
        run_id="run-1",
        status=status,
        active_version="1.0.0",
        version_sha="abc",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def test_save_and_get_task_roundtrip(tmp_path):
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    record = _make_record()
    store.create_task(record)

    fetched = store.get_task("ws1", "t1")
    assert fetched is not None
    assert fetched.task_id == "t1"
    assert fetched.ontology_id == "onto1"
    assert fetched.status == "submitted"


def test_get_task_missing_returns_none(tmp_path):
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    assert store.get_task("ws1", "nope") is None


def test_list_tasks_filters(tmp_path):
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    # "approved" is terminal (not pending); "completed" is pending (待审批); "running" is pending
    store.create_task(_make_record("t1", status="approved", ontology_id="a"))
    store.create_task(_make_record("t2", status="running", ontology_id="a"))
    store.create_task(_make_record("t3", status="completed", ontology_id="b"))

    all_a = store.list_tasks("ws1", ontology_id="a")
    assert {t.task_id for t in all_a} == {"t1", "t2"}

    pending = store.list_tasks("ws1", pending_only=True)
    assert {t.task_id for t in pending} == {"t2", "t3"}

    # Non-admin only sees own tasks
    store.create_task(_make_record("t4", status="completed", ontology_id="a"))
    rec = store.get_task("ws1", "t4")
    rec.triggered_by = "bob"
    store.save_task(rec)
    visible = store.list_tasks("ws1", triggered_by="bob", is_admin=False)
    assert {t.task_id for t in visible} == {"t4"}


def test_has_pending(tmp_path):
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    store.create_task(_make_record("t1", status="approved", ontology_id="a"))
    assert store.has_pending("ws1", "a") is False
    store.create_task(_make_record("t2", status="running", ontology_id="a"))
    assert store.has_pending("ws1", "a") is True
    assert store.has_pending("ws1", "b") is False


def test_temp_and_baseline_lifecycle(tmp_path):
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    tmp_dir = store.ensure_temp_dir("ws1", "t1")
    (tmp_dir / "evolved.txt").write_text("x")
    assert tmp_dir.exists()

    baseline = store.save_baseline("ws1", "t1", b"PKzip")
    assert baseline.exists()
    assert baseline.read_bytes() == b"PKzip"

    store.clean_temp("ws1", "t1")
    assert not tmp_dir.exists()
    store.clean_baseline("ws1", "t1")
    assert not baseline.exists()


def test_finalize_artifacts_clears_temp_and_baseline_on_terminal(tmp_path):
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    store.ensure_temp_dir("ws1", "t1")
    store.save_baseline("ws1", "t1", b"PK")
    rec = _make_record(status="approved")
    store.finalize_artifacts(rec)
    assert not store.temp_path("ws1", "t1").exists()
    assert not store.baseline_path("ws1", "t1").exists()


def test_finalize_artifacts_keeps_temp_when_not_terminal(tmp_path):
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    store.ensure_temp_dir("ws1", "t1")
    rec = _make_record(status="running")
    store.finalize_artifacts(rec)
    assert store.temp_path("ws1", "t1").exists()


def test_corrupt_record_quarantined(tmp_path):
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    path = store.task_path("ws1", "t1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json", encoding="utf-8")

    assert store.get_task("ws1", "t1") is None
    # Original quarantined, corrupted dir holds a copy
    assert not path.exists()
    quarantined = internal(store).corrupted_dir("ws1") / "t1.json"
    assert quarantined.exists()


def test_history_roundtrip(tmp_path):
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    payload = {"task_id": "t1", "decision": "approve_new_version"}
    store.save_history("ws1", "t1", payload)
    assert store.get_history("ws1", "t1") == payload
    assert store.get_history("ws1", "missing") is None


def test_atomic_write_does_not_leave_tmp(tmp_path):
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    store.create_task(_make_record())
    tasks_dir = internal(store).tasks_dir("ws1")
    # No leftover .tmp files after save
    assert list(tasks_dir.glob("*.tmp")) == []


def test_delete_task_removes_record_and_history(tmp_path):
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    store.create_task(_make_record(status="approved"))
    store.save_history("ws1", "t1", {"decision": "approve_new_version"})
    assert store.get_task("ws1", "t1") is not None
    store.delete_task("ws1", "t1")
    assert store.get_task("ws1", "t1") is None
    assert not store.history_path("ws1", "t1").exists()


def test_iter_workspace_ids(tmp_path):
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    store.create_task(_make_record("t1", workspace_id="ws1"))
    store.create_task(_make_record("t2", workspace_id="ws2"))
    assert set(store.iter_workspace_ids()) == {"ws1", "ws2"}


def test_create_task_if_no_pending_blocks_on_collision(tmp_path):
    """The authoritative gate-4 check: a second pending task for the same."""
    # Details: The authoritative gate-4 check: a second pending task for the same ontology is refused atomically
    # (closes the has_pending→create_task race).
    from agent.skill_evolution.store import PendingTaskExists

    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    store.create_task(_make_record("t1", status="running", ontology_id="a"))
    with pytest.raises(PendingTaskExists):
        store.create_task_if_no_pending(_make_record("t2", status="submitted", ontology_id="a"))
    # The losing task was NOT written.
    assert store.get_task("ws1", "t2") is None


def test_create_task_if_no_pending_allows_when_terminal(tmp_path):
    """A terminal task for the same ontology must not block a new pending one."""
    from agent.skill_evolution.store import PendingTaskExists

    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    store.create_task(_make_record("t1", status="approved", ontology_id="a"))
    rec2 = _make_record("t2", status="submitted", ontology_id="a")
    store.create_task_if_no_pending(rec2)
    assert store.get_task("ws1", "t2") is not None


def test_no_change_not_pending(tmp_path):
    """no_change is a terminal status — must NOT count toward pending (gate 4."""
    # Details: no_change is a terminal status — must NOT count toward pending (gate 4 must not block a new evolution
    # just because a prior one produced no change).
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    store.create_task(_make_record("t1", status="no_change", ontology_id="a"))
    assert store.has_pending("ws1", "a") is False
    assert store.list_tasks("ws1", pending_only=True) == []


def test_list_tasks_quarantines_corrupt_record(tmp_path):
    """list_tasks hitting an unparseable record must quarantine it and continue,."""
    # Details: list_tasks hitting an unparseable record must quarantine it and continue, not raise — mirrors the
    # get_task quarantine path (worker scans rely on this).
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    store.create_task(_make_record("t1", status="approved"))
    path = store.task_path("ws1", "t1")
    path.write_text("{ not valid json", encoding="utf-8")

    tasks = store.list_tasks("ws1")
    assert tasks == []
    assert not path.exists()
    assert (internal(store).corrupted_dir("ws1") / "t1.json").exists()  # noqa: SLF001
