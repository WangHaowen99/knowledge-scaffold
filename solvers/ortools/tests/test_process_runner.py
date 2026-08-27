from __future__ import annotations

import os
from threading import Event

import pytest

import solvers.ortools.process_runner as process_runner_module
from solvers.ortools.process_runner import (
    ProcessSolverRunner,
    RunnerLimits,
    UnsupportedResourceLimitsError,
)
from solvers.ortools.tests.test_refinery_route_engine import _problem


def _domain_request():
    problem = _problem()
    problem["payload"]["parameters"]["candidate_selection"][
        "candidate_evaluation_digest"
    ] = "sha256:solver-coordination.v1:" + "0" * 64
    problem["payload"]["limits"]["max_verification_candidates"] = 128
    return {
        "api_version": "ortools.solver.v1",
        "request_id": "req_SYNTHETIC_DOMAIN_0001",
        "problem": {
            "type": "binary_ordered_route",
            "version": "1.0",
            "data": problem["payload"],
        },
        "options": {"time_limit_ms": 5000, "search_workers": 1},
    }


def test_solver_runs_in_an_independent_child_process(submit_request):
    runner = ProcessSolverRunner(
        RunnerLimits(
            memory_limit_bytes=512 * 1024 * 1024,
            file_limit_bytes=1024 * 1024,
            enforce_os_limits=False,
        )
    )

    outcome = runner.run(submit_request, Event())

    assert outcome.worker_pid != os.getpid()
    assert outcome.engine_result.solve_outcome == "optimal"


def test_domain_evaluation_runs_in_the_independent_child_process():
    runner = ProcessSolverRunner(
        RunnerLimits(
            memory_limit_bytes=512 * 1024 * 1024,
            file_limit_bytes=1024 * 1024,
            enforce_os_limits=False,
        )
    )

    outcome = runner.run(_domain_request(), Event())

    assert outcome.worker_pid != os.getpid()
    assert outcome.engine_result.solve_outcome == "optimal"
    assert outcome.engine_result.assignments["candidate_id"] == (
        "OPTION_0_TO_OPTION_1_K0002"
    )
    assert outcome.engine_result.certificate["evaluation_source"] == (
        "refinery_route.v1"
    )


def test_cancelled_before_process_start_does_not_run_solver(submit_request):
    runner = ProcessSolverRunner(
        RunnerLimits(
            memory_limit_bytes=512 * 1024 * 1024,
            file_limit_bytes=1024 * 1024,
            enforce_os_limits=False,
        )
    )
    cancelled = Event()
    cancelled.set()

    outcome = runner.run(submit_request, cancelled)

    assert outcome.cancelled is True
    assert outcome.engine_result is None


def test_enforced_os_limits_fail_closed_when_platform_adapter_is_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(process_runner_module, "_PLATFORM_OS_NAME", "nt")

    with pytest.raises(
        UnsupportedResourceLimitsError,
        match="Windows Job Object support is not implemented",
    ):
        ProcessSolverRunner(
            RunnerLimits(
                memory_limit_bytes=512 * 1024 * 1024,
                file_limit_bytes=1024 * 1024,
                enforce_os_limits=True,
            )
        )
