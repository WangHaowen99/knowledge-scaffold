from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from solver_coordination.connectors.helpers import capability_fingerprint
from solver_coordination.connectors.http_transport import HttpSolverTransport
from solver_coordination.connectors.ortools import OrToolsConnector
from solver_coordination.contracts import ProblemIR


class _LocalHttpEndpointPolicy:
    def validate(self, endpoint: str) -> str:
        if not endpoint.startswith("http://127.0.0.1:"):
            raise ValueError("test endpoint must be loopback HTTP")
        return endpoint.rstrip("/")


def _problem() -> ProblemIR:
    return ProblemIR(
        problem_id="sha256:solver.problem.v1:" + "d" * 64,
        problem_family="binary_ordered_route.v1",
        decision_space={"variables": ["selected_candidate"]},
        parameters={
            "candidate_selection": {
                "profile_id": "synthetic_http.v1",
                "candidate_count": 1,
                "candidate_evaluation_digest": "sha256:solver-coordination.v1:" + "e" * 64,
                "candidates": [
                    {
                        "candidate_id": "CANDIDATE_SYNTHETIC_0001",
                        "eligible": True,
                        "assignment": {"ITEM_SYNTHETIC_0001": "OPTION_0"},
                        "metrics": {"primary_score": 10, "candidate_rank": 0},
                    }
                ],
            }
        },
        constraints=({"kind": "select_exactly_one_candidate"},),
        objectives=({"metric": "primary_score", "sense": "maximize"},),
        limits={"max_candidates": 1, "max_verification_candidates": 1},
    )


def _capabilities():
    return {
        "api_versions": ["ortools.solver.v1"],
        "service": {
            "name": "ortools-solver-service",
            "version": "1.0.0",
            "build": "sha256:" + "1" * 64,
        },
        "engine": {"name": "ortools-cp-sat", "version": "synthetic"},
        "execution": {
            "async_jobs": True,
            "cancellation": True,
            "prefer_wait": True,
            "job_retention_seconds": 600,
            "max_concurrent_jobs": 1,
        },
        "problem_types": [
            {
                "name": "binary_ordered_route",
                "versions": ["1.0"],
                "features": ["lexicographic_objective"],
                "result_assurances": ["feasible_solution", "best_bound", "gap"],
                "limits": {
                    "max_payload_bytes": 1048576,
                    "max_solve_time_ms": 60000,
                    "max_search_workers": 1,
                },
            }
        ],
    }


def test_ortools_connector_reaches_service_api_over_real_http():
    token = "synthetic-http-service-token"
    state = {}

    class SolverHandler(BaseHTTPRequestHandler):
        def _authorized(self):
            return self.headers.get("Authorization") == "Bearer " + token

        def _write_json(self, status, payload=None):
            body = (
                json.dumps(payload, separators=(",", ":")).encode("utf-8")
                if payload is not None
                else b""
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/api/v1/jobs" or not self._authorized():
                self._write_json(401 if not self._authorized() else 404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            state["job"] = {
                "api_version": "ortools.solver.v1",
                "job_id": "job_synthetic_0001",
                "request_id": body["request_id"],
                "problem_type": "binary_ordered_route",
                "problem_version": "1.0",
                "status": "completed",
            }
            self._write_json(202, state["job"])

        def do_GET(self):
            if not self._authorized():
                self._write_json(401)
                return
            if self.path == "/api/v1/capabilities":
                self._write_json(200, _capabilities())
                return
            if self.path == "/api/v1/jobs/job_synthetic_0001":
                self._write_json(200, state["job"])
                return
            if self.path == "/api/v1/jobs/job_synthetic_0001/result":
                self._write_json(200, {
                "api_version": "ortools.solver.v1",
                "job_id": "job_synthetic_0001",
                "request_id": state["job"]["request_id"],
                "problem_type": "binary_ordered_route",
                "problem_version": "1.0",
                "solve_outcome": "optimal",
                "assignments": {"candidate_id": "CANDIDATE_SYNTHETIC_0001"},
                "objective_values": {"primary_score": 10},
                "assurance": {"best_bound": 10, "gap": "0", "certificate": None},
                "engine": {
                    "name": "ortools-cp-sat",
                    "version": "synthetic",
                    "service_build": "sha256:" + "1" * 64,
                },
                })
                return
            self._write_json(404)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SolverHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_address[1]}"
        connector = OrToolsConnector(
            transport=HttpSolverTransport(
                credential_resolver=lambda ref: token if ref == "secret://solver/http-test" else None,
                endpoint_policy=_LocalHttpEndpointPolicy(),
                timeout_seconds=5,
                max_retries=0,
            )
        )
        binding = {
            "endpoint": endpoint,
            "service_credential_ref": "secret://solver/http-test",
            "allowed_families": {"binary_ordered_route": ["1.0"]},
            "neutral_limits": {
                "max_payload_bytes": 1048576,
                "max_solve_time_ms": 60000,
                "max_workers": 1,
            },
        }
        live = connector.capabilities(binding=binding)
        binding["capability_fingerprint"] = capability_fingerprint(live)
        connector.assert_binding_capabilities(binding)

        submitted = connector.submit(
            binding=binding,
            problem=_problem(),
            idempotency_key="hmac-sha256:" + "f" * 64,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
        )

        assert submitted["status"] == "completed"
        assert connector.poll(binding=binding, job_id=submitted["job_id"])["status"] == "completed"
        result = connector.result(binding=binding, job_id=submitted["job_id"])
        assert result.status == "optimal"
        assert result.problem_id == _problem().problem_id
        assert result.assignment["candidate_id"] == "CANDIDATE_SYNTHETIC_0001"
    finally:
        server.shutdown()
        thread.join(timeout=5)
