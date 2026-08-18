import asyncio
import queue
from types import SimpleNamespace

import pytest

from agent.runtime.contracts import (
    PublicEvent,
    PublicEventDraft,
    RunScope,
    RunSnapshot,
    RunStatus,
    RuntimeInvocationContext,
    RuntimeResult,
)
from agent.runtime.errors import (
    DuplicateInteractionError,
    RunConflictError,
    RunNotFoundError,
)
from agent.runtime.manager import AsyncRunManager
from agent.runtime.manager import request_fingerprint
from agent.runtime.jiuwen import JiuwenAsyncAgentRuntime
from agent.runtime.service import AsyncExecutionService
from agent.runtime.service import OrchestratorSessionAdapter
from agent.runtime.adapter import PublicEventAdapter
from agent.runtime.events import RuntimeEvent
from agent.runtime.bridge import legacy_public_event_envelope, wait_for_run
from agent.runtime.event_bus import (
    AsyncEventBus,
    BlockingSubscription,
    STREAM_CLOSED,
    STREAM_RECONNECT,
)
from agent.runtime.store import InMemoryRunRepository
from agent.runtime.store import utc_now_iso
from persistence.chat_store import ChatStore
from persistence.execution_store import ExecutionStore


def _context(run_id="run_demo"):
    return RuntimeInvocationContext(
        run_id=run_id,
        session_id="session_demo",
        turn_id=1,
        workspace_id="workspace_demo",
        username="operator_a",
        query="synthetic question",
    )


def _scope():
    return RunScope(
        "operator_a",
        "workspace_demo",
        ("agent:run", "agent:read", "agent:cancel", "agent:interact"),
    )


def test_request_fingerprint_covers_scope_but_ignores_allocated_run_id():
    base = _context("run_fingerprint_a")
    assert request_fingerprint(base) == request_fingerprint(
        base.model_copy(update={"run_id": "run_fingerprint_b"})
    )
    assert request_fingerprint(base) != request_fingerprint(
        base.model_copy(update={"workspace_id": "workspace_other"})
    )
    assert request_fingerprint(base) != request_fingerprint(
        base.model_copy(update={"supports_user_interaction": False})
    )


def test_replace_context_keeps_snapshot_turn_id_in_sync():
    async def run_test():
        repository = InMemoryRunRepository()
        initial = _context("run_turn_sync").model_copy(update={"turn_id": 1})
        snapshot = RunSnapshot(
            run_id=initial.run_id,
            session_id=initial.session_id,
            turn_id=initial.turn_id,
            status=RunStatus.QUEUED,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            last_seq=0,
            scope=_scope(),
            context=initial,
        )
        await repository.create(snapshot)

        prepared = initial.model_copy(update={"turn_id": 3})
        updated = await repository.replace_context(
            initial.run_id,
            prepared,
            expected={RunStatus.QUEUED},
        )

        assert updated.turn_id == 3
        assert updated.context.turn_id == 3

    asyncio.run(run_test())


def test_run_started_uses_turn_allocated_during_context_preparation():
    class _PreparedTurnRuntime(_SuccessRuntime):
        async def prepare_context(self, context, *, resumed=False):
            assert resumed is False
            return context.model_copy(update={"turn_id": 3})

    async def run_test():
        manager = AsyncRunManager(_PreparedTurnRuntime())
        await manager.create_run(_context("run_prepared_turn"), _scope())

        completed = await _wait_for_status(
            manager,
            "run_prepared_turn",
            RunStatus.COMPLETED,
        )
        events = await manager.repository.list_after("run_prepared_turn", 0)

        assert completed.turn_id == 3
        assert completed.context.turn_id == 3
        assert [event.event_type for event in events] == [
            "run.started",
            "turn.started",
            "turn.finished",
        ]
        assert {event.turn_id for event in events} == {3}

    asyncio.run(run_test())


def test_public_terminal_event_preserves_legacy_stream_shape():
    event = PublicEvent(
        schema_version="1.0",
        event_id="event_legacy_demo",
        run_id="run_legacy_demo",
        session_id="session_demo",
        turn_id=1,
        seq=1,
        event_type="turn.finished",
        status="completed",
        source={"node": "synthetic", "component": "test"},
        payload={
            "status": "completed",
            "content": "synthetic answer",
        },
        created_at=utc_now_iso(),
    )

    envelope = legacy_public_event_envelope(event)

    assert envelope["protocol"] == "agent-stream.v2"
    assert envelope["payload"]["status"] == "success"
    assert envelope["payload"]["final_output"] == "synthetic answer"


def _draft(context, event_type, *, status="running", payload=None):
    return PublicEventDraft(
        schema_version="1.0",
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        event_type=event_type,
        status=status,
        source={"node": "synthetic", "component": "test"},
        payload=dict(payload or {}),
        created_at=utc_now_iso(),
    )


class _SuccessRuntime:
    async def execute(self, context, sink):
        await sink.emit(_draft(context, "turn.started"))
        await sink.emit(_draft(
            context,
            "turn.finished",
            status="completed",
            payload={"status": "completed", "final_output": "synthetic answer"},
        ))
        return RuntimeResult(status="completed", content="synthetic answer")

    async def cancel(self, run_id, reason):
        del run_id, reason
        return True

    async def interact(self, context, sink, interaction_response):
        del interaction_response
        return await self.execute(context, sink)


class _BlockingRuntime:
    def __init__(self):
        self.started = asyncio.Event()
        self.released = asyncio.Event()
        self.cancelled = []

    async def execute(self, context, sink):
        del sink
        self.started.set()
        await self.released.wait()
        return RuntimeResult(status="completed", content="synthetic answer")

    async def cancel(self, run_id, reason):
        self.cancelled.append((run_id, reason))
        self.released.set()
        return True

    async def interact(self, context, sink, interaction_response):
        del interaction_response
        return await self.execute(context, sink)


class _InteractionRuntime:
    async def execute(self, context, sink):
        interaction = {
            "id": "interaction_demo",
            "type": "single_choice",
            "prompt": "Choose a synthetic option",
            "options": [
                {"id": "option_a", "label": "Option A", "value": "value_a"},
            ],
        }
        await sink.emit(_draft(
            context,
            "turn.finished",
            status="awaiting_input",
            payload={
                "status": "awaiting_input",
                "final_output": {
                    "type": "user_interaction",
                    "text": interaction["prompt"],
                    "interaction": interaction,
                    "options": interaction["options"],
                },
            },
        ))
        return RuntimeResult(status="awaiting_input")

    async def cancel(self, run_id, reason):
        del run_id, reason
        return True

    async def interact(self, context, sink, interaction_response):
        assert interaction_response["option_id"] == "option_a"
        await sink.emit(_draft(
            context,
            "turn.finished",
            status="completed",
            payload={"status": "completed", "final_output": "resumed answer"},
        ))
        return RuntimeResult(status="completed", content="resumed answer")


async def _wait_for_status(manager, run_id, status, timeout=1.0):
    async def wait_loop():
        while True:
            snapshot = await manager.get_run(run_id, _scope())
            if snapshot is not None and snapshot.status == status:
                return snapshot
            await asyncio.sleep(0.005)

    return await asyncio.wait_for(wait_loop(), timeout=timeout)


def test_run_persists_events_before_replay_and_finishes_once():
    async def run_test():
        manager = AsyncRunManager(_SuccessRuntime())
        snapshot, created = await manager.create_run(_context(), _scope())
        assert created is True
        assert snapshot.status == RunStatus.QUEUED

        completed = await _wait_for_status(manager, "run_demo", RunStatus.COMPLETED)
        subscription = await manager.subscribe("run_demo", _scope(), after_seq=0)
        try:
            assert [event.event_type for event in subscription.replay] == [
                "run.started",
                "turn.started",
                "turn.finished",
            ]
            assert [event.seq for event in subscription.replay] == [1, 2, 3]
            assert completed.last_seq == 3
            assert subscription.closed is True
        finally:
            await manager.unsubscribe(subscription)
    asyncio.run(run_test())


def test_concurrent_publish_persists_and_broadcasts_in_sequence_order():
    async def run_test():
        repository = InMemoryRunRepository()
        context = _context("run_ordered_publish")
        await repository.create(RunSnapshot(
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            status=RunStatus.RUNNING,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            last_seq=0,
            scope=_scope(),
            context=context,
        ))
        persisted = []

        async def persist(event):
            if event.seq == 1:
                await asyncio.sleep(0.02)
            persisted.append(event.seq)

        event_bus = AsyncEventBus(repository, event_persistor=persist)
        subscription = await event_bus.subscribe(context.run_id)
        try:
            first = _draft(context, "turn.started")
            second = _draft(
                context,
                "token.delta",
                payload={"channel": "content", "text": "synthetic"},
            )
            await asyncio.gather(
                event_bus.publish(first),
                event_bus.publish(second),
            )
            broadcast = [
                subscription.queue.get_nowait().seq,
                subscription.queue.get_nowait().seq,
            ]
            assert persisted == [1, 2]
            assert broadcast == [1, 2]
        finally:
            await event_bus.unsubscribe(subscription)

    asyncio.run(run_test())


def test_terminal_close_preserves_already_queued_live_events():
    async def run_test():
        class _GatedRuntime:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def execute(self, context, sink):
                self.started.set()
                await self.release.wait()
                await sink.emit(_draft(context, "turn.started"))
                await sink.emit(_draft(
                    context,
                    "token.delta",
                    payload={"channel": "content", "text": "synthetic answer"},
                ))
                await sink.emit(_draft(
                    context,
                    "turn.finished",
                    status="completed",
                    payload={"status": "completed", "content": "synthetic answer"},
                ))
                return RuntimeResult(status="completed", content="synthetic answer")

            async def cancel(self, run_id, reason):
                del run_id, reason
                self.release.set()
                return True

        runtime = _GatedRuntime()
        manager = AsyncRunManager(runtime)
        await manager.create_run(_context("run_live_queue"), _scope())
        await runtime.started.wait()
        subscription = await manager.subscribe("run_live_queue", _scope())
        runtime.release.set()
        await _wait_for_status(manager, "run_live_queue", RunStatus.COMPLETED)

        event_types = []
        while True:
            item = subscription.queue.get_nowait()
            if item is STREAM_CLOSED:
                break
            event_types.append(item.event_type)
            if item.event_type == "turn.finished":
                break
        assert event_types == ["turn.started", "token.delta", "turn.finished"]
        await manager.unsubscribe(subscription)

    asyncio.run(run_test())


def test_terminal_close_does_not_require_evicted_event_history():
    async def run_test():
        repository = InMemoryRunRepository(max_events_per_run=32)
        context = _context("run_retained_terminal")
        await repository.create(RunSnapshot(
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            status=RunStatus.RUNNING,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            last_seq=0,
            scope=_scope(),
            context=context,
        ))
        event_bus = AsyncEventBus(repository, subscriber_queue_size=64)
        subscription = await event_bus.subscribe(context.run_id)
        try:
            for index in range(32):
                await event_bus.publish(_draft(
                    context,
                    "token.delta",
                    payload={"channel": "content", "text": str(index)},
                ))
            await event_bus.publish(_draft(
                context,
                "turn.finished",
                status="completed",
                payload={"status": "completed", "content": "synthetic"},
            ))

            await event_bus.close(context.run_id)
            queued = []
            while not subscription.queue.empty():
                queued.append(subscription.queue.get_nowait())
            assert queued[-1] is STREAM_CLOSED
            assert queued[-2].event_type == "turn.finished"
        finally:
            await event_bus.unsubscribe(subscription)

    asyncio.run(run_test())


def test_finished_run_releases_manager_lock_registry():
    async def run_test():
        manager = AsyncRunManager(_SuccessRuntime())
        await manager.create_run(_context("run_cleanup"), _scope())
        await _wait_for_status(manager, "run_cleanup", RunStatus.COMPLETED)
        for _ in range(20):
            if "run_cleanup" not in manager._run_locks:
                break
            await asyncio.sleep(0)
        assert "run_cleanup" not in manager._run_locks

    asyncio.run(run_test())


def test_unknown_run_operations_do_not_allocate_lock_entries():
    async def run_test():
        manager = AsyncRunManager(_SuccessRuntime())
        with pytest.raises(RunNotFoundError):
            await manager.cancel_run("run_missing", _scope())
        with pytest.raises(RunNotFoundError):
            await manager.interact(
                "run_missing",
                _scope(),
                interaction_id="interaction_missing",
                value="synthetic value",
            )
        assert manager._run_locks == {}

    asyncio.run(run_test())


def test_cancel_is_idempotent_and_uses_runtime_boundary():
    async def run_test():
        runtime = _BlockingRuntime()
        manager = AsyncRunManager(runtime)
        await manager.create_run(_context(), _scope())
        await asyncio.wait_for(runtime.started.wait(), timeout=1.0)

        first = await manager.cancel_run("run_demo", _scope(), reason="user_requested")
        second = await manager.cancel_run("run_demo", _scope(), reason="user_requested")

        assert first.status == RunStatus.CANCELLED
        assert second.status == RunStatus.CANCELLED
        assert first.cancel_reason == "user_requested"
        assert runtime.cancelled == [("run_demo", "user_requested")]
        events = await manager.repository.list_after("run_demo", 0)
        terminal_events = [
            event for event in events if event.event_type == "turn.finished"
        ]
        assert len(terminal_events) == 1
        assert terminal_events[0].payload["cancel_reason"] == "user_requested"
    asyncio.run(run_test())


def test_blocking_wait_resubscribes_after_slow_consumer_reconnect():
    context = _context("run_wait_reconnect")
    scope = _scope()
    started = PublicEvent(
        schema_version="1.0",
        event_id="run_wait_reconnect:1",
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=1,
        seq=1,
        event_type="turn.started",
        status="running",
        source={"node": "synthetic", "component": "test"},
        payload={},
        created_at=utc_now_iso(),
    )
    terminal = PublicEvent(
        schema_version="1.0",
        event_id="run_wait_reconnect:2",
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=1,
        seq=2,
        event_type="turn.finished",
        status="completed",
        source={"node": "synthetic", "component": "test"},
        payload={"status": "completed", "content": "synthetic answer"},
        created_at=utc_now_iso(),
    )
    completed = RunSnapshot(
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=1,
        status=RunStatus.COMPLETED,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
        last_seq=2,
        scope=scope,
        context=context,
        result=RuntimeResult(status="completed", content="synthetic answer"),
    )

    class _ReconnectManager:
        def __init__(self):
            self.subscribe_cursors = []
            self.unsubscribed = []

        async def subscribe(self, run_id, requested_scope, *, after_seq=0):
            assert run_id == context.run_id
            assert requested_scope == scope
            self.subscribe_cursors.append(after_seq)
            if len(self.subscribe_cursors) == 1:
                live_queue = queue.Queue()
                live_queue.put_nowait(started)
                live_queue.put_nowait(STREAM_RECONNECT)
                return BlockingSubscription(
                    run_id=run_id,
                    subscriber_id="first",
                    queue=live_queue,
                    replay=[],
                    closed=False,
                )
            return BlockingSubscription(
                run_id=run_id,
                subscriber_id="second",
                queue=queue.Queue(),
                replay=[terminal],
                closed=True,
            )

        async def get_run(self, run_id, requested_scope):
            assert run_id == context.run_id
            assert requested_scope == scope
            return completed

        async def unsubscribe(self, subscription):
            self.unsubscribed.append(subscription.subscriber_id)

    manager = _ReconnectManager()
    observed = []
    result = wait_for_run(
        manager,
        asyncio.run,
        context.run_id,
        scope,
        timeout_seconds=1,
        on_event=lambda event: observed.append(event.seq),
    )

    assert result is completed
    assert manager.subscribe_cursors == [0, 1]
    assert manager.unsubscribed == ["first", "second"]
    assert observed == [1, 2]


def test_hitl_resumes_same_public_run_and_rejects_duplicate_submission():
    async def run_test():
        manager = AsyncRunManager(_InteractionRuntime())
        await manager.create_run(_context(), _scope())
        waiting = await _wait_for_status(manager, "run_demo", RunStatus.AWAITING_INPUT)
        assert waiting.pending_interaction["id"] == "interaction_demo"

        resumed = await manager.interact(
            "run_demo",
            _scope(),
            interaction_id="interaction_demo",
            option_id="option_a",
            value="value_a",
        )
        assert resumed.run_id == "run_demo"
        await _wait_for_status(manager, "run_demo", RunStatus.COMPLETED)

        with pytest.raises(DuplicateInteractionError):
            await manager.interact(
                "run_demo",
                _scope(),
                interaction_id="interaction_demo",
                option_id="option_a",
                value="value_a",
            )
    asyncio.run(run_test())


def test_shutdown_cancels_awaiting_input_without_an_active_task():
    async def run_test():
        manager = AsyncRunManager(_InteractionRuntime())
        await manager.create_run(_context("run_shutdown_hitl"), _scope())
        waiting = await _wait_for_status(
            manager,
            "run_shutdown_hitl",
            RunStatus.AWAITING_INPUT,
        )
        assert waiting.status == RunStatus.AWAITING_INPUT

        await manager.shutdown()

        cancelled = await manager.get_run("run_shutdown_hitl", _scope())
        assert cancelled is not None
        assert cancelled.status == RunStatus.CANCELLED
        terminal_events = [
            event
            for event in await manager.repository.list_after(
                "run_shutdown_hitl",
                0,
            )
            if event.event_type == "turn.finished"
        ]
        assert [event.status for event in terminal_events] == [
            "awaiting_input",
            "cancelled",
        ]

    asyncio.run(run_test())


def test_shutdown_uses_internal_cancel_capability_for_run_only_scope():
    async def run_test():
        runtime = _BlockingRuntime()
        manager = AsyncRunManager(runtime)
        run_only_scope = RunScope(
            "operator_a",
            "workspace_demo",
            ("agent:run",),
        )
        await manager.create_run(
            _context("run_shutdown_run_scope"),
            run_only_scope,
        )
        await asyncio.wait_for(runtime.started.wait(), timeout=1.0)

        await manager.shutdown()

        cancelled = await manager.repository.get("run_shutdown_run_scope")
        assert cancelled is not None
        assert cancelled.status == RunStatus.CANCELLED
        assert cancelled.cancel_reason == "service_shutdown"
        assert runtime.cancelled == [
            ("run_shutdown_run_scope", "service_shutdown"),
        ]

    asyncio.run(run_test())


def test_idempotency_key_returns_existing_run_and_rejects_changed_request():
    async def run_test():
        manager = AsyncRunManager(_BlockingRuntime())
        first, first_created = await manager.create_run(
            _context("run_first"),
            _scope(),
            idempotency_key="request_demo",
        )
        second, second_created = await manager.create_run(
            _context("run_second"),
            _scope(),
            idempotency_key="request_demo",
        )
        assert first_created is True
        assert second_created is False
        assert second.run_id == first.run_id
        await manager.cancel_run(first.run_id, _scope(), reason="test_cleanup")
    asyncio.run(run_test())


def test_admission_timeout_finishes_queued_run_without_calling_runtime_cancel():
    async def run_test():
        class _AdmissionRuntime(_BlockingRuntime):
            pass

        runtime = _AdmissionRuntime()
        manager = AsyncRunManager(runtime)
        first_context = _context("run_admission_first")
        second_context = RuntimeInvocationContext(
            run_id="run_admission_second",
            session_id="session_demo",
            turn_id=1,
            workspace_id="workspace_demo",
            username="operator_a",
            query="second synthetic question",
            concurrency_limit=1,
            admission_timeout_seconds=0.02,
        )
        await manager.create_run(first_context, _scope())
        await asyncio.wait_for(runtime.started.wait(), timeout=1.0)
        await manager.create_run(second_context, _scope())

        failed = await _wait_for_status(
            manager,
            "run_admission_second",
            RunStatus.FAILED,
        )
        assert failed.error is not None
        assert failed.error.error_code == "RUN_ADMISSION_TIMEOUT"
        assert runtime.cancelled == []

        await manager.cancel_run("run_admission_first", _scope(), reason="test_cleanup")
    asyncio.run(run_test())


def test_queued_cancel_preserves_caller_reason_without_runtime_cancel():
    async def run_test():
        runtime = _BlockingRuntime()
        manager = AsyncRunManager(runtime)
        await manager.create_run(_context("run_cancel_active"), _scope())
        await asyncio.wait_for(runtime.started.wait(), timeout=1.0)
        await manager.create_run(
            _context("run_cancel_queued"),
            _scope(),
        )

        cancelled = await manager.cancel_run(
            "run_cancel_queued",
            _scope(),
            reason="user_requested",
        )

        assert cancelled.status == RunStatus.CANCELLED
        assert cancelled.cancel_reason == "user_requested"
        assert runtime.cancelled == []
        await manager.cancel_run(
            "run_cancel_active",
            _scope(),
            reason="test_cleanup",
        )

    asyncio.run(run_test())


def test_runtime_interaction_request_is_materialized_as_awaiting_terminal_event():
    async def run_test():
        class _RequestedRuntime:
            async def execute(self, context, sink):
                await sink.emit(_draft(
                    context,
                    "interaction.requested",
                    payload={
                        "interaction_id": "interaction_requested",
                        "type": "input",
                        "prompt": "Provide synthetic input",
                    },
                ))
                return RuntimeResult(status="awaiting_input")

            async def cancel(self, run_id, reason):
                del run_id, reason
                return True

            async def interact(self, context, sink, interaction_response):
                del interaction_response
                await sink.emit(_draft(
                    context,
                    "turn.finished",
                    status="completed",
                    payload={"status": "completed", "final_output": "resumed"},
                ))
                return RuntimeResult(status="completed", content="resumed")

        manager = AsyncRunManager(_RequestedRuntime())
        await manager.create_run(_context("run_requested"), _scope())
        waiting = await _wait_for_status(manager, "run_requested", RunStatus.AWAITING_INPUT)
        assert waiting.pending_interaction["id"] == "interaction_requested"
        events = await manager.repository.list_after("run_requested", 0)
        assert [event.event_type for event in events].count("turn.finished") == 1
    asyncio.run(run_test())


def test_run_id_collision_rejects_different_request_or_owner():
    async def run_test():
        manager = AsyncRunManager(_BlockingRuntime())
        await manager.create_run(_context("run_collision"), _scope())

        changed = RuntimeInvocationContext(
            run_id="run_collision",
            session_id="session_demo",
            turn_id=1,
            workspace_id="workspace_demo",
            username="operator_a",
            query="different synthetic question",
        )
        with pytest.raises(RunConflictError):
            await manager.create_run(changed, _scope())

        other_scope = RunScope(
            "operator_b",
            "workspace_demo",
            ("agent:run", "agent:read", "agent:cancel"),
        )
        other_context = RuntimeInvocationContext(
            run_id="run_collision",
            session_id="session_demo",
            turn_id=1,
            workspace_id="workspace_demo",
            username="operator_b",
            query="synthetic question",
        )
        with pytest.raises(RunConflictError):
            await manager.create_run(other_context, other_scope)

        await manager.cancel_run("run_collision", _scope(), reason="test_cleanup")
    asyncio.run(run_test())


def test_public_adapter_projects_tool_input_to_public_preview():
    context = _context("run_public_tool")
    draft = PublicEventAdapter().adapt(RuntimeEvent(
        event_type="tool.started",
        status="running",
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=1,
        payload={
            "tool_name": "synthetic_lookup",
            "tool_call_id": "tool_demo",
            "input": {"query": "private synthetic query"},
            "status": "running",
        },
        source={"node": "synthetic", "component": "tool"},
    )
    )

    assert draft is not None
    assert "input" not in draft.payload
    assert draft.payload == {
        "name": "synthetic_lookup",
        "tool_call_id": "tool_demo",
        "input_preview": {"query": "private synthetic query"},
    }


def test_runtime_terminal_status_is_preserved():
    async def run_test():
        class _LegacyStatusRuntime:
            async def execute(self, context, sink):
                await sink.emit(_draft(
                    context,
                    "turn.finished",
                    status="completed",
                    payload={"status": "completed", "final_output": "synthetic answer"},
                ))
                return RuntimeResult(status="completed", content="synthetic answer")

            async def cancel(self, run_id, reason):
                del run_id, reason
                return True

        manager = AsyncRunManager(_LegacyStatusRuntime())
        await manager.create_run(_context("run_legacy_status"), _scope())
        completed = await _wait_for_status(
            manager,
            "run_legacy_status",
            RunStatus.COMPLETED,
        )
        assert completed.result is not None
        assert completed.result.status == "completed"
        events = await manager.repository.list_after("run_legacy_status", 0)
        assert events[-1].status == "completed"
        assert events[-1].payload["status"] == "completed"

    asyncio.run(run_test())


def test_timeout_finishes_once_even_when_runtime_cancel_fails():
    async def run_test():
        class _CancelFailureRuntime:
            async def execute(self, context, sink):
                del context, sink
                await asyncio.Event().wait()

            async def cancel(self, run_id, reason):
                del run_id, reason
                raise RuntimeError("synthetic cleanup failure")

        manager = AsyncRunManager(_CancelFailureRuntime())
        context = RuntimeInvocationContext(
            run_id="run_timeout",
            session_id="session_demo",
            turn_id=1,
            workspace_id="workspace_demo",
            username="operator_a",
            query="synthetic timeout question",
            timeout_seconds=0.01,
        )
        await manager.create_run(context, _scope())
        failed = await _wait_for_status(manager, "run_timeout", RunStatus.FAILED)
        assert failed.error is not None
        assert failed.error.error_code == "MODEL_TIMEOUT"
        events = await manager.repository.list_after("run_timeout", 0)
        assert [event.event_type for event in events].count("turn.finished") == 1

    asyncio.run(run_test())


def test_mixed_admission_limits_do_not_bypass_stricter_active_run():
    async def run_test():
        class _PerRunRuntime:
            def __init__(self):
                self.started = []
                self.release = {}

            async def execute(self, context, sink):
                del sink
                self.started.append(context.run_id)
                gate = self.release.setdefault(context.run_id, asyncio.Event())
                await gate.wait()
                return RuntimeResult(status="completed", content="synthetic answer")

            async def cancel(self, run_id, reason):
                del reason
                self.release.setdefault(run_id, asyncio.Event()).set()
                return True

        runtime = _PerRunRuntime()
        manager = AsyncRunManager(runtime)
        strict = RuntimeInvocationContext(
            **{
                **_context("run_strict").__dict__,
                "concurrency_limit": 1,
            }
        )
        relaxed = RuntimeInvocationContext(
            **{
                **_context("run_relaxed").__dict__,
                "query": "second synthetic question",
                "concurrency_limit": 100,
            }
        )
        await manager.create_run(strict, _scope())
        while "run_strict" not in runtime.started:
            await asyncio.sleep(0.005)
        await manager.create_run(relaxed, _scope())
        await asyncio.sleep(0.02)
        assert runtime.started == ["run_strict"]

        runtime.release["run_strict"].set()
        await _wait_for_status(manager, "run_strict", RunStatus.COMPLETED)
        while "run_relaxed" not in runtime.started:
            await asyncio.sleep(0.005)
        runtime.release["run_relaxed"].set()
        await _wait_for_status(manager, "run_relaxed", RunStatus.COMPLETED)

    asyncio.run(run_test())


def test_replay_retention_and_slow_subscriber_are_isolated():
    async def run_test():
        repository = InMemoryRunRepository(max_events_per_run=32)
        event_bus = AsyncEventBus(repository, subscriber_queue_size=1)
        manager = AsyncRunManager(
            _BlockingRuntime(),
            repository=repository,
            event_bus=event_bus,
        )
        await manager.create_run(_context("run_backpressure"), _scope())
        await _wait_for_status(manager, "run_backpressure", RunStatus.RUNNING)
        slow = await manager.subscribe("run_backpressure", _scope())
        context = _context("run_backpressure")
        await manager.accept_runtime_event(
            "run_backpressure",
            _draft(context, "token.delta", payload={"channel": "content", "text": "a"}),
        )
        await manager.accept_runtime_event(
            "run_backpressure",
            _draft(context, "token.delta", payload={"channel": "content", "text": "b"}),
        )
        assert slow.queue.get_nowait() is STREAM_RECONNECT

        replay = await manager.subscribe("run_backpressure", _scope(), after_seq=1)
        try:
            assert [event.seq for event in replay.replay] == [2, 3]
        finally:
            await manager.unsubscribe(replay)
            await manager.unsubscribe(slow)
        await manager.cancel_run("run_backpressure", _scope(), reason="test_cleanup")

    asyncio.run(run_test())


def test_duplicate_awaiting_terminal_is_ignored():
    async def run_test():
        class _DuplicateAwaitingRuntime(_InteractionRuntime):
            async def execute(self, context, sink):
                result = await super().execute(context, sink)
                await sink.emit(_draft(
                    context,
                    "turn.finished",
                    status="awaiting_input",
                    payload={
                        "status": "awaiting_input",
                        "final_output": {
                            "type": "user_interaction",
                            "interaction": {
                                "id": "interaction_demo",
                                "type": "single_choice",
                                "options": [{"id": "option_a", "value": "value_a"}],
                            },
                        },
                    },
                ))
                return result

        manager = AsyncRunManager(_DuplicateAwaitingRuntime())
        await manager.create_run(_context("run_duplicate_awaiting"), _scope())
        await _wait_for_status(
            manager,
            "run_duplicate_awaiting",
            RunStatus.AWAITING_INPUT,
        )
        events = await manager.repository.list_after("run_duplicate_awaiting", 0)
        assert [event.event_type for event in events].count("turn.finished") == 1

    asyncio.run(run_test())


def test_execution_service_supports_astream_runtime():
    async def run_test():
        class _StreamRuntime:
            async def astream(self, context):
                yield RuntimeEvent(
                    event_type="turn.started",
                    status="running",
                    run_id=context.run_id,
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    payload={"turn_id": context.turn_id},
                )
                yield RuntimeEvent(
                    event_type="turn.finished",
                    status="completed",
                    run_id=context.run_id,
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    payload={"status": "completed", "content": "streamed answer"},
                )

            async def cancel(self, run_id, reason):
                del run_id, reason
                return True

        manager = AsyncRunManager(AsyncExecutionService(_StreamRuntime()))
        context = RuntimeInvocationContext(
            **{
                **_context("run_astream").__dict__,
                "llm_request_mode": "stream",
            }
        )
        await manager.create_run(context, _scope())
        completed = await _wait_for_status(manager, "run_astream", RunStatus.COMPLETED)
        assert completed.result is not None
        assert completed.result.content == "streamed answer"

    asyncio.run(run_test())


def test_mcp_context_rejects_runtime_interaction_request():
    async def run_test():
        manager = AsyncRunManager(_InteractionRuntime())
        context = RuntimeInvocationContext(
            **{
                **_context("run_mcp_interaction").__dict__,
                "supports_user_interaction": False,
            }
        )
        await manager.create_run(context, _scope())
        failed = await _wait_for_status(manager, "run_mcp_interaction", RunStatus.FAILED)
        assert failed.error is not None
        assert failed.error.error_code == "INTERACTION_NOT_SUPPORTED"
        assert failed.pending_interaction is None

    asyncio.run(run_test())


def test_manager_public_event_filter_drops_reasoning_and_raw_tool_input():
    async def run_test():
        class _UnsafeRuntime:
            async def astream(self, context):
                yield RuntimeEvent(
                    event_type="reasoning.delta",
                    status="running",
                    run_id=context.run_id,
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    payload={"channel": "reasoning", "text": "private reasoning"},
                )
                yield RuntimeEvent(
                    event_type="tool.started",
                    status="running",
                    run_id=context.run_id,
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    payload={
                        "tool_name": "synthetic_lookup",
                        "tool_call_id": "call_public_filter",
                        "input": {"password": "synthetic-secret"},
                    },
                )
                yield RuntimeEvent(
                    event_type="turn.finished",
                    status="completed",
                    run_id=context.run_id,
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    payload={"status": "completed", "content": "safe answer"},
                )

            async def cancel(self, run_id, reason):
                del run_id, reason
                return True

        manager = AsyncRunManager(AsyncExecutionService(_UnsafeRuntime()))
        await manager.create_run(_context("run_public_filter"), _scope())
        await _wait_for_status(manager, "run_public_filter", RunStatus.COMPLETED)
        events = await manager.repository.list_after("run_public_filter", 0)
        assert all(event.event_type != "reasoning.delta" for event in events)
        tool_event = next(event for event in events if event.event_type == "tool.started")
        assert "input" not in tool_event.payload
        assert "synthetic-secret" not in str(tool_event.payload)
        assert tool_event.payload["input_preview"] == {
            "password": "[REDACTED]",
        }

    asyncio.run(run_test())


def test_native_jiuwen_runtime_executes_through_b_run_manager():
    async def run_test():
        class _NativeEngine:
            def __init__(self, **kwargs):
                del kwargs

            async def arun(self, state, **kwargs):
                del kwargs
                return {
                    **state,
                    "final_output": "native answer",
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                }

        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=_NativeEngine,
        )
        manager = AsyncRunManager(AsyncExecutionService(runtime))
        await manager.create_run(_context("run_native_manager"), _scope())

        completed = await _wait_for_status(
            manager,
            "run_native_manager",
            RunStatus.COMPLETED,
        )
        assert completed.result is not None
        assert completed.result.content == "native answer"
        assert completed.result.usage == {
            "input_tokens": 2,
            "output_tokens": 3,
        }
        events = await manager.repository.list_after("run_native_manager", 0)
        assert [event.event_type for event in events] == [
            "run.started",
            "turn.started",
            "turn.finished",
        ]
        assert runtime._runs == {}

    asyncio.run(run_test())


def test_native_runtime_hitl_persists_checkpoint_before_public_resume(tmp_path):
    async def run_test():
        class _SyntheticOrchestrator:
            def __init__(self):
                self.chat_store = ChatStore(str(tmp_path / "chat"))
                self.execution_store = ExecutionStore(str(tmp_path / "executions"))

            def _get_stores(self, username, workspace_id):
                assert username == "operator_a"
                assert workspace_id == "workspace_demo"
                return self.chat_store, self.execution_store

        class _NativeHitlRuntime:
            def __init__(self):
                self.resume_turn_ids = []

            async def astream(self, context):
                yield RuntimeEvent(
                    event_type="turn.started",
                    status="running",
                    run_id=context.run_id,
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    payload={"turn_id": context.turn_id},
                )
                if context.interaction_response is None:
                    interaction = {
                        "id": "interaction_native_demo",
                        "type": "single_choice",
                        "prompt": "Choose a synthetic option",
                        "options": [
                            {"id": "option_a", "label": "Option A", "value": "value_a"},
                        ],
                    }
                    yield RuntimeEvent(
                        event_type="interaction.requested",
                        status="awaiting_input",
                        run_id=context.run_id,
                        session_id=context.session_id,
                        turn_id=context.turn_id,
                        payload={
                            "interaction_id": interaction["id"],
                            "type": interaction["type"],
                            "prompt": interaction["prompt"],
                            "options": interaction["options"],
                        },
                    )
                    yield RuntimeEvent(
                        event_type="turn.finished",
                        status="awaiting_input",
                        run_id=context.run_id,
                        session_id=context.session_id,
                        turn_id=context.turn_id,
                        payload={
                            "status": "awaiting_input",
                            "content": interaction["prompt"],
                            "structured_content": {
                                "type": "user_interaction",
                                "text": interaction["prompt"],
                                "interaction": interaction,
                                "options": interaction["options"],
                            },
                        },
                    )
                    return

                self.resume_turn_ids.append(context.turn_id)
                if context.interaction_response["interaction_id"] == "interaction_native_demo":
                    assert context.interaction_response["option_id"] == "option_a"
                    interaction = {
                        "id": "interaction_native_followup",
                        "type": "input",
                        "prompt": "Provide a second synthetic value",
                        "options": [],
                    }
                    yield RuntimeEvent(
                        event_type="interaction.requested",
                        status="awaiting_input",
                        run_id=context.run_id,
                        session_id=context.session_id,
                        turn_id=context.turn_id,
                        payload={
                            "interaction_id": interaction["id"],
                            "type": interaction["type"],
                            "prompt": interaction["prompt"],
                            "options": [],
                        },
                    )
                    yield RuntimeEvent(
                        event_type="turn.finished",
                        status="awaiting_input",
                        run_id=context.run_id,
                        session_id=context.session_id,
                        turn_id=context.turn_id,
                        payload={
                            "status": "awaiting_input",
                            "content": interaction["prompt"],
                            "structured_content": {
                                "type": "user_interaction",
                                "text": interaction["prompt"],
                                "interaction": interaction,
                                "options": [],
                            },
                        },
                    )
                    return

                assert context.interaction_response["interaction_id"] == "interaction_native_followup"
                assert context.interaction_response["value"] == "synthetic follow-up"
                yield RuntimeEvent(
                    event_type="turn.finished",
                    status="completed",
                    run_id=context.run_id,
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    payload={"status": "completed", "content": "resumed answer"},
                )

            async def cancel(self, run_id, reason):
                del run_id, reason
                return True

        orchestrator = _SyntheticOrchestrator()
        runtime = _NativeHitlRuntime()
        service = AsyncExecutionService(
            runtime,
            session_adapter=OrchestratorSessionAdapter(lambda: orchestrator),
        )
        manager = AsyncRunManager(service)
        await manager.create_run(_context("run_native_hitl"), _scope())
        waiting = await _wait_for_status(
            manager,
            "run_native_hitl",
            RunStatus.AWAITING_INPUT,
        )

        first_turn = orchestrator.execution_store.get_turn("session_demo", 1)
        assert first_turn["status"] == "awaiting_input"
        assert first_turn["final_output"]["interaction"]["id"] == "interaction_native_demo"
        assert waiting.pending_interaction["id"] == "interaction_native_demo"

        resumed = await manager.interact(
            "run_native_hitl",
            _scope(),
            interaction_id="interaction_native_demo",
            option_id="option_a",
            value="value_a",
        )
        assert resumed.run_id == "run_native_hitl"
        waiting_again = await _wait_for_status(
            manager,
            "run_native_hitl",
            RunStatus.AWAITING_INPUT,
        )
        assert waiting_again.pending_interaction["id"] == "interaction_native_followup"

        await manager.interact(
            "run_native_hitl",
            _scope(),
            interaction_id="interaction_native_followup",
            value="synthetic follow-up",
        )
        completed = await _wait_for_status(
            manager,
            "run_native_hitl",
            RunStatus.COMPLETED,
        )
        assert completed.result is not None
        assert completed.result.content == "resumed answer"
        assert runtime.resume_turn_ids == [2, 3]

        history = orchestrator.execution_store.get_session_history("session_demo")
        assert [turn["run_id"] for turn in history["turns"]] == [
            "run_native_hitl",
            "run_native_hitl",
            "run_native_hitl",
        ]
        assert [turn["status"] for turn in history["turns"]] == [
            "awaiting_input",
            "awaiting_input",
            "success",
        ]
        assert history["turns"][0]["interaction_response"]["option_id"] == "option_a"
        assert (
            history["turns"][1]["interaction_response"]["value"]
            == "synthetic follow-up"
        )

        durable = await manager.repository.list_after("run_native_hitl", 0)
        persisted_session = orchestrator.chat_store.get_session("session_demo")
        projected = [
            event["data"]
            for event in persisted_session.events
            if event.get("event") == "agent.v2"
            and (event.get("data") or {}).get("run_id") == "run_native_hitl"
        ]
        assert [
            (event["event_id"], event["seq"], event["type"])
            for event in projected
        ] == [
            (event.event_id, event.seq, event.event_type)
            for event in durable
        ]
        assert sum(event["type"] == "interaction.resumed" for event in projected) == 2
        interaction_events = [
            event for event in durable if event.event_type == "interaction.requested"
        ]
        assert [event.turn_id for event in interaction_events] == [1, 2]
        assert interaction_events[1].payload["prompt"] == "Provide a second synthetic value"
        for _ in range(20):
            if not service.session_adapter._run_contexts:
                break
            await asyncio.sleep(0.005)
        assert service.session_adapter._run_contexts == {}

    asyncio.run(run_test())


def test_terminal_projection_failure_still_releases_session_context():
    async def run_test():
        adapter = OrchestratorSessionAdapter(lambda: SimpleNamespace())
        context = _context("run_projection_failure")
        adapter._run_contexts[context.run_id] = context

        def fail_projection(_event):
            raise RuntimeError("synthetic projection failure")

        adapter._persist_event_sync = fail_projection
        event = PublicEvent(
            schema_version="1.0",
            event_id="run_projection_failure:1",
            seq=1,
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            event_type="turn.finished",
            status="completed",
            source={"node": "synthetic", "component": "test"},
            payload={"status": "completed", "content": "synthetic answer"},
            created_at=utc_now_iso(),
        )

        with pytest.raises(RuntimeError, match="synthetic projection failure"):
            await adapter.persist_event(event)
        assert adapter._run_contexts == {}

    asyncio.run(run_test())


def test_direct_execution_routes_and_preserves_runtime_scope():
    class _RuntimeContext:
        latest = None

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            _RuntimeContext.latest = self

    class _DecisionRouter:
        decision = None

        def __init__(self, config):
            assert config.enabled is True

        def route(self, **kwargs):
            assert kwargs["runtime_context"].workspace_id == "workspace_demo"
            assert kwargs["runtime_context"].selected_ontology_id == "ontology_demo"
            assert kwargs["runtime_context"].selected_ontology_version == "v1"
            return self.decision

    class _ChatStore:
        @staticmethod
        def store_tool_output(**kwargs):
            del kwargs

    class _Orchestrator:
        def __init__(self):
            self.agent_config = SimpleNamespace(
                direct_execution=SimpleNamespace(enabled=True),
                execute_script_timeout=12,
            )

        @staticmethod
        def _get_stores(username, workspace_id):
            assert username == "operator_a"
            assert workspace_id == "workspace_demo"
            return _ChatStore(), SimpleNamespace()

    class _DirectResult:
        def __init__(self, status, output, error=None):
            self.status = status
            self.output = output
            self.error = error

    calls = []

    def run_direct_script(**kwargs):
        calls.append(kwargs)
        return (
            [
                {
                    "event": "agent.v2",
                    "data": {
                        "type": "tool.finished",
                        "payload": {
                            "tool_name": "synthetic_direct_tool",
                            "tool_call_id": "call_direct",
                            "status": "completed",
                        },
                    },
                }
            ],
            _DirectResult("success", "synthetic direct answer"),
        )

    def artifact_scope(_writer):
        from contextlib import nullcontext

        return nullcontext()

    dependencies = {
        "OperationDirectRouter": _DecisionRouter,
        "RuntimeContext": _RuntimeContext,
        "run_direct_script": run_direct_script,
        "tool_output_artifact_writer_context": artifact_scope,
        "load_system_config": lambda: {
            "storage": {"project_root": "/tmp/synthetic-project"}
        },
    }
    context = _context("run_direct")
    context = context.model_copy(update={
        "arguments": {
            "selected_ontology_id": "ontology_demo",
            "selected_ontology_version": "v1",
        }
    })
    adapter = OrchestratorSessionAdapter(lambda: _Orchestrator())

    _DecisionRouter.decision = SimpleNamespace(route="fallback_agent_loop")
    assert adapter._try_direct_execute_sync(context, dependencies) is None
    assert calls == []

    _DecisionRouter.decision = SimpleNamespace(
        route="missing_args",
        operation_key="synthetic_operation",
        missing_business_arguments=["synthetic_argument"],
    )
    missing = adapter._try_direct_execute_sync(context, dependencies)
    assert [event.event_type for event in missing] == ["turn.started", "turn.finished"]
    assert missing[-1].status == "completed"
    assert "synthetic_argument" in missing[-1].payload["content"]

    _DecisionRouter.decision = SimpleNamespace(route="execute")
    success = adapter._try_direct_execute_sync(context, dependencies)
    assert [event.event_type for event in success] == [
        "turn.started",
        "tool.finished",
        "turn.finished",
    ]
    assert success[-1].status == "completed"
    assert success[-1].payload["content"] == "synthetic direct answer"
    assert calls[-1]["project_root"] == "/tmp/synthetic-project"
    assert calls[-1]["timeout_seconds"] == 12
    assert _RuntimeContext.latest.workspace_id == "workspace_demo"

    dependencies["run_direct_script"] = lambda **_kwargs: (
        [],
        _DirectResult("error", "synthetic failure output", "synthetic_error"),
    )
    failed = adapter._try_direct_execute_sync(context, dependencies)
    assert failed[-1].status == "failed"
    assert failed[-1].payload["error"]["error_code"] == "SYNTHETIC_ERROR"
