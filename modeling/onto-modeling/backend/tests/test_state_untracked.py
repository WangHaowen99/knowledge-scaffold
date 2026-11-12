"""项目元数据与工具状态不进 git（A13）与 git 锁残留清理（E1）。

数据全部合成：项目名、用户名均为占位值。
"""

import json
import os
import time
import zipfile
from pathlib import Path

import pytest

from app import config
from app.services import ownership
from app.services import workspace as workspace_module
from app.services.workspace import (
    META_FILE,
    UNTRACK_COMMIT_MESSAGE,
    WorkspaceError,
    WorkspaceManager,
)
from tests.proc import git

LEGACY_GITIGNORE = ".claude\nCLAUDE.md\nREADME.md\ntmp/\n"


def tracked(root: Path) -> set[str]:
    return set(git(root, "ls-files").splitlines())


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    return WorkspaceManager(tmp_path / "data", assets)


def make_legacy(manager: WorkspaceManager, owner: str = "") -> tuple[dict, Path]:
    """模拟存量项目：旧模板 .gitignore，元数据与工具状态都进了 git。"""
    meta = manager.create_project("project_alpha", owner=owner)
    root = manager.project_root(meta["id"])
    (root / ".gitignore").write_text(LEGACY_GITIGNORE, encoding="utf-8")
    state = root / ".ontology-state"
    state.mkdir(exist_ok=True)
    (state / "progress.json").write_text("{}", encoding="utf-8")
    (root / "research" / "note.md").write_text("v1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "legacy: 元数据进了 git")
    assert {META_FILE, ".ontology-state/progress.json"} <= tracked(root)
    return meta, root


@pytest.mark.parametrize("use_template", [False, True])
def test_new_project_does_not_track_meta_or_state(manager, use_template):
    if use_template:
        real = Path(config.ASSETS_DIR) / "workspace-toolkit" / "templates" / "task-gitignore.template"
        target = manager.assets_dir / "workspace-toolkit" / "templates" / "task-gitignore.template"
        target.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
    meta = manager.create_project("project_alpha", owner="operator_a")
    root = manager.project_root(meta["id"])
    (root / ".ontology-state").mkdir(exist_ok=True)
    (root / ".ontology-state" / "lock-x.json").write_text("{}", encoding="utf-8")
    (root / ".onto" / "run").mkdir(parents=True)
    (root / ".onto" / "run" / "engine.json").write_text("{}", encoding="utf-8")

    files = tracked(root)
    assert META_FILE not in files
    assert git(root, "status", "--porcelain").strip() == ""
    gitignore = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in (".onto-web.json", ".ontology-state/", ".onto/run/"):
        assert entry in gitignore
    # 新项目无需迁移
    assert manager.migrate_state_tracking() == []


def test_legacy_migration_commits_only_state_paths(manager):
    meta, root = make_legacy(manager, owner="operator_a")
    (root / "ontology" / "object" / "a.md").write_text("# a\n", encoding="utf-8")
    (root / "ontology" / "object" / "b.md").write_text("# b\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "feat: 合成对象")
    # 工作区里有已暂存、未暂存与未跟踪的其他改动
    (root / "ontology" / "object" / "a.md").write_text("# a staged\n", encoding="utf-8")
    git(root, "add", "ontology/object/a.md")
    (root / "ontology" / "object" / "b.md").write_text("# b unstaged\n", encoding="utf-8")
    (root / "research" / "draft.md").write_text("draft\n", encoding="utf-8")
    manager._update_meta(meta["id"], name="project_alpha_renamed")
    before_head = git(root, "rev-parse", "HEAD").strip()

    assert manager.migrate_state_tracking() == [meta["id"]]

    assert git(root, "log", "-1", "--format=%s").strip() == UNTRACK_COMMIT_MESSAGE
    assert git(root, "rev-parse", "HEAD~1").strip() == before_head
    changed = git(root, "show", "--name-status", "--format=", "HEAD").split("\n")
    changed = sorted(line for line in changed if line)
    assert changed == sorted([
        "D\t.onto-web.json",
        "D\t.ontology-state/progress.json",
        "M\t.gitignore",
    ])
    assert META_FILE not in tracked(root)
    status = git(root, "status", "--porcelain").splitlines()
    assert "M  ontology/object/a.md" in status
    assert " M ontology/object/b.md" in status
    assert "?? research/draft.md" in status
    assert all(".onto-web.json" not in line and ".gitignore" not in line for line in status)
    # 元数据文件仍在磁盘上，内容不变
    on_disk = json.loads((root / META_FILE).read_text(encoding="utf-8"))
    assert on_disk["owner"] == "operator_a"
    assert on_disk["name"] == "project_alpha_renamed"
    assert (root / ".ontology-state" / "progress.json").is_file()

    # 幂等：第二次不再提交
    head = git(root, "rev-parse", "HEAD").strip()
    assert manager.migrate_state_tracking() == []
    assert git(root, "rev-parse", "HEAD").strip() == head


def test_migration_keeps_separately_staged_gitignore(manager):
    meta, root = make_legacy(manager)
    (root / ".gitignore").write_text(LEGACY_GITIGNORE + "scratch/\n", encoding="utf-8")
    git(root, "add", ".gitignore")

    assert manager.untrack_project_state(meta["id"])

    committed = git(root, "show", "HEAD:.gitignore")
    assert "scratch/" not in committed and ".onto-web.json" in committed
    worktree = (root / ".gitignore").read_text(encoding="utf-8")
    assert "scratch/" in worktree and ".onto-web.json" in worktree
    assert "scratch/" in git(root, "show", ":.gitignore")


def test_migration_unstages_meta_added_after_migration(manager):
    meta, root = make_legacy(manager)
    assert manager.untrack_project_state(meta["id"])
    git(root, "add", "-f", META_FILE)

    assert manager.untrack_project_state(meta["id"]) is False
    assert META_FILE not in tracked(root)


def test_revert_keeps_owner_and_name(manager):
    meta, root = make_legacy(manager, owner="")
    old = git(root, "rev-parse", "HEAD").strip()
    manager._update_meta(meta["id"], owner="operator_a", name="project_alpha_v2")
    (root / "research" / "note.md").write_text("v2\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "legacy: 改了所有者")

    manager.revert_to(meta["id"], old)

    restored = json.loads((root / META_FILE).read_text(encoding="utf-8"))
    assert restored["owner"] == "operator_a"
    assert restored["name"] == "project_alpha_v2"
    assert (root / "research" / "note.md").read_text(encoding="utf-8") == "v1\n"
    # 回到所有者为空的旧版本也不会让项目变成无主
    assert ownership.owns(restored, "operator_a") and not ownership.owns(restored, "user_c")


def test_revert_to_pre_migration_version_keeps_state_untracked(manager):
    meta, root = make_legacy(manager, owner="operator_a")
    old = git(root, "rev-parse", "HEAD").strip()
    assert manager.untrack_project_state(meta["id"])
    manager._update_meta(meta["id"], owner="operator_b")
    (root / "research" / "note.md").write_text("v2\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "feat: v2")

    manager.revert_to(meta["id"], old)

    assert json.loads((root / META_FILE).read_text(encoding="utf-8"))["owner"] == "operator_b"
    assert META_FILE not in tracked(root)
    assert not any(path.startswith(".ontology-state/") for path in tracked(root))
    assert (root / "research" / "note.md").read_text(encoding="utf-8") == "v1\n"


def test_clone_of_legacy_project_does_not_track_meta(manager):
    meta, _ = make_legacy(manager, owner="operator_a")
    (manager.project_root(meta["id"]) / ".onto" / "run").mkdir(parents=True)
    (manager.project_root(meta["id"]) / ".onto" / "run" / "engine.json").write_text("{}")

    clone = manager.clone_project(meta["id"], owner="operator_b")
    clone_root = manager.project_root(clone["id"])

    assert META_FILE not in tracked(clone_root)
    assert not (clone_root / ".onto" / "run" / "engine.json").exists()
    assert git(clone_root, "status", "--porcelain").strip() == ""


def test_import_transfer_of_legacy_export_untracks_meta(manager, tmp_path):
    meta, root = make_legacy(manager, owner="operator_a")
    (root / ".onto" / "run").mkdir(parents=True)
    (root / ".onto" / "run" / "engine.json").write_text("{}")
    archive = tmp_path / "transfer.zip"
    manager.export_zip(meta["id"], "workspace", archive)
    with zipfile.ZipFile(archive) as zf:
        assert ".onto/run/engine.json" not in zf.namelist()

    restored = manager.import_transfer_zip(archive, owner="operator_b")
    new_root = manager.project_root(restored["id"])

    assert META_FILE not in tracked(new_root)
    assert "legacy: 元数据进了 git" in git(new_root, "log", "--format=%s")
    assert json.loads((new_root / META_FILE).read_text(encoding="utf-8"))["owner"] == "operator_b"
    assert git(new_root, "status", "--porcelain").strip() == ""


def test_import_zip_with_legacy_gitignore_does_not_track_meta(manager, tmp_path):
    archive = tmp_path / "content.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(".gitignore", LEGACY_GITIGNORE)
        zf.writestr("research/note.md", "synthetic\n")
    meta = manager.import_zip("project_beta", archive, owner="operator_a")
    root = manager.project_root(meta["id"])
    assert META_FILE not in tracked(root)
    assert "research/note.md" in tracked(root)


# ---- git 锁残留（E1）----


def test_clear_git_locks_removes_known_lock_files(manager):
    meta = manager.create_project("project_alpha")
    root = manager.project_root(meta["id"])
    locks = [
        root / ".git" / "index.lock",
        root / ".git" / "HEAD.lock",
        root / ".git" / "packed-refs.lock",
        root / ".git" / "refs" / "heads" / "main.lock",
    ]
    for lock in locks:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("")
    keep = root / ".git" / "config"

    removed = manager.clear_git_locks()

    assert sorted(removed) == sorted(locks)
    assert not any(lock.exists() for lock in locks)
    assert keep.is_file()
    assert manager.clear_git_locks() == []


def _write_stale(path: Path, age: float) -> None:
    path.write_text("")
    past = time.time() - age
    os.utime(path, (past, past))


def test_git_retries_once_after_removing_stale_lock(manager):
    meta = manager.create_project("project_alpha")
    root = manager.project_root(meta["id"])
    lock = root / ".git" / "index.lock"
    _write_stale(lock, workspace_module.STALE_GIT_LOCK_SECONDS + 30)

    result = manager.write_file(meta["id"], "research/note.md", "synthetic\n")

    assert result["commit"]
    assert not lock.exists()


def test_git_keeps_fresh_lock_and_reports_error(manager):
    meta = manager.create_project("project_alpha")
    root = manager.project_root(meta["id"])
    lock = root / ".git" / "index.lock"
    _write_stale(lock, 5)

    with pytest.raises(WorkspaceError, match="git"):
        manager.write_file(meta["id"], "research/note.md", "synthetic\n")
    assert lock.exists()


def test_stale_lock_outside_git_dir_is_never_removed(tmp_path):
    outside = tmp_path / "elsewhere.lock"
    _write_stale(outside, 3600)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    stderr = f"fatal: Unable to create '{outside}': File exists."
    assert workspace_module._remove_stale_lock(tmp_path / "repo", stderr) is False
    assert outside.exists()


def test_startup_clears_locks_then_migrates_before_serving(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.api import deps

    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    assets = tmp_path / "assets"
    (assets / "resource-template").mkdir(parents=True)
    manager = WorkspaceManager(data, assets)
    meta, root = make_legacy(manager, owner="operator_a")
    lock = root / ".git" / "index.lock"
    lock.write_text("")  # 上次强杀留下的锁：不先清掉，迁移的 git 操作会失败
    monkeypatch.setattr(deps, "workspaces", manager)

    with TestClient(app):
        assert not lock.exists()
        assert META_FILE not in tracked(root)
        assert git(root, "log", "-1", "--format=%s").strip() == UNTRACK_COMMIT_MESSAGE
