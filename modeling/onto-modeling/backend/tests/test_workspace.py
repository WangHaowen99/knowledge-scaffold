"""工作区管理的单元测试：目录初始化、git 基线、文件树、路径守卫。"""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.services.workspace import WorkspaceError, WorkspaceManager


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
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


def test_create_project_uses_configured_default_engine(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch
):
    """配置中心设的默认引擎要落到新项目 meta，会话才会按它启动。"""
    from app import config
    from app.services import engines, model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "settings")
    monkeypatch.setattr(engines, "available_engines", lambda: {"claude": True, "jiuwen": True})

    assert manager.create_project("默认引擎前")["engine"] == "claude"
    model_settings.save_default_engine("jiuwen")
    meta = manager.create_project("默认引擎后")
    assert meta["engine"] == "jiuwen"
    assert manager.get_project(meta["id"])["engine"] == "jiuwen"


def test_list_and_get_project(manager: WorkspaceManager):
    created = manager.create_project("测试")
    projects = manager.list_projects()
    assert [p["id"] for p in projects] == [created["id"]]
    assert manager.get_project(created["id"])["name"] == "测试"


def _write(root: Path, rel: str, text: str = "x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_project_progress_counts_stages_after_missing_required(manager: WorkspaceManager):
    """research/ 只有 index.md 时，后续已有产物的阶段仍逐个计入（与前端 stageStatuses 一致）。"""
    meta = manager.create_project("进度")
    root = manager.project_root(meta["id"])
    _write(root, "ontology/object/订单.md", "# 订单")
    _write(root, "ontology/operation/创建订单.md", "# 创建订单")
    _write(root, "reports/quality.md", "# 质量检查")
    _write(root, "ontology/testcase/下单.md", "# 下单")

    overview = next(p for p in manager.list_projects() if p["id"] == meta["id"])
    assert overview["stages_done"] == 4
    assert overview["stages_total"] == 11


def test_project_progress_counts_solver_stage(manager: WorkspaceManager):
    meta = manager.create_project("求解")
    root = manager.project_root(meta["id"])
    before = manager._project_progress(root)["stages_done"]
    _write(root, "ontology/solver/route_choice/contract.json", "{}")

    assert manager._project_progress(root)["stages_done"] == before + 1


def test_project_progress_full_chain_reaches_total(manager: WorkspaceManager):
    meta = manager.create_project("全链")
    root = manager.project_root(meta["id"])
    for rel in (
        "research/资料.md",
        "ontology/diagrams/流程.md",
        "ontology/object/订单.md",
        "ontology/logic/创建订单.md",  # 旧目录别名同样命中操作阶段
        "ontology/metric/客单价.md",
        "reports/quality.md",
        "ontology/data-mock/订单.json",
        "ontology/testcase/下单.md",
        "ontology/scripts/calc.py",
        "ontology/solver/route_choice/contract.json",
        "reports/eval-001.md",
    ):
        _write(root, rel)

    progress = manager._project_progress(root)
    assert progress["stages_done"] == progress["stages_total"] == 11


def test_project_progress_ignores_index_only_directories(manager: WorkspaceManager):
    meta = manager.create_project("空项目")
    root = manager.project_root(meta["id"])
    _write(root, "ontology/solver/index.md", "# 索引")

    progress = manager._project_progress(root)
    assert progress["stages_done"] == 0
    assert progress["stages_total"] == 11

def test_file_tree_skips_git_and_symlinks(manager: WorkspaceManager):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    (root / "ontology" / "object" / "订单.md").write_text("# 订单", encoding="utf-8")

    tree = manager.file_tree(meta["id"])
    names = {node["name"] for node in tree}
    assert "ontology" in names
    assert ".git" not in names
    assert ".claude" not in names
    ontology = next(n for n in tree if n["name"] == "ontology")
    object_dir = next(n for n in ontology["children"] if n["name"] == "object")
    assert any(c["path"] == "ontology/object/订单.md" for c in object_dir["children"])


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


def test_concurrent_resource_uploads_keep_metadata_parseable(manager: WorkspaceManager):
    meta = manager.create_project("并发资料")

    def upload(index: int):
        return manager.save_resource(
            meta["id"], "others", f"item-{index}.md", f"内容 {index}".encode()
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(upload, range(20)))

    root = manager.project_root(meta["id"])
    parsed = json.loads((root / ".onto-web.json").read_text(encoding="utf-8"))
    assert parsed["resource_updated_at"]
    assert len(results) == 20
    assert {
        result["path"]: (root / result["path"]).read_bytes()
        for result in results
    } == {
        f"resource/others/item-{i}.md": f"内容 {i}".encode()
        for i in range(20)
    }


def test_concurrent_directives_do_not_overwrite_each_other(manager: WorkspaceManager):
    meta = manager.create_project("并发用途")
    for index in range(20):
        manager.save_resource(meta["id"], "others", f"item-{index}.md", b"x")

    def set_directive(index: int):
        return manager.set_resource_directive(
            meta["id"], f"resource/others/item-{index}.md", "term"
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(set_directive, range(20)))

    directives = manager.resource_directives(meta["id"])
    assert len(directives) == 20
    assert all(item["purpose"] == "term" for item in directives.values())


def test_resource_rolls_back_when_metadata_update_fails(manager: WorkspaceManager, monkeypatch):
    meta = manager.create_project("事务回滚")
    root = manager.project_root(meta["id"])

    def fail(*args, **kwargs):
        raise WorkspaceError("元数据不可用", 503, "project_metadata_unavailable")

    monkeypatch.setattr(manager, "_update_meta_unlocked", fail)
    with pytest.raises(WorkspaceError) as exc:
        manager.save_resource(meta["id"], "others", "rollback.md", b"should disappear")
    assert exc.value.code == "project_metadata_unavailable"
    assert not (root / "resource" / "others" / "rollback.md").exists()


def test_replacing_same_resource_is_atomic_and_reports_replaced(manager: WorkspaceManager):
    meta = manager.create_project("重复文件")
    first = manager.save_resource(meta["id"], "others", "same.md", b"one")
    second = manager.save_resource(meta["id"], "others", "same.md", b"two")
    root = manager.project_root(meta["id"])
    assert first["replaced"] is False
    assert second["replaced"] is True
    assert (root / second["path"]).read_bytes() == b"two"
    json.loads((root / ".onto-web.json").read_text(encoding="utf-8"))


def test_corrupt_project_metadata_has_distinguishable_error(manager: WorkspaceManager):
    meta = manager.create_project("元数据错误")
    root = manager.project_root(meta["id"])
    (root / ".onto-web.json").write_text("", encoding="utf-8")
    with pytest.raises(WorkspaceError) as exc:
        manager.get_project(meta["id"])
    assert exc.value.status == 503
    assert exc.value.code == "project_metadata_unavailable"


def test_rename_and_delete_project(manager: WorkspaceManager):
    meta = manager.create_project("原名")
    renamed = manager.rename_project(meta["id"], "新名")
    assert renamed["name"] == "新名"
    assert manager.get_project(meta["id"])["name"] == "新名"

    manager.delete_project(meta["id"])
    with pytest.raises(WorkspaceError):
        manager.project_root(meta["id"])
    trash = manager.workspaces_dir.parent / "trash"
    assert any(d.name.startswith(meta["id"]) for d in trash.iterdir())


def test_export_and_import_roundtrip(manager: WorkspaceManager, tmp_path: Path):
    meta = manager.create_project("导出")
    root = manager.project_root(meta["id"])
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    (root / "ontology" / "object" / "订单.md").write_text("# 订单", encoding="utf-8")
    (root / "research").mkdir(exist_ok=True)
    (root / "research" / "调研.md").write_text("调研", encoding="utf-8")

    dest = tmp_path / "out.zip"
    manager.export_zip(meta["id"], "ontology", dest)

    imported = manager.import_zip("导入副本", dest)
    imported_root = manager.project_root(imported["id"])
    assert (imported_root / "ontology" / "object" / "订单.md").read_text(
        encoding="utf-8"
    ) == "# 订单"
    assert (imported_root / ".claude").is_symlink()  # 资产重新挂载而非来自压缩包


def test_import_rejects_traversal_entries(manager: WorkspaceManager, tmp_path: Path):
    import zipfile

    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../outside.md", "x")
        zf.writestr("ok.md", "内容")
    imported = manager.import_zip("t", evil)
    root = manager.project_root(imported["id"])
    assert (root / "ok.md").is_file()
    assert not (root.parent / "outside.md").exists()


def test_write_file_guards(manager: WorkspaceManager):
    meta = manager.create_project("t")
    manager.write_file(meta["id"], "ontology/object/新对象.md", "# 新对象")
    root = manager.project_root(meta["id"])
    assert (root / "ontology" / "object" / "新对象.md").is_file()

    with pytest.raises(WorkspaceError):
        manager.write_file(meta["id"], ".claude/settings.json", "{}")
    with pytest.raises(WorkspaceError):
        manager.write_file(meta["id"], ".git/config", "x")


def test_file_at_revision_and_revert(manager: WorkspaceManager):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    (root / "a.md").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "c1"], cwd=root, check=True, capture_output=True
    )
    first = manager.versions(meta["id"])[0]["commit"]
    (root / "a.md").write_text("v2", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "c2"], cwd=root, check=True, capture_output=True
    )

    old = manager.file_at_revision(meta["id"], first, "a.md")
    assert old["content"] == "v1"

    manager.revert_to(meta["id"], first)
    assert (root / "a.md").read_text(encoding="utf-8") == "v1"
    assert manager.versions(meta["id"])[0]["message"].startswith("revert")


def test_delete_resource_scope(manager: WorkspaceManager):
    meta = manager.create_project("t")
    manager.save_resource(meta["id"], "others", "x.md", b"1")
    manager.delete_resource(meta["id"], "resource/others/x.md")
    with pytest.raises(WorkspaceError):
        manager.delete_resource(meta["id"], "ontology/a.md")


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
