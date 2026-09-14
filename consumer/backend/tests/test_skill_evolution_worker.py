"""Tests for the skill-evolution worker: timeout + restart recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from agent.skill_evolution.models import TaskRecord
from agent.skill_evolution.service import SkillEvolutionService
from agent.skill_evolution.store import SkillEvolutionStore
from agent.skill_evolution.worker import SkillEvolutionWorker


def _enabled_config():
    return {"enabled": True, "base_url": "https://service.example.net", "timeout_seconds": 30}


def _make_record(store, task_id="t1", status="running", age_minutes=0):
    created = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    rec = TaskRecord(
        task_id=task_id,
        workspace_id="ws1",
        ontology_id="onto1",
        session_id="sess1",
        triggered_by="alice",
        run_id="run-1",
        status=status,
        active_version="1.0.0",
        version_sha="abc",
        created_at=created.replace(microsecond=0).isoformat(),
        updated_at=created.replace(microsecond=0).isoformat(),
    )
    store.create_task(rec)
    return rec


def test_worker_times_out_long_running_task(tmp_path):
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    service = SkillEvolutionService(store)
    service.set_config_provider_for_test(_enabled_config)
    worker = SkillEvolutionWorker(store, service, poll_interval_seconds=10, task_timeout_seconds=60)
    # Task created 2 hours ago → exceeds 60s timeout
    rec = _make_record(store, age_minutes=120, status="running")

    worker.tick()

    refreshed = store.get_task("ws1", "t1")
    assert refreshed.status == "failed"
    assert refreshed.failed_stage == "timeout"


def test_worker_does_not_time_out_fresh_task(tmp_path):
    """A fresh in-flight task (age < timeout) is polled, not failed. poll_once."""
    # Details: A fresh in-flight task (age < timeout) is polled, not failed. poll_once flips submitted→running and the
    # fake client returns 'running', so the task stays in-flight. The running path only calls get_run_status (never
    # _resolve_ontology), so no ontology fixture is needed.

    class _NoopClient:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def get_run_status(self, run_id):
            return {"status": "running"}

    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    service = SkillEvolutionService(store, client=_NoopClient())
    service.set_config_provider_for_test(_enabled_config)
    worker = SkillEvolutionWorker(store, service, poll_interval_seconds=10, task_timeout_seconds=1800)
    _make_record(store, age_minutes=0, status="submitted")

    worker.tick()
    assert store.get_task("ws1", "t1").status == "running"


def test_mark_inflight_interrupted_on_restart(tmp_path):
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    service = SkillEvolutionService(store)
    service.set_config_provider_for_test(_enabled_config)
    # In-flight tasks
    _make_record(store, task_id="t1", status="submitted")
    _make_record(store, task_id="t2", status="running")
    _make_record(store, task_id="t3", status="downloading")
    # Terminal tasks stay as-is
    _make_record(store, task_id="t4", status="approved")
    _make_record(store, task_id="t5", status="completed")

    count = service.mark_inflight_interrupted("ws1")
    assert count == 3

    for tid in ("t1", "t2", "t3"):
        assert store.get_task("ws1", tid).status == "interrupted"
    assert store.get_task("ws1", "t4").status == "approved"
    assert store.get_task("ws1", "t5").status == "completed"


def test_init_worker_does_not_deadlock(tmp_path, monkeypatch):
    """init_skill_evolution_worker holds the singleton lock while calling the."""
    # Details: init_skill_evolution_worker holds the singleton lock while calling the store/service getters — must use
    # a re-entrant lock (regression: plain Lock self-deadlocked the gunicorn worker on boot).
    import agent.skill_evolution.registry as reg

    monkeypatch.setattr(reg, "get_runtime_root", lambda: str(tmp_path))
    # Don't start a real background thread in the test.
    monkeypatch.setattr(reg.SkillEvolutionWorker, "start", lambda self: None)
    monkeypatch.setattr(reg.SkillEvolutionWorker, "stop", lambda self: None)
    reg.clear_singletons_for_test()
    try:
        result = reg.init_skill_evolution_worker()
        assert result is not None
    finally:
        reg.clear_singletons_for_test()


def _setup_reg(monkeypatch, tmp_path):
    """Point the registry at an isolated tmp runtime and neuter the worker
    thread so tests inspect singletons without background loops."""
    import agent.skill_evolution.registry as reg

    monkeypatch.setattr(reg, "get_runtime_root", lambda: str(tmp_path))
    monkeypatch.setattr(reg.SkillEvolutionWorker, "start", lambda self: None)
    monkeypatch.setattr(reg.SkillEvolutionWorker, "stop", lambda self: None)
    reg.clear_singletons_for_test()
    return reg


def test_init_starts_worker_even_when_disabled(monkeypatch, tmp_path):
    """The worker is config-reactive: init starts it at boot regardless of."""
    # Details: The worker is config-reactive: init starts it at boot regardless of ``enabled`` (it self-disables in
    # _run_iteration). Regression for the 'admin enables at runtime → /start creates a task but no thread advances it'
    # blocker — there is no separate reconcile step to forget.
    reg = _setup_reg(monkeypatch, tmp_path)
    assert reg.singleton_state()["worker"] is None
    w = reg.init_skill_evolution_worker()
    assert w is not None
    assert reg.singleton_state()["worker"] is not None
    # idempotent: second init returns the same singleton
    assert reg.init_skill_evolution_worker() is w


def test_run_iteration_skips_tick_when_disabled(monkeypatch, tmp_path):
    """Disabled config → no _tick; intervals still refresh live."""
    import agent.skill_evolution.worker as worker_mod

    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    service = SkillEvolutionService(store)
    worker = SkillEvolutionWorker(store, service)
    ticks = []
    worker.set_iteration_callback_for_test(lambda: ticks.append(1))
    monkeypatch.setattr(
        worker_mod,
        "load_system_config",
        lambda: {"skill_evolution": {"enabled": False, "poll_interval_seconds": 7, "task_timeout_seconds": 120}},
    )
    worker.run_iteration()
    assert ticks == []
    assert worker.poll_interval_seconds == 7
    assert worker.task_timeout_seconds == 120


def test_run_iteration_ticks_and_adopts_new_intervals(monkeypatch, tmp_path):
    """Enabled config → _tick runs; runtime interval changes take effect the."""
    # Details: Enabled config → _tick runs; runtime interval changes take effect the very next iteration, with no
    # worker restart.
    import agent.skill_evolution.worker as worker_mod

    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    service = SkillEvolutionService(store)
    worker = SkillEvolutionWorker(store, service, poll_interval_seconds=10, task_timeout_seconds=60)
    ticks = []
    worker.set_iteration_callback_for_test(
        lambda: ticks.append(worker.task_timeout_seconds)
    )
    monkeypatch.setattr(
        worker_mod,
        "load_system_config",
        lambda: {"skill_evolution": {"enabled": True, "poll_interval_seconds": 3, "task_timeout_seconds": 90}},
    )
    worker.run_iteration()
    assert ticks == [90]
    assert worker.poll_interval_seconds == 3
    assert worker.task_timeout_seconds == 90


def test_fail_does_not_clobber_terminal(tmp_path):
    """_fail re-reads the latest on-disk state and must NOT overwrite a concurrent."""
    # Details: _fail re-reads the latest on-disk state and must NOT overwrite a concurrent terminal decision
    # (approve/reject/cancel made by a route between the worker's snapshot and its timeout/failed path). Regression
    # guard: the worker snapshotted a running task, then a route approved it; the timeout path must not clobber it
    # back to failed.
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    service = SkillEvolutionService(store)
    service.set_config_provider_for_test(_enabled_config)
    rec = _make_record(store, age_minutes=120, status="running")

    # Simulate a route approving the task after the worker snapshotted `rec`.
    approved = store.get_task("ws1", "t1")
    approved.status = "approved"
    approved.decision = "approve_new_version"
    store.save_task(approved)

    out = service.fail_task(rec, stage="timeout", message="超时")
    assert out.status == "approved"
    assert store.get_task("ws1", "t1").status == "approved"
