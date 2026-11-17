"""I1：运维管控的导出器（O-11、O-12）、恢复演练（O-38、O-39）、任务中心（O-40、O-41）、数据保留（O-43、O-44）。"""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, ops_app
from app.api import deps
from app.governance import telemetry
from app.main import app
from app.routers import ops as ops_routes
from app.services import auth, backup, ops_center, streams
from app.services.workspace import WorkspaceManager

OPS = "/api/v1/ops"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_DIR", "")
    monkeypatch.setattr(config, "OPS_ON_MAIN", True)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    streams.reset_all()
    with TestClient(app) as client:
        yield client, workspaces
    streams.reset_all()


def wait_task(client, task_id: str) -> dict:
    for _ in range(200):
        item = next(task for task in client.get(f"{OPS}/tasks").json()["items"] if task["id"] == task_id)
        if item["state"] not in ("queued", "running"):
            return item
        time.sleep(0.05)
    raise AssertionError("任务没有结束")


def test_exporters_list_and_prometheus_switch(api):
    client, _ = api
    items = client.get(f"{OPS}/exporters").json()["items"]
    assert [item["id"] for item in items] == ["prometheus", "file"]
    assert all(set(item) == {"id", "label", "implementation", "target", "signals", "enabled", "configured",
                                         "required"} for item in items)
    metrics = TestClient(ops_app.ops_app)
    assert metrics.get("/metrics").status_code == 200
    saved = client.put(f"{OPS}/exporters", json={"items": [{"id": "prometheus", "enabled": False, "target": "/metrics",
                                                             "signals": ["metrics"]}]}).json()
    assert saved["effective"] == "immediate" and saved["items"][0]["enabled"] is False
    assert metrics.get("/metrics").status_code == 404
    client.put(f"{OPS}/exporters", json={"items": [{"id": "prometheus", "enabled": True}]})
    assert metrics.get("/metrics").status_code == 200
    bad = client.put(f"{OPS}/exporters", json={"items": [{"id": "file", "enabled": False}]})
    assert bad.status_code == 400 and bad.json()["details"]["fields"] == {"file": "required"}
    assert client.put(f"{OPS}/exporters", json={"items": [{"id": "otel", "enabled": True}]}).status_code == 400


def test_retention_wired_to_cleanup(api, monkeypatch):
    client, workspaces = api
    items = {item["id"]: item for item in client.get(f"{OPS}/retention").json()["items"]}
    assert set(items) == {"logs", "backups", "diagnostics", "notifications", "audit", "trash"}  # 证据快照本期不出
    assert items["audit"] == {**items["audit"], "retention": {"days": 365}, "editable": False}
    assert items["backups"]["retention"] == {"keep_last": backup.BACKUP_KEEP} and items["logs"]["next_run_at"]
    bad = client.put(f"{OPS}/retention", json={"items": [{"id": "audit", "retention": {"days": 30}},
                                                         {"id": "logs", "retention": {"days": 0}}]})
    assert bad.status_code == 400 and bad.json()["details"]["fields"] == {"audit": "not_editable", "logs": "out_of_range"}
    handler = type("H", (), {"backupCount": 14})()
    monkeypatch.setattr(telemetry, "_file_handler", handler)
    saved = client.put(f"{OPS}/retention", json={"items": [{"id": "backups", "retention": {"keep_last": 2}},
                                                           {"id": "logs", "retention": {"days": 7}},
                                                           {"id": "diagnostics", "retention": {"days": 3}}]})
    assert saved.status_code == 200
    got = {item["id"]: item["retention"] for item in saved.json()["items"]}
    assert got["backups"] == {"keep_last": 2} and got["logs"] == {"days": 7} and got["diagnostics"] == {"days": 3}
    assert handler.backupCount == 7 and telemetry._configured_keep_days() == 7
    workspaces.create_project("保留合成", owner="")
    for _ in range(3):
        backup.run_backup(workspaces)
        time.sleep(1.1)  # 备份文件名按秒
    assert len(backup.list_backups()) == 2
    assert client.get(f"{OPS}/backups").json()["schedule"]["keep"] == 2


def test_backup_and_drill_go_through_task_center(api, monkeypatch):
    client, workspaces = api
    workspaces.create_project("演练合成", owner="")
    created = client.post(f"{OPS}/backups")
    assert created.status_code == 202 and created.json()["task_id"].startswith("backup-")
    task = wait_task(client, created.json()["task_id"])
    assert task["type"] == "backup" and task["state"] == "succeeded" and task["initiator"] == "local"
    assert task["progress_pct"] == 100 and task["finished_at"]
    name = created.json()["name"]
    assert client.get(f"{OPS}/backups").json()["last_drill"] is None
    started = client.post(f"{OPS}/backups/drills", json={"backup": name, "sample_projects": 3})
    assert started.status_code == 202
    drill_task = wait_task(client, started.json()["task_id"])
    assert drill_task["type"] == "drill" and drill_task["state"] == "succeeded"
    drills = client.get(f"{OPS}/backups/drills").json()
    assert drills["items"][0]["result"] == "passed" and drills["items"][0]["backup"] == name
    assert drills["items"][0]["detail"] == "抽检 1 个项目"
    assert client.get(f"{OPS}/backups").json()["last_drill"]["result"] == "passed"
    assert client.post(f"{OPS}/backups/drills", json={"backup": "nope.tar.gz"}).status_code == 404
    assert client.post(f"{OPS}/backups/drills", json={"backup": name, "sample_projects": 0}).status_code == 400
    # 筛选
    assert {item["type"] for item in client.get(f"{OPS}/tasks", params={"type": "drill"}).json()["items"]} == {"drill"}
    assert client.get(f"{OPS}/tasks", params={"type": "bogus"}).status_code == 400


def test_drill_fails_on_broken_backup(api):
    client, _ = api
    directory = backup.backups_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "onto-modeling-backup-broken.tar.gz").write_bytes(b"not a tarball")
    started = client.post(f"{OPS}/backups/drills", json={"backup": "onto-modeling-backup-broken.tar.gz"})
    task = wait_task(client, started.json()["task_id"])
    assert task["state"] == "failed" and "备份读不出" in task["error"]
    assert client.get(f"{OPS}/backups/drills").json()["items"][0]["result"] == "failed"


def test_task_actions(api, monkeypatch):
    client, workspaces = api
    workspaces.create_project("重试合成", owner="")
    real = backup.run_backup
    calls = {"n": 0}

    def flaky(manager):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("合成的磁盘错误")
        return real(manager)

    monkeypatch.setattr(backup, "run_backup", flaky)
    with TestClient(app, raise_server_exceptions=False) as quiet:
        assert quiet.post(f"{OPS}/backups").status_code == 500
    failed = client.get(f"{OPS}/tasks").json()["items"][0]
    assert failed["state"] == "failed" and "合成的磁盘错误" in failed["error"]
    retried = client.post(f"{OPS}/tasks/{failed['id']}/retry")
    assert retried.status_code == 200 and retried.json()["state"] == "succeeded" and retried.json()["id"] != failed["id"]
    for op in ("retry", "cancel"):
        res = client.post(f"{OPS}/tasks/{retried.json()['id']}/{op}")
        assert res.status_code == 409 and res.json()["code"] == "task_state_invalid"
    # 续跑只有证据采集用，随它删除（ADR-0101）
    assert client.post(f"{OPS}/tasks/{retried.json()['id']}/resume").status_code == 404
    assert client.post(f"{OPS}/tasks/nope/cancel").json()["code"] == "task_not_found"
    # 进程内可取消的演练：取消后任务记为已取消
    task = ops_center.start_task("drill", "合成备份", "local", cancellable=True)
    cancelled = client.post(f"{OPS}/tasks/{task['id']}/cancel").json()
    assert cancelled["state"] == "cancelled"
    # 重启时还在跑的任务改判失败
    live = ops_center.start_task("backup", "数据备份", "system")
    ops_center._sweep_interrupted()
    assert ops_center.get_task(live["id"])["status"] == "failed"


def test_pack_repo_backup_task(api):
    client, _ = api
    created = client.post(f"{OPS}/pack-repo/backups")
    assert created.status_code == 202 and created.json()["task_id"].startswith("pack-backup-")
    assert client.get(f"{OPS}/tasks").json()["items"][0]["type"] == "pack_repo_backup"
    assert ops_routes.ops_center is ops_center
