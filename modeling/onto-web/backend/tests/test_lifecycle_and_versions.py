"""项目生命周期扩展、成员角色、乐观并发写入、版本标记与知识包导出的测试。"""

import io
import zipfile
from pathlib import Path

import pytest

from app.services.workspace import WorkspaceError, WorkspaceManager


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "claude-workspace-dotclaude" / "templates").mkdir(parents=True)
    (assets / "claude-workspace-CLAUDE.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    return WorkspaceManager(tmp_path / "data", assets)


def test_archive_and_unarchive(manager: WorkspaceManager):
    meta = manager.create_project("归档测试")
    updated = manager.set_archived(meta["id"], True)
    assert updated["archived"] is True
    assert manager.set_archived(meta["id"], False)["archived"] is False


def test_clone_copies_content_but_not_history(manager: WorkspaceManager):
    meta = manager.create_project("原始项目")
    root = manager.project_root(meta["id"])
    (root / "ontology" / "object" / "订单.md").write_text("# 订单\n", encoding="utf-8")

    clone = manager.clone_project(meta["id"], owner="alice")
    clone_root = manager.project_root(clone["id"])
    assert clone["name"] == "原始项目-副本"
    assert clone["owner"] == "alice"
    assert (clone_root / "ontology" / "object" / "订单.md").read_text(encoding="utf-8") == "# 订单\n"
    assert not (clone_root / ".onto-web.json").read_text(encoding="utf-8").count(meta["id"])


def test_role_of_and_write_permission():
    meta = {"owner": "alice", "members": {"bob": "editor", "carol": "viewer"}}
    assert WorkspaceManager.role_of(meta, "alice", False) == "owner"
    assert WorkspaceManager.role_of(meta, "bob", False) == "editor"
    assert WorkspaceManager.role_of(meta, "carol", False) == "viewer"
    assert WorkspaceManager.role_of(meta, "dave", False) is None
    assert WorkspaceManager.role_of(meta, "dave", True) == "admin"
    assert WorkspaceManager.can_write(meta, "carol", False) is False
    assert WorkspaceManager.can_write(meta, "bob", False) is True
    # 无主历史项目对所有人开放为 editor
    assert WorkspaceManager.role_of({"owner": ""}, "anyone", False) == "editor"


def test_set_members_validates_roles(manager: WorkspaceManager):
    meta = manager.create_project("成员")
    updated = manager.set_members(meta["id"], {"bob": "viewer"})
    assert updated["members"] == {"bob": "viewer"}
    with pytest.raises(WorkspaceError):
        manager.set_members(meta["id"], {"bob": "boss"})


def test_write_file_creates_commit_and_returns_revision(manager: WorkspaceManager):
    meta = manager.create_project("写入")
    result = manager.write_file(meta["id"], "notes.md", "v1", author="alice")
    assert result["commit"]
    assert result["revision"]
    read = manager.read_file(meta["id"], "notes.md", 1024)
    assert read["revision"] == result["revision"]
    versions = manager.versions(meta["id"])
    assert versions[0]["message"].startswith("web: 保存 notes.md")


def test_write_file_conflict_on_stale_revision(manager: WorkspaceManager):
    meta = manager.create_project("并发")
    first = manager.write_file(meta["id"], "doc.md", "原始内容")
    # 模拟 agent 在 Web 编辑期间改了文件
    manager.write_file(meta["id"], "doc.md", "agent 改动")
    with pytest.raises(WorkspaceError) as exc:
        manager.write_file(meta["id"], "doc.md", "web 覆盖", base_revision=first["revision"])
    assert exc.value.status == 409
    # 未变化时保存不产生空提交
    current = manager.read_file(meta["id"], "doc.md", 1024)
    result = manager.write_file(
        meta["id"], "doc.md", "agent 改动", base_revision=current["revision"]
    )
    assert result["commit"] == ""


def test_tag_version_and_delete(manager: WorkspaceManager):
    meta = manager.create_project("标记")
    manager.write_file(meta["id"], "a.md", "内容")
    versions = manager.versions(meta["id"])
    commit = versions[0]["commit"]
    tag = manager.tag_version(meta["id"], commit, "校验通过的里程碑")
    tagged = manager.versions(meta["id"])
    assert tagged[0]["tags"] == [{"name": tag["name"], "label": "校验通过的里程碑"}]
    manager.delete_tag(meta["id"], tag["name"])
    assert manager.versions(meta["id"])[0]["tags"] == []


def test_import_summary_reports_contents(manager: WorkspaceManager, tmp_path: Path):
    archive = tmp_path / "in.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("ontology/object/订单.md", "# 订单")
        zf.writestr("ontology/logic/创建订单.md", "# 创建订单")
        zf.writestr("research/summary.md", "调研")
        zf.writestr(".git/config", "skip me")
    summary = manager.import_summary(archive)
    assert summary["objects"] == 1
    assert summary["operations"] == 1
    assert summary["files"] == 4
    assert ".git" in summary["skipped"]


def test_export_package_zip_layout(manager: WorkspaceManager, tmp_path: Path):
    meta = manager.create_project("外卖订单")
    root = manager.project_root(meta["id"])
    (root / "ontology" / "object" / "订单.md").write_text(
        "# 订单\n\n属性若干\n", encoding="utf-8"
    )
    (root / "ontology" / "logic").mkdir(exist_ok=True)  # 旧目录：验证兼容读取
    (root / "ontology" / "logic" / "创建订单.md").write_text(
        "---\nold: front\n---\n\n生成[[订单]]\n", encoding="utf-8"
    )
    (root / "ontology" / "rule" / "限购规则.md").write_text(
        "# 限购规则\n\n约束[[订单]]数量\n", encoding="utf-8"
    )
    (root / "ontology" / "metric" / "客单价.md").write_text(
        "# 客单价\n\n基于[[订单]]金额\n", encoding="utf-8"
    )
    (root / "ontology" / "scripts" / "calc_price.py").write_text(
        '"""计算客单价。"""\n', encoding="utf-8"
    )
    dest = tmp_path / "pkg.zip"
    manager.export_package_zip(meta["id"], dest)
    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
        pid = meta["id"]
        assert f"{pid}/manifest.yaml" in names
        assert f"{pid}/objects/index.md" in names
        assert f"{pid}/operations/創建订单.md" not in names  # 不应出现错名
        manifest = zf.read(f"{pid}/manifest.yaml").decode("utf-8")
        assert "kind: ontology-package" in manifest
        assert "schema_version: '0.1'" in manifest
        op = zf.read(f"{pid}/operations/创建订单.md").decode("utf-8")
        # 源文件 frontmatter 被剥离，注入的 frontmatter 带 uses.objects
        assert op.count("---") == 2
        assert 'objects: ["订单"]' in op
        assert "old: front" not in op
        obj_index = zf.read(f"{pid}/objects/index.md").decode("utf-8")
        assert "| object key | title |" in obj_index
        assert "| 订单 | 订单 |" in obj_index
        # 工作区占位 index.md 不应进包
        assert f"{pid}/objects/index.md" in names and "待生成" not in obj_index
        # 五元组：规则/指标/脚本各自成组导出（ADR-0004）
        rule = zf.read(f"{pid}/rules/限购规则.md").decode("utf-8")
        assert "resource: rule" in rule and 'objects: ["订单"]' in rule
        metric = zf.read(f"{pid}/metrics/客单价.md").decode("utf-8")
        assert "resource: metric" in metric
        assert f"{pid}/scripts/calc_price.py" in names
        script_doc = zf.read(f"{pid}/scripts/calc_price.md").decode("utf-8")
        assert "resource: script" in script_doc
        assert f"{pid}/rules/index.md" in names
        assert f"{pid}/metrics/index.md" in names


def test_excel_preview_roundtrip(tmp_path: Path):
    openpyxl = pytest.importorskip("openpyxl")
    from app.services import excel_preview

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "订单表"
    ws.append(["编号", "金额"])
    ws.append([1, 12.5])
    ws.merge_cells("A3:B3")
    ws["A3"] = "合并行"
    path = tmp_path / "t.xlsx"
    wb.save(path)

    result = excel_preview.preview(path)
    sheet = result["sheets"][0]
    assert sheet["name"] == "订单表"
    assert sheet["rows"][0] == ["编号", "金额"]
    assert sheet["rows"][1] == ["1", "12.5"]
    assert {"row": 2, "col": 0, "rowspan": 1, "colspan": 2} in sheet["merges"]
