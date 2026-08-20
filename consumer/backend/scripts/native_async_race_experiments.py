"""Repeatable quantitative gates for terminal arbitration and SSE resources.

The defaults implement the frozen phase-2/phase-4 acceptance counts:

* cancel versus complete: 1,000 runs;
* shutdown versus complete: 1,000 runs;
* non-terminal events after a terminal seal: 10,000 attempts;
* lazy creation, early disconnect, and normal disconnect: 1,000 streams total.

All data is synthetic and in memory.  The script exits non-zero when any
terminal is overwritten, a run gets more than one terminal event, a late event
is written, or a supervised/subscriber resource remains live.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from agent.runtime.contracts import (
    PublicEventDraft,
    RunScope,
    RunSnapshot,
    RunStatus,
    RuntimeInvocationContext,
)
from agent.runtime.errors import RunNotFoundError
from agent.runtime.event_bus import AsyncEventBus, EventPublishRejectedError
from agent.runtime.manager import AsyncRunManager
from agent.runtime.store import InMemoryRunRepository, utc_now_iso


_SCOPE = RunScope(
    username="operator_a",
    workspace_id="workspace_demo",
    scopes=("agent:run", "agent:read", "agent:cancel"),
)


@dataclass
class _RaceFixture:
    manager: AsyncRunManager
    repository: InMemoryRunRepository
    event_bus: AsyncEventBus
    runtime: "_GatedCancelRuntime"
    run_ids: tuple[str, ...]


class _GatedCancelRuntime:
    """Hold cancel cleanup open while a terminal event races with it."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._release = asyncio.Event()
        self.cancel_calls: list[tuple[str, str]] = []

    async def cancel(self, run_id: str, reason: str) -> bool:
        async with self._condition:
            self.cancel_calls.append((str(run_id), str(reason)))
            self._condition.notify_all()
        await self._release.wait()
        return True

    async def wait_for_cancel_calls(self, expected: int, *, timeout: float) -> None:
        async def wait() -> None:
            async with self._condition:
                await self._condition.wait_for(
                    lambda: len(self.cancel_calls) >= expected
                )

        await asyncio.wait_for(wait(), timeout=timeout)

    def release(self) -> None:
        self._release.set()


def _context(run_id: str, session_id: str) -> RuntimeInvocationContext:
    return RuntimeInvocationContext(
        run_id=run_id,
        session_id=session_id,
        turn_id=1,
        workspace_id=_SCOPE.workspace_id,
        username=_SCOPE.username,
        query="synthetic race experiment",
    )


def _draft(
    run_id: str,
    session_id: str,
    event_type: str,
    *,
    status: str = "running",
    payload: dict[str, Any] | None = None,
) -> PublicEventDraft:
    return PublicEventDraft(
        schema_version="1.0",
        run_id=run_id,
        session_id=session_id,
        turn_id=1,
        event_type=event_type,
        status=status,
        source={"node": "synthetic", "component": "race_experiment"},
        payload=dict(payload or {}),
        created_at=utc_now_iso(),
    )


async def _seed_running_fixture(prefix: str, iterations: int) -> _RaceFixture:
    repository = InMemoryRunRepository()
    event_bus = AsyncEventBus(repository)
    runtime = _GatedCancelRuntime()
    manager = AsyncRunManager(
        runtime,
        repository=repository,
        event_bus=event_bus,
    )
    run_ids = tuple(f"{prefix}_{index:04d}" for index in range(iterations))
    for index, run_id in enumerate(run_ids):
        context = _context(run_id, f"session_{prefix}_{index:04d}")
        snapshot = RunSnapshot(
            run_id=run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            status=RunStatus.RUNNING,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            last_seq=0,
            scope=_SCOPE,
            context=context,
        )
        stored, created = await repository.create(snapshot)
        if not created or stored.run_id != run_id:
            raise RuntimeError(f"failed to seed synthetic run {run_id}")
    return _RaceFixture(manager, repository, event_bus, runtime, run_ids)


async def _emit_completed_terminal(
    fixture: _RaceFixture,
    run_id: str,
) -> None:
    snapshot = await fixture.repository.get(run_id)
    if snapshot is None:
        raise RunNotFoundError(run_id)
    await fixture.manager.accept_runtime_event(
        run_id,
        _draft(
            run_id,
            snapshot.session_id,
            "turn.finished",
            status="completed",
            payload={
                "status": "completed",
                "final_output": "synthetic completion",
            },
        ),
    )


async def _summarize_terminal_runs(
    fixture: _RaceFixture,
    *,
    expected_reason: str,
) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    terminal_events = 0
    reason_matches = 0
    runs_with_wrong_terminal_count = 0
    for run_id in fixture.run_ids:
        snapshot = await fixture.repository.get(run_id)
        if snapshot is None:
            raise RunNotFoundError(run_id)
        status_counts[snapshot.status.value] += 1
        events = await fixture.repository.list_after(run_id, 0)
        terminals = [
            event for event in events if event.event_type == "turn.finished"
        ]
        terminal_events += len(terminals)
        if len(terminals) != 1:
            runs_with_wrong_terminal_count += 1
        elif terminals[0].payload.get("cancel_reason") == expected_reason:
            reason_matches += 1
    return {
        "runs": len(fixture.run_ids),
        "status_counts": dict(sorted(status_counts.items())),
        "terminal_events": terminal_events,
        "runs_with_wrong_terminal_count": runs_with_wrong_terminal_count,
        "cancel_reason_matches": reason_matches,
        "complete_overwrites": status_counts.get(RunStatus.COMPLETED.value, 0),
    }


async def _cleanup_terminal_entries(fixture: _RaceFixture) -> None:
    for run_id in fixture.run_ids:
        await fixture.manager._cleanup_finished_run(run_id)


def _assert_race_summary(summary: dict[str, Any], iterations: int) -> None:
    if summary["status_counts"] != {RunStatus.CANCELLED.value: iterations}:
        raise RuntimeError(
            "terminal arbitration produced unexpected statuses: {}".format(
                summary["status_counts"]
            )
        )
    if summary["terminal_events"] != iterations:
        raise RuntimeError("terminal event count does not equal run count")
    if summary["runs_with_wrong_terminal_count"] != 0:
        raise RuntimeError("one or more runs did not have exactly one terminal")
    if summary["cancel_reason_matches"] != iterations:
        raise RuntimeError("terminal cancel reasons did not match registrations")


async def _run_cancel_complete(iterations: int) -> tuple[dict[str, Any], _RaceFixture]:
    started = time.perf_counter()
    fixture = await _seed_running_fixture("run_cancel_complete", iterations)
    cancel_tasks = [
        asyncio.create_task(
            fixture.manager.cancel_run(run_id, _SCOPE, reason="synthetic_cancel"),
            name=f"cancel-race-{run_id}",
        )
        for run_id in fixture.run_ids
    ]
    await fixture.runtime.wait_for_cancel_calls(iterations, timeout=30.0)
    await asyncio.gather(
        *(_emit_completed_terminal(fixture, run_id) for run_id in fixture.run_ids)
    )
    fixture.runtime.release()
    await asyncio.gather(*cancel_tasks)

    summary = await _summarize_terminal_runs(
        fixture,
        expected_reason="synthetic_cancel",
    )
    _assert_race_summary(summary, iterations)
    await _cleanup_terminal_entries(fixture)
    await fixture.manager.supervisor.close(
        deadline=asyncio.get_running_loop().time() + 5.0
    )
    summary.update(
        {
            "cancel_calls": len(fixture.runtime.cancel_calls),
            "manager_entries_after_cleanup": len(fixture.manager._entries),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    )
    return summary, fixture


async def _run_shutdown_complete(
    iterations: int,
) -> tuple[dict[str, Any], _RaceFixture]:
    started = time.perf_counter()
    fixture = await _seed_running_fixture("run_shutdown_complete", iterations)
    shutdown_task = asyncio.create_task(
        fixture.manager.shutdown(
            deadline=asyncio.get_running_loop().time() + 30.0
        ),
        name="shutdown-complete-race",
    )
    await fixture.runtime.wait_for_cancel_calls(iterations, timeout=30.0)
    await asyncio.gather(
        *(_emit_completed_terminal(fixture, run_id) for run_id in fixture.run_ids)
    )
    fixture.runtime.release()
    shutdown_complete = await shutdown_task

    summary = await _summarize_terminal_runs(
        fixture,
        expected_reason="service_shutdown",
    )
    _assert_race_summary(summary, iterations)
    if not shutdown_complete:
        raise RuntimeError("run manager shutdown did not complete")
    await _cleanup_terminal_entries(fixture)
    summary.update(
        {
            "cancel_calls": len(fixture.runtime.cancel_calls),
            "shutdown_complete": shutdown_complete,
            "manager_entries_after_cleanup": len(fixture.manager._entries),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    )
    return summary, fixture


async def _run_late_event_gate(
    fixture: _RaceFixture,
    attempts: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    before_writes = sum(
        snapshot.last_seq
        for snapshot in await fixture.repository.list_snapshots()
    )
    rejected = 0
    for index in range(attempts):
        run_id = fixture.run_ids[index % len(fixture.run_ids)]
        snapshot = await fixture.repository.get(run_id)
        if snapshot is None:
            raise RunNotFoundError(run_id)
        try:
            await fixture.event_bus.publish(
                _draft(
                    run_id,
                    snapshot.session_id,
                    "token.delta",
                    payload={"channel": "content", "text": "late"},
                )
            )
        except EventPublishRejectedError as exc:
            if exc.duplicate_terminal:
                raise RuntimeError("late non-terminal was classified as duplicate terminal")
            rejected += 1
    after_writes = sum(
        snapshot.last_seq
        for snapshot in await fixture.repository.list_snapshots()
    )
    writes = after_writes - before_writes
    metrics = fixture.event_bus.metrics()
    if rejected != attempts or writes != 0:
        raise RuntimeError(
            f"late event seal failed: rejected={rejected}, writes={writes}"
        )
    if metrics["late_event_rejections"] != attempts:
        raise RuntimeError("late event rejection metric does not match attempts")
    return {
        "attempts": attempts,
        "rejected": rejected,
        "writes": writes,
        "late_event_rejection_metric": metrics["late_event_rejections"],
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


async def _wait_for_active_subscriber(
    event_bus: AsyncEventBus,
    expected: int,
) -> None:
    for _ in range(1000):
        if event_bus.active_subscriber_count == expected:
            return
        await asyncio.sleep(0)
    raise RuntimeError("subscriber activation did not reach the expected count")


async def _run_sse_lifecycle(iterations: int) -> dict[str, Any]:
    started = time.perf_counter()
    repository = InMemoryRunRepository()
    event_bus = AsyncEventBus(repository)
    run_id = "run_sse_lifecycle"
    context = _context(run_id, "session_sse_lifecycle")
    await repository.create(
        RunSnapshot(
            run_id=run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            status=RunStatus.RUNNING,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            last_seq=0,
            scope=_SCOPE,
            context=context,
        )
    )

    created_only = (iterations + 2) // 3
    early_disconnects = (iterations + 1) // 3
    normal_disconnects = iterations - created_only - early_disconnects

    for _ in range(created_only):
        snapshot = await repository.get(run_id)
        if snapshot is None:
            raise RunNotFoundError(run_id)
        reservation = await event_bus.reserve_subscriber(run_id)
        stream = event_bus.stream(
            run_id,
            after_seq=snapshot.last_seq,
            reservation_id=reservation,
        )
        await stream.aclose()
        await event_bus.release_subscriber_reservation(reservation)

    for index in range(early_disconnects):
        snapshot = await repository.get(run_id)
        if snapshot is None:
            raise RunNotFoundError(run_id)
        reservation = await event_bus.reserve_subscriber(run_id)
        stream = event_bus.stream(
            run_id,
            after_seq=snapshot.last_seq,
            reservation_id=reservation,
        )
        pending = asyncio.create_task(
            anext(stream),
            name=f"sse-early-disconnect-{index}",
        )
        await _wait_for_active_subscriber(event_bus, 1)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()

    for index in range(normal_disconnects):
        snapshot = await repository.get(run_id)
        if snapshot is None:
            raise RunNotFoundError(run_id)
        reservation = await event_bus.reserve_subscriber(run_id)
        stream = event_bus.stream(
            run_id,
            after_seq=snapshot.last_seq,
            reservation_id=reservation,
        )
        pending = asyncio.create_task(
            anext(stream),
            name=f"sse-normal-disconnect-{index}",
        )
        await _wait_for_active_subscriber(event_bus, 1)
        published, _updated = await event_bus.publish(
            _draft(
                run_id,
                context.session_id,
                "token.delta",
                payload={"channel": "content", "text": "synthetic"},
            )
        )
        received = await asyncio.wait_for(pending, timeout=5.0)
        if received.seq != published.seq:
            raise RuntimeError("normal SSE disconnect received the wrong event")
        await stream.aclose()

    metrics = await event_bus.metrics_snapshot()
    if metrics["active_subscribers"] != 0:
        raise RuntimeError("SSE experiment leaked active subscribers")
    if metrics["reserved_subscribers"] != 0:
        raise RuntimeError("SSE experiment leaked subscriber reservations")
    await event_bus.close()
    return {
        "total": iterations,
        "created_only": created_only,
        "early_disconnects": early_disconnects,
        "normal_disconnects": normal_disconnects,
        "active_subscribers": metrics["active_subscribers"],
        "reserved_subscribers": metrics["reserved_subscribers"],
        "slow_subscriber_disconnects": metrics["slow_subscriber_disconnects"],
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


async def run_experiments(
    *,
    cancel_iterations: int,
    shutdown_iterations: int,
    late_event_attempts: int,
    sse_iterations: int,
) -> dict[str, Any]:
    if min(
        cancel_iterations,
        shutdown_iterations,
        late_event_attempts,
        sse_iterations,
    ) <= 0:
        raise ValueError("all experiment counts must be positive")

    overall_started = time.perf_counter()
    cancel_summary, cancel_fixture = await _run_cancel_complete(
        cancel_iterations
    )
    late_summary = await _run_late_event_gate(
        cancel_fixture,
        late_event_attempts,
    )
    shutdown_summary, shutdown_fixture = await _run_shutdown_complete(
        shutdown_iterations
    )
    sse_summary = await _run_sse_lifecycle(sse_iterations)

    await cancel_fixture.event_bus.close()
    await shutdown_fixture.event_bus.close()
    await asyncio.sleep(0)
    current = asyncio.current_task()
    pending_tasks = [
        task
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    ]
    resources = {
        "cancel_supervisor_run_tasks": len(
            cancel_fixture.manager.supervisor.active_run_ids
        ),
        "cancel_supervisor_derived_tasks": (
            cancel_fixture.manager.supervisor.active_derived_count
        ),
        "shutdown_supervisor_run_tasks": len(
            shutdown_fixture.manager.supervisor.active_run_ids
        ),
        "shutdown_supervisor_derived_tasks": (
            shutdown_fixture.manager.supervisor.active_derived_count
        ),
        "cancel_event_bus_subscribers": (
            cancel_fixture.event_bus.active_subscriber_count
        ),
        "cancel_event_bus_reservations": (
            cancel_fixture.event_bus.reserved_subscriber_count
        ),
        "cancel_event_bus_lanes": cancel_fixture.event_bus.metrics()["lanes"],
        "shutdown_event_bus_subscribers": (
            shutdown_fixture.event_bus.active_subscriber_count
        ),
        "shutdown_event_bus_reservations": (
            shutdown_fixture.event_bus.reserved_subscriber_count
        ),
        "shutdown_event_bus_lanes": shutdown_fixture.event_bus.metrics()["lanes"],
        "pending_asyncio_tasks": len(pending_tasks),
    }
    if any(resources.values()):
        raise RuntimeError(f"runtime resources did not return to zero: {resources}")

    return {
        "passed": True,
        "cancel_complete": cancel_summary,
        "shutdown_complete": shutdown_summary,
        "late_events": late_summary,
        "sse_lifecycle": sse_summary,
        "resources": resources,
        "elapsed_ms": int((time.perf_counter() - overall_started) * 1000),
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cancel-iterations", type=_positive_int, default=1000)
    parser.add_argument("--shutdown-iterations", type=_positive_int, default=1000)
    parser.add_argument("--late-event-attempts", type=_positive_int, default=10000)
    parser.add_argument("--sse-iterations", type=_positive_int, default=1000)
    args = parser.parse_args()
    try:
        result = asyncio.run(
            run_experiments(
                cancel_iterations=args.cancel_iterations,
                shutdown_iterations=args.shutdown_iterations,
                late_event_attempts=args.late_event_attempts,
                sse_iterations=args.sse_iterations,
            )
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "passed": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
