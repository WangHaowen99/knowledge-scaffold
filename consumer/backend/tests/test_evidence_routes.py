"""HTTP contracts for ontology evidence management and lookup."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api.auth.auth import AuthManager
from api.routes.evidence import evidence_router
from api.testing import TestApplication
from config.evidence_config import EvidenceConfig
from ontology.evidence.manager import EvidenceIndexManager
from ontology.evidence.worker import EvidenceIndexWorker
from persistence.runtime_facts import RuntimeFactsStore
from workspace.workspaces import create_workspace, get_workspace_paths


def _client(tmp_path: Path):
    app = TestApplication()
    app.state.services["TESTING"] = True
    auth = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth.authenticate("admin", "TestBootstrap#2026")
    assert user is not None
    if user.password_change_required:
        user = auth.change_password(
            "admin",
            "TestBootstrap#2026",
            "RootStrong123!",
        )
    app.state.services["AUTH_MANAGER"] = auth
    app.include_router(evidence_router)
    client = TestClient(app)
    token = auth.issue_token(user)
    return client, {"Authorization": "Bearer " + token}


def _workspace(tmp_path: Path, monkeypatch):
    import ontology.ontologies as ontologies_module

    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Evidence Workspace", workspace_id="evidence-ws", set_as_default=True)
    paths = get_workspace_paths("evidence-ws")
    root = paths.ontologies_dir / "eco"
    version = root / "versions" / "1.0.0"
    (version / "objects").mkdir(parents=True)
    (root / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                "id: eco",
                "name: ECO",
                "description: Evidence route ontology",
                "version: 1.0.0",
                "active_version: 1.0.0",
                "versions:",
                "  - 1.0.0",
                "language: zh-CN",
                "enabled: true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (version / "index.md").write_text(
        "---\nresource: root\nkey: root\ntitle: ECO\n---\n# ECO\n",
        encoding="utf-8",
    )
    (version / "objects" / "eco.md").write_text(
        "---\nresource: object\nkey: eco\ntitle: 工程更改指令\n---\n# 工程更改指令\n\n## 字段\nECO编号。\n",
        encoding="utf-8",
    )
    ontologies_module._global_registry = None
    return paths


def _enabled() -> EvidenceConfig:
    return EvidenceConfig(enabled=True, rollout_mode="all", auto_build_enabled=True)


def test_status_is_read_only_when_feature_is_disabled(tmp_path: Path, monkeypatch):
    paths = _workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(
        EvidenceConfig,
        "load",
        classmethod(lambda cls: EvidenceConfig(enabled=False, rollout_mode="off")),
    )
    client, headers = _client(tmp_path)

    response = client.get("/agentic/ontologies/eco/evidence-index/status", headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert "performance_degraded" not in response.json()["feature"]
    assert "degraded_reasons" not in response.json()["feature"]
    assert not (paths.runtime_dir / "ontology-evidence").exists()


def test_rebuild_is_durable_idempotent_and_lookup_is_workspace_scoped(tmp_path: Path, monkeypatch):
    paths = _workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(EvidenceConfig, "load", classmethod(lambda cls: _enabled()))
    client, headers = _client(tmp_path)
    request_headers = {**headers, "Idempotency-Key": "rebuild-eco-001"}

    queued = client.post(
        "/agentic/ontologies/eco/evidence-index/rebuild",
        headers=request_headers,
        json={"reason": "route contract test", "semantic": "inherit"},
    )
    duplicate = client.post(
        "/agentic/ontologies/eco/evidence-index/rebuild",
        headers=request_headers,
        json={"reason": "route contract test", "semantic": "inherit"},
    )

    assert queued.status_code == 202
    assert queued.json()["status"] == "queued"
    assert duplicate.status_code == 200
    assert duplicate.json()["job_id"] == queued.json()["job_id"]
    assert duplicate.json()["duplicate"] is True

    stats = EvidenceIndexWorker().run_once()
    assert stats["build_succeeded"] == 1
    status = client.get("/agentic/ontologies/eco/evidence-index/status", headers=headers)
    assert status.json()["status"] == "ready"
    assert status.json()["counts"]["chunks"] > 0

    index = EvidenceIndexManager(paths.runtime_dir).open_active("eco")
    evidence_id = next(iter(index.iter_chunks()))["evidence_id"]
    lookup = client.get("/agentic/evidence/" + evidence_id, headers=headers)
    assert lookup.status_code == 200
    assert lookup.json()["contract"] == "EvidenceLookup/v1"
    assert lookup.json()["validity"]["active"] is True


def test_runtime_cleanup_validates_and_replays_idempotent_result(tmp_path: Path, monkeypatch):
    paths = _workspace(tmp_path, monkeypatch)
    client, headers = _client(tmp_path)
    store = RuntimeFactsStore(paths.runtime_dir / "ontology-evidence" / "runtime-facts.sqlite3")
    record = store.put(
        {
            "workspace_id": "evidence-ws",
            "run_id": "run-old",
            "tool_call_id": "call-old",
            "kind": "provider_query",
            "ontology_id": "eco",
            "ontology_epoch": "a" * 64,
            "subject": "eco",
            "status": "success",
            "summary": "old result",
            "grounding_status": "unverified",
            "grounding_warnings": ["DETAIL_NOT_LOADED"],
            "validity_class": "current_fact",
            "created_at": "2020-01-01T00:00:00+00:00",
        }
    )

    invalid = client.post(
        "/agentic/runtime-facts/cleanup",
        headers=headers,
        json={"dry_run": False, "older_than_days": 1, "reason": "too young"},
    )
    assert invalid.status_code == 400

    cleanup_headers = {**headers, "Idempotency-Key": "runtime-cleanup-001"}
    first = client.post(
        "/agentic/runtime-facts/cleanup",
        headers=cleanup_headers,
        json={
            "dry_run": False,
            "ontology_id": "eco",
            "older_than_days": 30,
            "max_items": 10,
            "reason": "retention test",
        },
    )
    second = client.post(
        "/agentic/runtime-facts/cleanup",
        headers=cleanup_headers,
        json={
            "dry_run": False,
            "ontology_id": "eco",
            "older_than_days": 30,
            "max_items": 10,
            "reason": "retention test",
        },
    )
    assert first.status_code == 200
    assert first.json()["summary"]["deleted_items"] == 1
    assert second.json()["duplicate"] is True
    assert store.lookup(workspace_id="evidence-ws", evidence_id=record["evidence_id"]) is None
