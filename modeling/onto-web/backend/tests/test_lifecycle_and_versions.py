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
        '"""\n'
        "关联operation：创建订单\n"
        "\n"
        "功能描述：计算客单价\n"
        "\n"
        "输入参数：\n"
        "    orders    list    订单列表，必填\n"
        "    factor    float   加权因子，默认 1.0\n"
        "\n"
        "输出：\n"
        "    return 客单价数值\n"
        '"""\n'
        "\n"
        "def run(orders):\n"
        "    return 0\n",
        encoding="utf-8",
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
        # consumer load_script_catalog 要求 entrypoint，缺失会被静默剔除
        assert "entrypoint: scripts/calc_price.py" in script_doc
        # 执行契约：绑定 operation + 参数表（consumer execute_script 按此组装 CLI）
        assert 'bound_operation: "创建订单"' in script_doc
        assert "| 参数 key | CLI 参数 | 类型 | 必填 | 默认值 | 说明 |" in script_doc
        assert "| orders | --orders | string | 是 |  | 订单列表，必填 |" in script_doc
        assert "| factor | --factor | number | 否 | 1.0 |" in script_doc
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


def test_ensure_package_layout_migrates_logic_and_indexes(
    manager: WorkspaceManager, tmp_path: Path
):
    meta = manager.create_project("迁移验证")
    root = manager.project_root(meta["id"])
    legacy = root / "ontology" / "logic"
    legacy.mkdir()
    (legacy / "创建订单.md").write_text("# 创建订单\n\n步骤若干\n", encoding="utf-8")
    (legacy / "index.md").write_text("旧占位\n", encoding="utf-8")

    assert manager.ensure_package_layout(meta["id"]) is True
    assert not legacy.exists()
    assert (root / "ontology" / "operation" / "创建订单.md").is_file()
    index = (root / "ontology" / "operation" / "index.md").read_text(encoding="utf-8")
    assert "| 创建订单.md | 创建订单 |" in index
    # 幂等：第二次无变化，也不产生新提交
    assert manager.ensure_package_layout(meta["id"]) is False
    # index.md 不计入进度探针与实体计数
    progress = manager._project_progress(root)
    assert progress["stages_done"] == 0  # 各目录只有 index.md 时不算阶段完成
    assert progress["operations"] == 1


def test_import_transfer_zip_restores_history(manager: WorkspaceManager, tmp_path: Path):
    meta = manager.create_project("迁移源", background="测试迁移")
    root = manager.project_root(meta["id"])
    (root / "research" / "note.md").write_text("# 记录\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: 记录"], cwd=root, check=True, capture_output=True
    )
    archive = tmp_path / "transfer.zip"
    manager.export_zip(meta["id"], "workspace", archive)

    restored = manager.import_transfer_zip(archive, owner="alice")
    assert restored["id"] != meta["id"]
    assert restored["name"] == "迁移源"
    assert restored["owner"] == "alice"
    assert restored["background"] == "测试迁移"
    new_root = manager.project_root(restored["id"])
    assert (new_root / "research" / "note.md").is_file()
    # git 历史保留（迁移包含 .git）
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=new_root, capture_output=True, text=True
    ).stdout
    assert "feat: 记录" in log


def test_resource_directives_roundtrip(manager: WorkspaceManager):
    meta = manager.create_project("资料用途")
    saved = manager.save_resource(
        meta["id"], "excel", "库表.xlsx", b"x", purpose="schema", reading_mode="full"
    )
    directives = manager.resource_directives(meta["id"])
    assert directives[saved["path"]] == {"purpose": "schema", "reading_mode": "full"}
    # 修改用途
    manager.set_resource_directive(meta["id"], saved["path"], "term")
    assert manager.resource_directives(meta["id"])[saved["path"]]["purpose"] == "term"
    # 非法用途拒绝
    try:
        manager.set_resource_directive(meta["id"], saved["path"], "bogus")
        raise AssertionError("should reject")
    except WorkspaceError:
        pass
    # 删除资料清条目；materials.json 本身不可删
    manager.delete_resource(meta["id"], saved["path"])
    assert saved["path"] not in manager.resource_directives(meta["id"])
    try:
        manager.delete_resource(meta["id"], "resource/materials.json")
        raise AssertionError("should protect materials.json")
    except WorkspaceError:
        pass
