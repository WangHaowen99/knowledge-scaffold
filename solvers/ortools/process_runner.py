# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Spawn-isolated execution for one OR-Tools solve attempt."""

from __future__ import annotations

from dataclasses import dataclass
import multiprocessing
import os
import queue
from threading import Event
from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from .engine.candidate_selection import EngineResult


_PLATFORM_OS_NAME = os.name


class UnsupportedResourceLimitsError(RuntimeError):
    """Raised when requested OS limits have no tested platform adapter."""


@dataclass(frozen=True)
class RunnerLimits:
    memory_limit_bytes: int
    file_limit_bytes: int
    enforce_os_limits: bool


@dataclass(frozen=True)
class RunnerOutcome:
    engine_result: EngineResult | None
    worker_pid: int | None
    cancelled: bool = False
    error_code: str | None = None


def _apply_posix_limits(*, cpu_seconds: int, memory_bytes: int, file_bytes: int) -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))


def _worker(
    request: Mapping[str, Any], limits: RunnerLimits, output: Any
) -> None:
    try:
        options = request["options"]
        solve_time_ms = int(options["time_limit_ms"])
        if limits.enforce_os_limits:
            # The parent validates this before spawning. Keep the child guard as
            # defense in depth so a direct worker invocation cannot bypass the
            # unsupported-platform decision.
            if _PLATFORM_OS_NAME != "posix":
                raise UnsupportedResourceLimitsError(
                    "Windows Job Object support is not implemented"
                )
            _apply_posix_limits(
                cpu_seconds=max(1, (solve_time_ms + 999) // 1000 + 1),
                memory_bytes=limits.memory_limit_bytes,
                file_bytes=limits.file_limit_bytes,
            )
        from .engine.candidate_selection import solve_candidate_selection
        from .engine.refinery_route import solve_refinery_ordered_route

        problem = {
            "schema_version": "solver.problem.v1",
            "problem_family": request["problem"]["type"],
            "family_version": request["problem"]["version"],
            "payload": request["problem"]["data"],
        }
        parameters = problem["payload"].get("parameters")
        if isinstance(parameters, Mapping) and "domain_evaluation" in parameters:
            result = solve_refinery_ordered_route(
                problem,
                solve_time_ms=solve_time_ms,
                workers=int(options["search_workers"]),
            )
        else:
            result = solve_candidate_selection(
                problem,
                solve_time_ms=solve_time_ms,
                workers=int(options["search_workers"]),
            )
        output.put(("ok", os.getpid(), result))
    except TimeoutError:
        output.put(("error", os.getpid(), "SOLVE_TIME_LIMIT_EXCEEDED"))
    except BaseException:
        # Never serialize vendor exceptions or local paths across the boundary.
        output.put(("error", os.getpid(), "SOLVER_EXECUTION_FAILED"))


class ProcessSolverRunner:
    def __init__(self, limits: RunnerLimits) -> None:
        if limits.enforce_os_limits and _PLATFORM_OS_NAME != "posix":
            raise UnsupportedResourceLimitsError(
                "Windows Job Object support is not implemented; "
                "set ORTOOLS_SOLVER_ENFORCE_OS_LIMITS=0 only for a "
                "non-production synthetic run"
            )
        self.limits = limits

    def run(
        self, request: Mapping[str, Any], cancelled: Event
    ) -> RunnerOutcome:
        if cancelled.is_set():
            return RunnerOutcome(None, None, cancelled=True)
        context = multiprocessing.get_context("spawn")
        output = context.Queue(maxsize=1)
        process = context.Process(
            target=_worker,
            args=(dict(request), self.limits, output),
            daemon=False,
        )
        process.start()
        solve_time_ms = int(request["options"]["time_limit_ms"])
        timeout_seconds = solve_time_ms / 1000.0 + 2.0
        elapsed = 0.0
        interval = 0.02
        while process.is_alive() and elapsed < timeout_seconds:
            if cancelled.wait(interval):
                process.terminate()
                process.join(timeout=1.0)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1.0)
                return RunnerOutcome(None, process.pid, cancelled=True)
            elapsed += interval
        if process.is_alive():
            process.terminate()
            process.join(timeout=1.0)
            if process.is_alive():
                process.kill()
                process.join(timeout=1.0)
            return RunnerOutcome(None, process.pid, error_code="SOLVE_TIME_LIMIT_EXCEEDED")
        try:
            kind, pid, value = output.get(timeout=1.0)
        except queue.Empty:
            return RunnerOutcome(None, process.pid, error_code="SOLVER_EXECUTION_FAILED")
        finally:
            output.close()
        if kind != "ok" or not hasattr(value, "solve_outcome"):
            return RunnerOutcome(None, pid, error_code=str(value))
        return RunnerOutcome(value, pid)


__all__ = [
    "ProcessSolverRunner",
    "RunnerLimits",
    "RunnerOutcome",
    "UnsupportedResourceLimitsError",
]
