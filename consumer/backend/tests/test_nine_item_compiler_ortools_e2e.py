from __future__ import annotations

import sys
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from backend_synthetic_credentials import synthetic_secret
from werkzeug.serving import make_server

# This test exercises the standalone Solver Service over HTTP through the
# Consumer-owned Connector.  Only the integration-test process imports the
# service application used to create the independent HTTP boundary.
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from solver_coordination.canonical import sha256_digest
from solver_coordination.compiler import ProblemIRCompiler
from solver_coordination.connectors.helpers import capability_fingerprint
from solver_coordination.connectors.http_transport import HttpSolverTransport
from solver_coordination.connectors.ortools import OrToolsConnector
from solver_coordination.contracts import NormalizedInput
from solver_coordination.fact_snapshot import FactSnapshotBuilder
from solver_coordination.presentation import ProfilePresentationService
from solver_coordination.problem_families.candidate_selection import (
    CandidateSelectionAdapter,
)
from solver_coordination.problem_families.registry import ProblemFamilyRegistry
from solver_coordination.profiles import build_default_solver_profile_registry
from solver_coordination.verification.ordered_binary_route import (
    OrderedBinaryRouteIndependentVerifier,
)

from solvers.ortools.app import create_app
from solvers.ortools.config import OrToolsSolverConfig
from solvers.ortools.tests.test_refinery_route_engine import _context as _synthetic_solver_context

_OBJECTIVES = [
    {"metric": "primary_score", "sense": "maximize"},
    {"metric": "transition_count", "sense": "minimize"},
    {"metric": "safety_margin", "sense": "maximize"},
    {"metric": "candidate_rank", "sense": "minimize"},
]
_ITEM_IDS = tuple("ITEM_SYNTHETIC_{:04d}".format(index) for index in range(1, 10))


class _LoopbackEndpointPolicy:
    """Test-only loopback policy; production defaults to HTTPS with explicit private-HTTP opt-in."""

    def validate(self, endpoint: str) -> str:
        if not endpoint.startswith("http://127.0.0.1:"):
            raise ValueError("endpoint must be a loopback test endpoint")
        return endpoint.rstrip("/")


def _solver_config(tmp_path) -> OrToolsSolverConfig:
    return OrToolsSolverConfig(
        database_path=tmp_path / "attempts.sqlite3",
        attempt_ttl_seconds=3600,
        max_payload_bytes=1024 * 1024,
        max_solve_time_ms=30_000,
        max_search_workers=1,
        max_concurrent_jobs=1,
        memory_limit_bytes=512 * 1024 * 1024,
        file_limit_bytes=1024 * 1024,
        prefer_wait_max_ms=5_000,
        service_token=synthetic_secret("nine_item_compiler_ortools_e2e-credential-01"),
        enforce_os_limits=False,
    )


def _normalized_input() -> NormalizedInput:
    # The shape mirrors the nine ordered batch fields (source, quantity,
    # duration) while all values and identifiers are synthetic.
    sources = ("SOURCE_A",) * 4 + ("SOURCE_B",) * 5
    return NormalizedInput(
        operation_key="synthetic_ordered_route",
        values={
            "plan_month": "2099-03",
            "items": [
                {
                    "item_order": index,
                    "item_id": item_id,
                    "source": sources[index - 1],
                    "quantity": "10.00",
                    "duration": "1.00",
                }
                for index, item_id in enumerate(_ITEM_IDS, start=1)
            ],
        },
    )


def _route_facts() -> dict:
    items = {}
    for index, item_id in enumerate(_ITEM_IDS, start=1):
        # Locked synthetic metrics make the intended boundary unambiguous:
        # OPTION_0 wins items 1..4 and OPTION_1 wins items 5..9.
        option_0_score = "100.00" if index <= 4 else "10.00"
        option_1_score = "10.00" if index <= 4 else "100.00"
        items[item_id] = {
            "OPTION_0": {
                "primary_score": option_0_score,
                "load": "0.00",
                "safety": "50.00",
            },
            "OPTION_1": {
                "primary_score": option_1_score,
                "load": "0.00",
                "safety": "50.00",
            },
        }
    return {
        "metric_scale": 100,
        "rounding": "ROUND_HALF_EVEN",
        "capacity_limit": "100.00",
        "items": items,
    }


def _domain_context() -> dict:
    context = deepcopy(_synthetic_solver_context(month="2099-03"))
    context["items"] = [
        {
            "item_id": item_id,
            "item_order": index,
            "source_id": "SOURCE_A" if index <= 4 else "SOURCE_B",
            "input_tons": "10.00",
            "process_days": "1.00",
        }
        for index, item_id in enumerate(_ITEM_IDS, start=1)
    ]
    return context


def _capability() -> dict:
    model = {
        "profile_id": "ordered_binary_route.v1",
        "required_facts": ["route_metrics", "refinery_route_context"],
        "fact_source": {"kind": "locked_model"},
        "materialized_facts": [
            {"name": "route_metrics", "value": _route_facts()},
            {"name": "refinery_route_context", "value": _domain_context()},
        ],
        "display_mapping": {
            "OPTION_0": "DESTINATION_A",
            "OPTION_1": "DESTINATION_B",
        },
        "objectives": list(_OBJECTIVES),
    }
    return {
        "package_id": "package-synthetic-route",
        "package_revision": "revision-synthetic-route",
        "operation_key": "synthetic_ordered_route",
        "problem_family": "candidate_selection.v1",
        "decision_model": model,
        "decision_model_digest": sha256_digest(model),
    }


def _snapshot(normalized_input: NormalizedInput):
    return FactSnapshotBuilder().build(
        workspace_id="workspace-synthetic",
        owner_id="owner-synthetic",
        package_id="package-synthetic-route",
        package_revision="revision-synthetic-route",
        normalized_input=normalized_input,
        facts=(
            {"name": "route_metrics", "value": _route_facts()},
            {"name": "refinery_route_context", "value": _domain_context()},
        ),
        bindings={"solver_binding_revision": "binding-synthetic-route"},
        as_of="2026-08-19T00:00:00+00:00",
        source_evidence=({"kind": "synthetic_locked_fixture"},),
        required_facts=("route_metrics", "refinery_route_context"),
    )


def test_nine_item_compiler_http_ortools_verifier_and_presentation_chain(tmp_path):
    normalized_input = _normalized_input()
    fact_snapshot = _snapshot(normalized_input)
    capability = _capability()
    profiles = build_default_solver_profile_registry()
    compiler = ProblemIRCompiler(registry=ProblemFamilyRegistry([CandidateSelectionAdapter(profiles)]))
    problem = compiler.compile(
        capability=capability,
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
    )
    assert problem.problem_family == "candidate_selection.v1"
    assert len(problem.parameters["candidate_selection"]["candidates"]) == 18

    # The service is deliberately started as a separate HTTP process boundary
    # inside this test.  The Consumer test imports only the platform Connector;
    # OR-Tools is loaded by the standalone Solver Service app below.
    app = create_app(_solver_config(tmp_path))
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = synthetic_secret("nine_item_compiler_ortools_e2e-credential-01")
    endpoint = "http://127.0.0.1:{}".format(server.server_port)
    transport = HttpSolverTransport(
        credential_resolver=lambda ref: token if ref == "secret://solver/synthetic" else None,
        endpoint_policy=_LoopbackEndpointPolicy(),
        timeout_seconds=5,
        max_retries=0,
    )
    connector = OrToolsConnector(transport=transport)
    binding_base = {
        "endpoint": endpoint,
        "connector_type": "ortools",
        "service_api_version": "ortools.solver.v1",
        "service_credential_ref": "secret://solver/synthetic",
    }
    try:
        capabilities = connector.capabilities(binding=binding_base)
        binding = {
            **binding_base,
            "service_credential_ref": "secret://solver/synthetic",
            "capability_fingerprint": capability_fingerprint(capabilities),
            "allowed_families": {"binary_ordered_route": ["1.0"]},
            "neutral_limits": {
                "max_payload_bytes": 1024 * 1024,
                "max_solve_time_ms": 30_000,
                "max_workers": 1,
            },
            "execution_profile_ref": "profile://ordered_binary_route.v1",
            "enabled": True,
        }
        connector.assert_binding_capabilities(binding)
        # The removed pre-federation API must not be reachable on the service.
        assert requests.get(endpoint + "/v1/health", timeout=5).status_code == 404

        idempotency_key = "hmac-sha256:" + "a" * 64
        submitted = connector.submit(
            binding=binding,
            problem=problem,
            idempotency_key=idempotency_key,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        job_id = submitted["job_id"]
        deadline = time.monotonic() + 10
        while submitted["status"] not in {"completed", "failed", "cancelled"}:
            if time.monotonic() >= deadline:
                raise AssertionError("synthetic Solver Service job did not finish")
            time.sleep(0.02)
            submitted = connector.poll(binding=binding, job_id=job_id)
        assert submitted["status"] == "completed"
        solver_result = connector.result(binding=binding, job_id=job_id)

        # This is an actual OR-Tools result, rather than a fixture backend.
        assert solver_result.engine["name"] == "ortools-cp-sat"
        assert solver_result.engine["version"] not in {"", "unknown"}
        assert solver_result.engine["service_build"].startswith("sha256:")
        assurance = solver_result.diagnostics[0]
        assert assurance["certificate"]["evaluation_source"] == "refinery_route.v1"
        assert assurance["certificate"]["candidate_count"] == 18

        verification = OrderedBinaryRouteIndependentVerifier().verify(
            problem=problem,
            capability=capability,
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            solver_result=solver_result,
        )
        assert verification.status == "verified"
        assert verification.optimality == "proven_optimal"
        assert solver_result.assignment["candidate_id"] == ("OPTION_0_TO_OPTION_1_K0004")
        assert [solver_result.assignment[item_id] for item_id in _ITEM_IDS] == [
            "OPTION_0",
            "OPTION_0",
            "OPTION_0",
            "OPTION_0",
            "OPTION_1",
            "OPTION_1",
            "OPTION_1",
            "OPTION_1",
            "OPTION_1",
        ]

        presentation = ProfilePresentationService(profiles).present(
            capability=capability,
            normalized_input=normalized_input,
            solver_result=solver_result,
            verification=verification,
        )
        assert [item["route"] for item in presentation["items"]] == [
            "DESTINATION_A",
            "DESTINATION_A",
            "DESTINATION_A",
            "DESTINATION_A",
            "DESTINATION_B",
            "DESTINATION_B",
            "DESTINATION_B",
            "DESTINATION_B",
            "DESTINATION_B",
        ]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        app.extensions["ortools_solver"].close()
