"""Background worker that advances in-flight skill-evolution tasks.

Mirrors the operation-evolution patcher lifecycle: a single daemon thread,
process-level singleton. Assumes onto-agent runs single-process (same
constraint as the operation patcher — multi-process needs a distributed lock).

Each tick:
  1. Scan every workspace's in-flight tasks.
  2. For each: if created_at age exceeds task_timeout -> fail; else poll_once.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional

from config.system_config import load_system_config
from utils.logging import get_logger
from agent.skill_evolution.models import PENDING_STATUSES, TaskRecord
from agent.skill_evolution.service import SkillEvolutionService
from agent.skill_evolution.store import SkillEvolutionStore

logger = get_logger("skill_evolution.worker")


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


class SkillEvolutionWorker:
    def __init__(
        self,
        store: SkillEvolutionStore,
        service: SkillEvolutionService,
        *,
        poll_interval_seconds: int = 10,
        task_timeout_seconds: int = 1800,
    ) -> None:
        self._store = store
        self._service = service
        # Initial defaults; re-read from config every tick in _run_iteration so
        # runtime changes to poll_interval / task_timeout / enabled take effect
        # without restarting the worker (see _read_live_config).
        self._poll_interval = max(1, int(poll_interval_seconds))
        self._task_timeout = max(60, int(task_timeout_seconds))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _read_live_config(self):
        """Read the current skill_evolution config from disk.

        The worker is config-reactive: it does NOT need a reconcile call on
        config change. Every tick it re-reads enabled / poll_interval /
        task_timeout here, so flipping ``enabled`` or changing intervals at
        runtime (PATCH, file edit, env) takes effect within one poll cycle
        without stopping/restarting the thread.
        """
        cfg = load_system_config().get("skill_evolution") or {}
        try:
            enabled = bool(cfg.get("enabled", False))
            poll = max(1, int(cfg.get("poll_interval_seconds", 10)))
            timeout = max(60, int(cfg.get("task_timeout_seconds", 1800)))
        except (TypeError, ValueError):
            enabled, poll, timeout = False, self._poll_interval, self._task_timeout
        return enabled, poll, timeout

    def _run_iteration(self) -> None:
        """One tick: refresh live config, then advance tasks only if enabled."""
        enabled, poll, timeout = self._read_live_config()
        self._poll_interval = poll
        self._task_timeout = timeout
        if enabled:
            self._tick()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # Restart recovery: mark in-flight tasks interrupted so users re-initiate.
        try:
            total = 0
            for ws_id in self._store.iter_workspace_ids():
                total += self._service.mark_inflight_interrupted(ws_id)
            if total:
                logger.info("skill-evolution: marked %d in-flight tasks interrupted on startup", total)
        except Exception:
            logger.exception("skill-evolution: startup recovery failed")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="skill-evolution-worker", daemon=True)
        self._thread.start()
        logger.info("skill-evolution worker started (poll=%ss, timeout=%ss)", self._poll_interval, self._task_timeout)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_iteration()
            except Exception:
                logger.exception("skill-evolution worker tick failed")
            self._stop.wait(self._poll_interval)

    def _tick(self) -> None:
        now = time.time()
        for ws_id in self._store.iter_workspace_ids():
            for snapshot in self._store.list_tasks(ws_id):
                if snapshot.status not in ("submitted", "running", "downloading"):
                    continue
                # Re-read the latest on-disk state before acting — a route may
                # have approved/rejected/cancelled since list_tasks snapshotted.
                record = self._store.get_task(ws_id, snapshot.task_id)
                if record is None or record.status not in ("submitted", "running", "downloading"):
                    continue
                if self._is_timed_out(record, now):
                    self._service.fail_task(
                        record,
                        stage="timeout",
                        message="进化任务超时（超过 {} 秒）".format(self._task_timeout),
                    )
                    continue
                try:
                    self._service.poll_once(record)
                except Exception:
                    logger.exception("skill-evolution: poll_once failed for task %s", record.task_id)

    def _is_timed_out(self, record: TaskRecord, now: float) -> bool:
        created = _parse_iso(record.created_at)
        if created is None:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = now - created.timestamp()
        return age > self._task_timeout
