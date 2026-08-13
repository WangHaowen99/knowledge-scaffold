"""Tests for skill-evolution HTTP routes: auth, admin gating, drift field."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from flask import Flask

import api.routes.skill_evolution as se_routes
from agent.skill_evolution.models import DiffResult, DriftStatus, TaskRecord
from api.routes.skill_evolution import skill_evolution_bp
from api.auth.auth import AuthManager


@dataclass
class _FakeStore:
    tasks: list

    def list_tasks(self, workspace_id, **kwargs):
        return list(self.tasks)

    def get_task(self, workspace_id, task_id):
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        return None

    def delete_task(self, workspace_id, task_id):
        self.tasks = [t for t in self.tasks if t.task_id != task_id]


class _FakeService:
    def __init__(self, tasks):
        self._store = _FakeStore(tasks)

    def start(self, **kwargs):
        return TaskRecord(
            task_id="new1", workspace_id="ws1", ontology_id="onto1", session_id="s",
            triggered_by="alice", run_id="r", status="submitted", active_version="1.0.0",
            version_sha="x", created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )

    def compute_diff(self, record):
        return DiffResult(task_id=record.task_id, ontology_id=record.ontology_id,
                          change_summary="改动 1 个文件", files=[], is_empty=False)

    def detect_drift(self, record):
        return DriftStatus(drifted=False, reason="clean")

    def approve(self, record, **kwargs):
        record.status = "approved"
        return record

    def reject(self, record, **kwargs):
        record.status = "rejected"
        return record

    def cancel(self, record, **kwargs):
        record.status = "cancelled"
        return record

    def delete_task(self, record):
        if record.status not in ("approved", "rejected", "no_change", "failed", "interrupted", "cancelled"):
            raise ValueError("仅终态任务可删除；进行中的任务请先取消")
        self._store.delete_task(record.workspace_id, record.task_id)


def _task(task_id="t1", status="completed", triggered_by="alice"):
    return TaskRecord(
        task_id=task_id, workspace_id="ws1", ontology_id="onto1", ontology_name="测试本体",
        session_id="s", triggered_by=triggered_by, run_id="r", status=status,
        active_version="1.0.0", version_sha="x", created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00", change_summary="改动 1 个文件",
    )


def _build_app(tmp_path, monkeypatch, tasks):
    app = Flask(__name__)
    app.config["TESTING"] = True
    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    admin = auth_manager.authenticate("admin", "TestBootstrap#2026")
    admin = auth_manager.change_password("admin", "TestBootstrap#2026", "RootStrong123!")
    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(skill_evolution_bp)

    fake = _FakeService(tasks)
    monkeypatch.setattr(se_routes, "get_skill_evolution_service", lambda: fake)
    monkeypatch.setattr(se_routes, "resolve_workspace_id", lambda req: "ws1")

    @contextmanager
    def _scope(ws=None):
        yield "ws1"

    monkeypatch.setattr(se_routes, "workspace_scope", _scope)
    # /start imports get_request_stores from api.routes; stub it so the
    # route doesn't require a real UserStoreManager.
    import api.routes as app_routes
    monkeypatch.setattr(app_routes, "get_request_stores", lambda: (None, object()))

    client = app.test_client()
    admin_token = auth_manager.issue_token(admin)
    # Create a normal user and clear its must_change_password flag (otherwise
    # require_auth rejects non-/auth paths). Password must not contain "alice".
    auth_manager.create_user("alice", "UserPass123!", role="user")
    auth_manager.change_password("alice", "UserPass123!", "UserPass456!")
    alice = auth_manager.authenticate("alice", "UserPass456!")
    alice_token = auth_manager.issue_token(alice)
    return client, {"Authorization": "Bearer {}".format(admin_token)}, {"Authorization": "Bearer {}".format(alice_token)}, fake


def test_start_returns_202(tmp_path, monkeypatch):
    client, admin_h, _, _ = _build_app(tmp_path, monkeypatch, [])
    res = client.post("/agentic/skill-evolution/start",
                      headers=admin_h, json={"ontology_id": "onto1", "session_id": "s"})
    assert res.status_code == 202
    assert res.get_json()["status"] == "submitted"


def test_start_rejects_missing_body(tmp_path, monkeypatch):
    client, admin_h, _, _ = _build_app(tmp_path, monkeypatch, [])
    res = client.post("/agentic/skill-evolution/start", headers=admin_h, json={})
    assert res.status_code == 400


def test_list_tasks(tmp_path, monkeypatch):
    client, admin_h, _, _ = _build_app(tmp_path, monkeypatch, [_task()])
    res = client.get("/agentic/skill-evolution/tasks", headers=admin_h)
    assert res.status_code == 200
    data = res.get_json()
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["task_id"] == "t1"


def test_get_task_includes_drift(tmp_path, monkeypatch):
    client, admin_h, _, _ = _build_app(tmp_path, monkeypatch, [_task()])
    res = client.get("/agentic/skill-evolution/tasks/t1", headers=admin_h)
    assert res.status_code == 200
    assert res.get_json()["drift"]["reason"] == "clean"


def test_non_owner_non_admin_forbidden_on_task(tmp_path, monkeypatch):
    # task triggered by alice; admin can see, a different non-admin user cannot
    client, admin_h, alice_h, _ = _build_app(tmp_path, monkeypatch, [_task(triggered_by="bob")])
    res = client.get("/agentic/skill-evolution/tasks/t1", headers=alice_h)
    assert res.status_code == 403


def test_approve_admin_only(tmp_path, monkeypatch):
    client, admin_h, alice_h, _ = _build_app(tmp_path, monkeypatch, [_task()])
    # non-admin → 403
    res = client.post("/agentic/skill-evolution/tasks/t1/approve",
                      headers=alice_h, json={"mode": "new_version"})
    assert res.status_code == 403
    # admin → 200
    res = client.post("/agentic/skill-evolution/tasks/t1/approve",
                      headers=admin_h, json={"mode": "new_version", "version_name": "evol-1"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "approved"


def test_reject_admin_only(tmp_path, monkeypatch):
    client, admin_h, alice_h, _ = _build_app(tmp_path, monkeypatch, [_task()])
    res = client.post("/agentic/skill-evolution/tasks/t1/reject", headers=alice_h)
    assert res.status_code == 403
    res = client.post("/agentic/skill-evolution/tasks/t1/reject", headers=admin_h)
    assert res.status_code == 200


def test_cancel_owner_allowed(tmp_path, monkeypatch):
    # alice triggered the task → she may cancel
    client, admin_h, alice_h, _ = _build_app(tmp_path, monkeypatch, [_task(triggered_by="alice", status="running")])
    res = client.post("/agentic/skill-evolution/tasks/t1/cancel", headers=alice_h)
    assert res.status_code == 200
    assert res.get_json()["status"] == "cancelled"


def test_cancel_non_owner_non_admin_forbidden(tmp_path, monkeypatch):
    client, admin_h, alice_h, _ = _build_app(tmp_path, monkeypatch, [_task(triggered_by="bob", status="running")])
    res = client.post("/agentic/skill-evolution/tasks/t1/cancel", headers=alice_h)
    assert res.status_code == 403


def test_diff_requires_completed(tmp_path, monkeypatch):
    client, admin_h, _, _ = _build_app(tmp_path, monkeypatch, [_task(status="running")])
    res = client.get("/agentic/skill-evolution/tasks/t1/diff", headers=admin_h)
    assert res.status_code == 400


def test_delete_admin_only(tmp_path, monkeypatch):
    client, admin_h, alice_h, _ = _build_app(tmp_path, monkeypatch, [_task(status="approved")])
    res = client.delete("/agentic/skill-evolution/tasks/t1", headers=alice_h)
    assert res.status_code == 403
    res = client.delete("/agentic/skill-evolution/tasks/t1", headers=admin_h)
    assert res.status_code == 200
    assert res.get_json()["success"] is True


def test_delete_rejects_non_terminal(tmp_path, monkeypatch):
    client, admin_h, _, _ = _build_app(tmp_path, monkeypatch, [_task(status="running")])
    res = client.delete("/agentic/skill-evolution/tasks/t1", headers=admin_h)
    assert res.status_code == 409
