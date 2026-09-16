# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Persist-first, per-run ordered event fan-out with bounded subscribers."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.runtime.contracts import (
    PublicEvent,
    PublicEventDraft,
    RunSnapshot,
    TERMINAL_RUN_STATUSES,
)
from agent.runtime.store import InMemoryRunRepository
from utils.logging import get_logger


logger = get_logger("runtime.event_bus")


_STREAM_CLOSED = object()
_STREAM_RECONNECT = object()

_TERMINAL_STATUS_VALUES = frozenset(status.value for status in TERMINAL_RUN_STATUSES)


class EventPublishRejectedError(RuntimeError):
    """Raised when an event arrives after a Run's terminal seal."""

    error_code = "EVENT_STREAM_SEALED"

    def __init__(self, run_id: str, *, duplicate_terminal: bool) -> None:
        self.run_id = str(run_id)
        self.duplicate_terminal = bool(duplicate_terminal)
        reason = "duplicate terminal" if duplicate_terminal else "late event"
        super().__init__("{} rejected for sealed run".format(reason))


class EventSubscriberLimitExceeded(RuntimeError):
    """Raised when the per-Run or process subscriber quota is exhausted."""

    error_code = "RUNTIME_CAPACITY_EXCEEDED"

    def __init__(self, *, scope: str, limit: int) -> None:
        self.scope = str(scope)
        self.limit = int(limit)
        super().__init__(
            "{} subscriber limit {} exceeded".format(self.scope, self.limit)
        )


class EventSubscriberReservationError(RuntimeError):
    """Raised when a subscriber reservation is missing or belongs elsewhere."""

    error_code = "SUBSCRIBER_RESERVATION_INVALID"


class EventBusClosedError(RuntimeError):
    """Raised when new work is attempted after process-level EventBus close."""

    error_code = "RUNTIME_DRAINING"


@dataclass
class _Subscriber:
    queue: asyncio.Queue[object]


@dataclass
class _RunLane:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sealed: bool = False


class AsyncEventBus:
    """Serialize each Run independently while preserving persist-first fan-out.

    The global lock owns only lane lookup, subscriber maps, and capacity. Slow
    persistence is awaited under the affected Run's lane, so another Run can
    continue assigning sequence numbers, projecting, and broadcasting.
    """

    def __init__(
        self,
        repository: InMemoryRunRepository,
        *,
        subscriber_queue_size: int = 512,
        max_subscribers_per_run: int = 3,
        max_subscribers_total: int = 100,
        event_persistor: Callable[[PublicEvent], Any] | None = None,
    ) -> None:
        self.repository = repository
        self.subscriber_queue_size = max(1, int(subscriber_queue_size))
        self.max_subscribers_per_run = max(1, int(max_subscribers_per_run))
        self.max_subscribers_total = max(1, int(max_subscribers_total))
        if self.max_subscribers_total < self.max_subscribers_per_run:
            raise ValueError(
                "max_subscribers_total must be greater than or equal to "
                "max_subscribers_per_run"
            )
        self.event_persistor = event_persistor
        self._lock = asyncio.Lock()
        self._lanes: dict[str, _RunLane] = {}
        self._subscribers: dict[str, dict[str, _Subscriber]] = {}
        self._subscriber_reservations: dict[str, str] = {}
        self._closed = False
        self._late_event_rejections = 0
        self._duplicate_terminal_rejections = 0
        self._slow_subscriber_disconnects = 0

    async def publish(
        self,
        draft: PublicEventDraft,
        *,
        snapshot_update=None,
    ) -> tuple[PublicEvent, RunSnapshot]:
        lane = await self._lane_for(draft.run_id)
        terminal = self._is_terminal(draft)
        async with lane.lock:
            current = await self.repository.get(draft.run_id)
            if current is not None and current.status in TERMINAL_RUN_STATUSES:
                lane.sealed = True
            if lane.sealed:
                self._reject_sealed(draft.run_id, duplicate_terminal=terminal)

            event, snapshot = await self.repository.append(
                draft,
                snapshot_update=snapshot_update,
            )
            await self._persist_projection(event)

            async with self._lock:
                subscribers = self._subscribers.get(draft.run_id, {})
                for subscriber_id, subscriber in list(subscribers.items()):
                    if subscribers.get(subscriber_id) is not subscriber:
                        continue
                    try:
                        subscriber.queue.put_nowait(event)
                    except asyncio.QueueFull:
                        subscribers.pop(subscriber_id, None)
                        self._slow_subscriber_disconnects += 1
                        self._replace_queue_contents(
                            subscriber.queue,
                            _STREAM_RECONNECT,
                        )
                        continue
                    if terminal:
                        subscribers.pop(subscriber_id, None)
                        self._try_put_marker(subscriber.queue, _STREAM_CLOSED)

                if not subscribers:
                    self._subscribers.pop(draft.run_id, None)
                if terminal:
                    self._release_run_reservations_locked(draft.run_id)

            # The terminal event is already present in replay, projected, and
            # offered to every live subscriber before the irreversible seal.
            if terminal:
                lane.sealed = True
            return event, snapshot

    def stream(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        reservation_id: str | None = None,
    ) -> AsyncIterator[PublicEvent]:
        """Return an ASGI-ready stream that registers on first iteration.

        Merely creating or closing the async generator cannot consume capacity.
        Queue overflow detaches the slow subscriber and closes the iterator;
        the client reconnects with its last delivered cursor.
        """

        async def consume() -> AsyncIterator[PublicEvent]:
            subscriber_id = uuid.uuid4().hex
            subscriber_queue: asyncio.Queue[object] = asyncio.Queue(
                maxsize=self.subscriber_queue_size
            )
            registered = False
            try:
                replay, closed = await self._activate_subscription(
                    run_id,
                    after_seq=after_seq,
                    subscriber_id=subscriber_id,
                    subscriber_queue=subscriber_queue,
                    reservation_id=reservation_id,
                )
                registered = not closed
                for event in replay:
                    yield event
                if closed:
                    return
                while True:
                    item = await subscriber_queue.get()
                    if item is _STREAM_CLOSED or item is _STREAM_RECONNECT:
                        return
                    if isinstance(item, PublicEvent):
                        yield item
                        # A terminal event is the durable stream boundary. The
                        # queue may be exactly full after that event, so the
                        # best-effort close marker cannot be the only exit
                        # signal for an ASGI consumer.
                        if (
                            item.event_type == "turn.finished"
                            and item.status in _TERMINAL_STATUS_VALUES
                        ):
                            return
            finally:
                if registered:
                    await self._unsubscribe(run_id, subscriber_id)
                elif reservation_id is not None:
                    await self._release_reservation_if_owned(
                        run_id,
                        reservation_id,
                    )

        return consume()

    async def reserve_subscriber(self, run_id: str) -> str:
        """Reserve capacity before a streaming admission, with explicit release."""

        reservation_id = uuid.uuid4().hex
        async with self._lock:
            self._ensure_open_locked()
            self._check_capacity_locked(run_id)
            self._subscriber_reservations[reservation_id] = run_id
        return reservation_id

    async def release_subscriber_reservation(self, reservation_id: str) -> None:
        async with self._lock:
            self._subscriber_reservations.pop(str(reservation_id), None)

    async def release_reservation(self, reservation_id: str) -> None:
        """Short alias for callers that do not expose subscriber terminology."""

        await self.release_subscriber_reservation(reservation_id)

    async def close(self, run_id: str | None = None) -> None:
        """Close one Run's transports, or close the whole process EventBus."""

        if run_id is None:
            async with self._lock:
                self._closed = True
                self._subscriber_reservations.clear()
                subscribers = [
                    subscriber
                    for run_subscribers in self._subscribers.values()
                    for subscriber in run_subscribers.values()
                ]
                self._subscribers.clear()
                self._lanes.clear()
                for subscriber in subscribers:
                    self._replace_queue_contents(
                        subscriber.queue,
                        _STREAM_CLOSED,
                    )
            return

        lane = await self._lane_for(run_id)
        async with lane.lock:
            snapshot = await self.repository.get(run_id)
            async with self._lock:
                subscribers = list(
                    self._subscribers.pop(run_id, {}).values()
                )
                self._release_run_reservations_locked(run_id)
                if (
                    snapshot is None
                    or snapshot.status in TERMINAL_RUN_STATUSES
                ) and self._lanes.get(run_id) is lane:
                    self._lanes.pop(run_id, None)
                for subscriber in subscribers:
                    self._replace_queue_contents(
                        subscriber.queue,
                        _STREAM_CLOSED,
                    )

    @property
    def active_subscriber_count(self) -> int:
        return sum(len(subscribers) for subscribers in self._subscribers.values())

    @property
    def reserved_subscriber_count(self) -> int:
        return len(self._subscriber_reservations)

    def metrics(self) -> dict[str, int]:
        """Return the non-blocking process snapshot used by readiness metrics."""

        return {
            "subscribers": self.active_subscriber_count,
            "reservations": self.reserved_subscriber_count,
            "lanes": len(self._lanes),
            "sealed_runs": sum(lane.sealed for lane in self._lanes.values()),
            "late_event_rejections": self._late_event_rejections,
            "duplicate_terminal_rejections": (
                self._duplicate_terminal_rejections
            ),
            "slow_subscriber_disconnects": self._slow_subscriber_disconnects,
        }

    async def metrics_snapshot(self) -> dict[str, int]:
        async with self._lock:
            snapshot = self.metrics()
            return {
                "active_subscribers": snapshot["subscribers"],
                "reserved_subscribers": snapshot["reservations"],
                **{
                    key: value
                    for key, value in snapshot.items()
                    if key not in {"subscribers", "reservations"}
                },
            }

    async def _activate_subscription(
        self,
        run_id: str,
        *,
        after_seq: int,
        subscriber_id: str,
        subscriber_queue: Any,
        reservation_id: str | None,
    ) -> tuple[list[PublicEvent], bool]:
        lane = await self._lane_for(run_id)
        async with lane.lock:
            replay = await self.repository.list_after(run_id, after_seq)
            snapshot = await self.repository.get(run_id)
            closed = (
                lane.sealed
                or snapshot is None
                or snapshot.status in TERMINAL_RUN_STATUSES
            )
            if snapshot is not None and snapshot.status in TERMINAL_RUN_STATUSES:
                lane.sealed = True
            async with self._lock:
                self._ensure_open_locked()
                if closed:
                    if reservation_id is not None:
                        self._consume_reservation_locked(run_id, reservation_id)
                    return replay, True
                if reservation_id is not None:
                    self._consume_reservation_locked(run_id, reservation_id)
                else:
                    self._check_capacity_locked(run_id)
                self._subscribers.setdefault(run_id, {})[subscriber_id] = (
                    _Subscriber(queue=subscriber_queue)
                )
            return replay, False

    async def _unsubscribe(self, run_id: str, subscriber_id: str) -> None:
        async with self._lock:
            subscribers = self._subscribers.get(run_id)
            if subscribers is None:
                return
            subscribers.pop(subscriber_id, None)
            if not subscribers:
                self._subscribers.pop(run_id, None)

    async def _release_reservation_if_owned(
        self,
        run_id: str,
        reservation_id: str,
    ) -> None:
        async with self._lock:
            if self._subscriber_reservations.get(reservation_id) == run_id:
                self._subscriber_reservations.pop(reservation_id, None)

    async def _lane_for(self, run_id: str) -> _RunLane:
        async with self._lock:
            self._ensure_open_locked()
            return self._lanes.setdefault(run_id, _RunLane())

    async def _persist_projection(self, event: PublicEvent) -> None:
        if self.event_persistor is None:
            return
        try:
            persisted = self.event_persistor(event)
            if inspect.isawaitable(persisted):
                await persisted
        except Exception:
            # InMemoryRunRepository remains the Run/Event authority. ChatStore
            # is a durable UI projection, attempted before fan-out, whose
            # failure cannot roll back a sequence already allocated there.
            logger.warning(
                "Failed to persist public event projection run_id=%s seq=%s",
                event.run_id,
                event.seq,
                exc_info=True,
            )

    def _check_capacity_locked(self, run_id: str) -> None:
        active_for_run = len(self._subscribers.get(run_id, {}))
        reserved_for_run = sum(
            reserved_run_id == run_id
            for reserved_run_id in self._subscriber_reservations.values()
        )
        if active_for_run + reserved_for_run >= self.max_subscribers_per_run:
            raise EventSubscriberLimitExceeded(
                scope="run",
                limit=self.max_subscribers_per_run,
            )
        if (
            self.active_subscriber_count + self.reserved_subscriber_count
            >= self.max_subscribers_total
        ):
            raise EventSubscriberLimitExceeded(
                scope="process",
                limit=self.max_subscribers_total,
            )

    def _consume_reservation_locked(
        self,
        run_id: str,
        reservation_id: str,
    ) -> None:
        reserved_run_id = self._subscriber_reservations.get(reservation_id)
        if reserved_run_id is None:
            raise EventSubscriberReservationError(
                "subscriber reservation does not exist"
            )
        if reserved_run_id != run_id:
            raise EventSubscriberReservationError(
                "subscriber reservation belongs to another run"
            )
        self._subscriber_reservations.pop(reservation_id, None)

    def _release_run_reservations_locked(self, run_id: str) -> None:
        stale: list[str] = []
        for reservation_id, reserved_run_id in self._subscriber_reservations.items():
            if reserved_run_id == run_id:
                stale.append(reservation_id)
        for reservation_id in stale:
            self._subscriber_reservations.pop(reservation_id, None)

    def _reject_sealed(self, run_id: str, *, duplicate_terminal: bool) -> None:
        if duplicate_terminal:
            self._duplicate_terminal_rejections += 1
        else:
            self._late_event_rejections += 1
        raise EventPublishRejectedError(
            run_id,
            duplicate_terminal=duplicate_terminal,
        )

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise EventBusClosedError("event bus is closed")

    @staticmethod
    def _is_terminal(draft: PublicEventDraft) -> bool:
        return (
            draft.event_type == "turn.finished"
            and str(draft.status) in _TERMINAL_STATUS_VALUES
        )

    @staticmethod
    def _try_put_marker(
        subscriber_queue: asyncio.Queue[object],
        marker: object,
    ) -> None:
        try:
            subscriber_queue.put_nowait(marker)
        except asyncio.QueueFull:
            # A successfully queued terminal event is sufficient for the
            # async consumer to stop after yielding it.
            pass

    @staticmethod
    def _replace_queue_contents(
        subscriber_queue: asyncio.Queue[object],
        marker: object,
    ) -> None:
        while True:
            try:
                subscriber_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            subscriber_queue.put_nowait(marker)
        except asyncio.QueueFull:
            pass
