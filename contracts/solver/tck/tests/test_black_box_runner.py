from __future__ import annotations

import json
import time
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from contracts.solver.tck import runner as runner_module
from contracts.solver.tck.runner import (
    ConnectorTckRunner,
    FaultScenario,
    TckReport,
)
from contracts.solver.tck.tests.tck_synthetic_credentials import SERVICE_TOKEN

CONTRACT_ROOT = Path(__file__).resolve().parents[2]


def _document(name: str) -> dict:
    return json.loads((CONTRACT_ROOT / "examples" / name).read_text(encoding="utf-8"))


def _error(code: str, category: str = "contract", retryable: bool = False) -> dict:
    return {
        "error": {
            "code": code,
            "category": category,
            "message": "synthetic TCK error",
            "retryable": retryable,
            "diagnostic_ref": "diag_synthetic_tck_0001",
        }
    }


class _SyntheticConnector(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    idempotency: dict[str, tuple[bytes, str]] = {}
    jobs: dict[str, dict] = {}
    fault: str | None = None
    late_result_job: str | None = None

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if status == 429:
            self.send_header("Retry-After", "1")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == "Bearer " + SERVICE_TOKEN

    def do_GET(self) -> None:
        if self.path == "/v1/health":
            self._send(200, {"status": "ok"})
            return
        if not self._authorized():
            self._send(401, _error("AUTHENTICATION_FAILED", "authentication"))
            return
        if self.path == "/v1/capabilities":
            capabilities = _document("capabilities.json")
            capabilities["problem_families"][0]["limits"]["max_workers"] = 2
            self._send(200, capabilities)
            return
        suffix = "/result"
        if self.path.startswith("/v1/solve-jobs/") and self.path.endswith(suffix):
            job_id = self.path.removeprefix("/v1/solve-jobs/")[: -len(suffix)]
            job = self.jobs.get(job_id)
            if job is None:
                self._send(404, _error("JOB_NOT_FOUND"))
                return
            if job["status"] != "completed":
                self._send(
                    409,
                    _error(
                        "RESULT_NOT_AVAILABLE",
                        "result",
                        job["status"] in {"accepted", "running"},
                    ),
                )
                return
            result = _document("result.json")
            result["request_ref"] = job["request_ref"]
            self._send(200, result)
            return
        if self.path.startswith("/v1/solve-jobs/"):
            job_id = self.path.removeprefix("/v1/solve-jobs/")
            job = self.jobs.get(job_id)
            if job and job_id == self.late_result_job:
                # A late worker completion must not move a cancelled attempt.
                job = deepcopy(job)
            self._send(200, job) if job else self._send(404, _error("JOB_NOT_FOUND"))
            return
        self._send(404, _error("RESOURCE_NOT_FOUND"))

    def do_POST(self) -> None:
        if not self._authorized():
            self._send(401, _error("AUTHENTICATION_FAILED", "authentication"))
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if self.fault == "429":
            self._send(429, _error("CAPACITY_UNAVAILABLE", "capacity", True))
            return
        if self.fault in {"502", "503"}:
            self._send(
                int(self.fault),
                _error("CONNECTOR_UNAVAILABLE", "transport", True),
            )
            return
        if self.fault == "timeout":
            time.sleep(0.5)
            return
        key = self.headers.get("Idempotency-Key")
        if not key:
            self._send(400, _error("IDEMPOTENCY_KEY_REQUIRED"))
            return
        try:
            payload = json.loads(raw)
        except ValueError:
            self._send(400, _error("CONTRACT_INVALID"))
            return
        if (
            set(payload) != {"protocol_version", "request_ref", "problem", "requirements"}
            or payload.get("problem", {}).get("problem_family") != "binary_ordered_route"
            or payload.get("problem", {}).get("family_version") != "1.0"
        ):
            self._send(400, _error("CONTRACT_INVALID"))
            return
        if payload["requirements"]["limits"]["workers"] > 2:
            self._send(400, _error("LIMIT_EXCEEDED", "capacity"))
            return
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        existing = self.idempotency.get(key)
        if existing is not None:
            if existing[0] != canonical:
                self._send(409, _error("IDEMPOTENCY_CONFLICT"))
                return
            self._send(202, self.jobs[existing[1]])
            return
        job_id = "job_synthetic_tck_{:04d}".format(len(self.jobs) + 1)
        job = deepcopy(_document("job.json"))
        job.update(
            {
                "job_id": job_id,
                "request_ref": payload["request_ref"],
                "status": (
                    "running"
                    if self.fault in {"attempt_lost", "restart", "late_result", "execution_failed"}
                    else "completed"
                ),
            }
        )
        self.idempotency[key] = (canonical, job_id)
        self.jobs[job_id] = job
        self._send(202, job)

    def do_DELETE(self) -> None:
        if not self._authorized():
            self._send(401, _error("AUTHENTICATION_FAILED", "authentication"))
            return
        job_id = self.path.removeprefix("/v1/solve-jobs/")
        job = self.jobs.get(job_id)
        if job and job["status"] not in {"completed", "failed", "cancelled", "attempt_lost"}:
            job["status"] = "cancelled"
        self._send(200, job) if job else self._send(404, _error("JOB_NOT_FOUND"))


@contextmanager
def _server():
    _SyntheticConnector.idempotency = {}
    _SyntheticConnector.jobs = {}
    _SyntheticConnector.fault = None
    _SyntheticConnector.late_result_job = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SyntheticConnector)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}".format(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_tck_runner_uses_only_a_http_endpoint():
    runner = ConnectorTckRunner(
        base_url="http://127.0.0.1:1",
        timeout_seconds=1,
    )

    assert runner.base_url == "http://127.0.0.1:1"


def test_tck_runner_verifies_the_public_job_contract_black_box():
    with _server() as base_url:
        report = ConnectorTckRunner(
            base_url=base_url,
            timeout_seconds=2,
            bearer_token=SERVICE_TOKEN,
        ).run()

    assert report.passed is True
    assert set(report.checks) == {
        "health",
        "capabilities",
        "missing_idempotency_key",
        "unknown_field",
        "unsupported_family_version",
        "idempotent_submit",
        "idempotency_conflict",
        "job_state_machine",
        "result_contract",
        "binary_ordered_route_golden",
        "idempotent_cancel",
        "sensitive_echo",
    }
    assert set(report.skipped) == {
        "fault_429",
        "fault_502",
        "fault_503",
        "fault_timeout",
        "fault_execution_failed",
        "fault_attempt_lost",
        "fault_restart",
        "fault_late_result",
        "verifier_acceptance",
    }
    assert "fault_scenario_driver_missing" in report.capability_gaps
    assert "public_verifier_missing" in report.capability_gaps


class _FaultDriver:
    """Out-of-band test harness; runner traffic remains HTTP/JSON only."""

    def scenario(self, name, _request):
        def before_submit(_payload):
            _SyntheticConnector.fault = name.removeprefix("fault_")

        def after_submit(job):
            if name.removeprefix("fault_") in {"attempt_lost", "restart"}:
                if name.removeprefix("fault_") == "attempt_lost":
                    _SyntheticConnector.jobs[job["job_id"]]["status"] = "attempt_lost"
                # restart intentionally leaves the accepted/running state in
                # the store, then simulates a restart recovery before polling.
                elif name.removeprefix("fault_") == "restart":
                    _SyntheticConnector.jobs[job["job_id"]]["status"] = "completed"
            elif name.removeprefix("fault_") == "late_result":
                _SyntheticConnector.jobs[job["job_id"]]["status"] = "running"

        def after_failure():
            if name.removeprefix("fault_") == "execution_failed":
                for job in _SyntheticConnector.jobs.values():
                    if job["status"] == "running":
                        job["status"] = "failed"

        def after_cancel(job):
            if name.removeprefix("fault_") == "late_result":
                # Simulate a worker publishing after cancellation.  The
                # endpoint's terminal-state guard must keep cancellation.
                _SyntheticConnector.late_result_job = job["job_id"]

        def cleanup():
            _SyntheticConnector.fault = None
            _SyntheticConnector.late_result_job = None

        return FaultScenario(
            before_submit=before_submit,
            after_submit=after_submit,
            after_failure=after_failure,
            after_cancel=after_cancel,
            cleanup=cleanup,
        )


def test_tck_runner_executes_optional_fault_driver_and_public_verifier():
    with _server() as base_url:
        report = ConnectorTckRunner(
            base_url=base_url,
            timeout_seconds=0.2,
            bearer_token=SERVICE_TOKEN,
            fault_driver=_FaultDriver(),
            result_verifier=lambda *, request, result: (
                result["request_ref"] == request["request_ref"] and result["solve_outcome"] == "optimal"
            ),
        ).run()

    assert report.passed is True
    assert report.skipped == ()
    assert report.capability_gaps == ()
    assert {
        "fault_429",
        "fault_502",
        "fault_503",
        "fault_timeout",
        "fault_execution_failed",
        "fault_attempt_lost",
        "fault_restart",
        "fault_late_result",
        "verifier_acceptance",
    }.issubset(set(report.checks))


def test_public_tck_runtime_has_no_consumer_or_solver_engine_imports():
    source = Path(runner_module.__file__).read_text(encoding="utf-8")

    assert "consumer." not in source
    assert "solver_coordination" not in source
    assert "import ortools" not in source
