"""工作区管理的单元测试：目录初始化、git 基线、文件树、路径守卫。"""

import subprocess
from pathlib import Path

import pytest

from app.services.workspace import WorkspaceError, WorkspaceManager


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "claude-workspace-dotclaude" / "templates").mkdir(parents=True)
    (assets / "claude-workspace-CLAUDE.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    (assets / "resource-template" / "others" / "README.md").write_text(
        "资料区", encoding="utf-8"
    )
    return WorkspaceManager(tmp_path / "data", assets)


def test_create_project_initializes_workspace(manager: WorkspaceManager):
    meta = manager.create_project("电商订单建模")
    root = manager.project_root(meta["id"])

    assert (root / ".git").is_dir()
    assert (root / ".claude").is_symlink()
    assert (root / "CLAUDE.md").is_symlink()
    assert (root / "resource" / "others" / "README.md").is_file()
    assert (root / "research").is_dir()

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True
    )
    assert len(log.stdout.strip().splitlines()) == 1


def test_list_and_get_project(manager: WorkspaceManager):
    created = manager.create_project("测试")
    projects = manager.list_projects()
    assert [p["id"] for p in projects] == [created["id"]]
    assert manager.get_project(created["id"])["name"] == "测试"


def test_file_tree_skips_git_and_symlinks(manager: WorkspaceManager):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    (root / "ontology" / "object").mkdir(parents=True)
    (root / "ontology" / "object" / "订单.md").write_text("# 订单", encoding="utf-8")

    tree = manager.file_tree(meta["id"])
    names = {node["name"] for node in tree}
    assert "ontology" in names
    assert ".git" not in names
    assert ".claude" not in names
    ontology = next(n for n in tree if n["name"] == "ontology")
    assert ontology["children"][0]["children"][0]["path"] == "ontology/object/订单.md"


def test_read_file_and_path_guard(manager: WorkspaceManager):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    (root / "note.md").write_text("hello", encoding="utf-8")

    result = manager.read_file(meta["id"], "note.md", 1024)
    assert result["content"] == "hello"

    with pytest.raises(WorkspaceError) as exc:
        manager.read_file(meta["id"], "../../etc/passwd", 1024)
    assert exc.value.status == 403


def test_save_resource_validates_subdir(manager: WorkspaceManager):
    meta = manager.create_project("t")
    saved = manager.save_resource(meta["id"], "others", "需求.md", "内容".encode())
    assert saved["path"] == "resource/others/需求.md"

    with pytest.raises(WorkspaceError):
        manager.save_resource(meta["id"], "evil", "x.md", b"")
    with pytest.raises(WorkspaceError):
        manager.save_resource(meta["id"], "others", "../x.md", b"")


def test_versions_numbering(manager: WorkspaceManager):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    (root / "a.md").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "research: demo"],
        cwd=root,
        check=True,
        capture_output=True,
    )

    versions = manager.versions(meta["id"])
    assert len(versions) == 2
    assert versions[0]["version"] == 2  # 最新在前
    assert versions[-1]["version"] == 1
