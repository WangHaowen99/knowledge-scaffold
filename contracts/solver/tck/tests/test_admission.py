# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from contracts.solver.tck.admission import (
    HttpFaultScenarioDriver,
    StrictAdmissionError,
    assert_admission_report,
)
from contracts.solver.tck.runner import FaultScenario, TckReport
from contracts.solver.tck.tests.tck_synthetic_credentials import CONTROL_TOKEN
from contracts.solver.tck.verifier import verify_binary_ordered_route_result


def _request() -> dict:
    root = Path(__file__).resolve().parents[2]
    request = json.loads((root / "examples" / "submit-request.json").read_text())
    request["request_ref"] = "req_SYNTHETIC0123456789AB"
    return request


def _result(request: dict) -> dict:
    return {
        "schema_version": "solver.result.v1",
        "request_ref": request["request_ref"],
        "problem_family": "binary_ordered_route",
        "family_version": "1.0",
        "solve_outcome": "optimal",
        "assignments": {"candidate_id": "CANDIDATE_SYNTHETIC_0001", "item_0001": "OPTION_1"},
        "objective_values": {"primary_score": 20, "candidate_rank": 1},
        "assurance": {"best_bound": 20, "gap": "0", "certificate": None},
        "engine": {
            "name": "synthetic-engine",
            "version": "1.0.0",
            "connector_build": "sha256:" + "1" * 64,
        },
    }


def test_public_verifier_recomputes_the_family_golden_result():
    request = _request()
    assert verify_binary_ordered_route_result(request=request, result=_result(request)) is True

    forged = _result(request)
    forged["assignments"]["item_0001"] = "OPTION_0"
    assert verify_binary_ordered_route_result(request=request, result=forged) is False


def test_strict_admission_rejects_skipped_or_capability_gaps():
    with pytest.raises(StrictAdmissionError):
        assert_admission_report(TckReport(passed=True, checks=("health",), skipped=("fault_429",)))
    with pytest.raises(StrictAdmissionError):
        assert_admission_report(TckReport(passed=True, checks=("health",), capability_gaps=("missing",)))

    assert assert_admission_report(TckReport(passed=True, checks=("health",))) is None


class _ControlHandler(BaseHTTPRequestHandler):
    commands: list[dict] = []

    def log_message(self, *_args):
        return

    def do_POST(self):  # pylint: disable=invalid-name
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.__class__.commands.append(payload)
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_http_fault_driver_controls_scenarios_out_of_band():
    _ControlHandler.commands = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ControlHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        driver = HttpFaultScenarioDriver(
            control_url=f"http://127.0.0.1:{server.server_port}/control",
            control_token=CONTROL_TOKEN,
        )
        scenario = driver.scenario("fault_attempt_lost", _request())
        assert isinstance(scenario, FaultScenario)
        scenario.before_submit(_request())
        scenario.after_submit({"job_id": "job_1"})
        scenario.cleanup()
        assert [command["action"] for command in _ControlHandler.commands] == [
            "set_fault",
            "transition",
            "clear",
        ]
        assert _ControlHandler.commands[0]["scenario"] == "fault_attempt_lost"
        assert _ControlHandler.commands[1]["status"] == "attempt_lost"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
