# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Job lifecycle service for the ``ortools.solver.v1`` Solver Service."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from threading import BoundedSemaphore, Event, RLock
from typing import Any, Mapping

from contracts.solver_services.ortools.validation import canonical_json_bytes

from .config import OrToolsSolverConfig
from .engine.candidate_selection import engine_version
from .process_runner import ProcessSolverRunner, RunnerLimits
from .result_serializer import serialize_result
from .store import AttemptRecord, AttemptStore, IdempotencyConflict


class CapacityUnavailable(RuntimeError):
    """No execution slot is available for a new idempotency key."""


class LimitExceeded(ValueError):
    """A request exceeds a service hard limit."""


class OrToolsSolverService:
    def __init__(self, config: OrToolsSolverConfig) -> None:
        self.config = config
        self.store = AttemptStore(
            config.database_path, ttl_seconds=config.attempt_ttl_seconds
        )
        self.runner = ProcessSolverRunner(
            RunnerLimits(
                memory_limit_bytes=config.memory_limit_bytes,
                file_limit_bytes=config.file_limit_bytes,
                enforce_os_limits=config.enforce_os_limits,
            )
        )
        self._executor = ThreadPoolExecutor(
            max_workers=config.max_concurrent_jobs,
            thread_name_prefix="ortools-solver",
        )
        self._capacity = BoundedSemaphore(config.max_concurrent_jobs)
        self._events: dict[str, Event] = {}
        self._held_capacity: set[str] = set()
        self._lock = RLock()
        self._submit_lock = RLock()
        self._dispatch_paused = False

    def pause_dispatch(self) -> None:
        self._dispatch_paused = True

    def resume_dispatch(self) -> None:
        self._dispatch_paused = False

    def restart_for_admission(self) -> None:
        with self._lock:
            if self._events or self._held_capacity:
                raise RuntimeError("cannot reopen solver store while a worker is active")
            self.store.reopen_after_restart()

    def capabilities(self) -> dict[str, Any]:
        return {
            "api_versions": ["ortools.solver.v1"],
            "service": {
                "name": "ortools-solver-service",
                "version": "1.0.0",
                "build": self.config.service_build,
            },
            "engine": {"name": "ortools-cp-sat", "version": engine_version()},
            "execution": {
                "async_jobs": True,
                "cancellation": True,
                "prefer_wait": True,
                "job_retention_seconds": self.config.attempt_ttl_seconds,
                "max_concurrent_jobs": self.config.max_concurrent_jobs,
            },
            "problem_types": [
                {
                    "name": "binary_ordered_route",
                    "versions": ["1.0"],
                    "features": [
                        "lexicographic_objective",
                        "domain_candidate_evaluation",
                    ],
                    "result_assurances": [
                        "feasible_solution",
                        "best_bound",
                        "gap",
                    ],
                    "limits": {
                        "max_payload_bytes": self.config.max_payload_bytes,
                        "max_solve_time_ms": self.config.max_solve_time_ms,
                        "max_search_workers": 1,
                    },
                }
            ],
        }

    def _validate_limits(self, document: Mapping[str, Any]) -> None:
        options = document["options"]
        if options["time_limit_ms"] > self.config.max_solve_time_ms:
            raise LimitExceeded("request exceeds solver hard limits")
        if options["search_workers"] != 1:
            raise LimitExceeded("search_workers must be 1")

    @staticmethod
    def _request_digest(document: Mapping[str, Any]) -> str:
        return "sha256:" + hashlib.sha256(canonical_json_bytes(document)).hexdigest()

    def submit(
        self, *, idempotency_key: str, document: Mapping[str, Any]
    ) -> tuple[AttemptRecord, bool]:
        self._validate_limits(document)
        digest = self._request_digest(document)
        with self._submit_lock:
            existing = self.store.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                if existing.request_digest != digest:
                    raise IdempotencyConflict("idempotency key payload conflict")
                return existing, False

            acquired = False
            transferred = False
            try:
                if not self._dispatch_paused:
                    if not self._capacity.acquire(blocking=False):
                        raise CapacityUnavailable("solver capacity is exhausted")
                    acquired = True
                record, created = self.store.create_or_get(
                    idempotency_key=idempotency_key,
                    request_digest=digest,
                    request_id=document["request_id"],
                    problem_type=document["problem"]["type"],
                    problem_version=document["problem"]["version"],
                )
                if not created:
                    return record, False
                if self._dispatch_paused:
                    return record, True
                cancellation = Event()
                with self._lock:
                    self._events[record.job_id] = cancellation
                    self._held_capacity.add(record.job_id)
                    transferred = True
                try:
                    self._executor.submit(
                        self._execute, record.job_id, dict(document), cancellation
                    )
                except BaseException:
                    with self._lock:
                        self._events.pop(record.job_id, None)
                        self._held_capacity.discard(record.job_id)
                    transferred = False
                    raise
                return record, True
            finally:
                if acquired and not transferred:
                    self._capacity.release()

    def _execute(
        self, job_id: str, document: Mapping[str, Any], cancellation: Event
    ) -> None:
        try:
            current = self.store.get(job_id)
            if current is None or current.status == "cancelled":
                return
            self.store.update_status(job_id, "running")
            outcome = self.runner.run(document, cancellation)
            if outcome.cancelled or cancellation.is_set():
                self.store.cancel(job_id)
                return
            if outcome.engine_result is None:
                self.store.update_status(
                    job_id,
                    "failed",
                    error_code=outcome.error_code or "SOLVER_EXECUTION_FAILED",
                )
                return
            result = serialize_result(
                job_id=job_id,
                request_id=document["request_id"],
                problem=document["problem"],
                engine_result=outcome.engine_result,
                service_build=self.config.service_build,
            )
            self.store.complete(job_id, result)
        except BaseException:
            current = self.store.get(job_id)
            if current is not None and current.status not in {"cancelled", "completed"}:
                self.store.update_status(job_id, "failed", error_code="SOLVER_EXECUTION_FAILED")
        finally:
            with self._lock:
                self._events.pop(job_id, None)
                held = job_id in self._held_capacity
                self._held_capacity.discard(job_id)
            if held:
                self._capacity.release()

    def cancel(self, job_id: str) -> AttemptRecord | None:
        with self._lock:
            event = self._events.get(job_id)
        if event is not None:
            event.set()
        return self.store.cancel(job_id)

    def get(self, job_id: str) -> AttemptRecord | None:
        return self.store.get(job_id)

    def close(self) -> None:
        with self._lock:
            events = tuple(self._events.values())
        for event in events:
            event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)
        self.store.close()


__all__ = [
    "CapacityUnavailable",
    "IdempotencyConflict",
    "LimitExceeded",
    "OrToolsSolverService",
]
