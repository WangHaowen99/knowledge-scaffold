from internal_access import internal
import json
import threading
import time
from operator import getitem
from types import SimpleNamespace

import pytest
import workspace.user_store_manager as user_store_manager_module
from utils.streaming import build_agent_event
from workspace.user_store_manager import SessionPruneResult, UserStoreManager
from workspace.workspaces import create_workspace


def _set_session_updated_at(storage_dir, session_id, updated_at):
    snapshot_path = storage_dir / session_id / "snapshot.json"
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["updated_at"] = updated_at
    snapshot_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _capture_thread_result(errors, results, operation):
    try:
        results.append(operation())
    except BaseException as exc:
        errors.append(exc)


def test_user_store_manager_prunes_old_sessions_and_execution_records(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)
    manager = UserStoreManager("", "")

    chat_store = manager.get_chat_store("alice", "demo")
    execution_store = manager.get_execution_store("alice", "demo")
    summary_index = manager.get_session_summary_index("alice", "demo")
    summary_index.rebuild()

    for index in range(3):
        session_id = "session-{}".format(index)
        chat_store.create_session(session_id=session_id, title=session_id)
        execution_store.start_turn(
            session_id=session_id,
            turn_id=1,
            run_id="run-{}".format(index),
            user_request="hello",
        )
        execution_store.complete_turn(
            session_id,
            1,
            {"answer": "synthetic"},
            "done",
        )
        _set_session_updated_at(
            tmp_path / ".onto-runtime" / "workspaces" / "demo" / "runtime" / "users" / "alice" / "chats",
            session_id,
            "2026-05-{:02d}T00:00:00+00:00".format(index + 1),
        )
    summary_index.rebuild()

    deleted = manager.prune_sessions("alice", "demo", max_sessions=2)

    assert deleted == ["session-0"]
    assert [session.id for session in chat_store.list_sessions()] == ["session-2", "session-1"]
    assert execution_store.get_recent_turns("session-0") == []
    assert execution_store.get_recent_turns("session-1")


def test_indexed_prune_skips_session_with_admission_reservation(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Lease Workspace", workspace_id="lease", set_as_default=True)
    manager = UserStoreManager("", "")
    chat_store = manager.get_chat_store("alice", "lease")
    execution_store = manager.get_execution_store("alice", "lease")
    summary_index = manager.get_session_summary_index("alice", "lease")
    storage_dir = tmp_path / ".onto-runtime" / "workspaces" / "lease" / "runtime" / "users" / "alice" / "chats"
    chat_store.create_session(session_id="old", title="old")
    _set_session_updated_at(
        storage_dir,
        "old",
        "2026-05-01T00:00:00+00:00",
    )
    summary_index.rebuild()
    token = execution_store.reserve_session_admission("old")
    assert token

    blocked = manager.prune_sessions_batch(
        "alice",
        "lease",
        max_sessions=0,
        batch_size=1,
    )

    assert blocked.deleted_session_ids == ()
    assert blocked.has_more is True
    assert chat_store.get_session("old") is not None
    assert execution_store.release_session_admission("old", token) is True

    completed = manager.prune_sessions_batch(
        "alice",
        "lease",
        max_sessions=0,
        batch_size=1,
    )
    assert completed.deleted_session_ids == ("old",)
    assert chat_store.get_session("old") is None


def test_user_store_manager_injects_one_shared_summary_index(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Summary Workspace", workspace_id="summary", set_as_default=True)
    manager = UserStoreManager("", "")

    chat_store = manager.get_chat_store("alice", "summary")
    execution_store = manager.get_execution_store("alice", "summary")
    index = manager.get_session_summary_index("alice", "summary")

    assert chat_store.session_summary_index is index
    assert execution_store.session_summary_index is index

    chat_store.create_session(session_id="shared-summary", title="Shared")
    execution_store.start_turn(
        "shared-summary",
        1,
        "run-shared-summary",
        "synthetic request",
    )
    chat_store.merge_session_metadata("shared-summary", {"agent_mode": "quick"})

    summary = index.list_page(limit=10).items[0]
    assert summary.session_id == "shared-summary"
    assert summary.active_run_id == "run-shared-summary"
    assert summary.last_run_status == "running"


def test_user_store_manager_feature_flag_keeps_real_stores_on_legacy_path(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    monkeypatch.setenv("PERF_SESSION_SUMMARY_INDEX_ENABLED", "false")
    create_workspace("Legacy Workspace", workspace_id="legacy", set_as_default=True)

    def reject_index_construction(*_args, **_kwargs):
        raise AssertionError("disabled summary index must not be constructed")

    monkeypatch.setattr(
        user_store_manager_module,
        "SessionSummaryIndex",
        reject_index_construction,
    )
    manager = UserStoreManager("", "")

    chat_store = manager.get_chat_store("alice", "legacy")
    execution_store = manager.get_execution_store("alice", "legacy")

    assert manager.get_session_summary_index("alice", "legacy") is None
    assert chat_store.session_summary_index is None
    assert execution_store.session_summary_index is None
    assert not (
        tmp_path
        / ".onto-runtime"
        / "workspaces"
        / "legacy"
        / "runtime"
        / "users"
        / "alice"
        / "session-summary-index.json"
    ).exists()


def test_slow_index_initialization_for_one_user_does_not_hold_manager_global_lock(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Parallel Workspace", workspace_id="parallel", set_as_default=True)
    alice_loader_entered = threading.Event()
    release_alice_loader = threading.Event()

    class BlockingIndex:
        def __init__(self, path, *, rebuild_loader, rebuild_scheduler=None):
            del rebuild_loader, rebuild_scheduler
            self.path = str(path)
            self.state = "ready"
            if "alice" in self.path:
                alice_loader_entered.set()
                assert release_alice_loader.wait(timeout=1.0)

        @property
        def index_revision(self):
            return 1

    monkeypatch.setattr(user_store_manager_module, "SessionSummaryIndex", BlockingIndex)
    manager = UserStoreManager("", "")
    errors = []
    results = []
    alice = threading.Thread(
        target=lambda: _capture_thread_result(
            errors,
            results,
            lambda: manager.get_chat_store("alice", "parallel"),
        ),
        daemon=True,
    )
    bob = threading.Thread(
        target=lambda: _capture_thread_result(
            errors,
            results,
            lambda: manager.get_chat_store("bob", "parallel"),
        ),
        daemon=True,
    )

    alice.start()
    assert alice_loader_entered.wait(timeout=1.0)
    bob.start()
    try:
        bob.join(timeout=0.25)
        assert bob.is_alive() is False, "one user's index rebuild held the manager-wide store lock"
    finally:
        release_alice_loader.set()
        alice.join(timeout=1.0)
        bob.join(timeout=1.0)

    assert alice.is_alive() is False
    assert bob.is_alive() is False
    assert errors == []
    assert len(results) == 2


def test_store_initialization_does_not_load_or_rebuild_a_cold_summary_index(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Cold Workspace", workspace_id="cold", set_as_default=True)
    revision_reads = []

    class ColdIndex:
        def __init__(self, path, *, rebuild_loader, rebuild_scheduler=None):
            del path, rebuild_loader, rebuild_scheduler
            self.state = "cold"

        @property
        def index_revision(self):
            revision_reads.append(True)
            raise OSError("synthetic cold rebuild")

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def mark_degraded(self, _reason):
            return None

    monkeypatch.setattr(user_store_manager_module, "SessionSummaryIndex", ColdIndex)
    manager = UserStoreManager("", "")

    manager.get_chat_store("alice", "cold")

    assert revision_reads == []


def test_manager_schedules_cold_summary_rebuild_on_one_owned_worker(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Rebuild Workspace", workspace_id="rebuild", set_as_default=True)
    manager = UserStoreManager("", "")
    index = manager.get_session_summary_index("alice", "rebuild")

    assert index is not None
    assert index.request_rebuild() is True
    deadline = time.monotonic() + 1.0
    while not index.load_persisted_for_read() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert index.load_persisted_for_read() is True
    assert manager.close_summary_rebuilds(wait_seconds=0.5) is True


def test_summary_rebuild_close_cancels_publish_after_running_loader_returns(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Cancel Rebuild", workspace_id="cancel-rebuild", set_as_default=True)
    manager = UserStoreManager("", "")
    index = manager.get_session_summary_index("alice", "cancel-rebuild")
    loader_started = threading.Event()
    release_loader = threading.Event()
    loader_returned = threading.Event()

    def blocking_loader():
        loader_started.set()
        assert release_loader.wait(timeout=1.0)
        loader_returned.set()
        return []

    internal(index).rebuild_loader = blocking_loader
    assert index.request_rebuild() is True
    assert loader_started.wait(timeout=1.0) is True

    assert manager.close_summary_rebuilds(wait_seconds=0.01) is False
    release_loader.set()
    assert loader_returned.wait(timeout=1.0) is True
    deadline = time.monotonic() + 1.0
    while internal(index).rebuild_in_progress and time.monotonic() < deadline:
        time.sleep(0.01)

    assert internal(index).rebuild_in_progress is False
    assert internal(index).path.exists() is False


def test_user_store_manager_indexed_prune_excludes_pinned_and_active_sessions(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Prune Workspace", workspace_id="prune", set_as_default=True)
    manager = UserStoreManager("", "")
    chat_store = manager.get_chat_store("alice", "prune")
    execution_store = manager.get_execution_store("alice", "prune")
    manager.get_session_summary_index("alice", "prune").rebuild()

    for index in range(5):
        session_id = "prune-{}".format(index)
        chat_store.create_session(session_id=session_id, title=session_id)
        execution_store.start_turn(
            session_id,
            1,
            "run-{}".format(index),
            "synthetic request",
        )
        execution_store.complete_turn(
            session_id,
            1,
            {"answer": "synthetic"},
            "done",
        )

    chat_store.set_session_pinned("prune-0", True)
    chat_store.set_active_run("prune-1", "run-active")

    original_list_sessions = internal(chat_store).store.list_sessions
    monkeypatch.setattr(
        internal(chat_store).store,
        "list_sessions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("indexed prune must not enumerate chat histories")
        ),
    )
    deleted = manager.prune_sessions("alice", "prune", max_sessions=3)
    monkeypatch.setattr(internal(chat_store).store, "list_sessions", original_list_sessions)

    assert len(deleted) == 2
    assert "prune-0" not in deleted
    assert "prune-1" not in deleted
    remaining = {session.id for session in chat_store.list_sessions()}
    assert {"prune-0", "prune-1"}.issubset(remaining)


def test_user_store_manager_recovers_pending_terminal_in_bounded_startup_batch(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Recovery Workspace", workspace_id="recovery", set_as_default=True)
    manager = UserStoreManager("", "")
    chat_store = manager.get_chat_store("alice", "recovery")
    execution_store = manager.get_execution_store("alice", "recovery")
    manager.get_session_summary_index("alice", "recovery").rebuild()
    chat_store.create_session(session_id="recover-session", title="Recover")
    execution_store.start_turn(
        "recover-session",
        1,
        "recover-run",
        "synthetic request",
    )
    chat_store.set_active_run("recover-session", "recover-run")
    event = build_agent_event(
        run_id="recover-run",
        session_id="recover-session",
        turn_id=1,
        event_type="turn.finished",
        payload={"status": "completed"},
    )
    execution_store.prepare_deterministic_terminal(
        "recover-session",
        1,
        status="success",
        final_output={"answer": "synthetic"},
        llm_context=[],
        events=[event],
    )

    outcome = manager.recover_pending_sessions_batch(
        max_items=1,
        max_seconds=1.0,
    )

    assert outcome == {
        "processed": 1,
        "recovered": 1,
        "failures": 0,
        "remaining": 0,
        "complete": True,
    }
    assert execution_store.get_turn("recover-session", 1)["status"] == "success"
    assert chat_store.get_active_run("recover-session") is None
    assert any(
        (item.get("data") or {}).get("event_id") == event["data"]["event_id"]
        for item in chat_store.get_session("recover-session").events
    )


def test_summary_rebuild_fails_fast_without_erasing_busy_session_runtime(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Lock Workspace", workspace_id="lock-order", set_as_default=True)
    manager = UserStoreManager("", "")
    chat_store = manager.get_chat_store("alice", "lock-order")
    execution_store = manager.get_execution_store("alice", "lock-order")
    index = manager.get_session_summary_index("alice", "lock-order")
    chat_store.create_session(session_id="lock-session", title="Lock")
    execution_store.start_turn(
        "lock-session",
        1,
        "run-lock-session",
        "synthetic request",
    )
    before = index.list_page(limit=10).items[0]
    assert before.active_run_id == "run-lock-session"
    assert before.last_run_status == "running"

    session_lock = execution_store.deterministic_terminal_transaction("lock-session")
    session_lock.acquire()
    errors = []
    rebuild = threading.Thread(
        target=lambda: _capture_thread_error(errors, index.rebuild),
        daemon=True,
    )
    try:
        rebuild.start()
        rebuild.join(timeout=0.5)
        assert rebuild.is_alive() is False
        assert len(errors) == 1
        assert index.state == "degraded"
        retained = index.list_page(limit=10).items[0]
        assert retained.active_run_id == "run-lock-session"
        assert retained.last_run_status == "running"
    finally:
        session_lock.release()
        rebuild.join(timeout=1.0)

    index.rebuild()
    recovered = index.list_page(limit=10).items[0]
    assert recovered.active_run_id == "run-lock-session"
    assert recovered.last_run_status == "running"


def _capture_thread_error(errors, operation):
    try:
        operation()
    except Exception as exc:  # pragma: no cover - asserted by the caller
        errors.append(exc)


def test_indexed_prune_rechecks_execution_fact_before_chat_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Admission Workspace", workspace_id="admission", set_as_default=True)
    manager = UserStoreManager("", "")
    chat_store = manager.get_chat_store("alice", "admission")
    execution_store = manager.get_execution_store("alice", "admission")
    index = manager.get_session_summary_index("alice", "admission")
    chat_store.create_session(session_id="admitted", title="Admitted")
    execution_store.start_turn("admitted", 1, "run-admitted", "synthetic")
    index.rebuild()

    monkeypatch.setattr(index, "prune_candidates", lambda **_options: ("admitted",))

    result = manager.prune_sessions_batch(
        "alice",
        "admission",
        max_sessions=0,
        batch_size=1,
    )
    assert result.deleted_session_ids == ()
    assert result.has_more is True
    assert result.candidates_exhausted is False
    assert result.no_progress is True
    assert chat_store.get_session("admitted") is not None
    assert execution_store.get_turn("admitted", 1)["status"] == "running"


def test_indexed_prune_does_not_delete_session_updated_after_candidate_selection(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Prune CAS", workspace_id="prune-cas", set_as_default=True)
    manager = UserStoreManager("", "")
    chat_store = manager.get_chat_store("alice", "prune-cas")
    index = manager.get_session_summary_index("alice", "prune-cas")
    storage_dir = tmp_path / ".onto-runtime" / "workspaces" / "prune-cas" / "runtime" / "users" / "alice" / "chats"
    for offset, session_id in enumerate(("oldest", "middle", "newest"), start=1):
        chat_store.create_session(session_id=session_id, title=session_id)
        _set_session_updated_at(
            storage_dir,
            session_id,
            f"2026-05-0{offset}T00:00:00+00:00",
        )
    index.rebuild()

    original_candidates = index.prune_candidates
    selection_count = 0

    def select_then_update(**options):
        nonlocal selection_count
        candidates = original_candidates(**options)
        if selection_count == 0 and candidates:
            selection_count += 1
            candidate = candidates[0]
            session_id = getattr(candidate, "session_id", candidate)
            monkeypatch.setattr(
                internal(chat_store).store,
                "_now_iso",
                lambda: "2026-06-01T00:00:00+00:00",
            )
            chat_store.update_session_title(session_id, "Recently updated")
        return candidates

    monkeypatch.setattr(index, "prune_candidates", select_then_update)

    result = manager.prune_sessions_batch(
        "alice",
        "prune-cas",
        max_sessions=2,
        batch_size=1,
    )

    assert "oldest" not in result.deleted_session_ids
    assert chat_store.get_session("oldest").title == "Recently updated"


def test_recovery_discovery_defers_unready_index_and_continues_other_users(
    tmp_path,
    monkeypatch,
):
    manager = UserStoreManager("", "")

    class Entry:
        def __init__(self, name):
            self.name = name

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def is_symlink(self):
            return False

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def is_dir(self, *, follow_symlinks):
            assert follow_symlinks is False
            return True

    class Scan:
        def __init__(self, entries):
            self._entries = entries

        def __enter__(self):
            return self

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter(self._entries)

    class Index:
        def __init__(self, availability, session_id):
            self._availability = iter(availability)
            self._ready = False
            self._session_id = session_id
            self.rebuild_requests = 0

        def load_persisted_for_read(self):
            try:
                self._ready = next(self._availability)
            except StopIteration:
                pass
            return self._ready

        def request_rebuild(self):
            self.rebuild_requests += 1
            return True

        def list_all(self):
            assert self._ready, "unready index must never synchronously rebuild"
            return (SimpleNamespace(session_id=self._session_id),)

    alice = Index([False, True], "alice-session")
    broken = Index([False, False], "broken-session")
    bob = Index([True], "bob-session")
    indexes = {"alice": alice, "broken": broken, "bob": bob}

    monkeypatch.setattr(
        user_store_manager_module,
        "list_workspaces",
        lambda: [{"id": "workspace"}],
    )
    monkeypatch.setattr(
        user_store_manager_module,
        "get_workspace_paths",
        lambda _workspace_id: SimpleNamespace(users_runtime_dir=tmp_path),
    )
    monkeypatch.setattr(
        user_store_manager_module.os,
        "scandir",
        lambda _path: Scan([Entry("alice"), Entry("broken"), Entry("bob")]),
    )
    monkeypatch.setattr(
        manager,
        "_ensure_stores_locked",
        lambda *, safe_user, workspace_id: (
            SimpleNamespace(list_sessions=lambda: ()),
            object(),
            getitem(indexes, safe_user),
        ),
    )

    targets = [target for target in internal(manager).iter_recovery_targets() if isinstance(target, tuple)]

    assert targets == [
        ("workspace", "bob", "bob-session"),
        ("workspace", "alice", "alice-session"),
    ]
    assert alice.rebuild_requests == 1
    assert broken.rebuild_requests >= 1


def test_cold_prune_schedules_rebuild_without_loading_history_on_caller(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Cold Prune", workspace_id="cold-prune", set_as_default=True)
    manager = UserStoreManager("", "")
    index = manager.get_session_summary_index("alice", "cold-prune")
    scheduled = []
    monkeypatch.setattr(index, "request_rebuild", lambda: scheduled.append(True) or True)
    monkeypatch.setattr(
        index,
        "prune_candidates",
        lambda **_options: (_ for _ in ()).throw(AssertionError("cold prune must not scan session history")),
    )

    result = manager.prune_sessions_batch(
        "alice",
        "cold-prune",
        max_sessions=0,
        batch_size=1,
    )

    assert scheduled == [True]
    assert result.deleted_session_ids == ()
    assert result.has_more is True
    assert result.candidates_exhausted is False
    assert result.no_progress is True


def test_prune_rechecks_readiness_before_final_candidate_scan(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Prune Degraded", workspace_id="prune-degraded", set_as_default=True)
    manager = UserStoreManager("", "")
    index = manager.get_session_summary_index("alice", "prune-degraded")
    readiness = iter((True, False))
    scheduled = []
    candidate_calls = []
    monkeypatch.setattr(index, "load_persisted_for_read", lambda: next(readiness))
    monkeypatch.setattr(index, "request_rebuild", lambda: scheduled.append(True) or True)

    def candidates(**_options):
        candidate_calls.append(True)
        if len(candidate_calls) > 1:
            raise AssertionError("degraded index must not run a final candidate scan")
        return ()

    monkeypatch.setattr(index, "prune_candidates", candidates)

    result = manager.prune_sessions_batch(
        "alice",
        "prune-degraded",
        max_sessions=0,
        batch_size=1,
    )

    assert candidate_calls == [True]
    assert scheduled == [True]
    assert result.has_more is True
    assert result.no_progress is True


def test_recovery_stop_prevents_catalog_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    manager = UserStoreManager("", "")
    stopped = threading.Event()
    stopped.set()
    monkeypatch.setattr(
        "workspace.user_store_manager.list_workspaces",
        lambda: (_ for _ in ()).throw(AssertionError("catalog scan must not start")),
    )

    outcome = manager.recover_pending_sessions_batch(
        max_items=25,
        max_seconds=0.25,
        stop_event=stopped,
    )

    assert outcome["processed"] == 0
    assert outcome["complete"] is False


def test_recovery_catalog_discovery_obeys_the_batch_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    manager = UserStoreManager("", "")

    def endless_catalog_scan():
        while True:
            yield internal(user_store_manager_module).RECOVERY_SCAN_TICK

    monkeypatch.setattr(manager, "_iter_recovery_targets", endless_catalog_scan)
    started = time.monotonic()
    outcome = manager.recover_pending_sessions_batch(
        max_items=25,
        max_seconds=0.01,
    )

    assert time.monotonic() - started < 0.2
    assert outcome == {
        "processed": 0,
        "recovered": 0,
        "failures": 0,
        "remaining": 1,
        "complete": False,
    }


def test_one_slow_recovery_discovery_step_cannot_overrun_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    manager = UserStoreManager("", "")

    def slow_catalog_scan():
        time.sleep(0.15)
        yield internal(user_store_manager_module).RECOVERY_SCAN_TICK

    monkeypatch.setattr(manager, "_iter_recovery_targets", slow_catalog_scan)
    started = time.monotonic()
    outcome = manager.recover_pending_sessions_batch(
        max_items=25,
        max_seconds=0.02,
    )

    assert time.monotonic() - started < 0.08
    assert outcome["processed"] == 0
    assert outcome["complete"] is False
    assert manager.close_recovery_discovery(wait_seconds=0.3) is True


def test_slow_recovery_target_is_deadline_bounded_and_single_flight(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    manager = UserStoreManager("", "")
    started = threading.Event()
    calls = []

    class Chat:
        @staticmethod
        def get_active_run(_session_id):
            return None

    class Execution:
        @staticmethod
        def get_session_summary_runtime(_session_id):
            return {"active_run_id": None}

    monkeypatch.setattr(
        manager,
        "_iter_recovery_targets",
        lambda: iter([("workspace", "user", "session")]),
    )
    monkeypatch.setattr(
        manager,
        "_ensure_stores_locked",
        lambda **_options: (Chat(), Execution(), None),
    )

    def slow_terminal(*_args, **_kwargs):
        calls.append("session")
        started.set()
        time.sleep(0.15)
        return False

    monkeypatch.setattr(
        user_store_manager_module,
        "recover_pending_deterministic_terminal",
        slow_terminal,
    )
    monkeypatch.setattr(
        user_store_manager_module,
        "recover_pending_deterministic_artifacts",
        lambda *_args, **_kwargs: False,
    )

    started_at = time.monotonic()
    first = manager.recover_pending_sessions_batch(
        max_items=1,
        max_seconds=0.02,
    )
    assert time.monotonic() - started_at < 0.08
    assert first["processed"] == 0
    assert first["complete"] is False
    assert started.wait(timeout=0.1) is True

    started_at = time.monotonic()
    second = manager.recover_pending_sessions_batch(
        max_items=1,
        max_seconds=0.02,
    )
    assert time.monotonic() - started_at < 0.08
    assert second["processed"] == 0
    assert calls == ["session"]

    time.sleep(0.16)
    final = manager.recover_pending_sessions_batch(
        max_items=1,
        max_seconds=0.2,
    )
    assert final["processed"] == 1
    assert final["complete"] is True
    assert calls == ["session"]
    assert manager.close_recovery_discovery(wait_seconds=0.1) is True


def test_recovery_shutdown_boundedly_drains_active_target(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    manager = UserStoreManager("", "")
    started = threading.Event()

    class Chat:
        @staticmethod
        def get_active_run(_session_id):
            return None

    class Execution:
        @staticmethod
        def get_session_summary_runtime(_session_id):
            return {"active_run_id": None}

    monkeypatch.setattr(
        manager,
        "_iter_recovery_targets",
        lambda: iter([("workspace", "user", "session")]),
    )
    monkeypatch.setattr(
        manager,
        "_ensure_stores_locked",
        lambda **_options: (Chat(), Execution(), None),
    )

    def slow_terminal(*_args, **_kwargs):
        started.set()
        time.sleep(0.12)
        return False

    monkeypatch.setattr(
        user_store_manager_module,
        "recover_pending_deterministic_terminal",
        slow_terminal,
    )
    monkeypatch.setattr(
        user_store_manager_module,
        "recover_pending_deterministic_artifacts",
        lambda *_args, **_kwargs: False,
    )

    manager.recover_pending_sessions_batch(max_items=1, max_seconds=0.01)
    assert started.wait(timeout=0.1) is True
    assert manager.close_recovery_discovery(wait_seconds=0.01) is False
    time.sleep(0.13)


def test_recovery_discovery_worker_propagates_errors_and_shuts_down(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    manager = UserStoreManager("", "")

    def broken_catalog_scan():
        yield from ()
        raise RuntimeError("synthetic discovery failure")

    monkeypatch.setattr(manager, "_iter_recovery_targets", broken_catalog_scan)

    with pytest.raises(RuntimeError, match="synthetic discovery failure"):
        manager.recover_pending_sessions_batch(
            max_items=1,
            max_seconds=0.1,
        )
    assert manager.close_recovery_discovery(wait_seconds=0.1) is True


def test_prune_batch_reports_more_work_when_authoritative_recheck_skips_candidate(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Prune State", workspace_id="prune-state", set_as_default=True)
    manager = UserStoreManager("", "")
    chat_store = manager.get_chat_store("alice", "prune-state")
    storage_dir = tmp_path / ".onto-runtime" / "workspaces" / "prune-state" / "runtime" / "users" / "alice" / "chats"
    for index in range(3):
        session_id = f"stale-{index}"
        chat_store.create_session(session_id=session_id, title=f"Stale {index}")
        _set_session_updated_at(
            storage_dir,
            session_id,
            f"2026-05-0{index + 1}T00:00:00+00:00",
        )
    # Bypass projection to reproduce a pin racing candidate selection.
    internal(chat_store).store.set_session_pinned("stale-0", True)

    result = manager.prune_sessions_batch(
        "alice",
        "prune-state",
        max_sessions=1,
        batch_size=1,
    )

    assert isinstance(result, SessionPruneResult)
    assert result.has_more is True
    assert result.candidates_exhausted is False
