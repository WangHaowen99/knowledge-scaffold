"""数据 Schema 的 Sheet 范围与表头预检（ADR-0096，移植自 develop 的同名测试）。

上传、资料页（R-01、R-03、R-16）、发布的数据绑定（package_mapping）和工作区脚本（table_profile）用同一份范围；
Excel 只读文本版（测试里直接调同一个转换器转），CSV 直接读。工作簿、表名、列名全部为合成数据。
"""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth, package_mapping, table_sources
from app.services.workspace import WorkspaceError, WorkspaceManager
from tests.test_artifact_verification import verifier  # noqa: F401  产物核验的夹具
from tests.text_versions import write_text_versions

OWNER, OTHER = "operator_a", "operator_b"
TOKENS = {"owner-token": {"sub": OWNER, "role": "user"}, "other-token": {"sub": OTHER, "role": "user"}}
SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    instance = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", instance)
    return instance


def xlsx(build) -> bytes:
    wb = openpyxl.Workbook()
    build(wb)
    out = io.BytesIO()
    wb.save(out)
    wb.close()
    return out.getvalue()


def sample_workbook(rename: str = "") -> bytes:
    """总览（合并的标题行 + 说明）+ 两张数据表；rename 给出时把 table_b 改成这个名字。"""
    def build(wb):
        overview = wb.active
        overview.title = "总览"
        overview.append(["Synthetic workbook overview"])
        overview.merge_cells("A1:C1")
        overview.append(["Reference description"])
        overview.append(["Table", "Fields", "Description"])
        for name in ("table_a", rename or "table_b"):
            sheet = wb.create_sheet(name)
            sheet.append(["id", "name"])
            sheet.append(["sample_1", "Synthetic item"])
    return xlsx(build)


def save(manager, pid, filename, data, **kwargs):
    saved = manager.save_resource(pid, "excel", filename, data, **kwargs)
    write_text_versions(manager.project_root(pid))
    return saved


def setup_project(manager):
    pid = manager.create_project("project_alpha", owner=OWNER)["id"]
    saved = save(manager, pid, "sample.xlsx", sample_workbook())
    return pid, manager.project_root(pid), saved["path"]


def preview(manager, pid, rel):
    return table_sources.preview(manager.project_root(pid) / rel, manager.resource_directives(pid).get(rel))


def summary(manager, pid, rel):
    return table_sources.summary(manager.project_root(pid) / rel, manager.resource_directives(pid).get(rel))


def manual(scope, included):
    return {"mode": "manual", "included_sheets": included, "revision": scope["revision"]}


def cli_headers(root, *flags):
    """工作区里的 table_profile.py --headers：返回 (退回码, 写进文件的表头)。"""
    script = (root / ".claude" / "scripts" / "table_profile.py").resolve()
    # 安全审查：解释器、项目工具脚本和参数均由测试固定；argv 不经 shell 解析，工作目录是 tmp_path 下的合成项目
    result = subprocess.run(  # NOSONAR(S4721)
        [sys.executable, *flags, str(script), "--headers"],
        cwd=root, capture_output=True, text=True, shell=False, check=False, timeout=60,
    )
    saved = root / ".ontology-state" / "table-headers.json"
    tables = json.loads(saved.read_text(encoding="utf-8"))["tables"] if result.returncode == 0 else None
    return result, tables


# ---- 上传、旧项目、工作区脚本同一份范围 ----


def test_upload_legacy_project_and_cli_share_scope(manager):
    pid, root, rel = setup_project(manager)
    directive = manager.resource_directives(pid)[rel]
    assert directive["auto_detected"] is True and "schema_selection" not in directive  # 现算，不存
    scope = summary(manager, pid, rel)
    assert scope["included_sheets"] == ["table_a", "table_b"] and scope["reference_sheets"] == ["总览"]
    # 旧项目：只有文件级的 schema 标注，读的时候不改写 materials.json
    meta = root / "resource" / "materials.json"
    meta.write_text(json.dumps({rel: {"purpose": "schema", "auto_detected": True}}), encoding="utf-8")
    before = meta.read_bytes()
    tables = package_mapping.read_source_tables(root)
    result, cli = cli_headers(root)
    assert result.returncode == 0, result.stderr
    assert cli == tables == {"table_a": ["id", "name"], "table_b": ["id", "name"]}
    assert meta.read_bytes() == before
    assert summary(manager, pid, rel)["reference_sheets"] == ["总览"]


def test_explicit_empty_and_manual_subset(manager):
    pid, root, rel = setup_project(manager)
    scope = preview(manager, pid, rel)
    manager.set_resource_directive(pid, rel, "schema", schema_selection=manual(scope, []))
    assert package_mapping.read_source_tables(root) == {}
    assert cli_headers(root)[1] == {}
    manager.set_resource_directive(pid, rel, "schema", schema_selection=manual(scope, ["table_b"]))
    assert set(package_mapping.read_source_tables(root)) == {"table_b"}
    # 只改读法不丢人工范围
    manager.set_resource_directive(pid, rel, "schema", "full")
    assert set(package_mapping.read_source_tables(root)) == {"table_b"}
    assert summary(manager, pid, rel)["mode"] == "manual"
    # 改成 auto 撤掉人工范围，回到现算
    manager.set_resource_directive(pid, rel, "schema", schema_selection={"mode": "auto"})
    assert set(package_mapping.read_source_tables(root)) == {"table_a", "table_b"}


def test_sheet_scope_save_creates_project_version(manager):
    pid, root, rel = setup_project(manager)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout
    manager.set_resource_directive(pid, rel, "schema", schema_selection=manual(preview(manager, pid, rel), ["table_b"]))
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout
    assert after != head
    changed = subprocess.run(["git", "show", "--pretty=format:", "--name-only", "HEAD"], cwd=root,
                             capture_output=True, text=True).stdout.split()
    assert "resource/materials.json" in changed
    committed = json.loads(subprocess.run(["git", "show", "HEAD:resource/materials.json"], cwd=root,
                                          capture_output=True, text=True).stdout)[rel]
    assert committed["schema_selection"]["included_sheets"] == ["table_b"]
    status = subprocess.run(["git", "status", "--short", "--", "resource/materials.json"], cwd=root,
                            capture_output=True, text=True).stdout
    assert status == ""
    assert set(package_mapping.read_source_tables(root)) == {"table_b"}


def test_replacing_workbook_versions_file_and_refreshes_auto_scope(manager):
    pid, root, rel = setup_project(manager)
    replaced = sample_workbook(rename="table_c")
    saved = save(manager, pid, "sample.xlsx", replaced, purpose="schema")
    committed = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=root, capture_output=True, check=True).stdout
    assert saved["commit"] and saved["replaced"] is True and committed == replaced
    scope = summary(manager, pid, rel)
    assert scope["included_sheets"] == ["table_a", "table_c"] and scope["error"] is None


def test_replacement_invalidates_manual_scope_without_losing_file(manager):
    pid, root, rel = setup_project(manager)
    scope = preview(manager, pid, rel)
    manager.set_resource_directive(pid, rel, "schema", schema_selection=manual(scope, ["table_b"]))
    save(manager, pid, "sample.xlsx", sample_workbook(rename="table_c"))
    with pytest.raises(WorkspaceError, match="重新确认") as stale:
        package_mapping.read_source_tables(root)
    assert (stale.value.status, stale.value.code) == (409, "schema_scope_stale")
    result, _ = cli_headers(root)
    assert result.returncode == 2 and "资料区重新确认" in result.stderr
    assert summary(manager, pid, rel)["error"]
    assert manager.resource_directives(pid)[rel]["schema_selection"]["included_sheets"] == ["table_b"]
    with pytest.raises(WorkspaceError, match="文件已变化"):
        manager.set_resource_directive(pid, rel, "schema", schema_selection=manual(scope, []))
    fresh = preview(manager, pid, rel)
    assert fresh["error"] and "table_c" in fresh["included_sheets"]
    manager.set_resource_directive(pid, rel, "schema", schema_selection=manual(fresh, ["table_c"]))
    assert set(package_mapping.read_source_tables(root)) == {"table_c"}


def test_invalid_selection_rolls_back_and_auto_scope_refreshes(manager):
    pid, root, rel = setup_project(manager)
    scope = preview(manager, pid, rel)
    old = (root / "resource" / "materials.json").read_bytes()
    with pytest.raises(WorkspaceError, match="没有 Sheet「missing」"):
        manager.set_resource_directive(pid, rel, "schema", schema_selection=manual(scope, ["missing"]))
    # 总览的表头是合并的标题行：表头有错误，不能当数据表
    with pytest.raises(WorkspaceError, match="总览") as invalid:
        manager.set_resource_directive(pid, rel, "schema", schema_selection=manual(scope, ["总览"]))
    assert invalid.value.status == 400
    assert (root / "resource" / "materials.json").read_bytes() == old
    save(manager, pid, "sample.xlsx", sample_workbook(rename="table_c"))
    assert set(package_mapping.read_source_tables(root)) == {"table_a", "table_c"}


def test_real_table_named_overview_and_chinese_manual_headers(tmp_path):
    def build(wb):
        wb.active.title = "总览"
        wb.active.append(["id", "name"])
        wb.create_sheet("table_b").append(["编号", "名称"])
    path = tmp_path / "resource" / "excel" / "sample.xlsx"
    path.parent.mkdir(parents=True)
    path.write_bytes(xlsx(build))
    write_text_versions(tmp_path)
    scope = table_sources.rules.resolve_selection(path, {"purpose": "schema"})
    assert scope["included_sheets"] == ["总览", "table_b"]
    assert table_sources.classify_sheet("总览", [["id", "name"]]) == "table"


def test_manual_schema_tag_preserves_chinese_headers_after_save(manager):
    pid = manager.create_project("project_alpha")["id"]

    def build(wb):
        wb.active.title = "table_a"
        wb.active.append(["编号", "名称"])
    rel = save(manager, pid, "sample.xlsx", xlsx(build), purpose="schema")["path"]
    root = manager.project_root(pid)
    assert package_mapping.read_source_tables(root) == {"table_a": ["编号", "名称"]}
    manager.set_resource_directive(pid, rel, "schema", "full")
    assert cli_headers(root)[1] == {"table_a": ["编号", "名称"]}


def test_data_merges_keep_sheet_and_all_readers_take_first_non_empty_row(manager):
    """develop 把第一行为空的 Sheet 排除（它的下游按物理第一行取表头）；这里所有读表头的地方都读文本版，
    表头一律是第一个非空行，不会错位，所以照常收进来，并核对后端与工作区脚本读到的一致。"""
    pid = manager.create_project("project_alpha")["id"]

    def build(wb):
        wb.active.title = "table_a"
        wb.active.append(["id", "name"])
        wb.active.append(["sample_1", "value"])
        wb.active.merge_cells("A2:A3")
        sheet = wb.create_sheet("table_b")
        sheet.append([None, None])
        sheet.append(["id", "name"])
    save(manager, pid, "sample.xlsx", xlsx(build), purpose="schema")
    root = manager.project_root(pid)
    expected = {"table_a": ["id", "name"], "table_b": ["id", "name"]}
    assert package_mapping.read_source_tables(root) == expected
    assert cli_headers(root)[1] == expected


@pytest.mark.parametrize("purpose", ["auto", "schema"])
def test_explicit_upload_does_not_discard_manual_scope(manager, purpose):
    pid, root, rel = setup_project(manager)
    manager.set_resource_directive(pid, rel, "schema", schema_selection=manual(preview(manager, pid, rel), []))
    save(manager, pid, "sample.xlsx", (root / rel).read_bytes(), purpose=purpose)
    assert manager.resource_directives(pid)[rel]["schema_selection"]["mode"] == "manual"
    assert package_mapping.read_source_tables(root) == {}


def test_purpose_change_with_stale_manual_scope_must_reconfirm(manager):
    pid, root, rel = setup_project(manager)
    manager.set_resource_directive(pid, rel, "schema", schema_selection=manual(preview(manager, pid, rel), ["table_b"]))
    save(manager, pid, "sample.xlsx", sample_workbook(rename="table_c"))
    with pytest.raises(WorkspaceError) as stale:
        manager.set_resource_directive(pid, rel, "schema", "full")
    assert stale.value.code == "schema_scope_stale"
    # 改成别的用途不受影响，人工范围随之撤掉
    manager.set_resource_directive(pid, rel, "evidence")
    assert "schema_selection" not in manager.resource_directives(pid)[rel]
    with pytest.raises(WorkspaceError, match="只有数据 Schema"):
        manager.set_resource_directive(pid, rel, "evidence", schema_selection={"mode": "auto"})


# ---- 接口：R-01 摘要、R-16 预检、R-03 保存；只有所有者能用 ----


def test_resource_scope_api_permissions_and_stale_request(manager, monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: TOKENS.get(token))
    pid, root, rel = setup_project(manager)
    owner, other = {"Authorization": "Bearer owner-token"}, {"Authorization": "Bearer other-token"}
    base = f"/api/v1/projects/{pid}/resources"
    with TestClient(app, raise_server_exceptions=False) as client:
        scope = client.get(f"{base}/schema-preview", params={"path": rel}, headers=owner)
        assert scope.status_code == 200
        scope = scope.json()
        assert {s["name"]: s["schema_selectable"] for s in scope["sheets"]} == {
            "总览": False, "table_a": True, "table_b": True}
        listed = client.get(base, headers=owner).json()
        material = next(m for g in listed["groups"] for m in g["items"] if m["path"] == rel)
        assert material["schema_scope"]["reference_sheets"] == ["总览"]
        body = {"path": rel, "purpose": "schema", "schema_selection": manual(scope, ["table_b"])}
        saved = client.patch(f"{base}/purpose", json=body, headers=owner)
        assert saved.status_code == 200, saved.text
        assert saved.json()["material"]["schema_scope"]["included_sheets"] == ["table_b"]
        assert package_mapping.read_source_tables(root) == {"table_b": ["id", "name"]}
        body["schema_selection"]["revision"] = "0" * 16
        stale = client.patch(f"{base}/purpose", json=body, headers=owner)
        assert stale.status_code == 409 and stale.json()["code"] == "schema_scope_stale"
        bad = client.patch(f"{base}/purpose", json={**body, "schema_selection": {"mode": "pick"}}, headers=owner)
        assert bad.status_code == 400 and bad.json()["code"] == "invalid_argument"
        assert client.get(f"{base}/schema-preview", params={"path": rel}, headers=other).status_code in (403, 404)
        assert client.get(base, headers=other).status_code in (403, 404)
        assert client.patch(f"{base}/purpose", json=body, headers=other).status_code in (403, 404)
        assert package_mapping.read_source_tables(root) == {"table_b": ["id", "name"]}


def test_csv_headers_work_without_excel_dependency(manager):
    pid = manager.create_project("project_alpha")["id"]
    manager.save_resource(pid, "excel", "sample.csv", b"id,name\nsample_1,Synthetic item\n", purpose="schema")
    # -S 去掉 site-packages（连同 openpyxl）：CSV 和范围判定只用标准库
    result, tables = cli_headers(manager.project_root(pid), "-S")
    assert result.returncode == 0, result.stderr
    assert tables == {"sample": ["id", "name"]}


# ---- 表头预检 ----


def test_schema_preview_explains_issues_without_changing_purpose(manager):
    pid = manager.create_project("project_alpha")["id"]

    def build(wb):
        wb.active.title = "duplicate"
        wb.active.append(["id", "ID"])
        for name, header in [("blank", ["id", None, "name"]), ("chinese", ["编号", "名称"]),
                             ("single", ["id"]), ("dictionary", ["字段名", "类型"]),
                             ("empty", [None, None]), ("valid", ["id", "name"])]:
            wb.create_sheet(name).append(header)
        # 合并的表头：被并掉的列下面有数据，文本版里就是没有列名的列
        merged = wb.create_sheet("merged")
        merged.append(["title", None])
        merged.append(["sample_1", "value"])
        merged.merge_cells("A1:B1")
    rel = save(manager, pid, "sample.xlsx", xlsx(build), purpose="evidence")["path"]
    before = manager.resource_directives(pid)
    scope = preview(manager, pid, rel)
    sheets = {s["name"]: s for s in scope["sheets"]}
    assert manager.resource_directives(pid) == before
    assert "第 1、2 列" in sheets["duplicate"]["issues"][0]["message"]
    assert "第 2 列" in sheets["blank"]["issues"][0]["message"]
    for name in ("duplicate", "blank", "merged", "empty"):
        assert not sheets[name]["schema_selectable"]
        assert name not in scope["included_sheets"]
    for name in ("chinese", "single", "dictionary"):
        assert sheets[name]["schema_selectable"]
        assert any(i["severity"] == "warning" for i in sheets[name]["issues"])
    assert sheets["valid"]["issues"] == []
    with pytest.raises(WorkspaceError, match="重复"):
        manager.set_resource_directive(pid, rel, "schema", schema_selection=manual(scope, ["duplicate"]))
    assert manager.resource_directives(pid) == before
    manager.set_resource_directive(pid, rel, "schema", schema_selection=manual(scope, ["chinese"]))
    assert manager.resource_directives(pid)[rel]["schema_selection"]["included_sheets"] == ["chinese"]


def test_schema_preview_csv_and_unreadable_files(manager, monkeypatch):
    pid = manager.create_project("project_alpha", owner=OWNER)["id"]
    rel = manager.save_resource(pid, "excel", "sample.csv", b"id,,name\n1,,sample\n", purpose="evidence")["path"]
    scope = preview(manager, pid, rel)
    assert scope["sheets"][0]["issues"][0]["code"] == "blank_columns"
    assert scope["included_sheets"] == []
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: TOKENS.get(token))
    owner = {"Authorization": "Bearer owner-token"}
    url = f"/api/v1/projects/{pid}/resources/schema-preview"
    with TestClient(app, raise_server_exceptions=False) as client:
        for filename, data, status, text in [("broken.csv", b"\xff\xfe", 400, "UTF-8"),
                                             ("legacy.xls", b"legacy", 415, "另存")]:
            rel = manager.save_resource(pid, "excel", filename, data, purpose="evidence")["path"]
            response = client.get(url, params={"path": rel}, headers=owner)
            assert response.status_code == status and text in response.json()["message"]
            assert manager.resource_directives(pid)[rel]["purpose"] == "evidence"
        # 坏的 xlsx 转不出文本版：转换还没结束时是 pending，转换失败了报 409
        rel = manager.save_resource(pid, "excel", "broken.xlsx", b"broken", purpose="evidence")["path"]
        assert client.get(url, params={"path": rel}, headers=owner).json()["pending"] is True
        from app.routers import resources
        monkeypatch.setattr(resources.material_conversion, "view", lambda *a, **k: {"state": "failed"})
        failed = client.get(url, params={"path": rel}, headers=owner)
        assert failed.status_code == 409 and failed.json()["code"] == "not_convertible"
        assert client.get(url, params={"path": "../outside.csv"}, headers=owner).status_code in (403, 404)


@pytest.mark.parametrize("suffix", ["xlsx", "csv"])
@pytest.mark.parametrize("row_number", [2, 40])
def test_schema_preview_detects_trailing_unnamed_data_column(manager, suffix, row_number):
    pid = manager.create_project("project_alpha")["id"]
    if suffix == "xlsx":
        def build(wb):
            wb.active.append(["id", None])
            wb.active.cell(row_number, 2, "synthetic value")
        data = xlsx(build)
    else:
        data = ("id,\n" + "\n" * (row_number - 2) + "sample_1,synthetic value\n").encode()
    rel = save(manager, pid, "sample." + suffix, data, purpose="evidence")["path"]
    scope = preview(manager, pid, rel)
    sheet = scope["sheets"][0]
    assert not sheet["schema_selectable"]
    assert any(i["code"] == "blank_columns" and "第 2 列" in i["message"] for i in sheet["issues"])
    assert not any(i["code"] == "single_column" for i in sheet["issues"])
    with pytest.raises(WorkspaceError, match="第 2 列"):
        manager.set_resource_directive(pid, rel, "schema", schema_selection=manual(scope, [sheet["name"]]))
    assert manager.resource_directives(pid)[rel]["purpose"] == "evidence"


def test_schema_preview_ignores_formatting_only_trailing_columns(manager):
    pid = manager.create_project("project_alpha")["id"]

    def build(wb):
        wb.active.append(["id", "name"])
        wb.active["Z40"].number_format = "0.00"
    rel = save(manager, pid, "sample.xlsx", xlsx(build), purpose="evidence")["path"]
    sheet = preview(manager, pid, rel)["sheets"][0]
    assert sheet["schema_selectable"]
    assert sheet["headers"] == ["id", "name"]
    assert sheet["issues"] == []


def test_excel_without_text_version_is_pending_not_parsed(manager, monkeypatch):
    """还没有文本版的 Excel：范围是 pending、表头为空，不去解析原件（ADR-0076）。"""
    pid = manager.create_project("project_alpha")["id"]
    rel = manager.save_resource(pid, "excel", "sample.xlsx", sample_workbook(), purpose="schema")["path"]
    monkeypatch.setitem(sys.modules, "openpyxl", None)  # 一 import 就失败
    root = manager.project_root(pid)
    assert preview(manager, pid, rel)["pending"] is True
    assert package_mapping.read_source_tables(root) == {}
    with pytest.raises(WorkspaceError) as pending:
        manager.set_resource_directive(pid, rel, "schema", schema_selection={
            "mode": "manual", "included_sheets": ["table_a"], "revision": table_sources.file_revision(root / rel)})
    assert pending.value.code == "material_text_pending"


# ---- 缓存：文本版只解析一次，返回值改了不污染缓存，原件或文本版一换就重读 ----


def test_text_parse_cached_but_not_poisoned_and_refreshed_on_change(manager, monkeypatch):
    pid, root, rel = setup_project(manager)
    rules = table_sources.rules
    original = rules.text_sheets
    calls = []

    def counted(markdown):
        calls.append(1)
        return original(markdown)

    monkeypatch.setattr(rules, "text_sheets", counted)
    rules._text_cached.cache_clear()
    scope = preview(manager, pid, rel)
    scope["sheets"][1]["headers"].append("must_not_poison_cache")
    again = preview(manager, pid, rel)
    assert len(calls) == 1
    assert "must_not_poison_cache" not in again["sheets"][1]["headers"]
    manager.set_resource_directive(pid, rel, "schema", schema_selection=manual(again, ["table_b"]))
    assert preview(manager, pid, rel)["included_sheets"] == ["table_b"]
    assert len(calls) == 1
    # 文本版重新转换（大小变了）就重读
    text =root / "resource" / "text" / "excel" / "sample.xlsx.md"
    text.write_text(text.read_text(encoding="utf-8").replace("| id | name |", "| id | code |"), encoding="utf-8")
    assert preview(manager, pid, rel)["sheets"][1]["headers"] == ["id", "code"]
    assert len(calls) == 2


def test_same_size_same_mtime_replacement_is_detected(manager):
    pid = manager.create_project("project_alpha")["id"]
    rel = manager.save_resource(pid, "excel", "sample.csv", b"id,name\n1,item\n", purpose="evidence")["path"]
    before = preview(manager, pid, rel)
    path = manager.project_root(pid) / rel
    stat = path.stat()
    path.write_bytes(b"id,code\n1,item\n")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = preview(manager, pid, rel)
    assert after["revision"] != before["revision"]
    assert after["sheets"][0]["headers"] == ["id", "code"]
    with pytest.raises(WorkspaceError, match="文件已变化"):
        manager.set_resource_directive(pid, rel, "schema", schema_selection=manual(before, ["sample"]))


# ---- 产物轻量核验（verify_artifacts 经 table_profile 读登记表）也只认 Sheet 范围 ----


def _verify_workspace(root, included, *, stale=False):
    from tests.test_artifact_verification import object_doc, put

    def build(wb):
        wb.active.title = "table_a"
        wb.active.append(["id_a", "name_a"])
        wb.create_sheet("table_b").append(["id_b", "name_b"])
    data = xlsx(build)
    table = root / "resource" / "table.xlsx"
    table.parent.mkdir(parents=True)
    table.write_bytes(data)
    write_text_versions(root)
    revision = "0" * 16 if stale else table_sources.rules.file_revision(table)
    put(root, "resource/materials.json", json.dumps({"resource/table.xlsx": {
        "purpose": "schema", "schema_selection": table_sources.manual_selection(included, revision)}}))
    return object_doc, put


def test_sheet_scope_limits_lightweight_verification(tmp_path, verifier):
    object_doc, put = _verify_workspace(tmp_path, ["table_b"])
    put(tmp_path, "ontology/object/EntityA.md", object_doc().replace("# 数据来源\n无", "# 数据来源\ntable_a"))
    result = verifier.verify(tmp_path, "objects")
    assert result["status"] == "failed"
    assert any(issue["category"] == "列名对账" and "table_a" in issue["message"] for issue in result["issues"])


def test_stale_manual_sheet_scope_cannot_pass_lightweight_verification(tmp_path, verifier):
    object_doc, put = _verify_workspace(tmp_path, ["table_b"], stale=True)
    put(tmp_path, "ontology/object/EntityA.md", object_doc().replace("# 数据来源\n无", "# 数据来源\ntable_b"))
    with pytest.raises(ValueError, match="重新确认"):
        verifier.verify(tmp_path, "objects")


def test_explicit_empty_sheet_scope_can_pass_lightweight_verification(tmp_path, verifier):
    object_doc, put = _verify_workspace(tmp_path, [])
    put(tmp_path, "ontology/object/EntityA.md", object_doc())
    assert verifier.verify(tmp_path, "objects")["status"] == "passed"
