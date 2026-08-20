"""Single-process run and event persistence for the current deployment."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from agent.runtime.contracts import (
    PublicEvent,
    PublicEventDraft,
    RunSnapshot,
    RunStatus,
    TERMINAL_RUN_STATUSES,
)
from agent.runtime.errors import (
    EventReplayUnavailableError,
    IdempotencyConflictError,
    RunConflictError,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class InMemoryRunRepository:
    """Atomic snapshots and replayable events for one process and event loop."""

    def __init__(
        self,
        *,
        max_events_per_run: int = 5000,
        max_completed_runs: int = 1000,
    ) -> None:
        self.max_events_per_run = max(32, int(max_events_per_run))
        self.max_completed_runs = max(32, int(max_completed_runs))
        self._lock = asyncio.Lock()
        self._snapshots: OrderedDict[str, RunSnapshot] = OrderedDict()
        self._events: dict[str, list[PublicEvent]] = {}
        self._first_retained_seq: dict[str, int] = {}
        self._request_fingerprints: dict[str, str] = {}
        self._idempotency: dict[tuple[str, str, str, str], tuple[str, str]] = {}
        self._consumed_interactions: dict[str, set[str]] = {}

    async def create(
        self,
        snapshot: RunSnapshot,
        *,
        idempotency_key: Optional[str] = None,
        request_fingerprint: str = "",
    ) -> tuple[RunSnapshot, bool]:
        async with self._lock:
            if idempotency_key:
                identity = (
                    snapshot.scope.username,
                    snapshot.scope.workspace_id,
                    snapshot.session_id,
                    idempotency_key,
                )
                existing = self._idempotency.get(identity)
                if existing is not None:
                    existing_run_id, existing_fingerprint = existing
                    if existing_fingerprint != request_fingerprint:
                        raise IdempotencyConflictError(
                            "同一个 Idempotency-Key 不能用于不同请求"
                        )
                    existing_snapshot = self._snapshots.get(existing_run_id)
                    if existing_snapshot is not None:
                        return existing_snapshot, False

            if snapshot.run_id in self._snapshots:
                existing_snapshot = self._snapshots[snapshot.run_id]
                if (
                    existing_snapshot.scope != snapshot.scope
                    or request_fingerprint
                    != self._request_fingerprints.get(snapshot.run_id, "")
                ):
                    raise RunConflictError("run_id 已被其他运行占用")
                return existing_snapshot, False
            self._snapshots[snapshot.run_id] = snapshot
            self._events[snapshot.run_id] = []
            self._first_retained_seq[snapshot.run_id] = 1
            self._request_fingerprints[snapshot.run_id] = request_fingerprint
            self._consumed_interactions[snapshot.run_id] = set()
            if idempotency_key:
                self._idempotency[
                    (
                        snapshot.scope.username,
                        snapshot.scope.workspace_id,
                        snapshot.session_id,
                        idempotency_key,
                    )
                ] = (snapshot.run_id, request_fingerprint)
            self._evict_completed_locked()
            return snapshot, True

    async def lookup_idempotency(
        self,
        *,
        scope: Any,
        session_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> Optional[RunSnapshot]:
        async with self._lock:
            existing = self._idempotency.get(
                (
                    scope.username,
                    scope.workspace_id,
                    session_id,
                    idempotency_key,
                )
            )
            if existing is None:
                return None
            run_id, fingerprint = existing
            if fingerprint != request_fingerprint:
                raise IdempotencyConflictError(
                    "同一个 Idempotency-Key 不能用于不同请求"
                )
            return self._snapshots.get(run_id)

    async def get(self, run_id: str) -> Optional[RunSnapshot]:
        async with self._lock:
            return self._snapshots.get(run_id)

    async def list_snapshots(self) -> list[RunSnapshot]:
        async with self._lock:
            return list(self._snapshots.values())

    async def transition(
        self,
        run_id: str,
        *,
        expected: set[RunStatus],
        status: RunStatus,
        result: Any = None,
        error: Any = None,
        pending_interaction: Any = None,
        cancel_reason: Any = None,
    ) -> RunSnapshot:
        async with self._lock:
            snapshot = self._require_snapshot_locked(run_id)
            if snapshot.status not in expected:
                raise RunConflictError(
                    "运行状态不允许从 {} 转换为 {}".format(
                        snapshot.status.value,
                        status.value,
                    )
                )
            updated = replace(
                snapshot,
                status=status,
                updated_at=utc_now_iso(),
                result=result,
                error=error,
                pending_interaction=pending_interaction,
                cancel_reason=cancel_reason,
            )
            self._snapshots[run_id] = updated
            self._snapshots.move_to_end(run_id)
            self._evict_completed_locked()
            return updated

    async def replace_context(
        self,
        run_id: str,
        context: Any,
        *,
        expected: set[RunStatus],
    ) -> RunSnapshot:
        async with self._lock:
            snapshot = self._require_snapshot_locked(run_id)
            if snapshot.status not in expected:
                raise RunConflictError(
                    "当前运行状态不允许更新 Runtime 上下文"
                )
            updated = replace(
                snapshot,
                context=context,
                # Context preparation allocates the durable turn number. Keep
                # the snapshot identity in lockstep so run-level events such
                # as ``run.started`` cannot reference the placeholder turn.
                turn_id=context.turn_id,
                updated_at=utc_now_iso(),
            )
            self._snapshots[run_id] = updated
            return updated

    async def append(
        self,
        draft: PublicEventDraft,
        *,
        snapshot_update: Optional[Callable[[RunSnapshot], RunSnapshot]] = None,
    ) -> tuple[PublicEvent, RunSnapshot]:
        async with self._lock:
            snapshot = self._require_snapshot_locked(draft.run_id)
            seq = snapshot.last_seq + 1
            event = PublicEvent(
                schema_version=draft.schema_version,
                run_id=draft.run_id,
                session_id=draft.session_id,
                turn_id=draft.turn_id,
                event_type=draft.event_type,
                status=draft.status,
                source=draft.source,
                payload=draft.payload,
                created_at=draft.created_at,
                event_id="{}:{}".format(draft.run_id, seq),
                seq=seq,
            )
            events = self._events.setdefault(draft.run_id, [])
            events.append(event)
            if len(events) > self.max_events_per_run:
                removed = len(events) - self.max_events_per_run
                del events[:removed]
                self._first_retained_seq[draft.run_id] = events[0].seq

            updated = replace(
                snapshot,
                last_seq=seq,
                updated_at=utc_now_iso(),
            )
            if snapshot_update is not None:
                updated = snapshot_update(updated)
            self._snapshots[draft.run_id] = updated
            self._snapshots.move_to_end(draft.run_id)
            self._evict_completed_locked()
            return event, updated

    async def list_after(self, run_id: str, after_seq: int) -> list[PublicEvent]:
        async with self._lock:
            self._require_snapshot_locked(run_id)
            cursor = max(0, int(after_seq or 0))
            first_retained = self._first_retained_seq.get(run_id, 1)
            if cursor < first_retained - 1:
                raise EventReplayUnavailableError("请求的事件已超出保留范围")
            return [
                event
                for event in self._events.get(run_id, [])
                if event.seq > cursor
            ]

    async def has_event_after(
        self,
        run_id: str,
        after_seq: int,
        event_type: str,
    ) -> bool:
        async with self._lock:
            self._require_snapshot_locked(run_id)
            return any(
                event.seq > after_seq and event.event_type == event_type
                for event in self._events.get(run_id, [])
            )

    async def latest_event_type(self, run_id: str) -> Optional[str]:
        async with self._lock:
            self._require_snapshot_locked(run_id)
            events = self._events.get(run_id, [])
            return events[-1].event_type if events else None

    async def consume_interaction(self, run_id: str, interaction_id: str) -> bool:
        """Atomically mark one HITL response as consumed for this retained run."""
        async with self._lock:
            self._require_snapshot_locked(run_id)
            consumed = self._consumed_interactions.setdefault(run_id, set())
            if interaction_id in consumed:
                return False
            consumed.add(interaction_id)
            return True

    async def interaction_consumed(self, run_id: str, interaction_id: str) -> bool:
        async with self._lock:
            self._require_snapshot_locked(run_id)
            return interaction_id in self._consumed_interactions.get(run_id, set())

    def _require_snapshot_locked(self, run_id: str) -> RunSnapshot:
        snapshot = self._snapshots.get(run_id)
        if snapshot is None:
            raise KeyError(run_id)
        return snapshot

    def _evict_completed_locked(self) -> None:
        completed = [
            run_id
            for run_id, snapshot in self._snapshots.items()
            if snapshot.status in TERMINAL_RUN_STATUSES
        ]
        excess = len(completed) - self.max_completed_runs
        for run_id in completed[:max(0, excess)]:
            self._snapshots.pop(run_id, None)
            self._events.pop(run_id, None)
            self._first_retained_seq.pop(run_id, None)
            self._request_fingerprints.pop(run_id, None)
            self._consumed_interactions.pop(run_id, None)
            stale_keys = [
                key
                for key, value in self._idempotency.items()
                if value[0] == run_id
            ]
            for key in stale_keys:
                self._idempotency.pop(key, None)
