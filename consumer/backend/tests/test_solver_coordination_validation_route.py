from __future__ import annotations

from internal_access import internal
import asyncio
import json
import secrets
import threading
import time
from types import SimpleNamespace

import api.routes.solver_coordination as solver_routes
import pytest
from api.auth.auth import AuthManager
from api.routes.solver_coordination import (
    solver_coordination_public_router,
    solver_coordination_router,
)
from api.testing import TestApplication
from fastapi.testclient import TestClient
from solver_coordination import (
    CandidateValidationResult,
    CandidateValidationStatus,
    ValidationContext,
    ValidationRunService,
)
from solver_coordination.run_ledger import SolverRunLedger
from workspace.workspaces import create_workspace


def _payload(item_count: int = 9):
    return {
        "knowledge_package_id": "knowledge_package_demo",
        "knowledge_package_revision": "revision_demo_1",
        "plan": {
            "option_domain": ["OPTION_0", "OPTION_1"],
            "max_transitions": 1,
            "items": [
                {
                    "item_order": index,
                    "item_id": "ITEM_SYNTHETIC_{}".format(index),
                    "quantity": str(1000 + index),
                    "duration": "1.5",
                }
                for index in range(1, item_count + 1)
            ],
        },
    }


class _SyntheticValidator:
    def __init__(
        self,
        failed_candidate_id: str | None = None,
        infeasible_candidate_id: str | None = None,
    ):
        self.failed_candidate_id = failed_candidate_id
        self.infeasible_candidate_id = infeasible_candidate_id
        self.calls = 0

    def validate(self, candidate_set, candidate, *, context):
        self.calls += 1
        if candidate.candidate_id == self.failed_candidate_id:
            raise RuntimeError("synthetic candidate failure")
        status = (
            CandidateValidationStatus.INFEASIBLE
            if candidate.candidate_id == self.infeasible_candidate_id
            else CandidateValidationStatus.FEASIBLE
        )
        return CandidateValidationResult(
            candidate_id=candidate.candidate_id,
            candidate_digest=candidate.candidate_digest,
            status=status,
            knowledge_package_id=candidate_set.knowledge_package_id,
            knowledge_package_revision=candidate_set.knowledge_package_revision,
            fact_snapshot_digest=context.fact_snapshot_digest,
            rules_revision=context.rules_revision,
            evidence_ref="validation_evidence_synthetic_{}".format(candidate.candidate_id.lower()),
        )


class _RecordingPool:
    def __init__(self, name, calls):
        self.name = name
        self.calls = calls

    async def run(self, function, *args, **kwargs):
        self.calls.append(self.name)
        return await asyncio.to_thread(function, *args, **kwargs)


class _TimeoutPool:
    async def run(self, *_args, **_kwargs):
        raise TimeoutError("synthetic solver pool timeout")


def _install_runtime_pools(client, calls, *, tool_pool=None):
    client.app.state.runtime_container = SimpleNamespace(
        external_io=_RecordingPool("external_io", calls),
        short_io=_RecordingPool("short_io", calls),
        tool_io=tool_pool or _RecordingPool("tool_io", calls),
    )


def _client(
    tmp_path,
    monkeypatch,
    *,
    failed_candidate_id: str | None = None,
    infeasible_candidate_id: str | None = None,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)
    app = TestApplication()
    auth_manager = AuthManager(str(tmp_path / "users.json"), secrets.token_urlsafe(32))
    initial_credential = secrets.token_urlsafe(32) + "Aa1!"
    replacement_credential = secrets.token_urlsafe(32) + "Bb2!"
    auth_manager.create_user("synthetic_operator", initial_credential, role="admin")
    user = auth_manager.change_password("synthetic_operator", initial_credential, replacement_credential)
    validator = _SyntheticValidator(failed_candidate_id, infeasible_candidate_id)
    app.state.services["AUTH_MANAGER"] = auth_manager
    app.state.services["SOLVER_COORDINATION_VALIDATION_SERVICE"] = ValidationRunService(
        validator=validator,
        context=ValidationContext(
            fact_snapshot_digest="sha256:solver-coordination.v1:" + "2" * 64,
            rules_revision="rules_revision_synthetic_1",
            validator_revision="synthetic_validator.v1",
        ),
    )
    app.include_router(solver_coordination_router)
    app.include_router(solver_coordination_public_router)
    return (
        TestClient(app),
        {
            "Authorization": "Bearer {}".format(auth_manager.issue_token(user)),
            "Content-Type": "application/json",
        },
        validator,
    )


def _create_candidate_set(client, headers):
    response = client.post(
        "/agentic/solver-coordination/v1/candidate-sets",
        data=json.dumps(_payload()),
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()


def _validation_payload(candidate_artifact):
    return {
        "source_run_id": candidate_artifact["source_run_id"],
        "candidate_set_ref": candidate_artifact["candidate_set_ref"],
        "candidate_set_digest": candidate_artifact["candidate_set_digest"],
        "validator_revision": "synthetic_validator.v1",
    }


@pytest.mark.parametrize(
    "path",
    (
        "/agentic/solver-coordination/v1/validation-runs",
        "/api/solver/validations",
    ),
)
def test_validation_routes_use_tool_pool_with_production_runtime(
    tmp_path,
    monkeypatch,
    path,
):
    client, headers, _validator = _client(tmp_path, monkeypatch)
    candidate_artifact = _create_candidate_set(client, headers)
    calls = []
    _install_runtime_pools(client, calls)

    response = client.post(
        path,
        json=_validation_payload(candidate_artifact),
        headers={**headers, "Idempotency-Key": "validation-tool-pool-001"},
    )

    assert response.status_code == 201
    assert calls == ["external_io", "short_io", "tool_io"]


def test_public_solver_run_uses_tool_pool_with_production_runtime(
    tmp_path,
    monkeypatch,
):
    client, headers, _validator = _client(tmp_path, monkeypatch)
    calls = []
    _install_runtime_pools(client, calls)
    client.app.state.services["SOLVER_COORDINATION_SERVICE"] = object()

    def complete(_service, **_kwargs):
        return internal(solver_routes).response({"status": "completed"}, 200)

    monkeypatch.setattr(
        solver_routes,
        "_create_solver_run_through_coordination_service",
        complete,
    )

    response = client.post(
        "/api/solver/runs",
        json={},
        headers={**headers, "Idempotency-Key": "solver-tool-pool-001"},
    )

    assert response.status_code == 200
    assert calls == ["external_io", "short_io", "tool_io"]


def test_validation_route_maps_tool_pool_timeout_to_service_unavailable(
    tmp_path,
    monkeypatch,
):
    client, headers, _validator = _client(tmp_path, monkeypatch)
    candidate_artifact = _create_candidate_set(client, headers)
    calls = []
    _install_runtime_pools(client, calls, tool_pool=_TimeoutPool())
    safe_client = TestClient(client.app, raise_server_exceptions=False)

    response = safe_client.post(
        "/agentic/solver-coordination/v1/validation-runs",
        json=_validation_payload(candidate_artifact),
        headers={**headers, "Idempotency-Key": "validation-timeout-001"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "code": "validation_service_unavailable",
        "retryable": True,
    }


def test_public_solver_run_maps_tool_pool_timeout_to_solver_unavailable(
    tmp_path,
    monkeypatch,
):
    client, headers, _validator = _client(tmp_path, monkeypatch)
    calls = []
    _install_runtime_pools(client, calls, tool_pool=_TimeoutPool())
    client.app.state.services["SOLVER_COORDINATION_SERVICE"] = object()
    safe_client = TestClient(client.app, raise_server_exceptions=False)

    response = safe_client.post(
        "/api/solver/runs",
        json={},
        headers={**headers, "Idempotency-Key": "solver-timeout-001"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "code": "solver_unavailable",
        "retryable": True,
    }


def test_formal_public_paths_preserve_create_and_replay_contract(tmp_path, monkeypatch):
    client, headers, validator = _client(tmp_path, monkeypatch)
    response = client.post(
        "/api/solver/runs",
        json=_payload(),
        headers=headers,
    )
    assert response.status_code == 201
    artifact = response.json()
    payload = _validation_payload(artifact)
    request_headers = {**headers, "Idempotency-Key": "public-validation-001"}

    first = client.post("/api/solver/validations", json=payload, headers=request_headers)
    replay = client.post("/api/solver/validations", json=payload, headers=request_headers)

    assert first.status_code == 201
    assert replay.status_code == 200
    assert first.json() == replay.json()
    assert validator.calls == 18

    public_run = client.get(
        "/api/solver/runs/{}".format(artifact["source_run_id"]),
        headers=headers,
    )
    assert public_run.status_code == 200
    run_body = public_run.json()
    assert run_body["run_id"] == artifact["source_run_id"]
    assert run_body["status"] == "completed"
    assert "owner_id" not in run_body
    assert "created_at" not in run_body


def test_equivalent_solve_runs_keep_independent_content_addresses_and_lineage(tmp_path, monkeypatch):
    client, headers, validator = _client(tmp_path, monkeypatch)
    first = _create_candidate_set(client, headers)
    second = _create_candidate_set(client, headers)

    assert first["candidate_set_digest"] != second["candidate_set_digest"]
    assert first["source_run_id"] != second["source_run_id"]

    response = client.post(
        "/api/solver/validations",
        json=_validation_payload(second),
        headers={**headers, "Idempotency-Key": "equivalent-run-validation-001"},
    )

    assert response.status_code == 201
    assert response.json()["source_run_id"] == second["source_run_id"]
    assert validator.calls == 18


def test_validation_rejects_a_parent_run_that_is_not_bound_to_the_artifact(tmp_path, monkeypatch):
    client, headers, validator = _client(tmp_path, monkeypatch)
    artifact = _create_candidate_set(client, headers)
    payload = _validation_payload(artifact)
    payload["source_run_id"] = "solve_synthetic_forged_parent"

    response = client.post(
        "/api/solver/validations",
        json=payload,
        headers={**headers, "Idempotency-Key": "forged-parent-001"},
    )

    assert response.status_code == 404
    assert response.json() == {"code": "candidate_not_found"}
    assert validator.calls == 0


def test_run_lookup_hides_unknown_or_unauthorized_identity(tmp_path, monkeypatch):
    client, headers, _validator = _client(tmp_path, monkeypatch)
    artifact = _create_candidate_set(client, headers)
    auth_manager = client.app.state.services["AUTH_MANAGER"]
    initial_credential = secrets.token_urlsafe(32) + "Cc3!"
    replacement_credential = secrets.token_urlsafe(32) + "Dd4!"
    auth_manager.create_user("synthetic_observer", initial_credential, role="admin")
    observer = auth_manager.change_password("synthetic_observer", initial_credential, replacement_credential)
    observer_headers = {
        "Authorization": "Bearer {}".format(auth_manager.issue_token(observer)),
        "Content-Type": "application/json",
    }

    unauthorized = client.get(
        "/api/solver/runs/{}".format(artifact["source_run_id"]),
        headers=observer_headers,
    )
    unknown = client.get(
        "/api/solver/runs/solve_synthetic_unknown",
        headers=headers,
    )

    assert unauthorized.status_code == unknown.status_code == 404
    assert unauthorized.json() == unknown.json() == {"code": "run_not_found"}


def test_validation_route_returns_all_eighteen_and_publishes_only_complete_results(tmp_path, monkeypatch):
    client, headers, validator = _client(tmp_path, monkeypatch)
    candidate_artifact = _create_candidate_set(client, headers)

    response = client.post(
        "/agentic/solver-coordination/v1/validation-runs",
        json=_validation_payload(candidate_artifact),
        headers={**headers, "Idempotency-Key": "validation-request-001"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["checked_candidate_count"] == 18
    assert body["result_count"] == 18
    assert len(body["all_candidate_results"]) == 18
    assert body["batch_status"] == "validation_completed"
    assert body["selection_status"] == "published"
    assert body["published_candidate_ids"] == body["feasible_candidate_ids"]
    assert validator.calls == 18
    run = SolverRunLedger("demo").get_run(body["validation_run_id"], owner_id="synthetic_operator")
    assert run is not None
    assert run["run_type"] == "validation"
    assert run["parent_run_id"] == candidate_artifact["source_run_id"]
    assert run["status"] == "completed"


def test_mixed_feasible_and_infeasible_results_remain_publishable_at_route_boundary(tmp_path, monkeypatch):
    client, headers, validator = _client(tmp_path, monkeypatch, infeasible_candidate_id="ALL_1")
    candidate_artifact = _create_candidate_set(client, headers)

    response = client.post(
        "/api/solver/validations",
        json=_validation_payload(candidate_artifact),
        headers={**headers, "Idempotency-Key": "mixed-validation-001"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["batch_status"] == "validation_completed"
    assert body["selection_status"] == "published"
    assert "ALL_1" not in body["feasible_candidate_ids"]
    assert body["published_candidate_ids"] == body["feasible_candidate_ids"]
    assert validator.calls == 18


def test_one_candidate_failure_keeps_eighteen_results_and_blocks_publish(tmp_path, monkeypatch):
    client, headers, validator = _client(tmp_path, monkeypatch, failed_candidate_id="OPTION_0_TO_OPTION_1_K0003")
    candidate_artifact = _create_candidate_set(client, headers)

    response = client.post(
        "/agentic/solver-coordination/v1/validation-runs",
        json=_validation_payload(candidate_artifact),
        headers={**headers, "Idempotency-Key": "validation-request-002"},
    )

    assert response.status_code == 201
    body = response.json()
    assert len(body["all_candidate_results"]) == 18
    assert body["error_count"] == 1
    assert body["batch_status"] == "validation_failed"
    assert body["selection_status"] == "not_publishable"
    assert body["published_candidate_ids"] == []
    failed = [item for item in body["all_candidate_results"] if item["status"] == "validation_error"]
    assert [item["candidate_id"] for item in failed] == ["OPTION_0_TO_OPTION_1_K0003"]
    assert validator.calls == 18


def test_validation_route_rejects_digest_mismatch_without_running_validator(tmp_path, monkeypatch):
    client, headers, validator = _client(tmp_path, monkeypatch)
    candidate_artifact = _create_candidate_set(client, headers)
    payload = _validation_payload(candidate_artifact)
    payload["candidate_set_digest"] = "sha256:solver-coordination.v1:" + "9" * 64

    response = client.post(
        "/agentic/solver-coordination/v1/validation-runs",
        json=payload,
        headers={**headers, "Idempotency-Key": "validation-request-003"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "candidate_digest_mismatch"
    assert validator.calls == 0


def test_validation_route_distinguishes_tampered_artifact_from_missing_reference(tmp_path, monkeypatch):
    client, headers, validator = _client(tmp_path, monkeypatch)
    artifact = _create_candidate_set(client, headers)
    artifact_path = (
        tmp_path / ".onto-runtime" / "workspaces" / "demo" / "runtime" / "solver_coordination" / "candidate_sets"
    )
    files = list(artifact_path.rglob(artifact["candidate_set_ref"] + ".json"))
    assert len(files) == 1
    tampered = json.loads(files[0].read_text(encoding="utf-8"))
    tampered["authoritative"] = True
    files[0].write_text(json.dumps(tampered), encoding="utf-8")

    response = client.post(
        "/api/solver/validations",
        json=_validation_payload(artifact),
        headers={**headers, "Idempotency-Key": "tampered-artifact-001"},
    )

    assert response.status_code == 409
    assert response.json() == {"code": "candidate_digest_mismatch"}
    assert validator.calls == 0


def test_validation_route_accepts_parent_run_and_candidate_digest_contract(tmp_path, monkeypatch):
    client, headers, validator = _client(tmp_path, monkeypatch)
    artifact = _create_candidate_set(client, headers)
    payload = {
        "parent_solve_run_id": artifact["source_run_id"],
        "candidate_set_ref": artifact["candidate_set_ref"],
        "candidate_set_digest": artifact["candidate_set_digest"],
        "candidate_digest": artifact["candidates"][0]["candidate_digest"],
        "validator_revision": "synthetic_validator.v1",
    }

    response = client.post(
        "/api/solver/validations",
        json=payload,
        headers={**headers, "Idempotency-Key": "candidate-reference-001"},
    )

    assert response.status_code == 201
    assert response.json()["checked_candidate_count"] == 18
    assert validator.calls == 18


def test_validation_route_rejects_unknown_candidate_digest_as_conflict(tmp_path, monkeypatch):
    client, headers, validator = _client(tmp_path, monkeypatch)
    artifact = _create_candidate_set(client, headers)
    payload = _validation_payload(artifact)
    payload["candidate_digest"] = "sha256:solver-coordination.v1:" + "8" * 64

    response = client.post(
        "/api/solver/validations",
        json=payload,
        headers={**headers, "Idempotency-Key": "candidate-reference-002"},
    )

    assert response.status_code == 409
    assert response.json() == {"code": "candidate_digest_mismatch"}
    assert validator.calls == 0


def test_validation_route_returns_same_404_for_missing_and_path_like_refs(tmp_path, monkeypatch):
    client, headers, _validator = _client(tmp_path, monkeypatch)
    for index, candidate_set_ref in enumerate(("candidate_set_" + "a" * 64, "candidate_set_../synthetic")):
        response = client.post(
            "/api/solver/validations",
            json={
                "source_run_id": "solve_synthetic_missing",
                "candidate_set_ref": candidate_set_ref,
                "candidate_set_digest": "sha256:solver-coordination.v1:" + "a" * 64,
                "validator_revision": "synthetic_validator.v1",
            },
            headers={
                **headers,
                "Idempotency-Key": "missing-candidate-{}".format(index),
            },
        )

        assert response.status_code == 404
        assert response.json() == {"code": "candidate_not_found"}


def test_validation_route_preserves_missing_workspace_404(tmp_path, monkeypatch):
    client, headers, _validator = _client(tmp_path, monkeypatch)

    response = client.post(
        "/agentic/solver-coordination/v1/validation-runs",
        json={
            "source_run_id": "solve_synthetic_missing",
            "candidate_set_ref": "candidate_set_synthetic_missing",
            "candidate_set_digest": "sha256:solver-coordination.v1:" + "3" * 64,
            "validator_revision": "synthetic_validator.v1",
        },
        headers={
            **headers,
            "Idempotency-Key": "validation-request-missing-workspace",
            "X-Workspace-Id": "missing_workspace",
        },
    )

    assert response.status_code == 404
    assert response.json()["code"] == "WORKSPACE_NOT_FOUND"


def test_validation_route_replays_same_result_for_same_idempotency_key(tmp_path, monkeypatch):
    client, headers, validator = _client(tmp_path, monkeypatch)
    candidate_artifact = _create_candidate_set(client, headers)
    payload = _validation_payload(candidate_artifact)
    request_headers = {**headers, "Idempotency-Key": "validation-request-004"}

    first = client.post(
        "/agentic/solver-coordination/v1/validation-runs",
        json=payload,
        headers=request_headers,
    )
    second = client.post(
        "/agentic/solver-coordination/v1/validation-runs",
        json=payload,
        headers=request_headers,
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json() == second.json()
    assert validator.calls == 18


def test_validation_route_rejects_idempotency_key_reuse_with_different_input(tmp_path, monkeypatch):
    client, headers, _validator = _client(tmp_path, monkeypatch)
    candidate_artifact = _create_candidate_set(client, headers)
    request_headers = {**headers, "Idempotency-Key": "validation-request-005"}
    first_payload = _validation_payload(candidate_artifact)
    first = client.post(
        "/agentic/solver-coordination/v1/validation-runs",
        json=first_payload,
        headers=request_headers,
    )
    assert first.status_code == 201
    changed_payload = dict(first_payload)
    changed_payload["source_run_id"] = "solve_synthetic_other"

    second = client.post(
        "/agentic/solver-coordination/v1/validation-runs",
        json=changed_payload,
        headers=request_headers,
    )

    assert second.status_code == 409
    assert second.json()["code"] == "idempotency_conflict"


def test_validation_route_requires_idempotency_key_and_injected_service(tmp_path, monkeypatch):
    client, headers, _validator = _client(tmp_path, monkeypatch)
    candidate_artifact = _create_candidate_set(client, headers)
    payload = _validation_payload(candidate_artifact)

    missing_key = client.post(
        "/agentic/solver-coordination/v1/validation-runs",
        json=payload,
        headers=headers,
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["code"] == "validation_input_invalid"

    # A separate app with no service configured must fail closed.
    bare_app = TestApplication()
    bare_app.state.services["AUTH_MANAGER"] = client.app.state.services["AUTH_MANAGER"]
    bare_app.include_router(solver_coordination_router)
    bare_client = TestClient(bare_app)
    unavailable = bare_client.post(
        "/agentic/solver-coordination/v1/validation-runs",
        json=payload,
        headers={**headers, "Idempotency-Key": "validation-request-006"},
    )
    assert unavailable.status_code == 503


def test_validation_route_requires_server_validator_revision(tmp_path, monkeypatch):
    client, headers, _validator = _client(tmp_path, monkeypatch)
    candidate_artifact = _create_candidate_set(client, headers)
    payload = _validation_payload(candidate_artifact)
    del payload["validator_revision"]

    response = client.post(
        "/agentic/solver-coordination/v1/validation-runs",
        json=payload,
        headers={**headers, "Idempotency-Key": "validation-request-007"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "validation_input_invalid"


def test_validation_route_rejects_revision_not_bound_to_server_service(tmp_path, monkeypatch):
    client, headers, _validator = _client(tmp_path, monkeypatch)
    candidate_artifact = _create_candidate_set(client, headers)
    payload = _validation_payload(candidate_artifact)
    payload["validator_revision"] = "client_selected_revision"

    response = client.post(
        "/agentic/solver-coordination/v1/validation-runs",
        json=payload,
        headers={**headers, "Idempotency-Key": "validation-request-008"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "validation_input_invalid"


def test_validation_route_serializes_same_idempotency_key_before_running_validator(tmp_path, monkeypatch):
    client, headers, validator = _client(tmp_path, monkeypatch)
    candidate_artifact = _create_candidate_set(client, headers)
    payload = _validation_payload(candidate_artifact)
    request_headers = {**headers, "Idempotency-Key": "validation-request-concurrent"}
    entered = threading.Event()
    release = threading.Event()
    started_candidates = []
    original_validate = validator.validate

    def blocking_validate(candidate_set, candidate, *, context):
        started_candidates.append(candidate.candidate_id)
        if candidate.candidate_id == "ALL_0":
            entered.set()
            assert release.wait(timeout=5)
        return original_validate(candidate_set, candidate, context=context)

    validator.validate = blocking_validate
    responses = []

    def post_request():
        responses.append(
            client.post(
                "/agentic/solver-coordination/v1/validation-runs",
                json=payload,
                headers=request_headers,
            )
        )

    first_thread = threading.Thread(target=post_request)
    second_thread = threading.Thread(target=post_request)
    first_thread.start()
    assert entered.wait(timeout=5)
    second_thread.start()
    time.sleep(0.1)
    assert started_candidates == ["ALL_0"]
    release.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert len(responses) == 2
    assert all(response.status_code in {200, 201} for response in responses)
    assert responses[0].json() == responses[1].json()
    assert validator.calls == 18
