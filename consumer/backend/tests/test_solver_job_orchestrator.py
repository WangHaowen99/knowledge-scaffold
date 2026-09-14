from __future__ import annotations

from internal_access import internal
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from backend_synthetic_credentials import synthetic_secret
from solver_coordination.contracts import ProblemIR, SolverResult
from solver_coordination.job_orchestrator import DurableJobOrchestrator
from solver_coordination.job_store import JobStoreConflict, LeaseConflict, SolverJobStore

WORKSPACE_ID = "workspace_synthetic"
OWNER_ID = "owner_synthetic"
BINDING_REVISION = "binding_revision_synthetic_1"
CONTRACT_DIGEST = "sha256:solver-coordination.v1:" + "c" * 64


def _problem(marker: str = "a") -> ProblemIR:
    return ProblemIR(
        problem_id="sha256:solver-coordination.v1:" + marker * 64,
        problem_family="synthetic_choice.v1",
        decision_space={"variables": ["choice"], "option_domain": ["OPTION_0", "OPTION_1"]},
        parameters={"quantity": "10"},
        constraints=(),
        objectives=(),
        limits={"max_candidates": 2},
    )


@dataclass
class _BackendEntry:
    job_id: str
    status: str = "running"


class _DurableFakeBackend:
    def __init__(self) -> None:
        self.entries: dict[str, _BackendEntry] = {}
        self.submit_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.poll_calls: list[str] = []
        self.poll_failures: set[str] = set()
        self.poll_hook = None
        self.raise_after_accept_once = False
        self.cancel_observer = None

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def capabilities(self):
        return {"problem_families": ["synthetic_choice.v1"]}

    def submit(self, *, problem, idempotency_key, deadline, binding=None):
        self.submit_calls.append(idempotency_key)
        entry = self.entries.setdefault(
            idempotency_key,
            _BackendEntry(job_id="backend_{}".format(len(self.entries) + 1)),
        )
        if self.raise_after_accept_once:
            self.raise_after_accept_once = False
            raise TimeoutError("synthetic transport timeout")
        return {"job_id": entry.job_id, "status": entry.status}

    def _entry_by_job_id(self, job_id: str) -> _BackendEntry:
        return next(entry for entry in self.entries.values() if entry.job_id == job_id)

    def poll(self, *, job_id, binding=None):
        self.poll_calls.append(job_id)
        if self.poll_hook is not None:
            self.poll_hook(job_id)
        if job_id in self.poll_failures:
            raise RuntimeError("synthetic backend poll failure")
        entry = internal(self).entry_by_job_id(job_id)
        return {"job_id": job_id, "status": entry.status, "progress": {"percent": 50}}

    def cancel(self, *, job_id, binding=None):
        if self.cancel_observer is not None:
            self.cancel_observer()
        self.cancel_calls.append(job_id)
        internal(self).entry_by_job_id(job_id).status = "cancelled"

    def result(self, *, job_id, binding=None):
        entry = internal(self).entry_by_job_id(job_id)
        if entry.status != "completed_optimal":
            raise RuntimeError("result is not ready")
        return SolverResult(
            problem_id=_problem().problem_id,
            status="optimal",
            assignment={"choice": "OPTION_0"},
            objective_values={},
            diagnostics=(),
            engine={"name": "synthetic-backend", "version": "1"},
        )


class _AttemptLostBackend(_DurableFakeBackend):
    def poll(self, *, job_id, binding=None):
        return {"job_id": job_id, "status": "attempt_lost"}


def _orchestrator(tmp_path, backend=None, *, clock=None):
    store = SolverJobStore(tmp_path / "jobs.sqlite3")
    store.register_binding_revision(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=BINDING_REVISION,
        configuration={"backend": "synthetic"},
    )
    selected_backend = backend or _DurableFakeBackend()
    return (
        DurableJobOrchestrator(
            store=store,
            backend=selected_backend,
            idempotency_secret=synthetic_secret("solver_job_orchestrator-credential-01").encode("utf-8"),
            binding_revision_id=BINDING_REVISION,
            lease_owner="worker_synthetic",
            clock=clock,
        ),
        store,
        selected_backend,
    )


def _start(
    orchestrator,
    *,
    run_suffix="a",
    attempt_id="attempt_synthetic_001",
    deadline_at=None,
    problem_marker="a",
    run_policy=None,
    binding_revision_id=BINDING_REVISION,
    defer_submit=False,
):
    return orchestrator.start_or_resume(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=binding_revision_id,
        problem=_problem(problem_marker),
        run_policy=run_policy or {"mode": "bounded", "max_attempts": 2},
        deadline_at=deadline_at or datetime(2099, 1, 1, tzinfo=timezone.utc),
        run_id="run_synthetic_{}".format(run_suffix),
        turn_id="turn_synthetic_{}".format(run_suffix),
        tool_call_id="tool_call_synthetic_{}".format(run_suffix),
        card_name="solve_synthetic_a1b2c3d4",
        contract_digest=CONTRACT_DIGEST,
        attempt_id=attempt_id,
        defer_submit=defer_submit,
    )


def test_submit_persists_link_before_backend_and_replay_reuses_attempt(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path)

    def assert_link_already_exists():
        assert (
            store.get_run_link(
                workspace_id=WORKSPACE_ID,
                owner_id=OWNER_ID,
                run_id="run_synthetic_a",
                turn_id="turn_synthetic_a",
                tool_call_id="tool_call_synthetic_a",
            )
            is not None
        )

    original_submit = backend.submit

    def submit_with_assertion(**kwargs):
        assert_link_already_exists()
        return original_submit(**kwargs)

    backend.submit = submit_with_assertion
    first = _start(orchestrator)
    replay = _start(orchestrator)

    assert first["job_id"] == replay["job_id"]
    assert first["attempt_id"] == replay["attempt_id"]
    assert len(backend.submit_calls) == 1
    attempt = store.get_attempt(first["attempt_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert attempt["backend_idempotency_key"].startswith("hmac-sha256:")
    assert first["attempt_id"] not in attempt["backend_idempotency_key"]


def test_different_runs_with_same_problem_and_binding_create_distinct_jobs(tmp_path):
    orchestrator, _store, backend = _orchestrator(tmp_path)

    first = _start(
        orchestrator,
        run_suffix="cross_run_a",
        attempt_id="attempt_synthetic_cross_run_a",
    )
    second = _start(
        orchestrator,
        run_suffix="cross_run_b",
        attempt_id="attempt_synthetic_cross_run_b",
    )

    assert first["job_id"] != second["job_id"]
    assert first["attempt_id"] != second["attempt_id"]
    assert len(backend.submit_calls) == 2


def test_same_tool_call_replay_rejects_changed_problem_payload(tmp_path):
    orchestrator, _, _ = _orchestrator(tmp_path)
    _start(orchestrator, run_suffix="payload_conflict", problem_marker="a")

    with pytest.raises(JobStoreConflict, match="payload|problem|attempt"):
        _start(orchestrator, run_suffix="payload_conflict", problem_marker="b")


def test_same_tool_call_replay_compares_full_problem_body_not_only_problem_id(tmp_path):
    orchestrator, _, _ = _orchestrator(tmp_path)
    original = _problem("a")
    changed = ProblemIR(
        problem_id=original.problem_id,
        problem_family=original.problem_family,
        decision_space=original.decision_space,
        parameters={"quantity": "11"},
        constraints=original.constraints,
        objectives=original.objectives,
        limits=original.limits,
    )
    arguments = {
        "workspace_id": WORKSPACE_ID,
        "owner_id": OWNER_ID,
        "binding_revision_id": BINDING_REVISION,
        "run_policy": {"mode": "bounded", "max_attempts": 2},
        "deadline_at": datetime(2099, 1, 1, tzinfo=timezone.utc),
        "run_id": "run_synthetic_full_payload",
        "turn_id": "turn_synthetic_full_payload",
        "tool_call_id": "tool_call_synthetic_full_payload",
        "card_name": "solve_synthetic_a1b2c3d4",
        "contract_digest": CONTRACT_DIGEST,
        "attempt_id": "attempt_synthetic_full_payload",
    }
    orchestrator.start_or_resume(problem=original, **arguments)
    with pytest.raises(JobStoreConflict, match="payload|problem"):
        orchestrator.start_or_resume(problem=changed, **arguments)


def test_same_tool_call_replay_compares_canonical_run_policy(tmp_path):
    orchestrator, _, _ = _orchestrator(tmp_path)
    first = _start(
        orchestrator,
        run_suffix="run_policy_identity",
        run_policy={"mode": "bounded", "max_attempts": 2},
    )
    equivalent = _start(
        orchestrator,
        run_suffix="run_policy_identity",
        run_policy={"max_attempts": 2, "mode": "bounded"},
    )
    assert equivalent["job_id"] == first["job_id"]

    with pytest.raises(JobStoreConflict, match="policy|request"):
        _start(
            orchestrator,
            run_suffix="run_policy_identity",
            run_policy={"mode": "bounded", "max_attempts": 3},
        )


def test_poll_does_not_submit_a_job_while_inputs_are_freezing(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path)
    started = _start(orchestrator, run_suffix="poll_freeze", defer_submit=True)

    observed = orchestrator.poll_once(started["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)

    assert observed["status"] == "created"
    assert observed["phase"] == "freeze_inputs"
    assert backend.submit_calls == []
    assert store.list_artifacts(started["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID) == ()


def test_same_tool_call_replay_compares_normalized_deadline(tmp_path):
    orchestrator, _, _ = _orchestrator(tmp_path)
    original_deadline = datetime(2099, 1, 1, tzinfo=timezone.utc)
    first = _start(
        orchestrator,
        run_suffix="deadline_identity",
        deadline_at=original_deadline,
    )
    equivalent = _start(
        orchestrator,
        run_suffix="deadline_identity",
        deadline_at=datetime(2099, 1, 1, 8, tzinfo=timezone(timedelta(hours=8))),
    )
    assert equivalent["job_id"] == first["job_id"]

    with pytest.raises(JobStoreConflict, match="deadline|request"):
        _start(
            orchestrator,
            run_suffix="deadline_identity",
            deadline_at=original_deadline + timedelta(seconds=1),
        )


def test_same_tool_call_replay_rejects_a_different_binding_revision(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path)
    _start(orchestrator, run_suffix="binding_identity")
    other_binding = "binding_revision_synthetic_2"
    store.register_binding_revision(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=other_binding,
        configuration={"backend": "synthetic", "revision": "synthetic-2"},
    )
    other_orchestrator = DurableJobOrchestrator(
        store=store,
        backend=backend,
        idempotency_secret=synthetic_secret("solver_job_orchestrator-credential-01").encode("utf-8"),
        binding_revision_id=other_binding,
        lease_owner="worker_other_binding",
    )

    with pytest.raises(JobStoreConflict, match="binding|request"):
        _start(
            other_orchestrator,
            run_suffix="binding_identity",
            binding_revision_id=other_binding,
        )


def test_one_generic_orchestrator_accepts_distinct_bindings_for_distinct_runs(tmp_path):
    orchestrator, store, _backend = _orchestrator(tmp_path)
    first = _start(orchestrator, run_suffix="binding-a")
    other_binding = "binding_revision_synthetic_2"
    store.register_binding_revision(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=other_binding,
        configuration={"backend": "synthetic", "revision": "synthetic-2"},
    )

    second = _start(
        orchestrator,
        run_suffix="binding-b",
        attempt_id="attempt_synthetic_binding_b",
        problem_marker="b",
        binding_revision_id=other_binding,
    )

    assert first["job_id"] != second["job_id"]
    assert (
        store.get_job(first["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)["binding_revision_id"]
        == BINDING_REVISION
    )
    assert (
        store.get_job(second["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)["binding_revision_id"]
        == other_binding
    )


def test_orchestrator_passes_each_jobs_frozen_binding_to_one_backend(tmp_path):
    class BindingRecordingBackend(_DurableFakeBackend):
        def __init__(self):
            super().__init__()
            self.bindings = []

        def submit(self, **kwargs):
            self.bindings.append(kwargs.get("binding"))
            return super().submit(**kwargs)

    backend = BindingRecordingBackend()
    orchestrator, store, _ = _orchestrator(tmp_path, backend=backend)
    first = _start(orchestrator, run_suffix="wire-binding-a")
    other_binding = "binding_revision_synthetic_2"
    other_configuration = {"backend": "synthetic", "revision": "synthetic-2"}
    store.register_binding_revision(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=other_binding,
        configuration=other_configuration,
    )
    second = _start(
        orchestrator,
        run_suffix="wire-binding-b",
        attempt_id="attempt_synthetic_wire_binding_b",
        problem_marker="b",
        binding_revision_id=other_binding,
    )

    assert (
        store.get_job(first["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)["binding_revision_id"]
        == BINDING_REVISION
    )
    assert (
        store.get_job(second["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)["binding_revision_id"]
        == other_binding
    )
    assert backend.bindings == [
        {"backend": "synthetic"},
        other_configuration,
    ]


def test_attempt_lost_fails_the_attempt_without_publishing_solver_result(tmp_path):
    orchestrator, store, _backend = _orchestrator(tmp_path, backend=_AttemptLostBackend())
    started = _start(orchestrator, run_suffix="attempt-lost")

    outcome = orchestrator.poll_once(started["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)

    assert outcome["status"] == "failed"
    assert outcome["error_code"] == "attempt_lost"
    attempt = store.get_attempt(started["attempt_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert attempt["status"] == "failed"
    assert attempt["result"] is None


def test_attempt_lost_can_be_explicitly_retried_as_a_new_attempt(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path, backend=_AttemptLostBackend())
    started = _start(orchestrator, run_suffix="attempt-lost-retry")
    failed = orchestrator.poll_once(started["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert failed["error_code"] == "attempt_lost"

    retried = orchestrator.retry(
        started["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id="attempt_synthetic_attempt_lost_002",
    )

    assert retried["job_id"] == started["job_id"]
    assert retried["attempt_id"] != started["attempt_id"]
    attempts = store.list_attempts(started["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert [attempt["attempt_number"] for attempt in attempts] == [1, 2]


def test_post_timeout_replay_uses_same_backend_idempotency_key(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path)
    backend.raise_after_accept_once = True

    uncertain = _start(orchestrator)
    assert uncertain["status"] == "submitting"
    replayed = _start(orchestrator)

    assert replayed["status"] == "waiting_solver"
    assert backend.submit_calls[0] == backend.submit_calls[1]
    assert len(backend.entries) == 1
    assert (
        store.get_attempt(replayed["attempt_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)["backend_job_id"]
        == replayed["backend_job_id"]
    )


def test_concurrent_subscribers_converge_across_repeated_start_races(tmp_path):
    first_orchestrator, store, backend = _orchestrator(tmp_path)
    second_orchestrator = DurableJobOrchestrator(
        store=store,
        backend=backend,
        idempotency_secret=synthetic_secret("solver_job_orchestrator-credential-01").encode("utf-8"),
        binding_revision_id=BINDING_REVISION,
        lease_owner="worker_synthetic_b",
    )
    markers = "abcdefgh"
    for index, marker in enumerate(markers):
        start_barrier = threading.Barrier(2)

        def launch(
            orchestrator,
            suffix,
            *,
            run_index=index,
            marker_value=marker,
            barrier=start_barrier,
        ):
            barrier.wait(timeout=3)
            return _start(
                orchestrator,
                run_suffix="concurrent_{}_{}".format(run_index, suffix),
                attempt_id="attempt_synthetic_concurrent_{}_{}".format(run_index, suffix),
                problem_marker=marker_value,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            launch_inputs = (
                (first_orchestrator, "a"),
                (second_orchestrator, "b"),
            )
            futures = [executor.submit(launch, orchestrator, suffix) for orchestrator, suffix in launch_inputs]
            outcomes = [future.result() for future in futures]
        assert outcomes[0]["job_id"] != outcomes[1]["job_id"]
        assert outcomes[0]["attempt_id"] != outcomes[1]["attempt_id"]

    assert len(backend.entries) == 2 * len(markers)


def test_retry_requires_a_retryable_terminal_failure(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path)
    first = _start(orchestrator)
    with pytest.raises(JobStoreConflict, match="terminal|retry"):
        orchestrator.retry(
            first["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            attempt_id="attempt_synthetic_rejected",
        )

    internal(backend).entry_by_job_id(first["backend_job_id"]).status = "failed"
    failed = orchestrator.poll_once(first["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert failed["status"] == "failed"
    retried = orchestrator.retry(
        first["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id="attempt_synthetic_002",
    )

    assert retried["job_id"] == first["job_id"]
    assert retried["attempt_id"] != first["attempt_id"]
    attempts = store.list_attempts(first["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert [attempt["attempt_number"] for attempt in attempts] == [1, 2]
    assert backend.submit_calls[0] != backend.submit_calls[1]
    link = store.get_run_link(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        run_id="run_synthetic_a",
        turn_id="turn_synthetic_a",
        tool_call_id="tool_call_synthetic_a",
    )
    assert link["attempt_id"] == retried["attempt_id"]
    replay = _start(orchestrator, run_suffix="a")
    assert replay["attempt_id"] == retried["attempt_id"]

    internal(backend).entry_by_job_id(retried["backend_job_id"]).status = "failed"
    orchestrator.poll_once(first["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    with pytest.raises(JobStoreConflict, match="attempt|max"):
        orchestrator.retry(
            first["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            attempt_id="attempt_synthetic_003",
        )


def test_retry_supersedes_and_cancels_an_old_active_attempt(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path)
    first = _start(orchestrator)
    current = store.get_job(first["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    store.update_job(
        first["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        expected_version=current["version"],
        status="failed",
        phase="solve",
        error_code="solver_unavailable",
    )

    retried = orchestrator.retry(
        first["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id="attempt_synthetic_002",
    )
    assert retried["attempt_id"] == "attempt_synthetic_002"
    assert first["backend_job_id"] in backend.cancel_calls
    assert store.get_attempt(first["attempt_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)["status"] == "failed"


def test_retry_rejects_nonretryable_terminal_error(tmp_path):
    orchestrator, store, _ = _orchestrator(tmp_path)
    first = _start(orchestrator)
    store.update_attempt_status(
        first["attempt_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        status="failed",
        error_code="model_compile_failed",
    )
    current = store.get_job(first["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    store.update_job(
        first["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        expected_version=current["version"],
        status="failed",
        phase="compile_problem",
        error_code="model_compile_failed",
    )
    with pytest.raises(JobStoreConflict, match="retryable"):
        orchestrator.retry(
            first["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            attempt_id="attempt_synthetic_002",
        )


def test_cancelling_one_run_does_not_cancel_another_run_backend_job(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path)
    first = _start(orchestrator, run_suffix="a")
    second = _start(
        orchestrator,
        run_suffix="b",
        attempt_id="attempt_synthetic_should_not_be_used",
    )
    assert first["job_id"] != second["job_id"]
    assert first["attempt_id"] != second["attempt_id"]

    cancelled_first = orchestrator.cancel_subscription(
        first["subscription_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID
    )
    assert cancelled_first["last_subscriber"] is True
    assert len(backend.cancel_calls) == 1
    assert store.get_job(first["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)["status"] == "cancelled"
    assert store.get_job(second["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)["status"] == "waiting_solver"


def test_replay_of_uncertain_submit_recovers_backend_identity_before_cancellation(
    tmp_path,
):
    orchestrator, store, backend = _orchestrator(tmp_path)
    backend.raise_after_accept_once = True
    started = _start(orchestrator, run_suffix="cancelling_replay")
    assert started["backend_job_id"] is None
    reservation = store.cancel_subscription(started["subscription_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert reservation["cancel_reserved"] is True
    submit_count = len(backend.submit_calls)

    replay = _start(orchestrator, run_suffix="cancelling_replay")
    assert replay["status"] == "cancelled"
    assert len(backend.submit_calls) == submit_count + 1
    assert backend.cancel_calls == ["backend_1"]
    attempt = store.get_attempt(started["attempt_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert attempt["status"] == "cancelled"


def test_cancelling_before_first_submit_does_not_create_backend_job(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path)
    started = _start(orchestrator, run_suffix="cancel_before_submit", defer_submit=True)

    cancellation = orchestrator.cancel_subscription(
        started["subscription_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID
    )

    assert cancellation["job_status"] == "cancelled"
    assert backend.submit_calls == []
    assert backend.cancel_calls == []
    assert (
        store.get_attempt(started["attempt_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)["status"] == "cancelled"
    )


def test_cancellation_waits_for_an_inflight_retry_submit(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path)
    started = _start(orchestrator, run_suffix="cancel_inflight_retry")
    internal(backend).entry_by_job_id(started["backend_job_id"]).status = "unsupported"
    failed = orchestrator.poll_once(started["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert failed["status"] == "failed"

    entered = threading.Event()
    release = threading.Event()
    original_submit = backend.submit

    def _blocked_submit(**kwargs):
        entered.set()
        assert release.wait(5)
        return original_submit(**kwargs)

    backend.submit = _blocked_submit
    with ThreadPoolExecutor(max_workers=1) as executor:
        retrying = executor.submit(
            orchestrator.retry,
            started["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            attempt_id="attempt_synthetic_cancel_inflight_retry",
        )
        assert entered.wait(5)
        cancellation = orchestrator.cancel_subscription(
            started["subscription_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
        )
        assert cancellation["job_status"] == "cancelling"
        assert len(backend.submit_calls) == 1
        release.set()
        outcome = retrying.result(timeout=5)

    assert outcome["status"] == "cancelled"
    assert len(backend.submit_calls) == 2
    assert backend.cancel_calls == ["backend_2"]
    assert store.get_job(started["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)["status"] == "cancelled"


def test_submit_waits_for_backend_thread_before_releasing_failed_renewal(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    renewal_failed = threading.Event()
    submit_invocations = []
    original_submit = backend.submit
    original_renew = store.renew_job_lease

    def _blocked_first_submit(**kwargs):
        submit_invocations.append(kwargs["idempotency_key"])
        if len(submit_invocations) == 1:
            entered.set()
            assert release.wait(5)
        return original_submit(**kwargs)

    def _failed_renewal(*args, **kwargs):
        renewal_failed.set()
        raise RuntimeError("synthetic lease storage failure")

    backend.submit = _blocked_first_submit
    store.renew_job_lease = _failed_renewal
    with ThreadPoolExecutor(max_workers=1) as executor:
        foreground = executor.submit(_start, orchestrator, run_suffix="renewal_failure")
        assert entered.wait(5)
        assert renewal_failed.wait(5)
        assert not foreground.done()
        link = store.get_run_link(
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            run_id="run_synthetic_renewal_failure",
            turn_id="turn_synthetic_renewal_failure",
            tool_call_id="tool_call_synthetic_renewal_failure",
        )
        with pytest.raises(LeaseConflict):
            orchestrator.poll_once(link["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
        assert len(submit_invocations) == 1
        store.renew_job_lease = original_renew
        release.set()
        with pytest.raises(RuntimeError, match="synthetic lease storage failure"):
            foreground.result(timeout=5)

    recovered = orchestrator.poll_once(link["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert recovered["status"] == "waiting_solver"
    assert submit_invocations[0] == submit_invocations[1]
    assert len(backend.entries) == 1


def test_timeout_cancels_backend_before_terminalization_and_late_poll_is_ignored(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock_value = [now]
    orchestrator, store, backend = _orchestrator(tmp_path, clock=lambda: clock_value[0])
    started = _start(orchestrator, deadline_at=now + timedelta(seconds=1))
    observed_statuses: list[str] = []

    def observe_cancelled_status():
        observed_statuses.append(
            store.get_job(started["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)["status"]
        )

    backend.cancel_observer = observe_cancelled_status
    clock_value[0] = now + timedelta(seconds=2)
    timed_out = orchestrator.poll_once(started["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert observed_statuses == ["waiting_solver"]
    assert timed_out["status"] == "timed_out"

    internal(backend).entry_by_job_id(started["backend_job_id"]).status = "completed_optimal"
    late = orchestrator.poll_once(started["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert late["status"] == "timed_out"


def test_new_orchestrator_reconstructs_running_and_completed_backend_job(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path)
    started = _start(orchestrator)

    reopened_store = SolverJobStore(store.database_path)
    recovered = DurableJobOrchestrator(
        store=reopened_store,
        backend=backend,
        idempotency_secret=synthetic_secret("solver_job_orchestrator-credential-01").encode("utf-8"),
        binding_revision_id=BINDING_REVISION,
        lease_owner="worker_recovered",
    )
    running = recovered.poll_once(started["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert running["status"] == "waiting_solver"

    internal(backend).entry_by_job_id(started["backend_job_id"]).status = "completed_optimal"
    completed = recovered.poll_once(started["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert completed["status"] == "solver_completed"
    restored_attempt = reopened_store.get_attempt(started["attempt_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert restored_attempt["result"]["status"] == "optimal"


def test_cancelling_after_backend_completion_does_not_mutate_completed_attempt(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path)
    started = _start(orchestrator)
    internal(backend).entry_by_job_id(started["backend_job_id"]).status = "completed_optimal"
    completed = orchestrator.poll_once(started["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert completed["status"] == "solver_completed"

    cancellation = orchestrator.cancel_subscription(
        started["subscription_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID
    )
    assert cancellation["job_status"] == "cancelled"
    assert backend.cancel_calls == []
    attempt = store.get_attempt(started["attempt_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert attempt["status"] == "completed"


def test_recovery_scan_polls_persisted_nonterminal_jobs(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path)
    started = _start(orchestrator)
    internal(backend).entry_by_job_id(started["backend_job_id"]).status = "completed_optimal"

    recovered = DurableJobOrchestrator(
        store=SolverJobStore(store.database_path),
        backend=backend,
        idempotency_secret=synthetic_secret("solver_job_orchestrator-credential-01").encode("utf-8"),
        binding_revision_id=BINDING_REVISION,
        lease_owner="worker_recovered",
    )
    outcomes = recovered.recover_once(workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert outcomes[0]["job_id"] == started["job_id"]
    assert outcomes[0]["status"] == "solver_completed"


def test_generic_recovery_scans_all_frozen_bindings_and_one_bad_job_does_not_block_next(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path)
    first = _start(orchestrator, run_suffix="recovery_a", problem_marker="a")
    second = _start(
        orchestrator,
        run_suffix="recovery_b",
        attempt_id="attempt_synthetic_recovery_b",
        problem_marker="b",
    )
    backend.poll_failures.add(first["backend_job_id"])

    other_binding = "binding_revision_synthetic_2"
    store.register_binding_revision(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=other_binding,
        configuration={"backend": "synthetic", "revision": "synthetic-2"},
    )
    other_orchestrator = DurableJobOrchestrator(
        store=store,
        backend=backend,
        idempotency_secret=synthetic_secret("solver_job_orchestrator-credential-01").encode("utf-8"),
        binding_revision_id=other_binding,
        lease_owner="worker_other_binding",
    )
    other = _start(
        other_orchestrator,
        run_suffix="other_binding",
        attempt_id="attempt_synthetic_other_binding",
        problem_marker="c",
        binding_revision_id=other_binding,
    )
    backend.poll_calls.clear()

    recovered = DurableJobOrchestrator(
        store=SolverJobStore(store.database_path),
        backend=backend,
        idempotency_secret=synthetic_secret("solver_job_orchestrator-credential-01").encode("utf-8"),
        binding_revision_id=BINDING_REVISION,
        lease_owner="worker_recovered",
    )
    outcomes = recovered.recover_once(workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)

    assert {outcome["job_id"] for outcome in outcomes} == {
        first["job_id"],
        second["job_id"],
        other["job_id"],
    }
    assert other["backend_job_id"] in backend.poll_calls
    assert second["backend_job_id"] in backend.poll_calls
    assert next(outcome for outcome in outcomes if outcome["job_id"] == first["job_id"])["status"] == "waiting_solver"


def test_recovery_failure_cannot_finalize_with_a_stale_fencing_token(tmp_path):
    orchestrator, store, backend = _orchestrator(tmp_path)
    started = _start(orchestrator, run_suffix="stale_recovery_fence")

    def replace_worker_lease(_backend_job_id):
        backend.poll_hook = None
        current = store.get_job(started["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
        store.claim_job(
            started["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            lease_owner=current["lease_owner"],
            lease_seconds=30,
        )
        raise RuntimeError("synthetic stale worker failure")

    backend.poll_hook = replace_worker_lease
    outcomes = orchestrator.recover_once(workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    assert outcomes == ()
    assert store.get_job(started["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)["status"] == "waiting_solver"
