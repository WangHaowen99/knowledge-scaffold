"""Run admission helpers for queueing and rate limiting."""

import time
from dataclasses import dataclass, field
from threading import BoundedSemaphore, Lock
from typing import Callable, Dict, List, Optional


class TokenBucketRateLimiter:
    """Single-process token bucket limiter.

    ``rate_per_second`` controls steady-state admission and ``burst`` controls
    how many requests can pass instantly after an idle period.
    """

    def __init__(
        self,
        *,
        rate_per_second: int,
        burst: Optional[int] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate_per_second = max(1, int(rate_per_second))
        self.burst = max(1, int(burst if burst is not None else rate_per_second))
        self._clock = clock
        self._tokens = float(self.burst)
        self._updated_at = float(self._clock())
        self._lock = Lock()

    def try_acquire(self) -> bool:
        """Consume one token if available; return False immediately otherwise."""
        with self._lock:
            now = float(self._clock())
            elapsed = max(0.0, now - self._updated_at)
            if elapsed:
                self._tokens = min(
                    float(self.burst),
                    self._tokens + elapsed * float(self.rate_per_second),
                )
                self._updated_at = now
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True


class RunSlotManager:
    """Per-user run slot manager using bounded semaphores.

    The first *limit* concurrent requests pass through immediately; subsequent
    requests block until a slot is released.  This replaces 429 rejection with
    transparent queueing.

    Thread-safe.
    """

    def __init__(self) -> None:
        self._semaphores: Dict[str, BoundedSemaphore] = {}
        self._lock = Lock()

    def _get_or_create(self, user_id: str, limit: int) -> BoundedSemaphore:
        with self._lock:
            if user_id not in self._semaphores:
                self._semaphores[user_id] = BoundedSemaphore(limit)
            return self._semaphores[user_id]

    def acquire(self, user_id: str, limit: int, timeout: Optional[float] = None) -> bool:
        """Acquire a run slot for *user_id*, blocking up to *timeout* seconds.

        Returns True if a slot was acquired, False on timeout.
        The *limit* parameter is only used when creating a semaphore for a
        previously unseen user_id; subsequent calls for the same user_id
        share the same semaphore.
        """
        sem = self._get_or_create(user_id, limit)
        return sem.acquire(blocking=True, timeout=timeout)

    def release(self, user_id: str) -> None:
        """Release a run slot for *user_id*, waking the next waiter if any."""
        with self._lock:
            sem = self._semaphores.get(user_id)
        if sem:
            sem.release()


@dataclass
class QueuedRunAdmission:
    """One process-local run waiting for a per-user execution slot."""

    run_id: str
    user_id: str
    limit: int
    on_admit: Callable[[], None]
    enqueued_at: float = field(default_factory=time.time)


class RunAdmissionQueue:
    """Cancellable FIFO admission queue layered over ``RunSlotManager``.

    Queue order is preserved per user. Callbacks are invoked outside the queue
    lock so starting a run cannot block status and cancellation requests.
    """

    def __init__(self, slot_manager: RunSlotManager) -> None:
        self.slot_manager = slot_manager
        self._queued: List[QueuedRunAdmission] = []
        self._lock = Lock()

    def try_acquire(self, user_id: str, limit: int) -> bool:
        """Acquire immediately unless this user already has queued work."""
        with self._lock:
            if any(entry.user_id == user_id for entry in self._queued):
                return False
            return self.slot_manager.acquire(user_id, limit, timeout=0)

    def enqueue(self, entry: QueuedRunAdmission) -> Optional[int]:
        """Enqueue a run and return its per-user position, or ``None`` if admitted."""
        with self._lock:
            if any(queued.run_id == entry.run_id for queued in self._queued):
                return self._position_locked(entry.run_id)
            self._queued.append(entry)
        self._drain()
        return self.position(entry.run_id)

    def cancel(self, run_id: str) -> bool:
        removed = False
        with self._lock:
            for index, entry in enumerate(self._queued):
                if entry.run_id == run_id:
                    self._queued.pop(index)
                    removed = True
                    break
        if removed:
            self._drain()
        return removed

    def position(self, run_id: str) -> Optional[int]:
        with self._lock:
            return self._position_locked(run_id)

    def release(self, user_id: str) -> None:
        self.slot_manager.release(user_id)
        self._drain()

    def _position_locked(self, run_id: str) -> Optional[int]:
        positions: Dict[str, int] = {}
        for entry in self._queued:
            positions[entry.user_id] = positions.get(entry.user_id, 0) + 1
            if entry.run_id == run_id:
                return positions[entry.user_id]
        return None

    def _drain(self) -> None:
        admitted: List[QueuedRunAdmission] = []
        with self._lock:
            remaining: List[QueuedRunAdmission] = []
            blocked_users = set()
            for entry in self._queued:
                if entry.user_id in blocked_users:
                    remaining.append(entry)
                    continue
                if self.slot_manager.acquire(entry.user_id, entry.limit, timeout=0):
                    admitted.append(entry)
                    continue
                blocked_users.add(entry.user_id)
                remaining.append(entry)
            self._queued = remaining

        for entry in admitted:
            entry.on_admit()
