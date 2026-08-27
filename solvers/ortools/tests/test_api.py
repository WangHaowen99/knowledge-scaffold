from __future__ import annotations

from copy import deepcopy
import json
import threading
import time
from importlib.metadata import version as package_version

import pytest

from contracts.solver_services.ortools.validation import validate_document
from solvers.ortools.app import create_app
from solvers.ortools.config import OrToolsSolverConfig
from solvers.ortools.service import OrToolsSolverService
from solvers.ortools.tests.test_process_runner import _domain_request


def _config(tmp_path, **overrides):
    values = {
        "database_path": tmp_path / "attempts.sqlite3",
        "attempt_ttl_seconds": 3600,
        "max_payload_bytes": 1024 * 1024,
        "max_solve_time_ms": 30_000,
        "max_search_workers": 1,
        "max_concurrent_jobs": 2,
        "memory_limit_bytes": 512 * 1024 * 1024,
        "file_limit_bytes": 1024 * 1024,
        "prefer_wait_max_ms": 5_000,
        "service_token": None,
        "enforce_os_limits": False,
    }
    values.update(overrides)
    return OrToolsSolverConfig(**values)


def _post(client, payload, *, key="opaque-key-00000001", prefer=None):
    headers = {"Idempotency-Key": key}
    if prefer is not None:
        headers["Prefer"] = prefer
    return client.post("/api/v1/jobs", json=payload, headers=headers)


def test_health_and_capabilities_are_stable_public_documents(tmp_path):
    app = create_app(_config(tmp_path))
    client = app.test_client()

    health = client.get("/health")
    capabilities = client.get("/api/v1/capabilities")

    assert health.status_code == 200
    assert health.get_json() == {"status": "ok"}
    assert capabilities.status_code == 200
    payload = capabilities.get_json()
    validate_document("capabilities", payload)
    assert payload["service"]["name"] == "ortools-solver-service"
    assert payload["problem_types"][0]["name"] == "binary_ordered_route"
    assert payload["engine"]["version"] == package_version("ortools")
    app.extensions["ortools_solver"].close()


def test_submit_releases_capacity_when_attempt_persistence_fails(
    tmp_path, submit_request, monkeypatch
):
    service = OrToolsSolverService(_config(tmp_path, max_concurrent_jobs=1))

    def fail_create_or_get(**kwargs):
        raise RuntimeError("synthetic persistence failure")

    monkeypatch.setattr(service.store, "create_or_get", fail_create_or_get)
    try:
        with pytest.raises(RuntimeError, match="synthetic persistence failure"):
            service.submit(idempotency_key="capacity-release-key-0001", document=submit_request)
        assert service._capacity.acquire(blocking=False) is True
        service._capacity.release()
    finally:
        service.close()


def test_legacy_v1_routes_are_not_exposed(tmp_path):
    app = create_app(_config(tmp_path))
    client = app.test_client()
    assert client.get("/v1/health").status_code == 404
    assert client.get("/v1/capabilities").status_code == 404
    assert client.post("/v1/solve-jobs", json={}).status_code == 404
    app.extensions["ortools_solver"].close()


def test_legacy_routes_remain_not_found_when_service_authentication_is_enabled(
    tmp_path,
):
    app = create_app(_config(tmp_path, service_token="synthetic-service-token"))
    client = app.test_client()
    try:
        assert client.get("/v1/capabilities").status_code == 404
        assert client.post("/v1/solve-jobs", json={}).status_code == 404
        assert client.get("/unrelated").status_code == 404
    finally:
        app.extensions["ortools_solver"].close()


def test_submit_wait_poll_and_result_follow_ortools_solver_v1(tmp_path, submit_request):
    app = create_app(_config(tmp_path))
    client = app.test_client()

    submitted = _post(client, submit_request, prefer="wait=5000")

    assert submitted.status_code == 202
    job = submitted.get_json()
    validate_document("job", job)
    assert job["status"] == "completed"

    polled = client.get(f"/api/v1/jobs/{job['job_id']}")
    assert polled.status_code == 200
    assert polled.get_json() == job

    result_response = client.get(f"/api/v1/jobs/{job['job_id']}/result")
    assert result_response.status_code == 200
    result = result_response.get_json()
    validate_document("result", result)
    assert result["request_id"] == submit_request["request_id"]
    assert result["assignments"]["candidate_id"] == "CANDIDATE_SYNTHETIC_0002"
    app.extensions["ortools_solver"].close()


def test_submit_domain_problem_recomputes_candidates_before_ortools_selection(tmp_path):
    app = create_app(_config(tmp_path))
    client = app.test_client()

    submitted = _post(
        client,
        _domain_request(),
        key="domain-evaluation-key-0001",
        prefer="wait=5000",
    )

    assert submitted.status_code == 202
    job = submitted.get_json()
    assert job["status"] == "completed"
    response = client.get("/api/v1/jobs/{}/result".format(job["job_id"]))
    assert response.status_code == 200
    result = response.get_json()
    validate_document("result", result)
    assert result["assignments"]["candidate_id"] == (
        "OPTION_0_TO_OPTION_1_K0002"
    )
    assert result["assurance"]["certificate"]["evaluation_source"] == (
        "refinery_route.v1"
    )
    app.extensions["ortools_solver"].close()


def test_submit_rejects_invalid_traceparent(tmp_path, submit_request):
    app = create_app(_config(tmp_path))
    response = app.test_client().post(
        "/api/v1/jobs",
        json=submit_request,
        headers={
            "Idempotency-Key": "opaque-key-trace-0001",
            "traceparent": "00-invalid",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "CONTRACT_INVALID"
    app.extensions["ortools_solver"].close()


def test_submit_is_idempotent_and_conflicts_on_changed_payload(tmp_path, submit_request):
    app = create_app(_config(tmp_path))
    client = app.test_client()

    first = _post(client, submit_request)
    second = _post(client, submit_request)
    changed = deepcopy(submit_request)
    changed["options"]["time_limit_ms"] = 4000
    conflict = _post(client, changed)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.get_json()["job_id"] == first.get_json()["job_id"]
    assert conflict.status_code == 409
    validate_document("error", conflict.get_json())
    assert conflict.get_json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    app.extensions["ortools_solver"].close()


def test_concurrent_identical_idempotency_key_reuses_one_job(
    tmp_path, submit_request
):
    app = create_app(_config(tmp_path, max_concurrent_jobs=1))
    service = app.extensions["ortools_solver"]
    service.pause_dispatch()
    barrier = threading.Barrier(3)
    responses = []

    def submit_once():
        client = app.test_client()
        barrier.wait()
        responses.append(
            _post(client, submit_request, key="concurrent-key-00000001")
        )

    threads = [threading.Thread(target=submit_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
    try:
        assert len(responses) == 2
        assert {response.status_code for response in responses} == {202}
        assert len({response.get_json()["job_id"] for response in responses}) == 1
    finally:
        service.close()


def test_submit_rejects_unknown_fields_and_missing_idempotency(tmp_path, submit_request):
    app = create_app(_config(tmp_path))
    client = app.test_client()
    unknown = deepcopy(submit_request)
    unknown["workspace_id"] = "workspace_demo"

    strict = _post(client, unknown)
    missing_key = client.post("/api/v1/jobs", json=submit_request)

    assert strict.status_code == 400
    assert strict.get_json()["error"]["category"] == "validation"
    assert missing_key.status_code == 400
    assert missing_key.get_json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    app.extensions["ortools_solver"].close()


def test_submit_rejects_noncanonical_json_and_hard_limit_overrides(
    tmp_path, submit_request
):
    app = create_app(_config(tmp_path, max_solve_time_ms=1000))
    client = app.test_client()
    floating = json.dumps(submit_request).replace('"search_workers": 1', '"search_workers": 1.0')
    oversized_limits = deepcopy(submit_request)
    oversized_limits["options"]["time_limit_ms"] = 1001

    noncanonical = client.post(
        "/api/v1/jobs",
        data=floating,
        content_type="application/json",
        headers={"Idempotency-Key": "opaque-key-00000001"},
    )
    too_large = _post(client, oversized_limits, key="opaque-key-00000002")

    assert noncanonical.status_code == 400
    assert too_large.status_code == 400
    assert too_large.get_json()["error"]["code"] == "LIMIT_EXCEEDED"
    app.extensions["ortools_solver"].close()


def test_submit_rejects_body_larger_than_the_configured_payload_limit(
    tmp_path, submit_request
):
    app = create_app(_config(tmp_path, max_payload_bytes=256))
    response = app.test_client().post(
        "/api/v1/jobs",
        data=json.dumps(submit_request, separators=(",", ":")),
        content_type="application/json",
        headers={"Idempotency-Key": "payload-limit-key-0001"},
    )
    try:
        assert response.status_code == 413
        validate_document("error", response.get_json())
        assert response.get_json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    finally:
        app.extensions["ortools_solver"].close()


def test_delete_is_idempotent_and_result_is_unavailable_after_cancel(
    tmp_path, submit_request
):
    app = create_app(_config(tmp_path))
    service = app.extensions["ortools_solver"]
    service.pause_dispatch()
    client = app.test_client()
    submitted = _post(client, submit_request)
    job_id = submitted.get_json()["job_id"]

    first = client.delete(f"/api/v1/jobs/{job_id}")
    second = client.delete(f"/api/v1/jobs/{job_id}")
    result = client.get(f"/api/v1/jobs/{job_id}/result")

    assert first.status_code == 200
    assert first.get_json()["status"] == "cancelled"
    assert second.get_json() == first.get_json()
    assert result.status_code == 409
    service.close()


def test_submit_rejects_lone_unicode_surrogate_as_json_contract_error(
    tmp_path,
):
    app = create_app(_config(tmp_path))
    client = app.test_client()

    response = client.post(
        "/api/v1/jobs",
        data=b'{"x":"\\udead"}',
        content_type="application/json",
        headers={"Idempotency-Key": "opaque-key-surrogate"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error"]["code"] == "CONTRACT_INVALID"
    assert response.content_type.startswith("application/json")
    app.extensions["ortools_solver"].close()


def test_service_token_is_service_local_and_never_echoed(tmp_path):
    token = "synthetic-service-token-value"
    app = create_app(_config(tmp_path, service_token=token))
    client = app.test_client()

    assert type(app.session_interface).__name__ == "BearerOnlySessionInterface"

    missing = client.get("/api/v1/capabilities")
    client.set_cookie("ambient_session", "synthetic-cookie-value")
    cookie_only = client.get("/api/v1/capabilities")
    invalid = client.get(
        "/api/v1/capabilities", headers={"Authorization": "Bearer invalid-value"}
    )
    accepted = client.get(
        "/api/v1/capabilities", headers={"Authorization": f"Bearer {token}"}
    )

    assert missing.status_code == 401
    assert cookie_only.status_code == 401
    assert invalid.status_code == 401
    assert token not in invalid.get_data(as_text=True)
    assert accepted.status_code == 200
    assert "Set-Cookie" not in accepted.headers
    app.extensions["ortools_solver"].close()


def test_capacity_rejection_does_not_consume_the_idempotency_key(
    tmp_path, submit_request
):
    class CapacityGate:
        def __init__(self):
            self.available = False

        def acquire(self, *, blocking):
            assert blocking is False
            return self.available

        def release(self):
            self.available = True

    app = create_app(_config(tmp_path, max_concurrent_jobs=1))
    service = app.extensions["ortools_solver"]
    gate = CapacityGate()
    service._capacity = gate
    client = app.test_client()

    rejected = _post(client, submit_request)
    gate.available = True
    accepted = _post(client, submit_request, prefer="wait=5000")

    assert rejected.status_code == 429
    assert accepted.status_code == 202
    assert accepted.get_json()["status"] == "completed"
    service.close()
