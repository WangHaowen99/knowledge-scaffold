"""Run admission helpers for queueing and rate limiting."""

import time
from threading import BoundedSemaphore, Lock
from typing import Callable, Dict, Optional


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
