import asyncio
from contextlib import suppress
from dataclasses import replace

import pytest

from agent.runtime.contracts import (
    PublicEventDraft,
    RunScope,
    RunSnapshot,
    RunStatus,
    RuntimeInvocationContext,
)
from agent.runtime.event_bus import (
    AsyncEventBus,
    EventPublishRejectedError,
    EventSubscriberLimitExceeded,
    EventSubscriberReservationError,
)
from agent.runtime.store import InMemoryRunRepository, utc_now_iso


def _context(run_id: str) -> RuntimeInvocationContext:
    return RuntimeInvocationContext(
        run_id=run_id,
        session_id="session_{}".format(run_id),
        turn_id=1,
        workspace_id="workspace_demo",
        username="operator_a",
        query="synthetic question",
    )


def _scope() -> RunScope:
    return RunScope(
        username="operator_a",
        workspace_id="workspace_demo",
        scopes=("agent:run", "agent:read"),
    )


async def _create_running(
    repository: InMemoryRunRepository,
    run_id: str,
) -> RuntimeInvocationContext:
    context = _context(run_id)
    await repository.create(
        RunSnapshot(
            run_id=run_id,
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
    return context


def _draft(
    context: RuntimeInvocationContext,
    event_type: str = "token.delta",
    *,
    status: str = "running",
) -> PublicEventDraft:
    payload = (
        {"status": status, "final_output": "synthetic result"}
        if event_type == "turn.finished"
        else {"channel": "content", "text": "synthetic delta"}
    )
    return PublicEventDraft(
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        event_type=event_type,
        status=status,
        payload=payload,
    )


async def _start_waiting_stream(
    event_bus: AsyncEventBus,
    run_id: str,
    *,
    after_seq: int = 0,
):
    previous_count = event_bus.active_subscriber_count
    stream = event_bus.stream(run_id, after_seq=after_seq)
    pending = asyncio.create_task(anext(stream))
    for _ in range(100):
        if (
            pending.done()
            or event_bus.active_subscriber_count > previous_count
        ):
            break
        await asyncio.sleep(0)
    return stream, pending


def test_slow_persistence_for_one_run_does_not_block_another_run():
    async def run_test():
        repository = InMemoryRunRepository()
        slow_context = await _create_running(repository, "run_slow")
        fast_context = await _create_running(repository, "run_fast")
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()
        persisted = []

        async def persist(event):
            if event.run_id == slow_context.run_id:
                slow_started.set()
                await release_slow.wait()
            persisted.append((event.run_id, event.seq))

        event_bus = AsyncEventBus(repository, event_persistor=persist)
        slow_stream, slow_next = await _start_waiting_stream(
            event_bus,
            slow_context.run_id,
        )
        slow_publish = asyncio.create_task(event_bus.publish(_draft(slow_context)))
        await asyncio.wait_for(slow_started.wait(), timeout=1)
        assert not slow_next.done()

        fast_event, _snapshot = await asyncio.wait_for(
            event_bus.publish(_draft(fast_context)),
            timeout=0.2,
        )
        assert fast_event.seq == 1
        assert persisted == [(fast_context.run_id, 1)]

        release_slow.set()
        slow_event, _snapshot = await slow_publish
        assert slow_event.seq == 1
        assert persisted == [
            (fast_context.run_id, 1),
            (slow_context.run_id, 1),
        ]
        assert await asyncio.wait_for(slow_next, timeout=1) == slow_event
        await slow_stream.aclose()

    asyncio.run(run_test())


def test_all_terminal_statuses_seal_after_persist_and_fanout():
    async def run_test():
        repository = InMemoryRunRepository()
        persisted = []

        async def persist(event):
            persisted.append((event.run_id, event.seq))

        event_bus = AsyncEventBus(
            repository,
            event_persistor=persist,
            subscriber_queue_size=1,
        )
        contexts = {}
        for status in (
            RunStatus.AWAITING_INPUT,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ):
            context = await _create_running(
                repository,
                "run_terminal_{}".format(status.value),
            )
            contexts[status] = context
            stream = event_bus.stream(context.run_id)

            async def collect_live_events(event_stream=stream):
                return [event async for event in event_stream]

            collector = asyncio.create_task(collect_live_events())
            for _ in range(100):
                if event_bus.active_subscriber_count == 1:
                    break
                await asyncio.sleep(0)
            assert event_bus.active_subscriber_count == 1

            event, snapshot = await event_bus.publish(
                _draft(context, "turn.finished", status=status.value),
                snapshot_update=lambda current, terminal=status: replace(
                    current,
                    status=terminal,
                ),
            )

            assert snapshot.status == status
            assert await asyncio.wait_for(collector, timeout=1) == [event]

        completed = contexts[RunStatus.COMPLETED]
        with pytest.raises(EventPublishRejectedError) as late:
            await event_bus.publish(_draft(completed))
        assert late.value.duplicate_terminal is False
        with pytest.raises(EventPublishRejectedError) as duplicate:
            await event_bus.publish(
                _draft(completed, "turn.finished", status="completed")
            )
        assert duplicate.value.duplicate_terminal is True

        events = await repository.list_after(completed.run_id, 0)
        assert [event.event_type for event in events] == ["turn.finished"]
        assert len(persisted) == 4
        metrics = await event_bus.metrics_snapshot()
        assert metrics == {
            "active_subscribers": 0,
            "reserved_subscribers": 0,
            "lanes": 4,
            "sealed_runs": 4,
            "late_event_rejections": 1,
            "duplicate_terminal_rejections": 1,
            "slow_subscriber_disconnects": 0,
        }

        assert not hasattr(event_bus, "reopen")

    asyncio.run(run_test())


def test_default_subscriber_limits_are_three_per_run_and_one_hundred_total():
    async def run_test():
        repository = InMemoryRunRepository()
        event_bus = AsyncEventBus(repository)
        first_context = await _create_running(repository, "run_capacity_first")
        first_subscriptions = []
        for _ in range(3):
            first_subscriptions.append(
                await _start_waiting_stream(event_bus, first_context.run_id)
            )
        assert event_bus.subscriber_queue_size == 512
        with pytest.raises(EventSubscriberLimitExceeded) as per_run:
            overflow = event_bus.stream(first_context.run_id)
            await anext(overflow)
        assert per_run.value.scope == "run"
        assert per_run.value.limit == 3

        first_stream, first_pending = first_subscriptions.pop()
        first_pending.cancel()
        with suppress(asyncio.CancelledError):
            await first_pending
        await first_stream.aclose()
        all_subscriptions = list(first_subscriptions)
        for index in range(2, 100):
            context = await _create_running(
                repository,
                "run_capacity_{:03d}".format(index),
            )
            all_subscriptions.append(
                await _start_waiting_stream(event_bus, context.run_id)
            )
        assert event_bus.active_subscriber_count == 100

        overflow_context = await _create_running(
            repository,
            "run_capacity_overflow",
        )
        with pytest.raises(EventSubscriberLimitExceeded) as process:
            overflow = event_bus.stream(overflow_context.run_id)
            await anext(overflow)
        assert process.value.scope == "process"
        assert process.value.limit == 100

        for stream, pending in all_subscriptions:
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
            await stream.aclose()
        assert event_bus.active_subscriber_count == 0

    asyncio.run(run_test())


def test_reservations_are_bounded_consumed_and_explicitly_releasable():
    async def run_test():
        repository = InMemoryRunRepository()
        context = await _create_running(repository, "run_reservation")
        event_bus = AsyncEventBus(repository)

        reservations = [
            await event_bus.reserve_subscriber(context.run_id)
            for _ in range(3)
        ]
        with pytest.raises(EventSubscriberLimitExceeded):
            await event_bus.reserve_subscriber(context.run_id)
        assert event_bus.reserved_subscriber_count == 3

        await event_bus.release_reservation(reservations.pop())
        stream = event_bus.stream(
            context.run_id,
            reservation_id=reservations.pop(),
        )
        pending = asyncio.create_task(stream.__anext__())
        for _ in range(100):
            if event_bus.active_subscriber_count == 1:
                break
            await asyncio.sleep(0)
        assert event_bus.active_subscriber_count == 1
        assert event_bus.reserved_subscriber_count == 1

        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
        await stream.aclose()
        await event_bus.release_subscriber_reservation(reservations.pop())
        assert event_bus.active_subscriber_count == 0
        assert event_bus.reserved_subscriber_count == 0

    asyncio.run(run_test())


def test_reservation_for_another_run_is_not_consumed_on_rejection():
    async def run_test():
        repository = InMemoryRunRepository()
        owner = await _create_running(repository, "run_reservation_owner")
        other = await _create_running(repository, "run_reservation_other")
        event_bus = AsyncEventBus(repository)
        reservation = await event_bus.reserve_subscriber(owner.run_id)

        rejected = event_bus.stream(
            other.run_id,
            reservation_id=reservation,
        )
        with pytest.raises(EventSubscriberReservationError):
            await anext(rejected)
        assert event_bus.reserved_subscriber_count == 1

        accepted = event_bus.stream(
            owner.run_id,
            reservation_id=reservation,
        )
        pending = asyncio.create_task(anext(accepted))
        for _ in range(100):
            if event_bus.active_subscriber_count == 1:
                break
            await asyncio.sleep(0)
        assert event_bus.active_subscriber_count == 1
        assert event_bus.reserved_subscriber_count == 0
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
        await accepted.aclose()

    asyncio.run(run_test())


def test_lazy_async_stream_early_disconnects_do_not_leak_subscribers():
    async def run_test():
        repository = InMemoryRunRepository()
        context = await _create_running(repository, "run_early_disconnect")
        event_bus = AsyncEventBus(repository)

        for _ in range(128):
            stream = event_bus.stream(context.run_id)
            assert event_bus.active_subscriber_count == 0
            await stream.aclose()
            assert event_bus.active_subscriber_count == 0

        for _ in range(128):
            stream = event_bus.stream(context.run_id)
            pending = asyncio.create_task(stream.__anext__())
            for _ in range(100):
                if event_bus.active_subscriber_count == 1:
                    break
                await asyncio.sleep(0)
            assert event_bus.active_subscriber_count == 1
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
            await stream.aclose()
            assert event_bus.active_subscriber_count == 0

        metrics = await event_bus.metrics_snapshot()
        assert metrics["active_subscribers"] == 0
        assert metrics["reserved_subscribers"] == 0

    asyncio.run(run_test())


def test_slow_subscriber_is_detached_and_told_to_reconnect_from_cursor():
    async def run_test():
        repository = InMemoryRunRepository()
        context = await _create_running(repository, "run_slow_subscriber")
        event_bus = AsyncEventBus(repository, subscriber_queue_size=1)
        stream, first_next = await _start_waiting_stream(
            event_bus,
            context.run_id,
        )

        first_event, _snapshot = await event_bus.publish(_draft(context))
        assert await asyncio.wait_for(first_next, timeout=1) == first_event
        await event_bus.publish(_draft(context))
        await event_bus.publish(_draft(context))

        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=1)
        assert event_bus.active_subscriber_count == 0
        metrics = await event_bus.metrics_snapshot()
        assert metrics["slow_subscriber_disconnects"] == 1
        replay = event_bus.stream(context.run_id, after_seq=1)
        assert [
            (await asyncio.wait_for(anext(replay), timeout=1)).seq,
            (await asyncio.wait_for(anext(replay), timeout=1)).seq,
        ] == [2, 3]
        await replay.aclose()

    asyncio.run(run_test())
