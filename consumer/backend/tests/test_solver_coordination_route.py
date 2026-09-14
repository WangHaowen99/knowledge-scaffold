# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

import json
import secrets

import pytest
from api.auth.auth import AuthManager
from api.middleware import MAX_REQUEST_BODY_BYTES
from api.routes.solver_coordination import (
    solver_coordination_public_router,
    solver_coordination_router,
)
from api.testing import TestApplication
from fastapi import APIRouter
from fastapi.testclient import TestClient
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


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)
    app = TestApplication()
    auth_manager = AuthManager(str(tmp_path / "users.json"), secrets.token_urlsafe(32))
    initial_credential = secrets.token_urlsafe(32) + "Aa1!"
    replacement_credential = secrets.token_urlsafe(32) + "Bb2!"
    auth_manager.create_user(
        "synthetic_operator",
        initial_credential,
        role="admin",
    )
    user = auth_manager.change_password(
        "synthetic_operator",
        initial_credential,
        replacement_credential,
    )
    app.state.services["AUTH_MANAGER"] = auth_manager
    app.include_router(solver_coordination_router)
    return TestClient(app), {
        "Authorization": "Bearer {}".format(auth_manager.issue_token(user)),
        "Content-Type": "application/json",
    }


def test_solver_coordination_exports_fastapi_router():
    assert isinstance(solver_coordination_router, APIRouter)


def test_candidate_set_route_returns_structured_eighteen_candidate_response(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)

    response = client.post(
        "/agentic/solver-coordination/v1/candidate-sets",
        data=json.dumps(_payload(9)),
        headers=headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["candidate_count"] == 18
    assert body["schema_version"] == "solver_coordination.v1"
    assert body["artifact_type"] == "candidate_set"
    assert body["authoritative"] is False
    assert body["source_run_id"].startswith("solve_")
    assert body["candidate_set_ref"].startswith("candidate_set_")
    assert body["candidate_set_digest"].startswith("sha256:")
    assert body["problem_digest"].startswith("sha256:")
    assert body["knowledge_package_revision"] == "revision_demo_1"
    assert body["problem_family"] == "binary_ordered_route.v1"
    assert len(body["candidates"]) == 18
    assert all(candidate["candidate_digest"].startswith("sha256:") for candidate in body["candidates"])
    assert "owner_id" not in body
    run = SolverRunLedger("demo").get_run(body["source_run_id"], owner_id="synthetic_operator")
    assert run is not None
    assert run["run_type"] == "solve"
    assert run["status"] == "completed"
    assert run["candidate_set_digest"] == body["candidate_set_digest"]


def test_solver_run_status_is_queryable_without_exposing_owner(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)
    created = client.post(
        "/agentic/solver-coordination/v1/candidate-sets",
        json=_payload(2),
        headers=headers,
    ).json()

    response = client.get(
        "/agentic/solver-coordination/v1/runs/{}".format(created["source_run_id"]),
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == created["source_run_id"]
    assert body["run_type"] == "solve"
    assert "owner_id" not in body


def test_solver_run_query_hides_unknown_run(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)
    response = client.get(
        "/agentic/solver-coordination/v1/runs/solve_unknown",
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


def test_candidate_set_route_accepts_arbitrary_positive_item_count(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)

    response = client.post(
        "/agentic/solver-coordination/v1/candidate-sets",
        data=json.dumps(_payload(2)),
        headers=headers,
    )

    assert response.status_code == 201
    assert response.json()["candidate_count"] == 4


def test_candidate_set_route_requires_authentication(tmp_path, monkeypatch):
    client, _headers = _client(tmp_path, monkeypatch)

    response = client.post(
        "/agentic/solver-coordination/v1/candidate-sets",
        json=_payload(),
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "content_type",
    ("application/jsonp", "text/foo+json"),
)
def test_candidate_set_route_rejects_non_json_media_types(
    tmp_path,
    monkeypatch,
    content_type,
):
    client, headers = _client(tmp_path, monkeypatch)

    response = client.post(
        "/agentic/solver-coordination/v1/candidate-sets",
        content=json.dumps(_payload(2)),
        headers={**headers, "Content-Type": content_type},
    )

    assert response.status_code == 400
    assert response.json() == {"code": "invalid_solver_input"}


def test_candidate_set_route_accepts_application_json_suffix_media_type(
    tmp_path,
    monkeypatch,
):
    client, headers = _client(tmp_path, monkeypatch)

    response = client.post(
        "/agentic/solver-coordination/v1/candidate-sets",
        content=json.dumps(_payload(2)),
        headers={**headers, "Content-Type": "application/vnd.solver+json"},
    )

    assert response.status_code == 201


def test_candidate_set_route_rejects_declared_body_over_limit(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)

    response = client.post(
        "/agentic/solver-coordination/v1/candidate-sets",
        content=b"{}",
        headers={
            **headers,
            "Content-Length": str(MAX_REQUEST_BODY_BYTES + 1),
        },
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body exceeds the configured size limit"}


def test_candidate_set_route_preserves_missing_workspace_404(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)

    response = client.post(
        "/agentic/solver-coordination/v1/candidate-sets",
        json=_payload(),
        headers={**headers, "X-Workspace-Id": "missing_workspace"},
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "path",
    (
        "/agentic/solver-coordination/v1/runs/solve_unknown",
        "/api/solver/runs/solve_unknown",
    ),
)
def test_solver_run_query_uses_platform_invalid_workspace_400_contract(
    tmp_path,
    monkeypatch,
    path,
):
    client, headers = _client(tmp_path, monkeypatch)
    client.app.include_router(solver_coordination_public_router)

    response = client.get(
        path,
        headers={**headers, "X-Workspace-Id": "../invalid"},
    )

    assert response.status_code == 400
    assert "detail" in response.json()
