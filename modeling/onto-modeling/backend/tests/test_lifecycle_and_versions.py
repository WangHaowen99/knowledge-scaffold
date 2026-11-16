"""项目生命周期扩展、成员角色、乐观并发写入、版本标记与知识包导出的测试。"""

import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from app.services.workspace import WorkspaceError, WorkspaceManager
from app.services.workspace import ScriptContractError, _script_header_meta


def test_script_header_meta_uses_declared_kebab_case_flag():
    code = '''"""
关联operation：无
输入参数：
    crude_name    string    名称，必填
"""
import argparse
def _cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--crude-name", dest="crude_name", required=True)
'''
    _, params = _script_header_meta(code)
    assert params[0]["name"] == "crude_name"
    assert params[0]["flag"] == "--crude-name"


def test_script_header_meta_rejects_documented_flag_mismatch():
    code = '''"""
关联operation：无
输入参数：
    crude_name    string    名称，必填
"""
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--other-name", required=True)
'''
    with pytest.raises(ScriptContractError, match="头部参数与 argparse 不一致"):
        _script_header_meta(code)


def test_script_header_meta_rejects_import_only_param_in_header():
    code = '''"""
关联operation：无
输入参数：
    device_id        str           装置代码；必填
    yield_records    list[dict]    收率记录，仅 import 调用时传入；默认空列表
"""
import argparse
def select(device_id, yield_records=None):
    return {}
def _cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device-id", required=True)
'''
    with pytest.raises(ScriptContractError, match=r"多余 \['yield_records'\].*仅供 import 调用的函数参数"):
        _script_header_meta(code)


def test_script_header_meta_rejects_bool_header_for_string_flag():
    code = '''"""
关联operation：无
输入参数：
    has_jet_fuel    bool    是否含航煤；默认 False
"""
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--has-jet-fuel", default="false")
'''
    with pytest.raises(ScriptContractError, match="has_jet_fuel 的类型与 argparse 不一致：头部为 boolean，argparse 为 string"):
        _script_header_meta(code)


def test_script_header_meta_accepts_store_true_bool_flag():
    code = '''"""
关联operation：无
输入参数：
    has_jet_fuel    bool    是否含航煤；默认 False
"""
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--has-jet-fuel", action="store_true")
'''
    _, params = _script_header_meta(code)
    assert params[0]["type"] == "boolean"
    assert params[0]["required"] == "否"


def _write_eval_table(root: Path, sheet: str, columns: list[str]) -> None:
    """写一张 schema 数据表：对象靠它拿到真实表名与列名。"""
    openpyxl = pytest.importorskip("openpyxl")

    excel = root / "resource" / "excel"
    excel.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    worksheet.append(columns)
    workbook.save(excel / "评测数据.xlsx")
    (root / "resource" / "materials.json").write_text(
        json.dumps({"resource/excel/评测数据.xlsx": {"purpose": "schema"}}, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
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


@pytest.mark.parametrize("status", ["confirmed", "pending", None])
def test_clone_preserves_goal_and_keeps_projects_independent(manager: WorkspaceManager, status):
    """All names and goal content in this case are synthetic."""
    goal = "Synthetic modeling goal.\nPreserve the complete text."
    source = manager.create_project("project_alpha", background=goal if status else "")
    if status == "confirmed":
        manager.confirm_goal(source["id"])
    elif status is None:
        # Legacy projects can have a background without a confirmation status.
        manager._update_meta(source["id"], background=goal)

    clone = manager.clone_project(source["id"], owner="operator_a", name="project_copy")
    persisted = manager.get_project(clone["id"])
    assert clone["background"] == persisted["background"] == goal
    assert clone.get("goal_status") == persisted.get("goal_status") == status
    assert clone["id"] != source["id"]
    assert clone["owner"] == "operator_a"
    manager._update_meta(clone["id"], background="Synthetic copy revision.")
    assert manager.get_project(source["id"])["background"] == goal
    manager._update_meta(source["id"], background="Synthetic source revision.")
    assert manager.get_project(clone["id"])["background"] == "Synthetic copy revision."


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
        zf.writestr("research/summary.md", "资料分析")
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
        "# 数据来源\nORDER_TABLE\n\n"
        "# 对象属性清单\n"
        "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
        "|--------|--------|-----|----|-----|--------|--------|\n"
        "| 订单号 | ORDER_ID | 编号 | 是 | 字符串 | 单号, 订单编号 | 无 |\n"
        "| 配送标识 | DELIVERY_FLAG | 标识 | 否 | 布尔 | 无 | 取值：0 / 1 |\n"
        "| 加急标识 | URGENT_FLAG | 标识 | 否 | 布尔 | 无 | 取值：0 / 1 |\n",
        encoding="utf-8",
    )
    _write_eval_table(root, "ORDER_TABLE", ["ORDER_ID", "DELIVERY_FLAG"])
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
        "    orders    json    订单列表，必填\n"
        "    factor    float   加权因子，默认 1.0\n"
        "\n"
        "输出：\n"
        "    return 客单价数值\n"
        '"""\n'
        "\n"
        "import argparse\n"
        "def run(orders, factor):\n"
        "    return 0\n"
        "def _cli():\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--orders', required=True, type=json.loads)\n"
        "    parser.add_argument('--factor', default=1.0, type=float)\n"
        "    run(**vars(parser.parse_args()))\n",
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
        # 对齐 modeling 知识包契约：四列且 来源Sheet 是真实表名（不是对象名回填）
        assert "| object key | 标题 | 说明 | 来源Sheet |" in obj_index
        assert "| 订单 | 订单 | 订单 | ORDER_TABLE |" in obj_index
        # 工作区占位 index.md 不应进包
        assert f"{pid}/objects/index.md" in names and "待生成" not in obj_index
        # 五元组：规则/指标/脚本各自成组导出（ADR-0004）
        obj = zf.read(f"{pid}/objects/订单.md").decode("utf-8")
        # 「对象属性清单」按 modeling 契约转成 `# 字段`（前六列按位解析）
        assert "| 字段 key | 中文名 | 类型 | 必填 | 业务说明 | 别名 | 属性规则 |" in obj
        assert "| ORDER_ID | 订单号 | 字符串 | 是 | 编号 | 单号, 订单编号 |" in obj
        assert "# 对象属性清单" not in obj
        # 绑定到真实表的对象带 mapping/sources frontmatter
        assert 'resource: "ORDER_TABLE"' in obj and 'sources: ["表格：ORDER_TABLE"]' in obj
        # 属性别名列的占位"无"打包时清洗为空（下游校验器会把它当真实别名，
        # 两行同名"无"即"同名指向多字段"冲突）；真实别名与其他列不动
        assert "单号, 订单编号" in obj
        assert "| 布尔 | 否 | 标识 | 无 |" not in obj
        assert "取值：0 / 1" in obj  # 属性规则列的"无"语义不同，不在清洗范围
        # 数据映射：只写对上真实列的属性，URGENT_FLAG 库里没有故不进 mapping
        mapping = json.loads(zf.read(f"{pid}/mappings/object-data-mappings.json").decode("utf-8"))
        assert mapping["schema_version"] == "0.1"
        订单 = mapping["objects"]["订单"]
        assert 订单["resource"] == "ORDER_TABLE" and 订单["primary_key"] == ["ORDER_ID"]
        assert set(订单["fields"]) == {"ORDER_ID", "DELIVERY_FLAG"}
        assert 订单["fields"]["ORDER_ID"]["source_column"] == "ORDER_ID"
        assert 订单["sources"][0]["role"] == "primary"
        assert 订单["sources"][0]["fields"]["DELIVERY_FLAG"]["source_column"] == "DELIVERY_FLAG"
        assert 订单["policy"]["read_only"] is True
        rule = zf.read(f"{pid}/rules/限购规则.md").decode("utf-8")
        assert "resource: rule" in rule and 'objects: ["订单"]' in rule
        metric = zf.read(f"{pid}/metrics/客单价.md").decode("utf-8")
        assert "resource: metric" in metric
        assert f"{pid}/scripts/calc_price.py" in names
        script_doc = zf.read(f"{pid}/scripts/calc_price.md").decode("utf-8")
        assert "resource: script" in script_doc
        # consumer load_script_catalog 要求 entrypoint，缺失会被静默剔除；
        # modeling 按 scripts/<entrypoint> 取源码，故只写文件名、不带 scripts/ 前缀
        assert "entrypoint: calc_price.py\n" in script_doc
        # 执行契约：绑定 operation + 参数表（consumer execute_script 按此组装 CLI）
        assert 'bound_operation: "创建订单"' in script_doc
        # 一级标题与 modeling scriptMd 一致：modeling 只认 `# 输入参数`
        assert "\n# 输入参数\n" in script_doc and "## 输入参数" not in script_doc
        assert "| 参数 key | CLI 参数 | 类型 | 必填 | 默认值 | 说明 |" in script_doc
        assert "| orders | --orders | json | 是 |  | 订单列表，必填 | 否 |  |" in script_doc
        assert "| factor | --factor | number | 否 | 1.0 | 加权因子，默认 1.0 | 否 |  |" in script_doc
        assert f"{pid}/rules/index.md" in names
        assert f"{pid}/metrics/index.md" in names


def test_export_package_zip_script_entrypoint_is_ascii_filename(manager: WorkspaceManager, tmp_path: Path):
    """modeling 要求 entrypoint 满足 ^[A-Za-z0-9_.-]+\\.py$，并按 scripts/<entrypoint> 取源码。"""
    meta = manager.create_project("脚本入口")
    root = manager.project_root(meta["id"])
    for operation in ("计价", "折扣"):
        (root / "ontology" / "operation" / f"{operation}.md").write_text(f"# 业务描述\n{operation}\n", encoding="utf-8")
    sources = {
        "calc_price": '"""\n关联operation：计价\n"""\ndef run():\n    return {\'price\': 1}\n',
        "计算折扣": '"""\n关联operation：折扣\n"""\ndef run():\n    return {\'discount\': 2}\n',
    }
    for key, code in sources.items():
        (root / "ontology" / "scripts" / f"{key}.py").write_text(code, encoding="utf-8")
    # 「关联operation：无」的库模块随包发布供 import，但不对外调用、没有描述符
    library = "def helper():\n    return 1\n"
    (root / "ontology" / "scripts" / "shared_math.py").write_text(library, encoding="utf-8")
    dest = tmp_path / "pkg.zip"
    manager.export_package_zip(meta["id"], dest)
    pid = meta["id"]
    with zipfile.ZipFile(dest) as zf:
        for key, code in sources.items():
            script_doc = zf.read(f"{pid}/scripts/{key}.md").decode("utf-8")
            assert f"key: {json.dumps(key, ensure_ascii=False)}" in script_doc
            entrypoint = re.search(r"^entrypoint: (.+)$", script_doc, re.M).group(1)
            assert re.fullmatch(r"[A-Za-z0-9_.-]+\.py", entrypoint), entrypoint
            assert zf.read(f"{pid}/scripts/{entrypoint}").decode("utf-8") == code
        assert f"{pid}/scripts/计算折扣.py" not in zf.namelist()
        assert zf.read(f"{pid}/scripts/shared_math.py").decode("utf-8") == library
        assert f"{pid}/scripts/shared_math.md" not in zf.namelist()
    # 同一脚本多次导出文件名不变，版本间 diff 与 consumer 升级不会把它当成新脚本
    again = tmp_path / "again.zip"
    manager.export_package_zip(meta["id"], again)
    with zipfile.ZipFile(dest) as first, zipfile.ZipFile(again) as second:
        doc = f"{pid}/scripts/计算折扣.md"
        assert first.read(doc) == second.read(doc)


def test_export_package_zip_keeps_helper_scripts_as_source_only(manager: WorkspaceManager, tmp_path: Path):
    """「关联operation」为「无」或不是现有操作的脚本是辅助模块：只带源码，不写描述符、不进脚本目录。"""
    meta = manager.create_project("辅助脚本")
    root = manager.project_root(meta["id"])
    (root / "ontology" / "operation" / "定价.md").write_text("# 定价\n", encoding="utf-8")
    scripts = {
        "calc_price": "定价",
        "shared_utils": "无",
        "orphan_calc": "不存在的操作",
    }
    for key, bound in scripts.items():
        (root / "ontology" / "scripts" / f"{key}.py").write_text(
            f'"""\n关联operation：{bound}\n"""\ndef run():\n    return 1\n', encoding="utf-8",
        )
    dest = tmp_path / "pkg.zip"
    manager.export_package_zip(meta["id"], dest)
    pid = meta["id"]
    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
        index = zf.read(f"{pid}/scripts/index.md").decode("utf-8")
    for key in scripts:
        assert f"{pid}/scripts/{key}.py" in names
    assert f"{pid}/scripts/calc_price.md" in names and "| calc_price |" in index
    for helper in ("shared_utils", "orphan_calc"):
        assert f"{pid}/scripts/{helper}.md" not in names
        assert f"| {helper} |" not in index


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
    # index.md 不计入进度探针与实体计数：只有迁移来的 operation 阶段算完成
    progress = manager._project_progress(root)
    assert progress["stages_done"] == 1
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


def test_import_transfer_zip_uses_default_engine(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch
):
    """引擎是部署侧配置：迁移包里带的引擎不跟着进来，按本部署默认引擎跑。"""
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "settings")
    meta = manager.create_project("迁移源")
    manager._update_meta(meta["id"], engine="jiuwen")
    archive = tmp_path / "transfer-engine.zip"
    manager.export_zip(meta["id"], "workspace", archive)

    assert manager.import_transfer_zip(archive)["engine"] == "claude"


def test_resource_directives_roundtrip(manager: WorkspaceManager):
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    workbook.active.title = "table_a"
    workbook.active.append(["id", "name"])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    meta = manager.create_project("资料用途")
    saved = manager.save_resource(
        meta["id"], "excel", "库表.xlsx", output.getvalue(), purpose="schema", reading_mode="full"
    )
    directives = manager.resource_directives(meta["id"])
    assert directives[saved["path"]]["purpose"] == "schema"
    assert directives[saved["path"]]["reading_mode"] == "full"
    assert directives[saved["path"]]["schema_selection"]["included_sheets"] == ["table_a"]
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


def test_migrate_from_modeling_end_to_end(manager: WorkspaceManager, tmp_path: Path, monkeypatch):
    import sqlite3
    import sys

    sys.path.append(str(Path(__file__).resolve().parents[1] / "scripts"))
    import migrate_from_modeling as mig

    # 构造 modeling 数据目录：db + 发布布局包
    source = tmp_path / "modeling-data"
    source.mkdir()
    db = sqlite3.connect(source / "modeling.db")
    db.execute(
        "CREATE TABLE projects (id TEXT, owner TEXT, name TEXT, domain TEXT, background TEXT)"
    )
    db.execute(
        "CREATE TABLE documents (project_id TEXT, filename TEXT, kind TEXT,"
        " text_content TEXT, tables_json TEXT, lifecycle_status TEXT)"
    )
    db.execute(
        "INSERT INTO projects VALUES ('pj_test1', 'alice', '迁移源', '对公营销', '')"
    )
    db.execute(
        "INSERT INTO documents VALUES ('pj_test1', '手册.docx', 'word', '产品说明正文', NULL, 'active')"
    )
    db.execute(
        "INSERT INTO documents VALUES ('pj_test1', '数据表.xlsx', 'excel', NULL,"
        " '[{\"name\":\"T1\",\"fields\":[{\"name\":\"ID\",\"inferredType\":\"string\","
        "\"sampleValues\":[\"a\"]}]}]', 'active')"
    )
    db.commit()
    db.close()

    pkg = source / "artifacts" / "pj_test1" / "package"
    (pkg / "objects").mkdir(parents=True)
    (pkg / "objects" / "customer.md").write_text(
        "---\nresource: object\nkey: customer\n---\n\n# 业务定义\n\n客户主体\n",
        encoding="utf-8",
    )
    (pkg / "objects" / "index.md").write_text("目录", encoding="utf-8")
    (pkg / "scripts").mkdir()
    (pkg / "scripts" / "calc.py").write_text("print(1)\n", encoding="utf-8")
    (pkg / "scripts" / "calc.md").write_text("descriptor", encoding="utf-8")

    from app import config as app_config

    monkeypatch.setattr(app_config, "DATA_DIR", manager.workspaces_dir.parent)
    monkeypatch.setattr(app_config, "ASSETS_DIR", manager.assets_dir)
    monkeypatch.setattr(sys, "argv", ["mig", "--source", str(source)])
    assert mig.main() == 0

    projects = manager.list_projects()
    migrated = next(p for p in projects if p.get("migrated_from") == "pj_test1")
    assert migrated["name"] == "迁移源"
    assert migrated["owner"] == "alice"
    root = manager.project_root(migrated["id"])
    body = (root / "ontology/object/customer.md").read_text(encoding="utf-8")
    assert body.startswith("# 业务定义")  # 发布 frontmatter 已剥离
    assert (root / "ontology/scripts/calc.py").is_file()
    assert not (root / "ontology/scripts/calc.md").exists()  # 描述符不迁；calc.py 没有关联operation，是库模块
    assert "解析文本" in (root / "resource/others/手册.md").read_text(encoding="utf-8")
    excel = (root / "resource/excel/数据表.md").read_text(encoding="utf-8")
    assert "| ID | string | a |" in excel
    assert manager.resource_directives(migrated["id"])["resource/excel/数据表.md"]["purpose"] == "schema"
    # 幂等：再跑一次跳过
    assert mig.main() == 0
    assert sum(1 for p in manager.list_projects() if p.get("migrated_from") == "pj_test1") == 1


def test_migrate_render_excel_tables_skips_null_entries():
    import sys

    sys.path.append(str(Path(__file__).resolve().parents[1] / "scripts"))
    import migrate_from_modeling as mig

    body = mig.render_excel_tables(
        '[null, {"name": null, "fields": null},'
        ' {"name": "T1", "fields": [null, {"name": "ID", "inferredType": "string",'
        ' "sampleValues": ["a"]}]}]'
    )
    assert "## 未命名表" in body
    assert "| ID | string | a |" in body
    assert mig.render_excel_tables('{"name": "T1"}') == ""


def test_migrate_from_modeling_failed_project_is_retried(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch
):
    import sqlite3
    import sys

    sys.path.append(str(Path(__file__).resolve().parents[1] / "scripts"))
    import migrate_from_modeling as mig

    source = tmp_path / "modeling-data"
    source.mkdir()
    db = sqlite3.connect(source / "modeling.db")
    db.execute("CREATE TABLE projects (id TEXT, owner TEXT, name TEXT, domain TEXT, background TEXT)")
    db.execute(
        "CREATE TABLE documents (project_id TEXT, filename TEXT, kind TEXT,"
        " text_content TEXT, tables_json TEXT, lifecycle_status TEXT)"
    )
    db.execute("INSERT INTO projects VALUES ('pj_bad', 'alice', '坏项目', '', '')")
    db.execute("INSERT INTO projects VALUES ('pj_ok', 'bob', '好项目', '', '')")
    db.execute("INSERT INTO documents VALUES ('pj_bad', '说明.docx', 'word', '正文', NULL, 'active')")
    db.commit()
    db.close()

    from app import config as app_config

    monkeypatch.setattr(app_config, "DATA_DIR", manager.workspaces_dir.parent)
    monkeypatch.setattr(app_config, "ASSETS_DIR", manager.assets_dir)
    monkeypatch.setattr(sys, "argv", ["mig", "--source", str(source)])

    real_migrate_documents = mig.migrate_documents

    def broken_for_bad(db, project_id, *args):
        if project_id == "pj_bad":
            raise AttributeError("boom")
        return real_migrate_documents(db, project_id, *args)

    monkeypatch.setattr(mig, "migrate_documents", broken_for_bad)
    assert mig.main() == 1
    projects = manager.list_projects()
    assert [p["name"] for p in projects] == ["好项目"]  # 失败项目不留半成品，也不阻断后续项目

    monkeypatch.setattr(mig, "migrate_documents", real_migrate_documents)
    assert mig.main() == 0
    sources = sorted(p.get("migrated_from") for p in manager.list_projects())
    assert sources == ["pj_bad", "pj_ok"]
