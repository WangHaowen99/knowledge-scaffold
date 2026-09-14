# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from api.auth.auth import AuthManager
from api.routes.solver_coordination import solver_coordination_public_router
from api.testing import TestApplication
from fastapi.testclient import TestClient
from solver_coordination.candidate_results import CandidateResultsArtifact
from solver_coordination.canonical import sha256_digest
from solver_coordination.job_orchestrator import JobOrchestrationError
from solver_coordination.job_store import SolverJobStore
from workspace.workspaces import WorkspaceNotFoundError, create_workspace

WORKSPACE = "solver-history-synthetic"
OWNER = "synthetic_history_operator"


class _ArtifactReader:
    def __init__(self):
        self.references = []
        self.payloads = {}

    def get(self, reference):
        self.references.append(reference)
        return self.payloads.get(reference, {"kind": "synthetic_evidence"})


def _candidate_results_payload(problem_digest):
    item_ids = ["batch_{}".format(index) for index in range(1, 10)]
    rows = []
    definitions = [(1, "ALL_1", ["OPTION_1"] * 9)]
    definitions.append((2, "ALL_0", ["OPTION_0"] * 9))
    for switch in range(1, 9):
        definitions.extend(
            [
                (
                    2 * switch + 1,
                    "OPTION_1_TO_OPTION_0_K{:04d}".format(switch),
                    ["OPTION_1"] * switch + ["OPTION_0"] * (9 - switch),
                ),
                (
                    2 * switch + 2,
                    "OPTION_0_TO_OPTION_1_K{:04d}".format(switch),
                    ["OPTION_0"] * switch + ["OPTION_1"] * (9 - switch),
                ),
            ]
        )
    ranks = list(range(18))
    ranks[5], ranks[9] = ranks[9], ranks[5]
    for combination_no, candidate_id, assignment in definitions:
        rows.append(
            {
                "combination_no": combination_no,
                "candidate_id": candidate_id,
                "assignment": assignment,
                "eligible": True,
                "validation_status": "feasible",
                "metrics": {"candidate_rank": ranks[combination_no - 1]},
                "rejection_reasons": [],
                "selected": combination_no == 10,
            }
        )
    source = [
        {
            "candidate_id": row["candidate_id"],
            "eligible": row["eligible"],
            "assignment": dict(zip(item_ids, row["assignment"])),
            "metrics": row["metrics"],
        }
        for row in rows
    ]
    source.sort(key=lambda row: row["metrics"]["candidate_rank"])
    payload = {
        "schema_version": "solver.candidate-results.v1",
        "problem_digest": problem_digest,
        "candidate_source_kind": "problem_ir.candidate_selection",
        "candidate_source_digest": sha256_digest(source),
        "presentation_revision": "ordered_binary_route.presentation.v1",
        "item_ids": item_ids,
        "option_labels": {
            "OPTION_0": "柴油加氢",
            "OPTION_1": "蜡油加氢",
        },
        "candidate_count": 18,
        "selected_candidate_id": "OPTION_0_TO_OPTION_1_K0004",
        "candidates": rows,
    }
    return CandidateResultsArtifact.from_dict(payload).to_dict()


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace(
        "Solver history synthetic",
        workspace_id=WORKSPACE,
        set_as_default=True,
    )
    app = TestApplication()
    auth = AuthManager(str(tmp_path / "users.json"), secrets.token_urlsafe(32))
    initial = secrets.token_urlsafe(32) + "Aa1!"
    replacement = secrets.token_urlsafe(32) + "Bb2!"
    auth.create_user(OWNER, initial, role="admin")
    user = auth.change_password(OWNER, initial, replacement)
    store = SolverJobStore(tmp_path / "solver-jobs.sqlite3")
    artifact_reader = _ArtifactReader()
    app.state.services["AUTH_MANAGER"] = auth
    app.state.services["SOLVER_COORDINATION_SERVICE"] = SimpleNamespace(
        store=store,
        artifact_store=artifact_reader,
    )
    app.include_router(solver_coordination_public_router)
    headers = {
        "Authorization": "Bearer {}".format(auth.issue_token(user)),
        "X-Workspace-Id": WORKSPACE,
    }
    return TestClient(app), headers, store, artifact_reader


def _add_completed_job(
    store,
    *,
    owner_id=OWNER,
    suffix="one",
    artifact_reader=None,
    candidate_payload=None,
    candidate_ledger_digest=None,
    candidate_authoritative_digest=None,
    authoritative=True,
    terminal=True,
):
    binding_id = "binding-synthetic-{}".format(suffix)
    store.register_binding_revision(
        workspace_id=WORKSPACE,
        owner_id=owner_id,
        binding_revision_id=binding_id,
        configuration={"backend": "synthetic"},
    )
    problem_digest = "sha256:{}".format((suffix[0] * 64)[:64])
    job, _created = store.create_or_get_job(
        workspace_id=WORKSPACE,
        owner_id=owner_id,
        binding_revision_id=binding_id,
        problem_digest=problem_digest,
        problem={"problem_id": problem_digest},
        run_policy={
            "operation_key": "solve_synthetic_{}".format(suffix),
            "package_revision": "revision-synthetic-{}".format(suffix),
        },
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        job_id="job-synthetic-{}".format(suffix),
    )
    attempt, _created = store.create_or_get_initial_attempt(
        job["job_id"],
        workspace_id=WORKSPACE,
        owner_id=owner_id,
        attempt_id="attempt-synthetic-{}".format(suffix),
        request_payload={"problem_id": problem_digest},
        backend_idempotency_key="hmac-sha256:{}".format(suffix),
    )
    store.update_attempt_status(
        attempt["attempt_id"],
        workspace_id=WORKSPACE,
        owner_id=owner_id,
        status="completed",
    )
    result_ref = "artifact://solver_result/{}".format("a" * 64)
    evidence_ref = "artifact://verification_evidence/{}".format("b" * 64)
    for artifact_type, digest, reference in (
        ("solver_result", "sha256:" + "a" * 64, result_ref),
        ("verification_evidence", "sha256:" + "b" * 64, evidence_ref),
    ):
        store.add_artifact(
            workspace_id=WORKSPACE,
            owner_id=owner_id,
            job_id=job["job_id"],
            attempt_id=attempt["attempt_id"],
            artifact_type=artifact_type,
            artifact_digest=digest,
            artifact_ref=reference,
        )
    candidate_ref = None
    if candidate_payload is not None:
        candidate_digest = candidate_ledger_digest or sha256_digest(candidate_payload)
        candidate_ref = "artifact://candidate_results/{}".format(sha256_digest(candidate_payload).rsplit(":", 1)[-1])
        store.add_artifact(
            workspace_id=WORKSPACE,
            owner_id=owner_id,
            job_id=job["job_id"],
            attempt_id=attempt["attempt_id"],
            artifact_type="candidate_results",
            artifact_digest=candidate_digest,
            artifact_ref=candidate_ref,
        )
        if artifact_reader is not None:
            artifact_reader.payloads[candidate_ref] = candidate_payload
    verification = {
        "schema_version": "solver.verification-result.v1",
        "problem_id": problem_digest,
        "status": "verified",
        "feasibility": "feasible",
        "optimality": "not_proven",
        "proof_kind": "synthetic_independent_check",
        "diagnostics": [],
        "evidence": [{"kind": "synthetic_proof"}],
    }
    store.add_verification_result(
        workspace_id=WORKSPACE,
        owner_id=owner_id,
        job_id=job["job_id"],
        attempt_id=attempt["attempt_id"],
        verification_id="verification-synthetic-{}".format(suffix),
        status="verified",
        result=verification,
    )
    if authoritative:
        authoritative_payload = {
            "schema_version": "solver.authoritative-result.v1",
            "run_id": "run-synthetic-{}".format(suffix),
            "job_id": job["job_id"],
            "status": "completed",
            "feasibility": "feasible",
            "optimality": "not_proven",
            "proof_kind": "synthetic_independent_check",
            "authoritative": True,
            "summary": {"choice": "OPTION_0"},
            "result_artifact_ref": result_ref,
            "verification_evidence_ref": evidence_ref,
            "knowledge_revision_id": "revision-synthetic-{}".format(suffix),
            "fact_snapshot_digest": "sha256:" + "c" * 64,
            "problem_digest": problem_digest,
            "problem_ir": {"must": "not leak"},
        }
        if candidate_ref is not None:
            authoritative_payload.update(
                {
                    "candidate_results_artifact_ref": candidate_ref,
                    "candidate_results_artifact_digest": (
                        candidate_authoritative_digest or candidate_ledger_digest or sha256_digest(candidate_payload)
                    ),
                }
            )
        store.append_event(
            job["job_id"],
            workspace_id=WORKSPACE,
            owner_id=owner_id,
            attempt_id=attempt["attempt_id"],
            event_type="solver.result.authoritative",
            payload=authoritative_payload,
        )
    if terminal:
        store.update_job(
            job["job_id"],
            workspace_id=WORKSPACE,
            owner_id=owner_id,
            expected_version=job["version"],
            status="completed",
            phase="publish",
        )
    return job["job_id"]


def test_candidate_results_are_owner_scoped_integrity_checked_and_complete(tmp_path, monkeypatch):
    client, headers, store, reader = _client(tmp_path, monkeypatch)
    problem_digest = "sha256:" + "c" * 64
    payload = _candidate_results_payload(problem_digest)
    job_id = _add_completed_job(
        store,
        suffix="candidates",
        artifact_reader=reader,
        candidate_payload=payload,
    )
    hidden_job = _add_completed_job(
        store,
        owner_id="another-synthetic-owner",
        suffix="hidden-candidates",
        artifact_reader=reader,
        candidate_payload=_candidate_results_payload("sha256:" + "h" * 64),
    )

    response = client.get(
        "/api/solver/history/{}/candidate-results".format(job_id),
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["candidate_count"] == 18
    assert [row["combination_no"] for row in body["candidates"]] == list(range(1, 19))
    winner = next(row for row in body["candidates"] if row["selected"])
    assert winner["combination_no"] == 10
    assert winner["metrics"]["candidate_rank"] == 5
    assert reader.references[-1].startswith("artifact://candidate_results/")

    history = client.get("/api/solver/history", headers=headers).json()
    history_item = next(item for item in history["items"] if item["job_id"] == job_id)
    assert history_item["candidate_results_artifact_ref"].startswith("artifact://candidate_results/")
    assert history_item["candidate_results_artifact_digest"] == sha256_digest(payload)

    hidden = client.get(
        "/api/solver/history/{}/candidate-results".format(hidden_job),
        headers=headers,
    )
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "run_not_found"


def test_candidate_results_reject_digest_and_payload_corruption(tmp_path, monkeypatch):
    client, headers, store, reader = _client(tmp_path, monkeypatch)
    payload = _candidate_results_payload("sha256:" + "d" * 64)
    mismatch_job = _add_completed_job(
        store,
        suffix="candidate-digest-mismatch",
        artifact_reader=reader,
        candidate_payload=payload,
        candidate_authoritative_digest="sha256:" + "e" * 64,
    )
    corrupt_payload = {"schema_version": "solver.candidate-results.v1"}
    corrupt_job = _add_completed_job(
        store,
        suffix="candidate-payload-corrupt",
        artifact_reader=reader,
        candidate_payload=corrupt_payload,
    )

    for job_id in (mismatch_job, corrupt_job):
        response = client.get(
            "/api/solver/history/{}/candidate-results".format(job_id),
            headers=headers,
        )
        assert response.status_code == 409
        assert response.json()["code"] == "candidate_digest_mismatch"


def test_candidate_results_reject_non_authoritative_or_incomplete_job(tmp_path, monkeypatch):
    client, headers, store, reader = _client(tmp_path, monkeypatch)
    payload = _candidate_results_payload("sha256:" + "f" * 64)
    non_authoritative = _add_completed_job(
        store,
        suffix="candidate-not-authoritative",
        artifact_reader=reader,
        candidate_payload=payload,
        authoritative=False,
    )
    incomplete = _add_completed_job(
        store,
        suffix="candidate-incomplete",
        artifact_reader=reader,
        candidate_payload=payload,
        terminal=False,
    )

    for job_id in (non_authoritative, incomplete):
        response = client.get(
            "/api/solver/history/{}/candidate-results".format(job_id),
            headers=headers,
        )
        assert response.status_code == 409
        assert response.json()["code"] == "solver_result_rejected"


def test_history_is_keyset_paginated_and_public_safe(tmp_path, monkeypatch):
    client, headers, store, _reader = _client(tmp_path, monkeypatch)
    _add_completed_job(store, suffix="alpha")
    _add_completed_job(store, suffix="beta")
    _add_completed_job(store, owner_id="another-synthetic-owner", suffix="hidden")

    first = client.get("/api/solver/history?limit=1", headers=headers)

    assert first.status_code == 200
    page = first.json()
    assert len(page["items"]) == 1
    assert page["next_cursor"]
    item = page["items"][0]
    assert item["authoritative"] is True
    assert item["feasibility"] == "feasible"
    assert item["optimality"] == "not_proven"
    serialized = first.text
    for forbidden in (
        "owner_id",
        "workspace_id",
        "binding_revision_id",
        "run_policy",
        "problem_ir",
    ):
        assert forbidden not in serialized

    second = client.get(
        "/api/solver/history?limit=1&cursor={}".format(page["next_cursor"]),
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json()["items"][0]["job_id"] != item["job_id"]


def test_evidence_detail_checks_scope_and_artifact_integrity(tmp_path, monkeypatch):
    client, headers, store, reader = _client(tmp_path, monkeypatch)
    job_id = _add_completed_job(store, suffix="evidence")
    hidden_job = _add_completed_job(store, owner_id="another-synthetic-owner", suffix="hidden-evidence")

    response = client.get("/api/solver/history/{}/evidence".format(job_id), headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["job"]["job_id"] == job_id
    assert len(body["artifacts"]) == 2
    assert all(item["integrity_verified"] for item in body["artifacts"])
    assert len(reader.references) == 2
    assert body["verification_results"][0]["result"]["evidence"] == [{"kind": "synthetic_proof"}]
    serialized = response.text
    assert "problem_ir" not in serialized
    assert "owner_id" not in serialized

    hidden = client.get("/api/solver/history/{}/evidence".format(hidden_job), headers=headers)
    assert hidden.status_code == 404


def test_history_rejects_invalid_cursor(tmp_path, monkeypatch):
    client, headers, _store, _reader = _client(tmp_path, monkeypatch)

    response = client.get("/api/solver/history?cursor=not-a-valid-cursor", headers=headers)

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_solver_input"


def test_history_retry_is_explicit_and_does_not_accept_new_solver_input(tmp_path, monkeypatch):
    client, headers, _store, _reader = _client(tmp_path, monkeypatch)
    service = client.app.state.services["SOLVER_COORDINATION_SERVICE"]
    calls = []

    def retry_job(**kwargs):
        calls.append(kwargs)
        return {
            "status": "running",
            "code": "retry_started",
            "phase": "retry",
            "job_id": kwargs["job_id"],
            "attempt_id": "attempt-retry-synthetic",
            "attempt_number": 2,
            "authoritative": False,
            "retryable": False,
        }

    service.retry_job = retry_job

    rejected = client.post(
        "/api/solver/history/job-synthetic/retry",
        json={"problem": {"malicious": "mutation"}},
        headers=headers,
    )
    assert rejected.status_code == 400
    assert rejected.json()["code"] == "invalid_solver_input"

    response = client.post(
        "/api/solver/history/job-synthetic/retry",
        json={"attempt_id": "attempt-retry-synthetic"},
        headers=headers,
    )
    assert response.status_code == 202
    assert response.json()["authoritative"] is False
    assert calls == [
        {
            "job_id": "job-synthetic",
            "workspace_id": WORKSPACE,
            "owner_id": OWNER,
            "attempt_id": "attempt-retry-synthetic",
        }
    ]


def test_history_retry_maps_missing_job_without_exposing_orchestrator_error(tmp_path, monkeypatch):
    client, headers, _store, _reader = _client(tmp_path, monkeypatch)
    service = client.app.state.services["SOLVER_COORDINATION_SERVICE"]

    def missing_job(**_kwargs):
        raise JobOrchestrationError("solver job was not found")

    service.retry_job = missing_job

    response = client.post(
        "/api/solver/history/job-missing/retry",
        json={},
        headers=headers,
    )

    assert response.status_code == 404
    assert response.json() == {"code": "run_not_found"}


def test_history_retry_maps_orchestration_failure_to_safe_unavailable_response(tmp_path, monkeypatch):
    client, headers, _store, _reader = _client(tmp_path, monkeypatch)
    service = client.app.state.services["SOLVER_COORDINATION_SERVICE"]

    def orchestration_failure(**_kwargs):
        raise JobOrchestrationError("connector state could not be recovered")

    service.retry_job = orchestration_failure

    response = client.post(
        "/api/solver/history/job-failure/retry",
        json={},
        headers=headers,
    )

    assert response.status_code == 503
    assert response.json() == {"code": "solver_unavailable", "retryable": True}


def test_history_retry_preserves_shared_workspace_not_found_response(tmp_path, monkeypatch):
    client, headers, _store, _reader = _client(tmp_path, monkeypatch)
    service = client.app.state.services["SOLVER_COORDINATION_SERVICE"]

    def missing_workspace(**_kwargs):
        raise WorkspaceNotFoundError(WORKSPACE)

    service.retry_job = missing_workspace

    response = client.post(
        "/api/solver/history/job-synthetic/retry",
        json={},
        headers=headers,
    )

    assert response.status_code == 404
    assert response.json()["code"] == "WORKSPACE_NOT_FOUND"


@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        ("{broken", "application/json"),
        ("not-json", "text/plain"),
    ],
)
def test_history_retry_rejects_nonempty_malformed_or_non_json_body(tmp_path, monkeypatch, data, content_type):
    client, headers, _store, _reader = _client(tmp_path, monkeypatch)
    service = client.app.state.services["SOLVER_COORDINATION_SERVICE"]
    calls = []

    def retry_job(**kwargs):
        calls.append(kwargs)
        return {"status": "running"}

    service.retry_job = retry_job

    response = client.post(
        "/api/solver/history/job-synthetic/retry",
        content=data,
        headers={**headers, "Content-Type": content_type},
    )

    assert response.status_code == 400
    assert response.json() == {"code": "invalid_solver_input"}
    assert calls == []
