import asyncio
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from agent.runtime.adapter import PublicEventAdapter
from agent.runtime.contracts import (
    USER_WORKSPACE_RUN_CONCURRENCY_LIMIT,
    PublicEvent,
    PublicEventDraft,
    RunPreflightSnapshot,
    RunScope,
    RunSnapshot,
    RunStatus,
    RuntimeErrorInfo,
    RuntimeInvocationContext,
    RuntimeResult,
)
from agent.runtime.errors import (
    RunConflictError,
    RunNotFoundError,
    SessionBusyError,
    WorkspaceBusyError,
    WorkspaceUnavailableError,
)
from agent.runtime.event_bus import AsyncEventBus
from agent.runtime.events import RuntimeEvent
from agent.runtime.jiuwen import JiuwenAsyncAgentRuntime
from agent.runtime.manager import AsyncRunManager, request_fingerprint
from agent.runtime.service import (
    AsyncExecutionService,
    OrchestratorSessionAdapter,
    _build_deterministic_artifact_writer,
)
from agent.runtime.store import InMemoryRunRepository, utc_now_iso
from backend_synthetic_credentials import synthetic_invalid_password
from ontology.view_cache import (
    build_package_content_revision,
    build_package_revision_source_signature,
    get_ontology_mutation_revision,
)
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
    assert request_fingerprint(base) == request_fingerprint(base.model_copy(update={"run_id": "run_fingerprint_b"}))
    assert request_fingerprint(base) != request_fingerprint(base.model_copy(update={"workspace_id": "workspace_other"}))
    assert request_fingerprint(base) != request_fingerprint(
        base.model_copy(update={"supports_user_interaction": False})
    )
    assert request_fingerprint(base) != request_fingerprint(
        base.model_copy(update={"requested_operation_key": "operation_demo"})
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


def test_public_terminal_event_uses_canonical_runtime_shape():
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

    envelope = event.to_dict()

    assert envelope["event_type"] == "turn.finished"
    assert envelope["status"] == "completed"
    assert envelope["payload"] == {
        "status": "completed",
        "content": "synthetic answer",
    }
    assert "protocol" not in envelope


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
        await sink.emit(
            _draft(
                context,
                "turn.finished",
                status="completed",
                payload={"status": "completed", "final_output": "synthetic answer"},
            )
        )
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
        await sink.emit(
            _draft(
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
            )
        )
        return RuntimeResult(status="awaiting_input")

    async def cancel(self, run_id, reason):
        del run_id, reason
        return True

    async def interact(self, context, sink, interaction_response):
        assert interaction_response["option_id"] == "option_a"
        await sink.emit(
            _draft(
                context,
                "turn.finished",
                status="completed",
                payload={"status": "completed", "final_output": "resumed answer"},
            )
        )
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
        replay = [event async for event in manager.event_bus.stream("run_demo", after_seq=0)]
        assert [event.event_type for event in replay] == [
            "run.started",
            "turn.started",
            "turn.finished",
        ]
        assert [event.seq for event in replay] == [1, 2, 3]
        assert completed.last_seq == 3

    asyncio.run(run_test())


def test_concurrent_publish_persists_and_broadcasts_in_sequence_order():
    async def run_test():
        repository = InMemoryRunRepository()
        context = _context("run_ordered_publish")
        await repository.create(
            RunSnapshot(
                run_id=context.run_id,
                session_id=context.session_id,
                turn_id=context.turn_id,
                status=RunStatus.RUNNING,
                created_at=utc_now_iso(),
                updated_at=utc_now_iso(),
                last_seq=0,
                scope=_scope(),
                context=context,
            )
        )
        persisted = []

        async def persist(event):
            if event.seq == 1:
                await asyncio.sleep(0.02)
            persisted.append(event.seq)

        event_bus = AsyncEventBus(repository, event_persistor=persist)
        stream = event_bus.stream(context.run_id)
        first_live = asyncio.create_task(anext(stream))
        for _ in range(100):
            if event_bus.active_subscriber_count == 1:
                break
            await asyncio.sleep(0)
        assert event_bus.active_subscriber_count == 1
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
                (await asyncio.wait_for(first_live, timeout=1)).seq,
                (await asyncio.wait_for(anext(stream), timeout=1)).seq,
            ]
            assert persisted == [1, 2]
            assert broadcast == [1, 2]
        finally:
            await stream.aclose()

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
                await sink.emit(
                    _draft(
                        context,
                        "token.delta",
                        payload={"channel": "content", "text": "synthetic answer"},
                    )
                )
                await sink.emit(
                    _draft(
                        context,
                        "turn.finished",
                        status="completed",
                        payload={"status": "completed", "content": "synthetic answer"},
                    )
                )
                return RuntimeResult(status="completed", content="synthetic answer")

            async def cancel(self, run_id, reason):
                del run_id, reason
                self.release.set()
                return True

        runtime = _GatedRuntime()
        manager = AsyncRunManager(runtime)
        await manager.create_run(_context("run_live_queue"), _scope())
        await runtime.started.wait()
        running = await manager.get_run("run_live_queue", _scope())
        assert running is not None
        stream = manager.event_bus.stream(
            "run_live_queue",
            after_seq=running.last_seq,
        )

        async def collect_live_events():
            return [event async for event in stream]

        collector = asyncio.create_task(collect_live_events())
        for _ in range(100):
            if manager.event_bus.active_subscriber_count == 1:
                break
            await asyncio.sleep(0)
        assert manager.event_bus.active_subscriber_count == 1
        runtime.release.set()
        await _wait_for_status(manager, "run_live_queue", RunStatus.COMPLETED)

        event_types = [event.event_type for event in await asyncio.wait_for(collector, timeout=1)]
        assert event_types == ["turn.started", "token.delta", "turn.finished"]

    asyncio.run(run_test())


def test_terminal_close_does_not_require_evicted_event_history():
    async def run_test():
        repository = InMemoryRunRepository(max_events_per_run=32)
        context = _context("run_retained_terminal")
        await repository.create(
            RunSnapshot(
                run_id=context.run_id,
                session_id=context.session_id,
                turn_id=context.turn_id,
                status=RunStatus.RUNNING,
                created_at=utc_now_iso(),
                updated_at=utc_now_iso(),
                last_seq=0,
                scope=_scope(),
                context=context,
            )
        )
        event_bus = AsyncEventBus(repository, subscriber_queue_size=64)
        stream = event_bus.stream(context.run_id)

        async def collect_live_events():
            return [event async for event in stream]

        collector = asyncio.create_task(collect_live_events())
        for _ in range(100):
            if event_bus.active_subscriber_count == 1:
                break
            await asyncio.sleep(0)
        assert event_bus.active_subscriber_count == 1
        try:
            for index in range(32):
                await event_bus.publish(
                    _draft(
                        context,
                        "token.delta",
                        payload={"channel": "content", "text": str(index)},
                    )
                )
            await event_bus.publish(
                _draft(
                    context,
                    "turn.finished",
                    status="completed",
                    payload={"status": "completed", "content": "synthetic"},
                )
            )

            live_events = await asyncio.wait_for(collector, timeout=1)
            assert [event.seq for event in live_events] == list(range(1, 34))
            assert live_events[-1].event_type == "turn.finished"
        finally:
            await stream.aclose()

    asyncio.run(run_test())


def test_finished_run_releases_manager_entry_registry():
    async def run_test():
        manager = AsyncRunManager(_SuccessRuntime())
        await manager.create_run(_context("run_cleanup"), _scope())
        await _wait_for_status(manager, "run_cleanup", RunStatus.COMPLETED)
        for _ in range(20):
            if "run_cleanup" not in manager._entries:
                break
            await asyncio.sleep(0)
        assert "run_cleanup" not in manager._entries
        assert manager.supervisor.get_run_task("run_cleanup") is None

    asyncio.run(run_test())


def test_unknown_run_cancel_does_not_allocate_entries_and_interact_is_removed():
    async def run_test():
        manager = AsyncRunManager(_SuccessRuntime())
        with pytest.raises(RunNotFoundError):
            await manager.cancel_run("run_missing", _scope())
        assert not hasattr(manager, "interact")
        assert manager._entries == {}

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
        terminal_events = [event for event in events if event.event_type == "turn.finished"]
        assert len(terminal_events) == 1
        assert terminal_events[0].payload["cancel_reason"] == "user_requested"

    asyncio.run(run_test())


def test_terminal_run_replays_from_native_async_cursor():
    async def run_test():
        manager = AsyncRunManager(_SuccessRuntime())
        await manager.create_run(_context("run_cursor_replay"), _scope())
        await _wait_for_status(
            manager,
            "run_cursor_replay",
            RunStatus.COMPLETED,
        )

        event_stream = manager.event_bus.stream("run_cursor_replay", after_seq=1)
        replay = [event async for event in event_stream]
        assert [event.seq for event in replay] == [2, 3]
        assert replay[-1].event_type == "turn.finished"

    asyncio.run(run_test())


def test_hitl_answer_uses_new_run_and_turn_and_each_run_executes_once():
    class _OneRunPerTurnRuntime(_InteractionRuntime):
        def __init__(self):
            self.executions = []

        async def execute(self, context, sink):
            self.executions.append((context.run_id, context.turn_id, context.interaction_response))
            if context.interaction_response is None:
                return await super().execute(context, sink)

            assert context.interaction_response == {
                "interaction_id": "interaction_demo",
                "type": "single_choice",
                "option_id": "option_a",
                "value": "value_a",
            }
            await sink.emit(
                _draft(
                    context,
                    "turn.finished",
                    status="completed",
                    payload={"status": "completed", "final_output": "synthetic answer"},
                )
            )
            return RuntimeResult(status="completed", content="synthetic answer")

    async def run_test():
        runtime = _OneRunPerTurnRuntime()
        manager = AsyncRunManager(runtime)
        await manager.create_run(
            _context("run_hitl_original"),
            _scope(),
            idempotency_key="initial-request-key",
        )
        waiting = await _wait_for_status(
            manager,
            "run_hitl_original",
            RunStatus.AWAITING_INPUT,
        )
        assert waiting.pending_interaction["id"] == "interaction_demo"

        response = {
            "interaction_id": "interaction_demo",
            "type": "single_choice",
            "option_id": "option_a",
            "value": "value_a",
        }
        answer_context = RuntimeInvocationContext(
            run_id="run_hitl_answer",
            session_id="session_demo",
            turn_id=2,
            workspace_id="workspace_demo",
            username="operator_a",
            query="Choose option A",
            interaction_response=response,
        )
        answer, answer_created = await manager.create_run(
            answer_context,
            _scope(),
            idempotency_key="answer-request-key",
        )
        assert answer_created is True
        assert answer.run_id != waiting.run_id
        assert answer.turn_id == waiting.turn_id + 1
        await _wait_for_status(manager, answer.run_id, RunStatus.COMPLETED)

        retry, retry_created = await manager.create_run(
            answer_context.model_copy(update={"run_id": "run_hitl_retry"}),
            _scope(),
            idempotency_key="answer-request-key",
        )
        assert retry_created is False
        assert retry.run_id == answer.run_id

        original = await manager.get_run(waiting.run_id, _scope())
        assert original is not None
        assert original.status == RunStatus.AWAITING_INPUT
        assert original.pending_interaction == waiting.pending_interaction
        assert runtime.executions == [
            ("run_hitl_original", 1, None),
            ("run_hitl_answer", 2, response),
        ]

    asyncio.run(run_test())


def test_shutdown_preserves_terminal_awaiting_input_without_an_active_task():
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

        after_shutdown = await manager.get_run("run_shutdown_hitl", _scope())
        assert after_shutdown is not None
        assert after_shutdown.status == RunStatus.AWAITING_INPUT
        assert after_shutdown.pending_interaction == waiting.pending_interaction
        events = await manager.repository.list_after("run_shutdown_hitl", 0)
        terminal_events = [event for event in events if event.event_type == "turn.finished"]
        assert [event.status for event in terminal_events] == ["awaiting_input"]

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


def test_same_session_second_run_is_rejected_without_queueing():
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
        with pytest.raises(SessionBusyError) as exc_info:
            await manager.create_run(second_context, _scope())

        assert exc_info.value.run_id == "run_admission_first"
        assert await manager.repository.get("run_admission_second") is None
        assert runtime.cancelled == []

        await manager.cancel_run("run_admission_first", _scope(), reason="test_cleanup")

    asyncio.run(run_test())


def test_workspace_deletion_guard_rejects_active_run():
    async def run_test():
        runtime = _BlockingRuntime()
        manager = AsyncRunManager(runtime)
        await manager.create_run(_context("run_workspace_active"), _scope())
        await asyncio.wait_for(runtime.started.wait(), timeout=1.0)

        with pytest.raises(WorkspaceBusyError) as exc_info:
            async with manager.workspace_deletion_guard("workspace_demo"):
                pass

        assert exc_info.value.run_id == "run_workspace_active"
        assert exc_info.value.session_id == "session_demo"
        assert exc_info.value.status == "running"

        await manager.cancel_run(
            "run_workspace_active",
            _scope(),
            reason="test_cleanup",
        )
        async with manager.workspace_deletion_guard("workspace_demo"):
            pass

    asyncio.run(run_test())


def test_workspace_deletion_guard_rejects_waiting_creation_after_deletion():
    async def run_test():
        runtime = _SuccessRuntime()
        available = {
            "workspace_demo": True,
            "workspace_other": True,
        }

        async def workspace_available(workspace_id: str) -> bool:
            return available.get(workspace_id, False)

        manager = AsyncRunManager(
            runtime,
            workspace_available=workspace_available,
        )

        async with manager.workspace_deletion_guard("workspace_demo"):
            creation = asyncio.create_task(manager.create_run(_context("run_after_workspace_guard"), _scope()))
            other_context = _context("run_other_workspace").model_copy(
                update={
                    "session_id": "session_other",
                    "workspace_id": "workspace_other",
                }
            )
            other_scope = RunScope(
                "operator_a",
                "workspace_other",
                ("agent:run", "agent:read", "agent:cancel", "agent:interact"),
            )
            other_snapshot, other_created = await asyncio.wait_for(
                manager.create_run(other_context, other_scope),
                timeout=1.0,
            )
            await asyncio.sleep(0)
            assert creation.done() is False
            assert other_created is True
            assert other_snapshot.run_id == "run_other_workspace"
            available["workspace_demo"] = False

        with pytest.raises(WorkspaceUnavailableError) as exc_info:
            await creation

        assert exc_info.value.workspace_id == "workspace_demo"
        assert await manager.repository.get("run_after_workspace_guard") is None

    asyncio.run(run_test())


def test_queued_cancel_preserves_caller_reason_without_runtime_cancel():
    async def run_test():
        runtime = _BlockingRuntime()
        manager = AsyncRunManager(runtime)
        await manager.create_run(_context("run_cancel_active"), _scope())
        await asyncio.wait_for(runtime.started.wait(), timeout=1.0)
        await manager.create_run(
            _context("run_cancel_queued").model_copy(update={"session_id": "session_queue_demo"}),
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
                await sink.emit(
                    _draft(
                        context,
                        "interaction.requested",
                        payload={
                            "interaction_id": "interaction_requested",
                            "type": "input",
                            "prompt": "Provide synthetic input",
                        },
                    )
                )
                return RuntimeResult(status="awaiting_input")

            async def cancel(self, run_id, reason):
                del run_id, reason
                return True

            async def interact(self, context, sink, interaction_response):
                del interaction_response
                await sink.emit(
                    _draft(
                        context,
                        "turn.finished",
                        status="completed",
                        payload={"status": "completed", "final_output": "resumed"},
                    )
                )
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
    draft = PublicEventAdapter().adapt(
        RuntimeEvent(
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
                await sink.emit(
                    _draft(
                        context,
                        "turn.finished",
                        status="completed",
                        payload={"status": "completed", "final_output": "synthetic answer"},
                    )
                )
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
                "session_id": "session_relaxed",
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


def test_user_workspace_limit_admits_100_runs_and_queues_the_101st():
    async def run_test():
        class _CountingBlockingRuntime:
            def __init__(self):
                self.started = []
                self.first_hundred_started = asyncio.Event()
                self.release = asyncio.Event()

            async def execute(self, context, sink):
                del sink
                self.started.append(context.run_id)
                if len(self.started) == USER_WORKSPACE_RUN_CONCURRENCY_LIMIT:
                    self.first_hundred_started.set()
                await self.release.wait()
                return RuntimeResult(
                    status="completed",
                    content="synthetic answer",
                )

            async def cancel(self, run_id, reason):
                del run_id, reason
                self.release.set()
                return True

        runtime = _CountingBlockingRuntime()
        manager = AsyncRunManager(runtime)
        total_runs = USER_WORKSPACE_RUN_CONCURRENCY_LIMIT + 1
        for index in range(total_runs):
            context = _context("run_limit_{}".format(index)).model_copy(
                update={
                    "session_id": "session_limit_{}".format(index),
                    "concurrency_limit": USER_WORKSPACE_RUN_CONCURRENCY_LIMIT,
                }
            )
            await manager.create_run(context, _scope())

        await asyncio.wait_for(runtime.first_hundred_started.wait(), timeout=2.0)
        await asyncio.sleep(0)
        assert len(runtime.started) == USER_WORKSPACE_RUN_CONCURRENCY_LIMIT
        queued = await manager.get_run("run_limit_100", _scope())
        assert queued is not None
        assert queued.status == RunStatus.QUEUED

        runtime.release.set()
        await _wait_for_status(
            manager,
            "run_limit_100",
            RunStatus.COMPLETED,
            timeout=2.0,
        )
        assert len(runtime.started) == total_runs
        assert runtime.started[-1] == "run_limit_100"

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
        slow = manager.event_bus.stream("run_backpressure")
        first = asyncio.create_task(anext(slow))
        assert (await asyncio.wait_for(first, timeout=1)).seq == 1
        context = _context("run_backpressure")
        await manager.accept_runtime_event(
            "run_backpressure",
            _draft(context, "token.delta", payload={"channel": "content", "text": "a"}),
        )
        await manager.accept_runtime_event(
            "run_backpressure",
            _draft(context, "token.delta", payload={"channel": "content", "text": "b"}),
        )
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(slow), timeout=1)

        replay = manager.event_bus.stream("run_backpressure", after_seq=1)
        try:
            assert [
                (await asyncio.wait_for(anext(replay), timeout=1)).seq,
                (await asyncio.wait_for(anext(replay), timeout=1)).seq,
            ] == [2, 3]
        finally:
            await replay.aclose()
            await slow.aclose()
        await manager.cancel_run("run_backpressure", _scope(), reason="test_cleanup")

    asyncio.run(run_test())


def test_duplicate_awaiting_terminal_is_ignored():
    async def run_test():
        class _DuplicateAwaitingRuntime(_InteractionRuntime):
            async def execute(self, context, sink):
                result = await super().execute(context, sink)
                await sink.emit(
                    _draft(
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
                    )
                )
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


def test_manager_snapshot_preserves_complete_long_terminal_output():
    async def run_test():
        content = "x" * 5000

        class _LongTerminalRuntime:
            async def astream(self, context):
                yield RuntimeEvent(
                    event_type="turn.finished",
                    status="completed",
                    run_id=context.run_id,
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    payload={"status": "completed", "content": content},
                )

            async def cancel(self, run_id, reason):
                del run_id, reason
                return True

        manager = AsyncRunManager(AsyncExecutionService(_LongTerminalRuntime()))
        context = _context("run_long_terminal").model_copy(update={"llm_request_mode": "stream"})
        await manager.create_run(context, _scope())

        completed = await _wait_for_status(
            manager,
            context.run_id,
            RunStatus.COMPLETED,
        )
        terminal = [
            event
            for event in await manager.repository.list_after(context.run_id, 0)
            if event.event_type == "turn.finished"
        ][0]

        assert completed.result is not None
        assert completed.result.content == content
        assert terminal.payload["content"] == content
        assert "truncated" not in terminal.payload

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
                        "input": {"password": synthetic_invalid_password("async_run_manager-credential-02")},
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
        assert synthetic_invalid_password("async_run_manager-credential-02") not in str(tool_event.payload)
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


def test_native_runtime_hitl_persists_checkpoint_before_new_run(tmp_path):
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

        first_response = {
            "interaction_id": "interaction_native_demo",
            "type": "single_choice",
            "option_id": "option_a",
            "value": "value_a",
        }
        second_turn, second_created = orchestrator.execution_store.consume_interaction_and_create_turn(
            session_id="session_demo",
            interaction_id="interaction_native_demo",
            user_request="Choose option A",
            interaction_response=first_response,
            idempotency_key_hash="native-answer-one-key",
            request_fingerprint="native-answer-one-fingerprint",
        )
        assert second_created is True
        second_context = RuntimeInvocationContext(
            run_id=second_turn["run_id"],
            session_id="session_demo",
            turn_id=second_turn["turn_id"],
            workspace_id="workspace_demo",
            username="operator_a",
            query="Choose option A",
            interaction_response=first_response,
        )
        await manager.create_run(second_context, _scope())
        waiting_again = await _wait_for_status(
            manager,
            second_turn["run_id"],
            RunStatus.AWAITING_INPUT,
        )
        assert waiting_again.pending_interaction["id"] == "interaction_native_followup"
        original_after_answer = await manager.get_run("run_native_hitl", _scope())
        assert original_after_answer.status == RunStatus.AWAITING_INPUT

        second_response = {
            "interaction_id": "interaction_native_followup",
            "type": "input",
            "value": "synthetic follow-up",
        }
        third_turn, third_created = orchestrator.execution_store.consume_interaction_and_create_turn(
            session_id="session_demo",
            interaction_id="interaction_native_followup",
            user_request="synthetic follow-up",
            interaction_response=second_response,
            idempotency_key_hash="native-answer-two-key",
            request_fingerprint="native-answer-two-fingerprint",
        )
        assert third_created is True
        third_context = RuntimeInvocationContext(
            run_id=third_turn["run_id"],
            session_id="session_demo",
            turn_id=third_turn["turn_id"],
            workspace_id="workspace_demo",
            username="operator_a",
            query="synthetic follow-up",
            interaction_response=second_response,
        )
        await manager.create_run(third_context, _scope())
        completed = await _wait_for_status(
            manager,
            third_turn["run_id"],
            RunStatus.COMPLETED,
        )
        assert completed.result is not None
        assert completed.result.content == "resumed answer"
        assert runtime.resume_turn_ids == [2, 3]

        history = orchestrator.execution_store.get_session_history("session_demo")
        assert [turn["run_id"] for turn in history["turns"]] == [
            "run_native_hitl",
            second_turn["run_id"],
            third_turn["run_id"],
        ]
        assert [turn["status"] for turn in history["turns"]] == [
            "awaiting_input",
            "awaiting_input",
            "success",
        ]
        assert history["turns"][0]["interaction_response"]["option_id"] == "option_a"
        assert history["turns"][1]["interaction_response"]["value"] == "synthetic follow-up"

        persisted_session = orchestrator.chat_store.get_session("session_demo")
        interaction_events = []
        for current_run_id in (
            "run_native_hitl",
            second_turn["run_id"],
            third_turn["run_id"],
        ):
            durable = await manager.repository.list_after(current_run_id, 0)
            projected = [
                event["data"]
                for event in persisted_session.events
                if event.get("event") == "agent.v2" and (event.get("data") or {}).get("run_id") == current_run_id
            ]
            assert [(event["event_id"], event["seq"], event["type"]) for event in projected] == [
                (event.event_id, event.seq, event.event_type) for event in durable
            ]
            interaction_events.extend(event for event in durable if event.event_type == "interaction.requested")
        assert [event.turn_id for event in interaction_events] == [1, 2]
        assert interaction_events[1].payload["prompt"] == "Provide a second synthetic value"
        for _ in range(20):
            if not service.session_adapter._run_contexts:
                break
            await asyncio.sleep(0.005)
        assert service.session_adapter._run_contexts == {}

    asyncio.run(run_test())


def test_prepare_failure_compensates_allocated_turn(tmp_path, caplog):
    private_marker = "prepare-private-marker-7f3a"

    async def run_test():
        class _SyntheticOrchestrator:
            def __init__(self):
                self.chat_store = ChatStore(str(tmp_path / "chat"))
                self.execution_store = ExecutionStore(str(tmp_path / "executions"))

            def _get_stores(self, username, workspace_id):
                assert username == "operator_a"
                assert workspace_id == "workspace_demo"
                return self.chat_store, self.execution_store

        orchestrator = _SyntheticOrchestrator()

        def fail_message_write(*_args, **_kwargs):
            raise RuntimeError(private_marker)

        orchestrator.chat_store.add_message = fail_message_write
        service = AsyncExecutionService(
            SimpleNamespace(),
            session_adapter=OrchestratorSessionAdapter(lambda: orchestrator),
        )
        manager = AsyncRunManager(service)
        await manager.create_run(_context("run_prepare_compensation"), _scope())

        failed = await _wait_for_status(
            manager,
            "run_prepare_compensation",
            RunStatus.FAILED,
        )
        turn = orchestrator.execution_store.get_turn(
            "session_demo",
            1,
            run_id="run_prepare_compensation",
        )

        assert failed.error.error_code == "SESSION_PREPARE_FAILED"
        assert turn["status"] == "failed"
        assert orchestrator.chat_store.get_active_run("session_demo") is None
        assert service.session_adapter._pending_prepare_turns == {}
        assert service.session_adapter._run_contexts == {}

    with caplog.at_level(logging.ERROR, logger="agent.runtime.manager"):
        asyncio.run(run_test())

    records = [
        record
        for record in caplog.records
        if record.name == "agent.runtime.manager" and "run context preparation failed" in record.getMessage()
    ]
    assert len(records) == 1
    assert private_marker not in caplog.text
    assert "run_id=run_prepare_compensation" in records[0].getMessage()
    assert "error_type=RuntimeError" in records[0].getMessage()
    assert records[0].exc_info is None


def test_terminal_projection_failure_preserves_completed_public_state(tmp_path):
    async def run_test():
        class _SyntheticOrchestrator:
            def __init__(self):
                self.chat_store = ChatStore(str(tmp_path / "chat"))
                self.execution_store = ExecutionStore(str(tmp_path / "executions"))

            def _get_stores(self, username, workspace_id):
                assert username == "operator_a"
                assert workspace_id == "workspace_demo"
                return self.chat_store, self.execution_store

        class _TerminalRuntime:
            async def astream(self, context):
                yield RuntimeEvent(
                    event_type="turn.finished",
                    status="completed",
                    run_id=context.run_id,
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    payload={
                        "status": "completed",
                        "content": "synthetic answer",
                    },
                )

            async def cancel(self, run_id, reason):
                del run_id, reason
                return True

        orchestrator = _SyntheticOrchestrator()

        def fail_history_projection(_session_id, _messages):
            raise RuntimeError("synthetic history projection failure")

        orchestrator.execution_store.set_llm_context = fail_history_projection
        service = AsyncExecutionService(
            _TerminalRuntime(),
            session_adapter=OrchestratorSessionAdapter(lambda: orchestrator),
        )
        manager = AsyncRunManager(service)
        await manager.create_run(_context("run_terminal_projection"), _scope())

        completed = await _wait_for_status(
            manager,
            "run_terminal_projection",
            RunStatus.COMPLETED,
        )
        turn = orchestrator.execution_store.get_turn(
            "session_demo",
            1,
            run_id="run_terminal_projection",
        )
        events = await manager.repository.list_after("run_terminal_projection", 0)
        terminal_events = [event for event in events if event.event_type == "turn.finished"]

        assert completed.result.content == "synthetic answer"
        assert turn["status"] == "success"
        assert [event.status for event in terminal_events] == ["completed"]
        assert orchestrator.chat_store.get_active_run("session_demo") is None

    asyncio.run(run_test())


def test_truncated_terminal_projection_persists_partial_output(tmp_path):
    async def run_test():
        class _SyntheticOrchestrator:
            def __init__(self):
                self.chat_store = ChatStore(str(tmp_path / "chat"))
                self.execution_store = ExecutionStore(str(tmp_path / "executions"))

            def _get_stores(self, username, workspace_id):
                assert username == "operator_a"
                assert workspace_id == "workspace_demo"
                return self.chat_store, self.execution_store

        class _TruncatedRuntime:
            async def astream(self, context):
                error = RuntimeErrorInfo(
                    error_code="MODEL_OUTPUT_TRUNCATED",
                    message="回答达到输出上限，内容尚未完成",
                    retriable=True,
                    source="model",
                )
                yield RuntimeEvent(
                    event_type="turn.finished",
                    status="failed",
                    run_id=context.run_id,
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    payload={
                        "status": "failed",
                        "content": "synthetic partial answer",
                        "error": error.model_dump(mode="json"),
                    },
                )

            async def cancel(self, run_id, reason):
                del run_id, reason
                return True

        orchestrator = _SyntheticOrchestrator()
        service = AsyncExecutionService(
            _TruncatedRuntime(),
            session_adapter=OrchestratorSessionAdapter(lambda: orchestrator),
        )
        manager = AsyncRunManager(service)
        await manager.create_run(_context("run_truncated_projection"), _scope())

        failed = await _wait_for_status(
            manager,
            "run_truncated_projection",
            RunStatus.FAILED,
        )
        reloaded_store = ExecutionStore(str(tmp_path / "executions"))
        turn = reloaded_store.get_turn(
            "session_demo",
            1,
            run_id="run_truncated_projection",
        )

        assert failed.result.content == "synthetic partial answer"
        assert failed.error.error_code == "MODEL_OUTPUT_TRUNCATED"
        assert turn["status"] == "failed"
        assert turn["error_code"] == "MODEL_OUTPUT_TRUNCATED"
        assert turn["final_output"]["text"] == "synthetic partial answer"
        assert turn["final_output"]["error"]["error_code"] == "MODEL_OUTPUT_TRUNCATED"

    asyncio.run(run_test())


def test_retriable_failure_persists_resume_history_and_public_action(tmp_path):
    async def run_test():
        class _SyntheticOrchestrator:
            def __init__(self):
                self.chat_store = ChatStore(str(tmp_path / "chat"))
                self.execution_store = ExecutionStore(str(tmp_path / "executions"))

            def _get_stores(self, username, workspace_id):
                assert username == "operator_a"
                assert workspace_id == "workspace_demo"
                return self.chat_store, self.execution_store

        class _RecoverableRuntime:
            async def astream(self, context):
                yield RuntimeEvent(
                    event_type="turn.finished",
                    status="failed",
                    run_id=context.run_id,
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    payload={
                        "status": "failed",
                        "error": {
                            "error_code": "MODEL_PROVIDER_ERROR",
                            "message": "模型服务暂时不可用",
                            "retriable": True,
                            "source": "model",
                            "details": {"http_status": 503},
                        },
                        "recovery": {
                            "action": "continue",
                            "label": "继续执行",
                            "message": "Continue from the preserved context.",
                        },
                        "history_messages": [
                            {"role": "user", "content": "synthetic request"},
                            {"role": "assistant", "content": "synthetic progress"},
                        ],
                    },
                )

            async def cancel(self, run_id, reason):
                del run_id, reason
                return True

        orchestrator = _SyntheticOrchestrator()
        service = AsyncExecutionService(
            _RecoverableRuntime(),
            session_adapter=OrchestratorSessionAdapter(lambda: orchestrator),
        )
        manager = AsyncRunManager(service)
        await manager.create_run(_context("run_recoverable_failure"), _scope())

        failed = await _wait_for_status(
            manager,
            "run_recoverable_failure",
            RunStatus.FAILED,
        )
        history = orchestrator.execution_store.get_llm_context("session_demo")
        events = await manager.repository.list_after("run_recoverable_failure", 0)
        terminal = [event for event in events if event.event_type == "turn.finished"][0]

        assert failed.result.recovery.action == "continue"
        assert [message["role"] for message in history] == ["user", "assistant"]
        assert terminal.payload["recovery"]["action"] == "continue"
        assert "history_messages" not in terminal.payload

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


def test_deterministic_terminal_projection_uses_recovery_journal(tmp_path):
    chat_store = ChatStore(str(tmp_path / "chat"))
    chat_store.create_session(session_id="session_demo")
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    execution_store.start_turn(
        "session_demo",
        1,
        "run_deterministic_terminal",
        "synthetic request",
    )

    class _Orchestrator:
        @staticmethod
        def _get_stores(_username, _workspace_id):
            return chat_store, execution_store

    adapter = OrchestratorSessionAdapter(lambda: _Orchestrator())
    context = _context("run_deterministic_terminal")
    adapter._run_contexts[context.run_id] = context
    event = PublicEvent(
        schema_version="1.0",
        event_id="run_deterministic_terminal:1",
        seq=1,
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        event_type="turn.finished",
        status="completed",
        source={"node": "deterministic_execution", "component": "service"},
        payload={
            "status": "completed",
            "content": "synthetic answer",
            "structured_content": {
                "status": "success",
                "text": "synthetic answer",
                "structured_result": {"value": "synthetic"},
                "artifacts": [],
                "execution": {"type": "deterministic", "authoritative": True},
            },
        },
        created_at=utc_now_iso(),
    )

    adapter._persist_event_sync(event)

    assert execution_store.get_pending_deterministic_terminal("session_demo") is None
    assert execution_store.get_turn("session_demo", 1)["status"] == "success"
    persisted = chat_store.get_session("session_demo")
    assert [(item.get("data") or {}).get("event_id") for item in persisted.events] == ["run_deterministic_terminal:1"]


def test_deterministic_finalize_waits_for_canonical_terminal_event(tmp_path):
    chat_store = ChatStore(str(tmp_path / "chat"))
    chat_store.create_session(session_id="session_demo")
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    execution_store.start_turn(
        "session_demo",
        1,
        "run_deterministic_deferred",
        "synthetic request",
    )

    class _Orchestrator:
        @staticmethod
        def _get_stores(_username, _workspace_id):
            return chat_store, execution_store

    adapter = OrchestratorSessionAdapter(lambda: _Orchestrator())
    context = _context("run_deterministic_deferred")
    result = RuntimeResult(
        status="completed",
        content="synthetic answer",
        structured_content={
            "status": "success",
            "text": "synthetic answer",
            "structured_result": {"value": "synthetic"},
            "artifacts": [],
            "execution": {"type": "deterministic", "authoritative": True},
        },
    )

    adapter._finalize_sync(context, result, None)

    assert execution_store.get_turn("session_demo", 1)["status"] == "running"
    assert execution_store.get_pending_deterministic_terminal("session_demo") is None


def test_cancelled_public_terminal_overrides_precommitted_deterministic_success(
    tmp_path,
):
    chat_store = ChatStore(str(tmp_path / "chat"))
    chat_store.create_session(session_id="session_demo")
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    execution_store.start_turn(
        "session_demo",
        1,
        "run_deterministic_cancel_race",
        "synthetic request",
    )
    execution_store.complete_turn(
        "session_demo",
        1,
        {"status": "success", "execution": {"type": "deterministic"}},
        None,
        run_id="run_deterministic_cancel_race",
    )

    class _Orchestrator:
        @staticmethod
        def _get_stores(_username, _workspace_id):
            return chat_store, execution_store

    adapter = OrchestratorSessionAdapter(lambda: _Orchestrator())
    context = _context("run_deterministic_cancel_race")
    adapter._run_contexts[context.run_id] = context
    event = PublicEvent(
        schema_version="1.0",
        event_id="run_deterministic_cancel_race:1",
        seq=1,
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        event_type="turn.finished",
        status="cancelled",
        source={"node": "deterministic_execution", "component": "service"},
        payload={"status": "cancelled", "cancel_reason": "user_stop"},
        created_at=utc_now_iso(),
    )

    adapter._persist_event_sync(event)

    turn = execution_store.get_turn("session_demo", 1)
    assert turn["status"] == "cancelled"
    assert turn["final_output"]["cancel_reason"] == "user_stop"


def test_cancelled_public_terminal_reconciles_precommitted_regular_success(
    tmp_path,
):
    chat_store = ChatStore(str(tmp_path / "chat"))
    chat_store.create_session(session_id="session_demo")
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    execution_store.start_turn(
        "session_demo",
        1,
        "run_regular_cancel_race",
        "synthetic request",
    )
    execution_store.complete_turn(
        "session_demo",
        1,
        {"text": "synthetic answer"},
        None,
        run_id="run_regular_cancel_race",
    )
    reflection_calls = []

    class _Orchestrator:
        @staticmethod
        def _get_stores(_username, _workspace_id):
            return chat_store, execution_store

        @staticmethod
        async def _submit_reflection(**kwargs):
            reflection_calls.append(kwargs)

    adapter = OrchestratorSessionAdapter(lambda: _Orchestrator())
    context = _context("run_regular_cancel_race").model_copy(
        update={"arguments": {"selected_ontology_id": "ontology_demo"}}
    )
    adapter._run_contexts[context.run_id] = context
    event = PublicEvent(
        schema_version="1.0",
        event_id="run_regular_cancel_race:1",
        seq=1,
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        event_type="turn.finished",
        status="cancelled",
        source={"node": "react", "component": "runtime"},
        payload={"status": "cancelled", "cancel_reason": "user_stop"},
        created_at=utc_now_iso(),
    )

    adapter._persist_event_sync(event)
    asyncio.run(
        adapter.submit_reflection(
            context,
            RuntimeResult(status="completed", content="synthetic answer"),
        )
    )

    turn = execution_store.get_turn("session_demo", 1)
    assert turn["status"] == "cancelled"
    assert turn["final_output"] == {"cancel_reason": "user_stop"}
    assert reflection_calls == []


def test_deterministic_artifact_rollback_deletes_every_reserved_ref():
    class _ChatStore:
        def __init__(self):
            self.deleted = []

        @staticmethod
        def store_tool_output(**_kwargs):
            raise RuntimeError("synthetic post-write failure")

        def delete_tool_output(self, **kwargs):
            self.deleted.append(kwargs["output_ref"])
            return True

    class _ExecutionStore:
        def __init__(self):
            self.reserved = []
            self.released = []

        def reserve_deterministic_artifacts(self, _session_id, _turn_id, _run_id, refs):
            self.reserved.extend(refs)

        def release_deterministic_artifacts(self, _session_id, _run_id, refs):
            self.released.extend(refs)

    chat_store = _ChatStore()
    execution_store = _ExecutionStore()
    writer = _build_deterministic_artifact_writer(
        chat_store,
        session_id="session_demo",
        run_id="run_artifact_failure",
        execution_store=execution_store,
        turn_id=1,
    )
    writer.prepare_artifacts(["result"])

    with pytest.raises(RuntimeError, match="synthetic post-write failure"):
        writer("result", "synthetic output", "text/plain")
    writer.rollback(())

    assert len(execution_store.reserved) == 1
    assert chat_store.deleted == execution_store.reserved
    assert execution_store.released == execution_store.reserved


def test_deterministic_preflight_receives_only_business_arguments(
    tmp_path,
    monkeypatch,
):
    import ontology.ontologies as ontologies_module

    requests = []

    class _DeterministicService:
        def is_potentially_applicable(self, request):
            requests.append(request)
            return True

    deterministic_module = ModuleType("agent.direct_execution.service")
    deterministic_module.DeterministicExecutionService = _DeterministicService
    monkeypatch.setitem(
        sys.modules,
        "agent.direct_execution.service",
        deterministic_module,
    )
    selected_ontology = SimpleNamespace(
        id="ontology_demo",
        version="v1",
        active_version="v1",
        name="Synthetic ontology",
        enabled=True,
    )
    monkeypatch.setattr(
        ontologies_module,
        "get_runtime_ontology_manifest",
        lambda *_args, **_kwargs: selected_ontology,
    )

    chat_store = ChatStore(str(tmp_path / "chat"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))

    class _Orchestrator:
        @staticmethod
        def _get_stores(username, workspace_id):
            assert username == "operator_a"
            assert workspace_id == "workspace_demo"
            return chat_store, execution_store

    context = _context("run_deterministic_preflight").model_copy(
        update={
            "arguments": {
                "selected_ontology_id": "ontology_demo",
                "selected_ontology_version": "v1",
                "selected_version": "v1",
                "synthetic_argument": "synthetic_value",
            }
        }
    )
    adapter = OrchestratorSessionAdapter(lambda: _Orchestrator())

    prepared = adapter._prepare_sync_impl(
        context,
        {"quick"},
        lambda value: value,
        lambda *_args, **_kwargs: pytest.fail(
            "deterministic preflight must defer model profile resolution"
        ),
    )

    assert requests[-1].arguments == {"synthetic_argument": "synthetic_value"}
    assert requests[-1].runtime_context.selected_ontology_id == "ontology_demo"
    assert requests[-1].runtime_context.selected_ontology_version == "v1"
    assert prepared.arguments == {
        "selected_ontology_id": "ontology_demo",
        "selected_ontology_version": "v1",
        "selected_version": "v1",
        "synthetic_argument": "synthetic_value",
    }


def test_direct_execution_routes_and_preserves_runtime_scope(tmp_path):
    from agent.direct_execution.contracts import Completed, NotApplicable

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
                deterministic_execution_enabled=True,
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
    legacy_stage_observations = []

    class _DeterministicService:
        outcome = NotApplicable("synthetic-fallback")
        calls = 0
        staged_manifests = []
        requests = []

        def execute(self, request, **kwargs):
            type(self).requests.append(request)
            type(self).calls += 1
            manifest = kwargs.get("ontology_manifest")
            if manifest is not None:
                staged_root = Path(manifest.ontology_dir)
                type(self).staged_manifests.append(
                    {
                        "root": staged_root,
                        "exists_during_execute": staged_root.is_dir(),
                        "content": (staged_root / "handler.py").read_text(encoding="utf-8"),
                    }
                )
            return self.outcome

    def run_direct_script(**kwargs):
        manifest = kwargs.get("ontology_manifest")
        if manifest is not None:
            staged_root = Path(manifest.ontology_dir)
            legacy_stage_observations.append(
                {
                    "root": staged_root,
                    "exists": staged_root.is_dir(),
                    "content": (staged_root / "handler.py").read_text(encoding="utf-8"),
                }
            )
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
        "load_system_config": lambda: {"storage": {"project_root": str(tmp_path)}},
        "DeterministicExecutionService": _DeterministicService,
        "NotApplicable": NotApplicable,
    }
    context = _context("run_direct")
    context = context.model_copy(
        update={
            "arguments": {
                "selected_ontology_id": "ontology_demo",
                "selected_ontology_version": "v1",
                "synthetic_argument": "synthetic_value",
            }
        }
    )
    adapter = OrchestratorSessionAdapter(lambda: _Orchestrator())

    _DeterministicService.outcome = Completed(
        envelope={
            "status": "success",
            "structured_result": {"text": "synthetic deterministic answer"},
            "artifacts": [],
            "execution": {"type": "deterministic", "authoritative": True},
        },
        operation_key="synthetic_operation",
        elapsed_ms=4,
    )
    deterministic = adapter._try_direct_execute_sync(context, dependencies)
    assert [event.event_type for event in deterministic] == [
        "turn.started",
        "tool.started",
        "tool.finished",
        "turn.finished",
    ]
    assert deterministic[-1].payload["structured_content"]["execution"] == {
        "type": "deterministic",
        "authoritative": True,
    }

    assert _DeterministicService.requests[-1].arguments == {
        "synthetic_argument": "synthetic_value"
    }

    ontology_dir = tmp_path / "workspaces" / "workspace_demo" / "ontologies" / "ontology_demo"
    ontology_dir.mkdir(parents=True)
    package_file = ontology_dir / "handler.py"
    package_file.write_text("old handler", encoding="utf-8")
    source_signature = build_package_revision_source_signature(ontology_dir)
    pinned_context = context.model_copy(
        update={
            "preflight_snapshot": RunPreflightSnapshot(
                workspace_id="workspace_demo",
                workspace_config_generation=1,
                system_config_generation=1,
                ontology_id="ontology_demo",
                ontology_version="v1",
                mutation_revision=get_ontology_mutation_revision(
                    "workspace_demo",
                    "ontology_demo",
                ),
                ontology_manifest={
                    "id": "ontology_demo",
                    "version": "v1",
                    "ontology_dir": str(ontology_dir),
                },
                ontology_source_signature=source_signature,
                ontology_content_revision=build_package_content_revision(ontology_dir),
                deterministic_candidate=True,
            )
        }
    )
    pinned = adapter._try_direct_execute_sync(pinned_context, dependencies)
    assert pinned[-1].status == "completed"
    staged_manifest = _DeterministicService.staged_manifests[-1]
    assert staged_manifest["exists_during_execute"] is True
    assert staged_manifest["content"] == "old handler"
    assert staged_manifest["root"].is_relative_to(tmp_path)
    assert staged_manifest["root"].parent.parent == (tmp_path / ".ontology-run-staging")
    assert ".ontology-run-staging" in staged_manifest["root"].parts
    assert not staged_manifest["root"].exists()

    _DeterministicService.outcome = NotApplicable("explicit_legacy_operation")
    _DecisionRouter.decision = SimpleNamespace(route="execute")
    legacy_pinned = adapter._try_direct_execute_sync(pinned_context, dependencies)

    assert legacy_pinned[-1].status == "completed"
    assert legacy_stage_observations[-1]["exists"] is True
    assert legacy_stage_observations[-1]["content"] == "old handler"
    assert not legacy_stage_observations[-1]["root"].exists()
    assert calls[-1]["ontology_manifest"].ontology_dir == str(legacy_stage_observations[-1]["root"])
    calls.clear()

    original_stat = package_file.stat()
    package_file.write_text("new handler", encoding="utf-8")
    os.utime(
        package_file,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    assert package_file.stat().st_size == original_stat.st_size
    assert build_package_revision_source_signature(ontology_dir) == source_signature
    calls_before_drift = _DeterministicService.calls

    drifted = adapter._try_direct_execute_sync(pinned_context, dependencies)

    assert drifted[-1].status == "failed"
    assert drifted[-1].payload["error"]["error_code"] == "ontology_revision_changed"
    assert _DeterministicService.calls == calls_before_drift
    _DeterministicService.outcome = NotApplicable("synthetic-fallback")

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
    assert calls[-1]["project_root"] == str(tmp_path)
    assert calls[-1]["timeout_seconds"] == 12
    assert _RuntimeContext.latest.workspace_id == "workspace_demo"

    def failed_direct_script(**_kwargs):
        return [], _DirectResult("error", "synthetic failure output", "synthetic_error")

    dependencies["run_direct_script"] = failed_direct_script
    failed = adapter._try_direct_execute_sync(context, dependencies)
    assert failed[-1].status == "failed"
    assert failed[-1].payload["error"]["error_code"] == "SYNTHETIC_ERROR"


def test_cancelled_deterministic_worker_rolls_back_late_artifacts(
    tmp_path,
    monkeypatch,
):
    import agent.direct_execution.service as deterministic_module
    import agent.runtime.service as service_module
    from agent.direct_execution.contracts import Completed

    chat_store = ChatStore(str(tmp_path / "chat"))
    chat_store.create_session(session_id="session_demo")
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    execution_store.start_turn(
        "session_demo",
        1,
        "run_cancelled_deterministic",
        "synthetic request",
    )
    started = threading.Event()
    stored_refs = []

    class _Orchestrator:
        agent_config = SimpleNamespace(
            direct_execution=SimpleNamespace(enabled=False),
            deterministic_execution_enabled=True,
            execute_script_timeout=5,
        )

        @staticmethod
        def _get_stores(_username, _workspace_id):
            return chat_store, execution_store

    def execute_after_cancel(
        _self,
        _request,
        *,
        artifact_writer,
        cancel_event,
        **_kwargs,
    ):
        started.set()
        deadline = time.monotonic() + 2
        while not cancel_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
        artifact_writer.prepare_artifacts(["late.txt"])
        stored = artifact_writer("late.txt", "late output", "text/plain")
        stored_refs.append(stored["output_ref"])
        return Completed(
            envelope={
                "status": "success",
                "structured_result": {"text": "late"},
                "artifacts": [
                    {
                        "name": "late.txt",
                        "media_type": "text/plain",
                        "size": 11,
                        "sha256": stored["sha256"],
                        "output_ref": stored["output_ref"],
                    }
                ],
                "execution": {"type": "deterministic"},
            },
            operation_key="operation_demo",
            elapsed_ms=1,
        )

    executor = ThreadPoolExecutor(max_workers=1)

    async def offload(function, *args, **kwargs):
        kwargs.pop("run_id", None)
        kwargs.pop("task_name", None)
        return await asyncio.get_running_loop().run_in_executor(
            executor,
            partial(function, *args, **kwargs),
        )

    monkeypatch.setattr(
        deterministic_module.DeterministicExecutionService,
        "execute",
        execute_after_cancel,
    )
    monkeypatch.setattr(service_module, "run_sync_compat", offload)
    monkeypatch.setattr(service_module, "_WORKSPACE_SCOPE_RESOLVED", True)
    monkeypatch.setattr(service_module, "_WORKSPACE_SCOPE", None)
    adapter = OrchestratorSessionAdapter(lambda: _Orchestrator())
    context = _context("run_cancelled_deterministic").model_copy(
        update={
            "arguments": {
                "selected_ontology_id": "ontology_demo",
                "selected_ontology_version": "v1",
            }
        }
    )

    async def scenario():
        task = asyncio.create_task(adapter.try_direct_execute(context))
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=8)

    try:
        asyncio.run(scenario())
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    assert len(stored_refs) == 1
    assert execution_store.get_pending_deterministic_artifacts("session_demo") == []
    assert (
        chat_store.get_tool_output(
            "session_demo",
            "run_cancelled_deterministic",
            stored_refs[0],
        )
        is None
    )


def test_noninteractive_direct_result_does_not_publish_interaction_request():
    context = _context("run_noninteractive_direct").model_copy(update={"supports_user_interaction": False})
    interaction = {
        "id": "interaction_demo",
        "type": "input",
        "prompt": "Synthetic prompt",
        "options": [],
    }
    events = [
        RuntimeEvent(
            event_type="interaction.requested",
            status="awaiting_input",
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            payload=interaction,
        ),
        RuntimeEvent(
            event_type="turn.finished",
            status="awaiting_input",
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            payload={
                "status": "awaiting_input",
                "structured_content": {
                    "type": "user_interaction",
                    "interaction": interaction,
                },
            },
        ),
    ]

    class _SessionAdapter:
        async def finalize(self, _context, _result):
            return None

        async def submit_reflection(self, _context, _result):
            return None

    class _Sink:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)

    async def scenario():
        service = AsyncExecutionService(
            _SuccessRuntime(),
            session_adapter=_SessionAdapter(),
        )
        sink = _Sink()
        result = await service._consume_events(
            context,
            sink,
            PublicEventAdapter(include_trace=False),
            events,
        )
        return result, sink.events

    result, emitted = asyncio.run(scenario())

    assert result.status == "failed"
    assert result.error.error_code == "INTERACTION_NOT_SUPPORTED"
    assert [event.event_type for event in emitted] == ["turn.finished"]
    assert emitted[0].status == "failed"


@pytest.mark.parametrize("llm_request_mode", ["stream", "sync"])
def test_noninteractive_runtime_result_publishes_normalized_terminal(
    llm_request_mode,
):
    context = _context("run_noninteractive_runtime_{}".format(llm_request_mode)).model_copy(
        update={
            "llm_request_mode": llm_request_mode,
            "supports_user_interaction": False,
        }
    )
    interaction = {
        "id": "interaction_demo",
        "type": "input",
        "prompt": "Synthetic prompt",
        "options": [],
    }

    def runtime_events():
        return [
            RuntimeEvent(
                event_type="interaction.requested",
                status="awaiting_input",
                run_id=context.run_id,
                session_id=context.session_id,
                turn_id=context.turn_id,
                payload=interaction,
            ),
            RuntimeEvent(
                event_type="turn.finished",
                status="awaiting_input",
                run_id=context.run_id,
                session_id=context.session_id,
                turn_id=context.turn_id,
                payload={
                    "status": "awaiting_input",
                    "structured_content": {
                        "type": "user_interaction",
                        "interaction": interaction,
                    },
                },
            ),
        ]

    class _Runtime:
        async def astream(self, _context):
            for event in runtime_events():
                yield event

        async def arun(self, _context, sink):
            for event in runtime_events():
                await sink.emit(event)
            return RuntimeResult(status="awaiting_input")

        async def cancel(self, run_id, reason):
            del run_id, reason
            return True

    class _Sink:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)

    async def scenario():
        sink = _Sink()
        result = await AsyncExecutionService(_Runtime()).execute(context, sink)
        return result, sink.events

    result, emitted = asyncio.run(scenario())

    assert result.status == "failed"
    assert result.error.error_code == "INTERACTION_NOT_SUPPORTED"
    assert [event.event_type for event in emitted] == ["turn.finished"]
    assert emitted[0].status == "failed"
    assert emitted[0].payload["error"]["error_code"] == "INTERACTION_NOT_SUPPORTED"
