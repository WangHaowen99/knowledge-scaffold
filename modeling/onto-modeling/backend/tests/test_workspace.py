"""工作区管理的单元测试：目录初始化、git 基线、文件树、路径守卫。"""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.services import workspace as workspace_service
from app.services.workspace import (
    MAX_RESOURCE_FILE_SIZE,
    WorkspaceError,
    WorkspaceManager,
    _git,
)


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


def test_save_resource_enforces_type_and_size_before_writing(manager: WorkspaceManager):
    meta = manager.create_project("资料契约")
    root = manager.project_root(meta["id"])
    saved = manager.save_resource(
        meta["id"], "others", "边界.PDF", b"x" * MAX_RESOURCE_FILE_SIZE
    )
    assert (root / saved["path"]).stat().st_size == MAX_RESOURCE_FILE_SIZE

    with pytest.raises(WorkspaceError) as oversized:
        manager.save_resource(
            meta["id"],
            "others",
            "超限.pdf",
            b"x" * (MAX_RESOURCE_FILE_SIZE + 1),
        )
    assert oversized.value.status == 413
    assert oversized.value.code == "resource_file_too_large"
    assert not (root / "resource" / "others" / "超限.pdf").exists()

    with pytest.raises(WorkspaceError) as unsupported:
        manager.save_resource(meta["id"], "others", "程序.exe", b"x")
    assert unsupported.value.code == "unsupported_resource_file_type"
    assert not (root / "resource" / "others" / "程序.exe").exists()


def test_rejected_resource_replacement_keeps_original(manager: WorkspaceManager):
    meta = manager.create_project("替换保护")
    root = manager.project_root(meta["id"])
    manager.save_resource(meta["id"], "others", "原件.md", b"original")

    with pytest.raises(WorkspaceError):
        manager.save_resource(
            meta["id"],
            "others",
            "原件.md",
            b"x" * (MAX_RESOURCE_FILE_SIZE + 1),
        )
    assert (root / "resource" / "others" / "原件.md").read_bytes() == b"original"


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


def test_resource_upload_creates_complete_isolated_version(manager: WorkspaceManager):
    meta = manager.create_project("上传资料版本")
    root = manager.project_root(meta["id"])
    other = root / "resource" / "others" / "other.md"
    other.write_text("other", encoding="utf-8")
    _git(root, "add", "--", "resource/others/other.md")
    _git(root, "commit", "-m", "test: add other resource")
    materials = root / "resource" / "materials.json"
    materials.write_text(
        json.dumps(
            {
                "resource/others/other.md": {
                    "purpose": "term",
                    "reading_mode": "on_demand",
                }
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    unfinished = root / "ontology" / "object" / "unfinished.md"
    unfinished.write_text("unfinished", encoding="utf-8")
    _git(root, "add", "--", "ontology/object/unfinished.md")

    saved = manager.save_resource(
        meta["id"], "others", "source.md", b"source", purpose="goal", author="operator_a"
    )

    committed = json.loads(_git(root, "show", "HEAD:resource/materials.json"))
    working = json.loads(materials.read_text(encoding="utf-8"))
    assert saved["commit"]
    assert _git(root, "show", "--pretty=format:", "--name-only", "HEAD").splitlines() == [
        "resource/materials.json",
        "resource/others/source.md",
    ]
    assert _git(root, "show", "HEAD:resource/others/source.md") == "source"
    assert committed == {
        "resource/others/source.md": {
            "purpose": "goal",
            "reading_mode": "on_demand",
        }
    }
    assert working["resource/others/other.md"]["purpose"] == "term"
    assert working["resource/others/source.md"]["purpose"] == "goal"
    assert _git(root, "status", "--short", "--", "ontology/object/unfinished.md").startswith("A ")
    assert _git(root, "status", "--short", "--", "resource/materials.json").startswith(" M")


def test_identical_resource_upload_does_not_create_version(
    manager: WorkspaceManager, monkeypatch
):
    meta = manager.create_project("重复上传资料")
    first = manager.save_resource(
        meta["id"], "others", "source.md", b"source", purpose="goal"
    )
    version = manager.versions(meta["id"], limit=1)[0]["version"]
    root = manager.project_root(meta["id"])
    meta_after_first = (root / ".onto-web.json").read_bytes()
    monkeypatch.setattr(workspace_service.time, "time", lambda: 4102444800)

    second = manager.save_resource(
        meta["id"], "others", "source.md", b"source", purpose="goal"
    )

    assert first["commit"]
    assert second["commit"] == ""
    assert second["replaced"] is True
    assert manager.versions(meta["id"], limit=1)[0]["version"] == version
    assert (root / ".onto-web.json").read_bytes() == meta_after_first


@pytest.mark.parametrize("filename", ["a[1].md", "a?.md", "a*.md"])
def test_resource_upload_treats_git_metacharacters_as_literal_paths(
    manager: WorkspaceManager, filename: str
):
    meta = manager.create_project("特殊文件名上传")
    root = manager.project_root(meta["id"])
    manager.save_resource(meta["id"], "others", "a1.md", b"old", purpose="goal")
    decoy = root / "resource" / "others" / "a1.md"
    decoy.write_bytes(b"unfinished")

    saved = manager.save_resource(
        meta["id"], "others", filename, b"new", purpose="goal"
    )

    assert saved["commit"]
    assert _git(root, "show", "HEAD:resource/others/a1.md") == "old"
    assert decoy.read_bytes() == b"unfinished"
    assert _git(
        root,
        "ls-tree",
        "--name-only",
        "HEAD",
        "--",
        f":(literal)resource/others/{filename}",
    ).strip() == f"resource/others/{filename}"
    committed = _git(root, "show", "--pretty=format:", "--name-only", "HEAD")
    assert f"resource/others/{filename}" in committed.splitlines()
    assert "resource/others/a1.md" not in committed.splitlines()


@pytest.mark.parametrize("conflict_path", ["resource/others/source.md", "resource/materials.json"])
def test_resource_upload_rejects_staged_managed_files(
    manager: WorkspaceManager, conflict_path: str
):
    meta = manager.create_project("暂存资料上传")
    root = manager.project_root(meta["id"])
    manager.save_resource(meta["id"], "others", "source.md", b"old", purpose="goal")
    target = root / conflict_path
    if conflict_path.endswith("materials.json"):
        directives = json.loads(target.read_text(encoding="utf-8"))
        directives["resource/others/source.md"]["purpose"] = "term"
        target.write_text(json.dumps(directives, ensure_ascii=False), encoding="utf-8")
    else:
        target.write_bytes(b"staged")
    _git(root, "add", "--", conflict_path)
    before = target.read_bytes()
    head = _git(root, "rev-parse", "HEAD").strip()

    with pytest.raises(WorkspaceError) as exc:
        manager.save_resource(meta["id"], "others", "source.md", b"new", purpose="evidence")

    assert exc.value.code == "resource_upload_conflict"
    assert target.read_bytes() == before
    assert _git(root, "rev-parse", "HEAD").strip() == head
    assert conflict_path in _git(root, "diff", "--cached", "--name-only", "--").splitlines()


def test_resource_upload_commit_failure_restores_transaction(
    manager: WorkspaceManager, monkeypatch
):
    meta = manager.create_project("上传资料提交失败")
    root = manager.project_root(meta["id"])
    manager.save_resource(meta["id"], "others", "source.md", b"old", purpose="goal")
    resource = root / "resource" / "others" / "source.md"
    materials = root / "resource" / "materials.json"
    snapshots = {
        "resource": resource.read_bytes(),
        "materials": materials.read_bytes(),
        "meta": (root / ".onto-web.json").read_bytes(),
        "head": _git(root, "rev-parse", "HEAD").strip(),
    }
    original_git = workspace_service._git

    def fail_commit(cwd, *args):
        if args and args[0] == "commit":
            raise WorkspaceError("synthetic upload commit failure", 500)
        return original_git(cwd, *args)

    monkeypatch.setattr(workspace_service, "_git", fail_commit)
    with pytest.raises(WorkspaceError, match="synthetic upload commit failure"):
        manager.save_resource(
            meta["id"], "others", "source.md", b"new", purpose="evidence"
        )

    assert resource.read_bytes() == snapshots["resource"]
    assert materials.read_bytes() == snapshots["materials"]
    assert (root / ".onto-web.json").read_bytes() == snapshots["meta"]
    assert original_git(root, "rev-parse", "HEAD").strip() == snapshots["head"]
    assert original_git(root, "status", "--short", "--", "resource").strip() == ""


def test_new_resource_commit_failure_removes_created_files(
    manager: WorkspaceManager, monkeypatch
):
    meta = manager.create_project("新资料提交失败")
    root = manager.project_root(meta["id"])
    meta_before = (root / ".onto-web.json").read_bytes()
    head = _git(root, "rev-parse", "HEAD").strip()
    original_git = workspace_service._git

    def fail_commit(cwd, *args):
        if args and args[0] == "commit":
            raise WorkspaceError("synthetic new upload failure", 500)
        return original_git(cwd, *args)

    monkeypatch.setattr(workspace_service, "_git", fail_commit)
    with pytest.raises(WorkspaceError, match="synthetic new upload failure"):
        manager.save_resource(
            meta["id"], "others", "source.md", b"new", purpose="goal"
        )

    assert not (root / "resource" / "others" / "source.md").exists()
    assert not (root / "resource" / "materials.json").exists()
    assert (root / ".onto-web.json").read_bytes() == meta_before
    assert original_git(root, "rev-parse", "HEAD").strip() == head
    assert original_git(root, "status", "--short", "--", "resource").strip() == ""


def test_resource_upload_failure_after_commit_restores_head_and_worktree(
    manager: WorkspaceManager, monkeypatch
):
    meta = manager.create_project("上传资料提交后失败")
    root = manager.project_root(meta["id"])
    other = root / "resource" / "others" / "other.md"
    other.write_text("other", encoding="utf-8")
    _git(root, "add", "--", "resource/others/other.md")
    _git(root, "commit", "-m", "test: add other resource")
    materials = root / "resource" / "materials.json"
    materials.write_text(
        json.dumps(
            {
                "resource/others/other.md": {
                    "purpose": "term",
                    "reading_mode": "on_demand",
                }
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    snapshots = {
        "materials": materials.read_bytes(),
        "meta": (root / ".onto-web.json").read_bytes(),
        "head": _git(root, "rev-parse", "HEAD").strip(),
    }
    original_write = manager._write_resource_directives
    writes = 0

    def fail_second_write(workspace_root, directives):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise WorkspaceError("synthetic post-commit failure", 500)
        original_write(workspace_root, directives)

    monkeypatch.setattr(manager, "_write_resource_directives", fail_second_write)
    with pytest.raises(WorkspaceError, match="synthetic post-commit failure"):
        manager.save_resource(
            meta["id"], "others", "source.md", b"source", purpose="goal"
        )

    assert _git(root, "rev-parse", "HEAD").strip() == snapshots["head"]
    assert not (root / "resource" / "others" / "source.md").exists()
    assert materials.read_bytes() == snapshots["materials"]
    assert (root / ".onto-web.json").read_bytes() == snapshots["meta"]
    assert _git(root, "status", "--short", "--", "resource/materials.json").startswith("??")


def test_resource_upload_can_be_previewed_and_reverted(manager: WorkspaceManager):
    meta = manager.create_project("上传资料回退")
    root = manager.project_root(meta["id"])
    target = _git(root, "rev-parse", "HEAD").strip()
    saved = manager.save_resource(
        meta["id"], "others", "source.md", b"source", purpose="goal"
    )

    preview = manager.preview_revert(meta["id"], target)
    reverted = manager.revert_to(
        meta["id"], target, expected_head=preview["expected_head"]
    )

    assert saved["commit"]
    assert manager._managed_dirty_paths(root) == []
    assert preview["changes"]["deleted"] == [
        "resource/materials.json",
        "resource/others/source.md",
    ]
    assert reverted["reverted_to"] == target
    assert not (root / "resource" / "others" / "source.md").exists()
    assert manager.resource_directives(meta["id"]) == {}


def test_resource_upload_does_not_block_shared_revert_preview(
    manager: WorkspaceManager, monkeypatch
):
    from app.services import version_revert

    meta = manager.create_project("上传资料编排回退", owner="operator_a")
    root = manager.project_root(meta["id"])
    target_version = manager.versions(meta["id"], limit=1)[0]["version"]
    manager.save_resource(
        meta["id"], "others", "source.md", b"source", purpose="goal"
    )
    monkeypatch.setattr(
        version_revert,
        "_blocking_issues",
        lambda project_id, path: (
            [{"code": "WORKSPACE_DIRTY", "message": "dirty"}]
            if version_revert.manager_for_dirty(path)
            else []
        ),
    )

    preview = version_revert.preview_by_version(
        manager, meta["id"], target_version, "synthetic-token", "operator_a"
    )

    assert preview["ready"] is True
    assert preview["changes"]["deleted"] == [
        "resource/materials.json",
        "resource/others/source.md",
    ]


def test_resource_delete_creates_complete_isolated_version(manager: WorkspaceManager):
    meta = manager.create_project("删除资料版本")
    root = manager.project_root(meta["id"])
    manager.save_resource(meta["id"], "others", "source.md", b"source", purpose="goal")
    other = root / "resource" / "others" / "other.md"
    other.write_text("other", encoding="utf-8")
    _git(root, "add", "--", "resource/others/other.md")
    _git(root, "commit", "-m", "test: add other resource")
    materials = root / "resource" / "materials.json"
    directives = json.loads(materials.read_text(encoding="utf-8"))
    directives["resource/others/other.md"] = {
        "purpose": "term",
        "reading_mode": "on_demand",
    }
    materials.write_text(
        json.dumps(directives, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    unfinished = root / "ontology" / "object" / "unfinished.md"
    unfinished.write_text("unfinished", encoding="utf-8")
    _git(root, "add", "--", "ontology/object/unfinished.md")

    deleted = manager.delete_resource(meta["id"], "resource/others/source.md", author="operator_a")

    committed = json.loads(_git(root, "show", "HEAD:resource/materials.json"))
    working = json.loads(materials.read_text(encoding="utf-8"))
    assert deleted["commit"]
    assert _git(root, "show", "--pretty=format:", "--name-only", "HEAD").splitlines() == [
        "resource/materials.json",
        "resource/others/source.md",
    ]
    assert "resource/others/source.md" not in committed
    assert "resource/others/other.md" not in committed
    assert working["resource/others/other.md"]["purpose"] == "term"
    assert _git(root, "status", "--short", "--", "ontology/object/unfinished.md").startswith("A ")
    assert _git(root, "status", "--short", "--", "resource/materials.json").startswith(" M")


def test_resource_delete_can_be_previewed_and_reverted(manager: WorkspaceManager):
    meta = manager.create_project("删除资料回退")
    manager.save_resource(meta["id"], "others", "source.md", b"source", purpose="goal")
    target = _git(manager.project_root(meta["id"]), "rev-parse", "HEAD").strip()
    manager.delete_resource(meta["id"], "resource/others/source.md")
    preview = manager.preview_revert(meta["id"], target)
    manager.revert_to(meta["id"], target, expected_head=preview["expected_head"])

    root = manager.project_root(meta["id"])
    assert (root / "resource" / "others" / "source.md").exists()
    assert manager.resource_directives(meta["id"])["resource/others/source.md"]["purpose"] == "goal"


def test_resource_delete_treats_git_metacharacters_as_literal_paths(
    manager: WorkspaceManager,
):
    meta = manager.create_project("特殊文件名删除")
    root = manager.project_root(meta["id"])
    manager.save_resource(meta["id"], "others", "a1.md", b"old", purpose="goal")
    special = manager.save_resource(
        meta["id"], "others", "a[1].md", b"special", purpose="goal"
    )
    decoy = root / "resource" / "others" / "a1.md"
    decoy.write_bytes(b"unfinished")

    deleted = manager.delete_resource(meta["id"], special["path"])

    assert deleted["commit"]
    assert _git(root, "show", "HEAD:resource/others/a1.md") == "old"
    assert decoy.read_bytes() == b"unfinished"
    assert not _git(
        root,
        "ls-tree",
        "--name-only",
        "HEAD",
        "--",
        ":(literal)resource/others/a[1].md",
    ).strip()


def test_layout_refresh_commits_generated_index_despite_dirty_metadata(
    manager: WorkspaceManager,
):
    from app.services import version_revert

    meta = manager.create_project("索引规范化")
    root = manager.project_root(meta["id"])
    target = _git(root, "rev-parse", "HEAD").strip()
    manager.write_file(meta["id"], "ontology/object/item.md", "# item")
    meta_path = root / ".onto-web.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["resource_updated_at"] = 123
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")

    manager.ensure_package_layout(meta["id"])

    assert _git(root, "status", "--short", "--", ".onto-web.json").startswith(" M")
    assert _git(root, "status", "--short", "--", "ontology/object/index.md") == ""
    assert version_revert.manager_for_dirty(root) is False
    assert version_revert.preview_by_commit(manager, meta["id"], target)["ready"] is True


@pytest.mark.parametrize("operation", ["delete", "set"])
def test_resource_mutation_rejects_staged_managed_file(manager: WorkspaceManager, operation: str):
    meta = manager.create_project("资料变更冲突")
    root = manager.project_root(meta["id"])
    saved = manager.save_resource(meta["id"], "others", "source.md", b"source", purpose="goal")
    target = root / (saved["path"] if operation == "delete" else "resource/materials.json")
    target.write_bytes(target.read_bytes() + (b"\n" if operation == "delete" else b" "))
    _git(root, "add", "--", target.relative_to(root).as_posix())

    with pytest.raises(WorkspaceError) as exc:
        if operation == "delete":
            manager.delete_resource(meta["id"], saved["path"])
        else:
            manager.set_resource_directive(meta["id"], saved["path"], "term")

    assert exc.value.code in {"resource_delete_conflict", "resource_directive_conflict"}


@pytest.mark.parametrize(
    ("tracked", "expected_code"),
    [
        (False, "resource_source_unversioned"),
        (True, "resource_source_modified"),
    ],
)
def test_resource_directive_requires_clean_versioned_source(
    manager: WorkspaceManager, tracked: bool, expected_code: str
):
    meta = manager.create_project("资料设置源文件状态")
    root = manager.project_root(meta["id"])
    resource = root / "resource" / "others" / "source.md"
    resource.write_text("source", encoding="utf-8")
    if tracked:
        _git(root, "add", "--", "resource/others/source.md")
        _git(root, "commit", "-m", "test: add source")
        resource.write_text("modified", encoding="utf-8")

    with pytest.raises(WorkspaceError) as exc:
        manager.set_resource_directive(meta["id"], "resource/others/source.md", "goal")

    assert exc.value.code == expected_code
    assert not (root / "resource" / "materials.json").exists()


def test_resource_delete_commit_failure_restores_transaction(
    manager: WorkspaceManager, monkeypatch
):
    meta = manager.create_project("删除资料提交失败")
    root = manager.project_root(meta["id"])
    saved = manager.save_resource(meta["id"], "others", "source.md", b"source", purpose="goal")
    resource = root / saved["path"]
    materials = root / "resource" / "materials.json"
    before = {
        "resource": resource.read_bytes(),
        "materials": materials.read_bytes(),
        "meta": (root / ".onto-web.json").read_bytes(),
        "head": _git(root, "rev-parse", "HEAD").strip(),
    }
    original_git = workspace_service._git

    def fail_commit(cwd, *args):
        if args and args[0] == "commit":
            raise WorkspaceError("synthetic delete commit failure", 500)
        return original_git(cwd, *args)

    monkeypatch.setattr(workspace_service, "_git", fail_commit)
    with pytest.raises(WorkspaceError, match="synthetic delete commit failure"):
        manager.delete_resource(meta["id"], saved["path"])

    assert resource.read_bytes() == before["resource"]
    assert materials.read_bytes() == before["materials"]
    assert (root / ".onto-web.json").read_bytes() == before["meta"]
    assert _git(root, "rev-parse", "HEAD").strip() == before["head"]


def test_resource_delete_failure_after_commit_restores_head_and_worktree(
    manager: WorkspaceManager, monkeypatch
):
    meta = manager.create_project("删除资料提交后失败")
    root = manager.project_root(meta["id"])
    saved = manager.save_resource(
        meta["id"], "others", "source.md", b"source", purpose="goal"
    )
    other = root / "resource" / "others" / "other.md"
    other.write_text("other", encoding="utf-8")
    _git(root, "add", "--", "resource/others/other.md")
    _git(root, "commit", "-m", "test: add other resource")
    materials = root / "resource" / "materials.json"
    directives = json.loads(materials.read_text(encoding="utf-8"))
    directives["resource/others/other.md"] = {
        "purpose": "term",
        "reading_mode": "on_demand",
    }
    materials.write_text(
        json.dumps(directives, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    before = {
        "resource": (root / saved["path"]).read_bytes(),
        "materials": materials.read_bytes(),
        "meta": (root / ".onto-web.json").read_bytes(),
        "head": _git(root, "rev-parse", "HEAD").strip(),
    }
    original_write = manager._write_resource_directives
    writes = 0

    def fail_second_write(workspace_root, next_directives):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise WorkspaceError("synthetic delete post-commit failure", 500)
        original_write(workspace_root, next_directives)

    monkeypatch.setattr(manager, "_write_resource_directives", fail_second_write)
    with pytest.raises(WorkspaceError, match="synthetic delete post-commit failure"):
        manager.delete_resource(meta["id"], saved["path"])

    assert _git(root, "rev-parse", "HEAD").strip() == before["head"]
    assert (root / saved["path"]).read_bytes() == before["resource"]
    assert materials.read_bytes() == before["materials"]
    assert (root / ".onto-web.json").read_bytes() == before["meta"]


def test_resource_delete_does_not_block_shared_revert_preview(
    manager: WorkspaceManager, monkeypatch
):
    from app.services import version_revert

    meta = manager.create_project("删除资料编排回退", owner="operator_a")
    root = manager.project_root(meta["id"])
    manager.save_resource(meta["id"], "others", "source.md", b"source", purpose="goal")
    target_version = manager.versions(meta["id"], limit=1)[0]["version"]
    manager.delete_resource(meta["id"], "resource/others/source.md")
    monkeypatch.setattr(
        version_revert,
        "_blocking_issues",
        lambda project_id, path: (
            [{"code": "WORKSPACE_DIRTY", "message": "dirty"}]
            if version_revert.manager_for_dirty(path)
            else []
        ),
    )

    preview = version_revert.preview_by_version(
        manager, meta["id"], target_version, "synthetic-token", "operator_a"
    )

    assert preview["ready"] is True
    assert preview["changes"]["added"] == ["resource/others/source.md"]
    assert preview["changes"]["modified"] == ["resource/materials.json"]


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


def test_resource_directive_creates_only_its_own_version(manager: WorkspaceManager):
    meta = manager.create_project("资料设置版本")
    root = manager.project_root(meta["id"])
    resource = root / "resource" / "others" / "source.md"
    resource.write_text("source", encoding="utf-8")
    _git(root, "add", "--", "resource/others/source.md")
    _git(root, "commit", "-m", "test: add source")
    unrelated = root / "ontology" / "object" / "unfinished.md"
    unrelated.write_text("unfinished", encoding="utf-8")
    _git(root, "add", "--", "ontology/object/unfinished.md")
    version = manager.versions(meta["id"], limit=1)[0]["version"]

    saved = manager.set_resource_directive(
        meta["id"], "resource/others/source.md", "goal", author="operator_a"
    )

    assert saved["commit"]
    assert manager.versions(meta["id"], limit=1)[0]["version"] == version + 1
    assert _git(root, "show", "--pretty=format:", "--name-only", "HEAD").strip() == (
        "resource/materials.json"
    )
    assert _git(root, "status", "--short", "--", "resource/materials.json") == ""
    assert _git(root, "status", "--short", "--", "ontology/object/unfinished.md").startswith("A ")


def test_resource_directive_version_can_be_previewed_and_reverted(
    manager: WorkspaceManager,
):
    meta = manager.create_project("资料设置回退")
    root = manager.project_root(meta["id"])
    resource = root / "resource" / "others" / "source.md"
    resource.write_text("source", encoding="utf-8")
    _git(root, "add", "--", "resource/others/source.md")
    _git(root, "commit", "-m", "test: add source")
    target_commit = _git(root, "rev-parse", "HEAD").strip()
    manager.set_resource_directive(meta["id"], "resource/others/source.md", "goal")

    preview = manager.preview_revert(meta["id"], target_commit)
    reverted = manager.revert_to(
        meta["id"], target_commit, expected_head=preview["expected_head"]
    )

    assert "resource/materials.json" in preview["changes"]["deleted"]
    assert reverted["reverted_to"] == target_commit
    assert not (root / "resource" / "materials.json").exists()
    assert manager.resource_directives(meta["id"]) == {}


def test_resource_directive_does_not_block_shared_revert_preview(
    manager: WorkspaceManager, monkeypatch
):
    from app.services import version_revert

    meta = manager.create_project("资料设置编排回退", owner="operator_a")
    root = manager.project_root(meta["id"])
    resource = root / "resource" / "others" / "source.md"
    resource.write_text("source", encoding="utf-8")
    _git(root, "add", "--", "resource/others/source.md")
    _git(root, "commit", "-m", "test: add source")
    target_version = manager.versions(meta["id"], limit=1)[0]["version"]
    manager.set_resource_directive(meta["id"], "resource/others/source.md", "goal")
    monkeypatch.setattr(version_revert, "_blocking_issues", lambda project_id, path: (
        [{"code": "WORKSPACE_DIRTY", "message": "dirty"}]
        if version_revert.manager_for_dirty(path) else []
    ))

    preview = version_revert.preview_by_version(
        manager, meta["id"], target_version, "synthetic-token", "operator_a"
    )

    assert preview["ready"] is True
    assert preview["changes"]["deleted"] == ["resource/materials.json"]


def test_unchanged_resource_directive_does_not_create_version(manager: WorkspaceManager):
    meta = manager.create_project("重复资料设置")
    root = manager.project_root(meta["id"])
    resource = root / "resource" / "others" / "source.md"
    resource.write_text("source", encoding="utf-8")
    _git(root, "add", "--", "resource/others/source.md")
    _git(root, "commit", "-m", "test: add source")
    first = manager.set_resource_directive(meta["id"], "resource/others/source.md", "goal")
    version = manager.versions(meta["id"], limit=1)[0]["version"]

    second = manager.set_resource_directive(meta["id"], "resource/others/source.md", "goal")

    assert first["commit"]
    assert second["commit"] == ""
    assert manager.versions(meta["id"], limit=1)[0]["version"] == version


def test_resource_directive_does_not_commit_other_unfinished_directives(
    manager: WorkspaceManager,
):
    meta = manager.create_project("隔离资料设置")
    root = manager.project_root(meta["id"])
    for name in ("source-a.md", "source-b.md"):
        (root / "resource" / "others" / name).write_text("source", encoding="utf-8")
    _git(root, "add", "--", "resource/others/source-a.md", "resource/others/source-b.md")
    _git(root, "commit", "-m", "test: add sources")
    manager.set_resource_directive(meta["id"], "resource/others/source-a.md", "goal")
    materials_path = root / "resource" / "materials.json"
    directives = json.loads(materials_path.read_text(encoding="utf-8"))
    directives["resource/others/source-b.md"] = {
        "purpose": "term",
        "reading_mode": "on_demand",
    }
    materials_path.write_text(
        json.dumps(directives, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    saved = manager.set_resource_directive(
        meta["id"], "resource/others/source-a.md", "evidence"
    )

    committed = json.loads(_git(root, "show", "HEAD:resource/materials.json"))
    working = json.loads(materials_path.read_text(encoding="utf-8"))
    assert saved["commit"]
    assert committed["resource/others/source-a.md"]["purpose"] == "evidence"
    assert "resource/others/source-b.md" not in committed
    assert working["resource/others/source-b.md"]["purpose"] == "term"
    assert _git(root, "status", "--short", "--", "resource/materials.json").startswith(" M")


def test_resource_directive_rejects_staged_settings_file(manager: WorkspaceManager):
    meta = manager.create_project("暂存资料设置")
    root = manager.project_root(meta["id"])
    resource = root / "resource" / "others" / "source.md"
    resource.write_text("source", encoding="utf-8")
    _git(root, "add", "--", "resource/others/source.md")
    _git(root, "commit", "-m", "test: add source")
    materials_path = root / "resource" / "materials.json"
    materials_path.write_text(
        json.dumps(
            {
                "resource/others/source.md": {
                    "purpose": "term",
                    "reading_mode": "on_demand",
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _git(root, "add", "--", "resource/materials.json")
    before = materials_path.read_bytes()
    head = _git(root, "rev-parse", "HEAD").strip()

    with pytest.raises(WorkspaceError) as exc:
        manager.set_resource_directive(meta["id"], "resource/others/source.md", "goal")

    assert exc.value.code == "resource_directive_conflict"
    assert materials_path.read_bytes() == before
    assert _git(root, "rev-parse", "HEAD").strip() == head
    assert _git(root, "diff", "--cached", "--name-only", "--").strip() == (
        "resource/materials.json"
    )


def test_resource_directive_commit_failure_restores_files(manager: WorkspaceManager, monkeypatch):
    meta = manager.create_project("资料设置提交失败")
    root = manager.project_root(meta["id"])
    resource = root / "resource" / "others" / "source.md"
    resource.write_text("source", encoding="utf-8")
    _git(root, "add", "--", "resource/others/source.md")
    _git(root, "commit", "-m", "test: add source")
    meta_before = (root / ".onto-web.json").read_bytes()
    original_git = workspace_service._git

    def fail_commit(cwd, *args):
        if args and args[0] == "commit":
            raise WorkspaceError("synthetic commit failure", 500)
        return original_git(cwd, *args)

    monkeypatch.setattr(workspace_service, "_git", fail_commit)
    with pytest.raises(WorkspaceError, match="synthetic commit failure"):
        manager.set_resource_directive(meta["id"], "resource/others/source.md", "goal")

    assert not (root / "resource" / "materials.json").exists()
    assert (root / ".onto-web.json").read_bytes() == meta_before
    assert original_git(root, "status", "--short", "--", "resource/materials.json") == ""


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

    root = manager.project_root(meta["id"])
    manager.delete_project(meta["id"])
    with pytest.raises(WorkspaceError):
        manager.project_root(meta["id"])
    assert not root.exists()  # 永久删除：git 历史一并清除，不进回收站
    assert manager.list_trash() == []
    assert not any(manager.purge_dir().iterdir())


def test_export_and_import_roundtrip(manager: WorkspaceManager, tmp_path: Path):
    meta = manager.create_project("导出")
    root = manager.project_root(meta["id"])
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    (root / "ontology" / "object" / "订单.md").write_text("# 订单", encoding="utf-8")
    (root / "research").mkdir(exist_ok=True)
    (root / "research" / "资料分析.md").write_text("资料分析", encoding="utf-8")

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
    assert manager.versions(meta["id"])[0]["message"] == (
        f"revert: 回退到版本 2（{first[:8]}）"
    )


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


def test_revert_restores_complete_business_snapshot_and_keeps_metadata(
    manager: WorkspaceManager,
):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    _write(root, "ontology/object/kept.md", "old")
    _write(root, "ontology/object/restored.md", "restore me")
    target = _commit(root, "target")

    _write(root, "ontology/object/kept.md", "new")
    (root / "ontology/object/restored.md").unlink()
    _write(root, "ontology/object/later.md", "remove me")
    current_meta = {**manager.get_project(meta["id"]), "name": "current name", "members": {"editor_a": "editor"}}
    (root / ".onto-web.json").write_text(json.dumps(current_meta), encoding="utf-8")
    current_ignore = (root / ".gitignore").read_text(encoding="utf-8") + "custom-cache/\n"
    (root / ".gitignore").write_text(current_ignore, encoding="utf-8")
    _commit(root, "later")

    preview = manager.preview_revert(meta["id"], target)
    assert preview["changes"]["total"] == 3
    result = manager.revert_to(meta["id"], target, preview["expected_head"])

    assert result["changes"] == preview["changes"]
    assert (root / "ontology/object/kept.md").read_text(encoding="utf-8") == "old"
    assert (root / "ontology/object/restored.md").read_text(encoding="utf-8") == "restore me"
    assert not (root / "ontology/object/later.md").exists()
    assert manager.get_project(meta["id"])["name"] == "current name"
    assert manager.get_project(meta["id"])["members"] == {"editor_a": "editor"}
    assert (root / ".gitignore").read_text(encoding="utf-8") == current_ignore
    tracked = _git(root, "ls-tree", "HEAD", "--", "ontology/object/kept.md")
    assert tracked.startswith("100644 ")


def test_web_revert_preview_marks_success_as_ready(manager: WorkspaceManager):
    from app.services import version_revert

    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "v1")
    _write(root, "ontology/object/item.md", "v2")
    _commit(root, "v2")

    preview = version_revert.preview_by_commit(manager, meta["id"], target)

    assert preview["ready"] is True
    assert preview["reverted_to"] == target
    assert preview["changes"]["modified"] == ["ontology/object/item.md"]


def test_revert_ignores_generated_index_only_difference(manager: WorkspaceManager):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    index = root / "reports" / "index.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("# historical projection\n", encoding="utf-8")
    target = _commit(root, "historical index")

    manager._refresh_indexes(root)
    _commit(root, "refresh generated index")

    with pytest.raises(WorkspaceError, match="业务内容.*一致"):
        manager.preview_revert(meta["id"], target)


def test_revert_commits_regenerated_indexes_and_allows_another_revert(
    manager: WorkspaceManager,
):
    from app.services import version_revert

    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    _write(root, "ontology/object/old.md", "# old")
    manager._refresh_indexes(root)
    target = _commit(root, "old object")

    (root / "ontology/object/old.md").unlink()
    _write(root, "ontology/object/new.md", "# new")
    manager._refresh_indexes(root)
    current = _commit(root, "new object")

    manager.revert_to(meta["id"], target)

    assert _git(root, "status", "--porcelain").strip() == ""
    preview = version_revert.preview_by_commit(manager, meta["id"], current)
    assert preview["ready"] is True
    assert preview["changes"]["total"] == 2


def test_resource_stale_follows_resource_and_derived_commit_order(
    manager: WorkspaceManager,
):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    _write(root, "resource/others/source.md", "v1")
    _commit(root, "resource v1")
    _write(root, "research/report.md", "report v1")
    _commit(root, "research v1")
    assert manager._project_progress(root)["resource_stale"] is False

    _write(root, "resource/others/source.md", "v2")
    _commit(root, "resource v2")
    assert manager._project_progress(root)["resource_stale"] is True

    _write(root, "research/report.md", "report v2")
    _commit(root, "research v2")
    assert manager._project_progress(root)["resource_stale"] is False


def test_resource_stale_ignores_unrelated_ontology_and_index_commits(
    manager: WorkspaceManager,
):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    _write(root, "resource/others/source.md", "v1")
    _commit(root, "resource v1")
    _write(root, "research/report.md", "report v1")
    _commit(root, "research v1")

    _write(root, "resource/others/source.md", "v2")
    _commit(root, "resource v2")
    assert manager._project_progress(root)["resource_stale"] is True

    _write(root, "ontology/object/item.md", "object")
    _write(root, "research/index.md", "# index")
    _commit(root, "unrelated generated output")
    assert manager._project_progress(root)["resource_stale"] is True


def test_resource_stale_uses_reviewed_resource_fingerprints(
    manager: WorkspaceManager,
):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    _write(root, "resource/materials.json", '{}')
    _write(root, "research/report.md", "report")
    _commit(root, "analyzed resources")
    revision = workspace_service._research_review.file_revision(
        root / "resource/materials.json"
    )
    workspace_service._research_review.write_review(
        root,
        workspace_service._research_review.PASSED,
        {
            "resource/materials.json": revision,
            "research/report.md": workspace_service._research_review.file_revision(
                root / "research/report.md"
            ),
        },
        [],
        [],
    )
    assert manager._project_progress(root)["resource_stale"] is False

    _write(root, "resource/materials.json", '{"changed": true}')
    _commit(root, "resource changed")
    _write(root, "ontology/object/index.md", "# index")
    _commit(root, "unrelated index")
    assert manager._project_progress(root)["resource_stale"] is True

    _write(root, "research/report.md", "report refreshed")
    _commit(root, "research refreshed")
    workspace_service._research_review.write_review(
        root,
        workspace_service._research_review.PASSED,
        {
            "resource/materials.json": workspace_service._research_review.file_revision(
                root / "resource/materials.json"
            ),
            "research/report.md": workspace_service._research_review.file_revision(
                root / "research/report.md"
            ),
        },
        [],
        [],
    )
    assert manager._project_progress(root)["resource_stale"] is False


@pytest.mark.parametrize("dirty_kind", ["modified", "staged", "untracked"])
def test_revert_rejects_dirty_workspace(manager: WorkspaceManager, dirty_kind: str):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "v1")
    _write(root, "ontology/object/item.md", "v2")
    _commit(root, "v2")

    if dirty_kind == "untracked":
        _write(root, "ontology/object/untracked.md", "draft")
    else:
        _write(root, "ontology/object/item.md", "draft")
        if dirty_kind == "staged":
            _git(root, "add", "ontology/object/item.md")
    with pytest.raises(WorkspaceError, match="未提交或暂存"):
        manager.revert_to(meta["id"], target)


def test_revert_rejects_current_missing_and_non_ancestor(manager: WorkspaceManager):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    current = manager.versions(meta["id"])[0]["commit"]
    with pytest.raises(WorkspaceError, match="当前已是"):
        manager.preview_revert(meta["id"], current)
    with pytest.raises(WorkspaceError, match="不存在"):
        manager.preview_revert(meta["id"], "f" * 40)

    _git(root, "checkout", "--orphan", "side")
    _write(root, "side.md", "side")
    side = _commit(root, "side")
    _git(root, "checkout", "master")
    with pytest.raises(WorkspaceError, match="当前版本历史"):
        manager.preview_revert(meta["id"], side)


def test_revert_rejects_stale_preview_and_busy_project(manager: WorkspaceManager):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "v1")
    _write(root, "ontology/object/item.md", "v2")
    _commit(root, "v2")
    preview = manager.preview_revert(meta["id"], target)
    _write(root, "ontology/object/item.md", "v3")
    _commit(root, "v3")

    with pytest.raises(WorkspaceError, match="重新预览"):
        manager.revert_to(meta["id"], target, preview["expected_head"])
    with pytest.raises(WorkspaceError, match="busy"):
        manager.revert_to(
            meta["id"],
            target,
            busy_check=lambda _root: (_ for _ in ()).throw(WorkspaceError("busy", 409)),
        )


def test_revert_invalidates_derived_state(manager: WorkspaceManager):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "v1")
    _write(root, "ontology/object/item.md", "v2")
    _commit(root, "v2")
    for rel in (
        ".ontology-state/research-review.json",
        ".ontology-state/script-verifications.json",
        ".ontology-state/solver-validations.json",
        ".ontology-state/artifact-reviews/review.json",
    ):
        _write(root, rel, "{}")
    _write(root, ".ontology-state/eval-sets.json", "keep")

    manager.revert_to(meta["id"], target)
    assert not (root / ".ontology-state/research-review.json").exists()
    assert not (root / ".ontology-state/artifact-reviews").exists()
    assert (root / ".ontology-state/eval-sets.json").read_text(encoding="utf-8") == "keep"


def test_revert_failure_restores_business_files_without_overwriting_metadata(
    manager: WorkspaceManager, monkeypatch
):
    from app.services import workspace

    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "v1")
    _write(root, "ontology/object/item.md", "v2")
    _write(root, "ontology/object/later.md", "later")
    _commit(root, "v2")
    current_meta = (root / ".onto-web.json").read_text(encoding="utf-8") + "\n"
    (root / ".onto-web.json").write_text(current_meta, encoding="utf-8")

    original_git = workspace._git

    def fail_commit(cwd, *args):
        if args and args[0] == "commit":
            raise WorkspaceError("simulated commit failure", 500)
        return original_git(cwd, *args)

    monkeypatch.setattr(workspace, "_git", fail_commit)
    with pytest.raises(WorkspaceError, match="simulated"):
        manager.revert_to(meta["id"], target)

    assert (root / "ontology/object/item.md").read_text(encoding="utf-8") == "v2"
    assert (root / "ontology/object/later.md").read_text(encoding="utf-8") == "later"
    assert (root / ".onto-web.json").read_text(encoding="utf-8") == current_meta
    assert manager._managed_dirty_paths(root) == []


def test_revert_removes_later_symlink_without_following_it(
    manager: WorkspaceManager, tmp_path: Path
):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "v1")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    link = root / "ontology/object/later-link"
    link.symlink_to(outside)
    _commit(root, "symlink")

    manager.revert_to(meta["id"], target)
    assert not link.exists()
    assert not link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_revert_handles_business_filename_with_newline(manager: WorkspaceManager):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    path = root / "ontology/object/line\nbreak.md"
    path.write_text("v1", encoding="utf-8")
    target = _commit(root, "v1")
    path.write_text("v2", encoding="utf-8")
    _commit(root, "v2")

    manager.revert_to(meta["id"], target)
    assert path.read_text(encoding="utf-8") == "v1"


@pytest.mark.parametrize("target_is_directory", [True, False])
def test_revert_handles_file_directory_type_changes(
    manager: WorkspaceManager, target_is_directory: bool
):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    path = root / "ontology" / "object" / "sample"
    child = path / "item.md"

    if target_is_directory:
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_text("target child", encoding="utf-8")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("target file", encoding="utf-8")
    target = _commit(root, "target")

    if target_is_directory:
        child.unlink()
        path.rmdir()
        path.write_text("current file", encoding="utf-8")
    else:
        path.unlink()
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_text("current child", encoding="utf-8")
    _commit(root, "current")

    manager.revert_to(meta["id"], target)
    if target_is_directory:
        assert path.is_dir()
        assert child.read_text(encoding="utf-8") == "target child"
    else:
        assert path.is_file()
        assert path.read_text(encoding="utf-8") == "target file"
    assert manager._managed_dirty_paths(root) == []


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
    assert versions[0]["current"] is True
    assert versions[-1]["current"] is False


def test_versions_keep_absolute_numbers_when_limited(manager: WorkspaceManager):
    meta = manager.create_project("t")
    root = manager.project_root(meta["id"])
    for index in range(55):
        _write(root, "ontology/object/item.md", str(index))
        _commit(root, f"v{index}")
    versions = manager.versions(meta["id"], limit=50)
    assert [versions[0]["version"], versions[-1]["version"]] == [56, 7]
    older = manager.versions(meta["id"], limit=10, offset=50)
    assert [older[0]["version"], older[-1]["version"]] == [6, 1]
    assert all(entry["current"] is False for entry in older)
