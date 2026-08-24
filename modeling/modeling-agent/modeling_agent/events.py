from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import AsyncIterator

from .schemas import RuntimeEvent
from .store import RuntimeStore


class EventHub:
    def __init__(self, store: RuntimeStore) -> None:
        self._store = store
        self._subscribers: dict[str, set[asyncio.Queue[RuntimeEvent]]] = defaultdict(set)

    def publish(
        self,
        project_id: str,
        run_id: str,
        kind: str,
        payload: dict,
    ) -> RuntimeEvent:
        event = self._store.append_event(project_id, run_id, kind, payload)
        for queue in tuple(self._subscribers.get(project_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # The durable event log remains authoritative. A slow client can reconnect
                # with its last sequence instead of forcing the Agent to block.
                pass
        return event

    def publish_external_once(
        self,
        event_id: str,
        project_id: str,
        run_id: str,
        kind: str,
        payload: dict,
    ) -> RuntimeEvent | None:
        event = self._store.append_external_event_once(
            event_id, project_id, run_id, kind, payload,
        )
        if event is None:
            return None
        for queue in tuple(self._subscribers.get(project_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    async def stream(self, project_id: str, after: int = 0) -> AsyncIterator[RuntimeEvent | None]:
        queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue(maxsize=256)
        self._subscribers[project_id].add(queue)
        cursor = max(0, after)
        try:
            while True:
                replay = self._store.events_after(project_id, cursor)
                if replay:
                    for event in replay:
                        if event.seq > cursor:
                            cursor = event.seq
                            yield event
                    continue
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield None
                    continue
                if event.seq > cursor:
                    cursor = event.seq
                    yield event
        finally:
            self._subscribers[project_id].discard(queue)
            if not self._subscribers[project_id]:
                self._subscribers.pop(project_id, None)
