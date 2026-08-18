"""Persist-first event fan-out with bounded per-subscriber queues."""

from __future__ import annotations

import asyncio
import inspect
import queue
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from agent.runtime.contracts import (
    PublicEvent,
    PublicEventDraft,
    RunSnapshot,
    RunStatus,
    TERMINAL_RUN_STATUSES,
)
from agent.runtime.store import InMemoryRunRepository
from utils.logging import get_logger


logger = get_logger("runtime.event_bus")


STREAM_CLOSED = object()
STREAM_RECONNECT = object()


@dataclass
class BlockingSubscription:
    run_id: str
    subscriber_id: str
    queue: "queue.Queue[object]"
    replay: list[PublicEvent]
    closed: bool


class AsyncEventBus:
    def __init__(
        self,
        repository: InMemoryRunRepository,
        *,
        subscriber_queue_size: int = 512,
        event_persistor: Callable[[PublicEvent], Any] | None = None,
    ) -> None:
        self.repository = repository
        self.subscriber_queue_size = max(1, int(subscriber_queue_size))
        self.event_persistor = event_persistor
        self._lock = asyncio.Lock()
        self._subscribers: dict[str, dict[str, "queue.Queue[object]"]] = {}

    async def publish(
        self,
        draft: PublicEventDraft,
        *,
        snapshot_update=None,
    ) -> tuple[PublicEvent, RunSnapshot]:
        async with self._lock:
            # Subscription takes the same lock while loading replay. This makes
            # replay registration, durable projection, and fan-out one ordered
            # operation. Keeping persistence under this lock also prevents a
            # slower seq=N projection from being broadcast after seq=N+1.
            event, snapshot = await self.repository.append(
                draft,
                snapshot_update=snapshot_update,
            )
            if self.event_persistor is not None:
                try:
                    persisted = self.event_persistor(event)
                    if inspect.isawaitable(persisted):
                        await persisted
                except Exception:
                    # The Run/EventStore remains authoritative. ChatStore is a
                    # durable UI projection and must not corrupt the run state
                    # if its append fails.
                    logger.warning(
                        "Failed to persist public event projection run_id=%s seq=%s",
                        event.run_id,
                        event.seq,
                        exc_info=True,
                    )

            subscribers = list(self._subscribers.get(draft.run_id, {}).items())
            for subscriber_id, subscriber_queue in subscribers:
                if (
                    self._subscribers.get(draft.run_id, {}).get(subscriber_id)
                    is not subscriber_queue
                ):
                    continue
                try:
                    subscriber_queue.put_nowait(event)
                except queue.Full:
                    self._subscribers.get(draft.run_id, {}).pop(subscriber_id, None)
                    self._replace_queue_contents(subscriber_queue, STREAM_RECONNECT)
        return event, snapshot

    async def subscribe(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
    ) -> BlockingSubscription:
        async with self._lock:
            replay = await self.repository.list_after(run_id, after_seq)
            snapshot = await self.repository.get(run_id)
            subscriber_id = uuid.uuid4().hex
            subscriber_queue: "queue.Queue[object]" = queue.Queue(
                maxsize=self.subscriber_queue_size
            )
            closed = snapshot is None or snapshot.status in {
                *TERMINAL_RUN_STATUSES,
                RunStatus.AWAITING_INPUT,
            }
            if not closed:
                self._subscribers.setdefault(run_id, {})[subscriber_id] = subscriber_queue
            return BlockingSubscription(
                run_id=run_id,
                subscriber_id=subscriber_id,
                queue=subscriber_queue,
                replay=replay,
                closed=closed,
            )

    async def unsubscribe(self, subscription: BlockingSubscription) -> None:
        async with self._lock:
            subscribers = self._subscribers.get(subscription.run_id)
            if subscribers is None:
                return
            subscribers.pop(subscription.subscriber_id, None)
            if not subscribers:
                self._subscribers.pop(subscription.run_id, None)

    async def reopen(self, run_id: str) -> None:
        # Closed/open state is derived from the repository snapshot. The
        # RunManager transitions awaiting_input to running before reopening.
        del run_id

    async def close(self, run_id: str) -> None:
        async with self._lock:
            subscribers = list(self._subscribers.pop(run_id, {}).values())
            terminal_is_latest = (
                await self.repository.latest_event_type(run_id)
                == "turn.finished"
            )
            for subscriber_queue in subscribers:
                try:
                    subscriber_queue.put_nowait(STREAM_CLOSED)
                except queue.Full:
                    if not terminal_is_latest:
                        self._replace_queue_contents(
                            subscriber_queue,
                            STREAM_RECONNECT,
                        )
                    # Otherwise the terminal event is already queued. The
                    # consumer exits when it observes turn.finished.

    @staticmethod
    def _replace_queue_contents(
        subscriber_queue: "queue.Queue[object]",
        marker: object,
    ) -> None:
        while True:
            try:
                subscriber_queue.get_nowait()
            except queue.Empty:
                break
        subscriber_queue.put_nowait(marker)
