"""备份（E2）与运维接口测试。数据全部合成，凭据均为占位值。"""

import asyncio
import json
import os
import sqlite3
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.routers import admin, projects
from app.services import auth, backup, conversation_store
from app.services.engines import jiuwen
from app.services.onto_mcp import tokens
from app.services.workspace import WorkspaceManager


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "BACKUP_DIR", "")
    monkeypatch.setattr(config, "MODELING_URL", "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assets = tmp_path / "assets"
    (assets / "resource-template").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    manager = WorkspaceManager(data, assets)
    monkeypatch.setattr(projects, "workspaces", manager)
    monkeypatch.setattr(admin, "workspaces", manager)
    return manager, tmp_path


def seed(manager: WorkspaceManager, home: Path) -> dict:
    """在数据目录各处放一份合成状态。"""
    meta = manager.create_project("project_alpha", owner="operator_a")
    root = manager.project_root(meta["id"])
    (root / "research" / "note.md").write_text("synthetic\n", encoding="utf-8")

    data = Path(config.DATA_DIR)
    conversation_store.history_path(meta["id"]).write_text('{"type": "user_message"}\n')
    (data / "studio").mkdir(exist_ok=True)
    (data / "studio" / f"{meta['id']}.drafts.json").write_text("{}")

    checkpoints = jiuwen._checkpoint_db_path()
    checkpoints.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(checkpoints)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE checkpoint (id TEXT)")
    conn.execute("INSERT INTO checkpoint VALUES ('synthetic-checkpoint')")
    conn.commit()  # 连接不关：WAL 里有未合并的内容，直接打包文件会丢数据
    (checkpoints.parent / meta["id"]).mkdir()
    (checkpoints.parent / meta["id"] / "todo.md").write_text("synthetic")

    with tokens.database(manager) as db:
        db.execute(
            "INSERT INTO tokens VALUES ('t1', ?, 'digest', 'synthetic', 'operator_a', 'operator_a', '[]', 0, NULL, NULL, NULL)",
            (meta["id"],),
        )

    for name in (
        "model-settings.json", "user-model-settings.json", "runtime-settings.json",
        "mcp-servers.json", "model-context-presets.json", "modeling-cleanup-tasks.json",
    ):
        (data / name).write_text('{"synthetic": true}')
    (data / "audit.jsonl").write_text('{"user": "operator_a"}\n')
    (data / "errors.jsonl").write_text('{"kind": "synthetic"}\n')
    (data / ".model-secret-key").write_text("synthetic-master-key-not-for-backup")
    (data / "eval-data-sources").mkdir()
    (data / "eval-data-sources" / f"{meta['id']}.json").write_text("{}")

    transcripts = home / ".claude" / "projects" / "workspace_demo"
    transcripts.mkdir(parents=True)
    (transcripts / "session.jsonl").write_text("{}\n")
    return {"meta": meta, "wal_conn": conn}


def members(archive: Path) -> set[str]:
    with tarfile.open(archive) as tar:
        return set(tar.getnames())


def test_backup_covers_state_and_excludes_master_key(env, tmp_path):
    manager, _ = env
    seeded = seed(manager, tmp_path / "home")
    project = seeded["meta"]["id"]

    name = backup.run_backup(manager)
    seeded["wal_conn"].close()
    archive = backup.backups_dir() / name
    names = members(archive)

    expected = {
        f"data/workspaces/{project}/research/note.md",
        f"data/workspaces/{project}/.onto-web.json",
        f"data/workspaces/{project}/.git/HEAD",
        f"data/sessions/{project}.jsonl",
        f"data/studio/{project}.drafts.json",
        "data/jiuwen/checkpoints.db",
        f"data/jiuwen/{project}/todo.md",
        "data/onto-mcp.sqlite",
        "data/model-settings.json",
        "data/user-model-settings.json",
        "data/runtime-settings.json",
        "data/mcp-servers.json",
        "data/model-context-presets.json",
        "data/modeling-cleanup-tasks.json",
        "data/audit.jsonl",
        "data/errors.jsonl",
        f"data/eval-data-sources/{project}.json",
        "home/.claude/projects/workspace_demo/session.jsonl",
    }
    assert expected <= names
    assert not any(".model-secret-key" in n for n in names)
    assert not any(n.startswith("data/backups") for n in names)
    assert not any(n.endswith(("-wal", "-shm")) for n in names)
    assert oct(archive.stat().st_mode & 0o777) == oct(0o600)

    extracted = tmp_path / "restore"
    with tarfile.open(archive) as tar:
        tar.extractall(extracted, filter="tar")
    rows = sqlite3.connect(extracted / "data/jiuwen/checkpoints.db").execute(
        "SELECT id FROM checkpoint").fetchall()
    assert rows == [("synthetic-checkpoint",)]
    tokens_rows = sqlite3.connect(extracted / "data/onto-mcp.sqlite").execute(
        "SELECT project_id FROM tokens").fetchall()
    assert tokens_rows == [(project,)]
    # 工作区里指向资产的软链按链接本身保存，不跟进去
    with tarfile.open(archive) as tar:
        assert tar.getmember(f"data/workspaces/{project}/CLAUDE.md").issym()


def test_backup_packs_each_workspace_under_project_lock(env, tmp_path, monkeypatch):
    manager, _ = env
    first = manager.create_project("project_alpha")["id"]
    second = manager.create_project("project_beta")["id"]
    locked: list[str] = []
    original = manager._project_lock

    @contextmanager
    def recording_lock(project_id):
        with original(project_id):
            locked.append(project_id)
            yield

    monkeypatch.setattr(manager, "_project_lock", recording_lock)
    backup.run_backup(manager)
    assert sorted(locked) == sorted([first, second])


def test_backup_keeps_three_newest_and_honours_backup_dir(env, tmp_path, monkeypatch):
    manager, _ = env
    target = tmp_path / "separate-volume"
    monkeypatch.setattr(config, "BACKUP_DIR", str(target))
    manager.create_project("project_alpha")
    (target).mkdir()
    (target / "stale.tar.gz.partial").write_text("")  # 上次被打断的半成品

    created = []
    for index in range(4):
        created.append(backup.run_backup(manager))
        past = time.time() - (10 - index) * 60
        os.utime(target / created[-1], (past, past))

    listed = [entry["name"] for entry in backup.list_backups()]
    assert listed == list(reversed(created[1:]))
    assert sorted(p.name for p in target.iterdir()) == sorted(created[1:])
    assert not (Path(config.DATA_DIR) / "backups").exists()


def test_seconds_until_due(env, tmp_path):
    manager, _ = env
    period = 24 * 3600
    assert backup.seconds_until_due(period) == 0  # 从没备份过：立即补
    name = backup.run_backup(manager)
    assert period - 60 < backup.seconds_until_due(period) <= period
    old = time.time() - 2 * period
    os.utime(backup.backups_dir() / name, (old, old))
    assert backup.seconds_until_due(period) == 0  # 已过期：立即补


class _Stop(Exception):
    pass


def _run_scheduler(manager, monkeypatch, run_backup):
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 24)
    monkeypatch.setattr(backup, "run_backup", run_backup)
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        raise _Stop

    with pytest.raises(_Stop):
        asyncio.run(backup.scheduler_loop(manager, sleep=fake_sleep))
    return sleeps


def test_scheduler_catches_up_at_startup_when_overdue(env, monkeypatch):
    manager, _ = env
    calls = []

    def fake_backup(m):
        calls.append(m)
        directory = backup.backups_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{backup.BACKUP_PREFIX}synthetic.tar.gz").write_text("")
        return "synthetic"

    sleeps = _run_scheduler(manager, monkeypatch, fake_backup)
    assert calls == [manager]  # 先补跑，再等下一个间隔
    assert 24 * 3600 - 60 < sleeps[0] <= 24 * 3600


def test_scheduler_waits_when_recent_backup_exists(env, monkeypatch):
    manager, _ = env
    directory = backup.backups_dir()
    directory.mkdir(parents=True)
    recent = directory / f"{backup.BACKUP_PREFIX}recent.tar.gz"
    recent.write_text("")
    past = time.time() - 3600
    os.utime(recent, (past, past))
    calls = []

    sleeps = _run_scheduler(manager, monkeypatch, lambda m: calls.append(m))
    assert calls == []
    assert 23 * 3600 - 60 < sleeps[0] <= 23 * 3600


def test_scheduler_backs_off_after_failure(env, monkeypatch):
    manager, _ = env

    def failing(m):
        raise OSError("synthetic disk full")

    sleeps = _run_scheduler(manager, monkeypatch, failing)
    assert sleeps == [backup.FAILURE_RETRY_SECONDS]


# ---- 运维接口 ----


@pytest.fixture()
def client(env, monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", True)
    claims = {
        "admin-token": {"sub": "operator_a", "role": "admin"},
        "user-token": {"sub": "user_b", "role": "user"},
    }
    monkeypatch.setattr(auth, "verify_access_token", lambda token: claims.get(token))
    return TestClient(app)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_admin_backup_route_creates_and_lists(client, env):
    manager, _ = env
    manager.create_project("project_alpha")
    response = client.post("/api/admin/backup", headers=_auth("admin-token"))
    assert response.status_code == 200
    body = response.json()
    assert body["created"].startswith(backup.BACKUP_PREFIX)
    assert [entry["name"] for entry in body["backups"]] == [body["created"]]

    health = client.get("/api/admin/health", headers=_auth("admin-token")).json()
    assert health["backup"]["last"]["name"] == body["created"]
    assert health["backup"]["interval_hours"] == config.BACKUP_INTERVAL_HOURS

    overview = client.get("/api/admin/overview", headers=_auth("admin-token")).json()
    assert [entry["name"] for entry in overview["backups"]] == [body["created"]]


@pytest.mark.parametrize("method, path", [
    ("post", "/api/admin/backup"),
    ("get", "/api/admin/health"),
    ("get", "/api/admin/overview"),
    ("get", "/api/admin/audit"),
])
def test_admin_routes_reject_non_admin(client, method, path):
    response = getattr(client, method)(path, headers=_auth("user-token"))
    assert response.status_code == 403
    assert not backup.list_backups()


def test_admin_routes_require_login(client):
    assert client.post("/api/admin/backup").status_code == 401
