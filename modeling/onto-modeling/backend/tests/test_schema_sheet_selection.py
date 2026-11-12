"""Synthetic workbooks: shared Sheet scope for uploads, project tools and mapping.

移植自 develop 的同名测试，断言不变，只改了路径与导入，以及下面几处环境差异（ADR-0076、ADR-0096）：
- Excel 只读网关转出的文本版：夹具里的 manager 上传 Excel 后，用同一个转换器转出文本版、经写入路径落盘
  （真实部署里由多源连接器做，Sheet 范围在同一个提交里存下），坏文件按转换失败记录；
- 项目工具的 --headers 把表头写进 .ontology-state/table-headers.json（stdout 是摘要），cli_headers 从文件取；
- 版本号字段是 n，接口走 /api/v1（单人所有权：别人的项目一律不可见）；
- develop 的 test_evaluation_excludes_reference_sheet_too 不适用：评测导入已移出 onto（评测服务）。
"""

import io
import json
import posixpath
import subprocess
import sys
from pathlib import Path

import openpyxl
import pytest

from app import config
from app.api import deps
from app.evidence import convert
from app.services import material_detect, package_mapping
from app.services.material_conversion import service as conversion
from app.services.workspace import WorkspaceManager, WorkspaceError, _git
from app.services.workspace.layout import _content_revision
from tests.text_versions import _converter


class GatewayManager(WorkspaceManager):
    """上传 Excel 后像多源连接器那样转出文本版（同一个转换器、同一条写入路径）。"""

    def save_resource(self, project_id, subdir, filename, data, *args, **kwargs):
        saved = super().save_resource(project_id, subdir, filename, data, *args, **kwargs)
        if filename.lower().endswith((".xlsx", ".xlsm")):
            convert_text(project_id, saved["path"], data)
        return saved


def convert_text(project_id, rel, data):
    revision = _content_revision(data)
    try:
        result = _converter().convert(data, rel.rsplit("/", 1)[-1], convert.ConvertLimits())
    except convert.ConvertError:
        conversion._record(project_id, rel, revision, "failed", reason="synthetic failure")
        return
    body = convert.strip_forged_headers(result.markdown).strip("\n")
    markdown = convert.header(rel.rsplit("/", 1)[-1], revision, "convert-xlsx@test") + "\n\n" + body + "\n"
    assert conversion._write_text(project_id, rel, revision, markdown)


def write_text(path):
    """项目之外的合成工作簿：文本版直接写到它旁边的 resource/text 下。"""
    data = path.read_bytes()
    result = _converter().convert(data, path.name, convert.ConvertLimits())
    text = path.parent.parent / "text" / path.parent.name / f"{path.name}.md"
    text.parent.mkdir(parents=True, exist_ok=True)
    text.write_text(convert.header(path.name, _content_revision(data), "convert-xlsx@test") + "\n\n"
                    + result.markdown, encoding="utf-8")


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "projects")
    instance = GatewayManager(tmp_path / "projects", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", instance)
    return instance


def workbook_bytes():
    wb = openpyxl.Workbook()
    overview = wb.active
    overview.title = "总览"
    overview.append(["Synthetic workbook overview"])
    overview.merge_cells("A1:C1")
    overview.append(["Reference description"])
    overview.append(["Table", "Fields", "Description"])
    for name in ("table_a", "table_b"):
        sheet = wb.create_sheet(name)
        sheet.append(["id", "name"])
        sheet.append(["sample_1", "Synthetic item"])
    out = io.BytesIO()
    wb.save(out)
    wb.close()
    return out.getvalue()


def setup_project(manager):
    pid = manager.create_project("project_alpha")["id"]
    saved = manager.save_resource(pid, "excel", "sample.xlsx", workbook_bytes())
    return pid, manager.project_root(pid), saved["path"]


class _Cli:
    def __init__(self, result, root):
        self.returncode, self.stderr = result.returncode, result.stderr
        saved = root / ".ontology-state" / "table-headers.json"
        self.stdout = json.dumps(json.loads(saved.read_text(encoding="utf-8"))["tables"]) if result.returncode == 0 else ""


def cli_headers(root):
    script = (root / ".claude" / "scripts" / "check_eval_data.py").resolve()
    # 安全审查：解释器、项目工具脚本和参数均由测试固定；argv 不经 shell 解析，
    # 且工作目录仅为 tmp_path 下的合成项目。
    return _Cli(subprocess.run(  # NOSONAR(S4721)
        [sys.executable, str(script), "--headers"],
        cwd=root,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
        timeout=60,
    ), root)


def manual_request(scope, included):
    return {"mode": "manual", "included_sheets": included, "file_sha256": scope["file_sha256"]}


def test_upload_legacy_project_and_cli_share_scope(manager):
    pid, root, rel = setup_project(manager)
    digest = material_detect.resolve_selection(root / rel)["file_sha256"]
    directive = manager.resource_directives(pid)[rel]
    assert directive["schema_selection"]["included_sheets"] == ["table_a", "table_b"]
    assert directive["schema_selection"]["reference_sheets"] == ["总览"]
    # Simulate an existing project: file-level schema metadata only, no migration/write.
    meta = root / "resource/materials.json"
    meta.write_text(json.dumps({rel: {"purpose": "schema", "auto_detected": True}}))
    before = meta.read_bytes()
    tables = package_mapping.read_source_tables(root)
    result = cli_headers(root)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == tables == {"table_a": ["id", "name"], "table_b": ["id", "name"]}
    assert meta.read_bytes() == before
    assert material_detect.resolve_selection(root / rel)["file_sha256"] == digest
    assert manager.schema_scopes(pid)[rel]["reference_sheets"] == ["总览"]


def test_explicit_empty_and_manual_subset(manager):
    pid, root, rel = setup_project(manager)
    scope = manager.schema_scopes(pid)[rel]
    manager.set_resource_directive(pid, rel, "schema", schema_selection=manual_request(scope, []))
    assert package_mapping.read_source_tables(root) == {}
    assert json.loads(cli_headers(root).stdout) == {}
    manager.set_resource_directive(pid, rel, "schema", schema_selection=manual_request(scope, ["table_b"]))
    assert set(package_mapping.read_source_tables(root)) == {"table_b"}
    # Ordinary reading-mode changes must retain manually selected scope.
    manager.set_resource_directive(pid, rel, "schema", "full")
    assert set(package_mapping.read_source_tables(root)) == {"table_b"}


def test_sheet_scope_save_creates_project_version(manager):
    pid, root, rel = setup_project(manager)
    scope = manager.schema_scopes(pid)[rel]
    version = manager.versions(pid)[0]["n"]

    saved = manager.set_resource_directive(
        pid,
        rel,
        "schema",
        schema_selection=manual_request(scope, ["table_b"]),
    )

    assert saved["commit"]
    assert manager.versions(pid)[0]["n"] == version + 1
    assert _git(root, "show", "--pretty=format:", "--name-only", "HEAD").strip() == (
        "resource/materials.json"
    )
    assert _git(root, "status", "--short", "--", "resource/materials.json") == ""
    assert set(package_mapping.read_source_tables(root)) == {"table_b"}


def test_replacing_workbook_versions_file_and_matching_scope(manager):
    pid, root, rel = setup_project(manager)
    before = manager.versions(pid)[0]["n"]
    wb = openpyxl.load_workbook(io.BytesIO(workbook_bytes()))
    wb["table_b"].title = "table_c"
    out = io.BytesIO()
    wb.save(out)
    wb.close()

    saved = manager.save_resource(
        pid, "excel", "sample.xlsx", out.getvalue(), purpose="schema"
    )

    committed_file = subprocess.run(  # NOSONAR(S4721)
        ["git", "show", f"HEAD:{rel}"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    committed_directive = json.loads(
        _git(root, "show", "HEAD:resource/materials.json")
    )[rel]
    assert saved["commit"]
    assert saved["replaced"] is True
    versions = manager.versions(pid)
    assert versions[1]["commit"] == saved["commit"] and versions[1]["n"] == before + 1
    assert committed_file == out.getvalue()
    assert committed_directive["schema_selection"]["file_sha256"] == (
        material_detect.resolve_selection(root / rel)["file_sha256"]
    )


def test_replacement_invalidates_manual_scope_without_losing_file(manager):
    pid, root, rel = setup_project(manager)
    scope = manager.schema_scopes(pid)[rel]
    manager.set_resource_directive(pid, rel, "schema", schema_selection=manual_request(scope, ["table_b"]))
    wb = openpyxl.load_workbook(io.BytesIO(workbook_bytes()))
    wb["table_b"].title = "table_c"
    out = io.BytesIO(); wb.save(out); wb.close()
    manager.save_resource(pid, "excel", "sample.xlsx", out.getvalue())
    with pytest.raises(material_detect.SheetSelectionError, match="重新确认"):
        package_mapping.read_source_tables(root)
    assert cli_headers(root).returncode == 2
    assert "error" in manager.schema_scopes(pid)[rel]
    with pytest.raises(WorkspaceError, match="文件已变化"):
        manager.set_resource_directive(pid, rel, "schema", schema_selection=manual_request(scope, []))
    fresh = manager.schema_scopes(pid)[rel]
    manager.set_resource_directive(pid, rel, "schema", schema_selection=manual_request(fresh, ["table_c"]))
    assert set(package_mapping.read_source_tables(root)) == {"table_c"}


def test_auto_scope_refresh_and_invalid_selection_rollback(manager):
    pid, root, rel = setup_project(manager)
    scope = manager.schema_scopes(pid)[rel]
    old = (root / "resource/materials.json").read_bytes()
    with pytest.raises(material_detect.SheetSelectionError):
        manager.set_resource_directive(pid, rel, "schema", schema_selection=manual_request(scope, ["missing"]))
    assert (root / "resource/materials.json").read_bytes() == old
    with pytest.raises(material_detect.SheetSelectionError):
        manager.set_resource_directive(pid, rel, "schema", schema_selection=manual_request(scope, ["总览"]))
    wb = openpyxl.load_workbook(io.BytesIO(workbook_bytes()))
    wb["table_b"].title = "table_c"
    out = io.BytesIO(); wb.save(out); wb.close()
    manager.save_resource(pid, "excel", "sample.xlsx", out.getvalue())
    assert set(package_mapping.read_source_tables(root)) == {"table_a", "table_c"}


def test_real_table_named_overview_and_chinese_manual_headers(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.title = "总览"
    wb.active.append(["id", "name"])
    sheet = wb.create_sheet("table_b")
    sheet.append(["编号", "名称"])
    p = tmp_path / "resource" / "excel" / "sample.xlsx"; p.parent.mkdir(parents=True); wb.save(p); wb.close()
    write_text(p)
    scope = material_detect.resolve_selection(p, {"purpose": "schema"})
    assert scope["included_sheets"] == ["总览", "table_b"]
    assert material_detect.classify_sheet("总览", [["id", "name"]]) == "table"


def test_manual_schema_tag_preserves_chinese_headers_after_save(manager):
    pid = manager.create_project("project_alpha")["id"]
    wb = openpyxl.Workbook()
    wb.active.title = "table_a"
    wb.active.append(["编号", "名称"])
    out = io.BytesIO()
    wb.save(out)
    wb.close()
    rel = manager.save_resource(pid, "excel", "sample.xlsx", out.getvalue(), purpose="schema")["path"]
    root = manager.project_root(pid)
    assert package_mapping.read_source_tables(root) == {"table_a": ["编号", "名称"]}
    manager.set_resource_directive(pid, rel, "schema", "full")
    assert json.loads(cli_headers(root).stdout) == {"table_a": ["编号", "名称"]}


def test_data_merges_do_not_exclude_sheet_and_blank_first_row_is_not_shifted(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.title = "table_a"
    wb.active.append(["id", "name"])
    wb.active.append(["sample_1", "value"])
    wb.active.merge_cells("A2:A3")
    sheet = wb.create_sheet("table_b")
    sheet.append([None, None])
    sheet.append(["id", "name"])
    path = tmp_path / "resource" / "excel" / "sample.xlsx"
    path.parent.mkdir(parents=True)
    wb.save(path)
    wb.close()
    write_text(path)
    assert material_detect.read_selected_headers(path) == {"table_a": ["id", "name"]}


@pytest.mark.parametrize("purpose", ["auto", "schema"])
def test_explicit_upload_does_not_discard_manual_scope(manager, purpose):
    pid, root, rel = setup_project(manager)
    scope = manager.schema_scopes(pid)[rel]
    manager.set_resource_directive(pid, rel, "schema", schema_selection=manual_request(scope, []))
    manager.save_resource(pid, "excel", "sample.xlsx", (root / rel).read_bytes(), purpose=purpose)
    assert manager.resource_directives(pid)[rel]["schema_selection"]["mode"] == "manual"
    assert package_mapping.read_source_tables(root) == {}


def test_resource_scope_api_permissions_and_stale_request(manager, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services import auth

    pid, root, rel = setup_project(manager)
    manager._update_meta(pid, owner="operator_a")
    tokens = {"owner": {"sub": "operator_a", "role": "user"}, "other": {"sub": "reviewer_a", "role": "user"}}
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: tokens.get(token))
    user = {"Authorization": "Bearer owner"}

    with TestClient(app, raise_server_exceptions=False) as client:
        url = f"/api/v1/projects/{pid}/resources"
        preview_url = posixpath.join(url, "schema-preview")
        assert client.get(preview_url, params={'path': rel}, headers=user).status_code == 200
        response = client.get(url, headers=user)
        assert response.status_code == 200
        scope = next(m for g in response.json()["groups"] for m in g["items"] if m["path"] == rel)["schema_scope"]
        body = {"path": rel, "purpose": "schema", "schema_selection": manual_request(scope, ["table_b"])}
        assert client.patch(posixpath.join(url, "purpose"), json=body, headers=user).status_code == 200
        assert package_mapping.read_source_tables(root) == {"table_b": ["id", "name"]}
        body["schema_selection"]["file_sha256"] = "0" * 64
        assert client.patch(posixpath.join(url, "purpose"), json=body, headers=user).status_code == 409
        user = {"Authorization": "Bearer other"}
        assert client.get(preview_url, params={'path': rel}, headers=user).status_code in (403, 404)
        assert client.get(url, headers=user).status_code in (403, 404)
        assert client.patch(posixpath.join(url, "purpose"), json=body, headers=user).status_code in (403, 404)
        assert package_mapping.read_source_tables(root) == {"table_b": ["id", "name"]}


def test_csv_headers_work_without_excel_dependency(tmp_path):
    script = Path(config.ASSETS_DIR) / "workspace-toolkit/scripts/check_eval_data.py"
    resource = tmp_path / "resource"
    resource.mkdir()
    (resource / "sample.csv").write_text("id,name\nsample_1,Synthetic item\n")
    (resource / "materials.json").write_text(json.dumps({"resource/sample.csv": {"purpose": "schema"}}))
    # -S removes site-packages, including openpyxl, from this subprocess.
    # 安全审查：解释器、仓库内工具脚本和参数均由测试固定；argv 不经 shell
    # 解析，且工作目录仅为 tmp_path 下的合成项目。
    result = subprocess.run(  # NOSONAR(S4721)
        [sys.executable, "-S", str(script.resolve()), "--headers"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    saved = json.loads((tmp_path / ".ontology-state" / "table-headers.json").read_text(encoding="utf-8"))
    assert saved["tables"] == {"sample": ["id", "name"]}


def test_schema_preview_explains_issues_without_changing_purpose(manager):
    pid = manager.create_project('project_alpha')['id']
    wb = openpyxl.Workbook()
    wb.active.title = 'duplicate'
    wb.active.append(['id', 'ID'])
    for name, header in [('blank', ['id', None, 'name']), ('chinese', ['编号', '名称']),
                         ('single', ['id']), ('dictionary', ['字段名', '类型']),
                         ('empty', [None, None]), ('valid', ['id', 'name'])]:
        wb.create_sheet(name).append(header)
    wb.create_sheet('merged').append(['title', None])
    wb['merged'].merge_cells('A1:B1')
    out = io.BytesIO(); wb.save(out); wb.close()
    rel = manager.save_resource(pid, 'excel', 'sample.xlsx', out.getvalue(), purpose='evidence')['path']
    before = manager.resource_directives(pid)
    scope = manager.preview_schema(pid, rel)
    sheets = {s['name']: s for s in scope['sheets']}
    assert manager.resource_directives(pid) == before
    assert '第 1、2 列' in sheets['duplicate']['issues'][0]['message']
    assert '第 2 列' in sheets['blank']['issues'][0]['message']
    for name in ('duplicate', 'blank', 'merged', 'empty'):
        assert not sheets[name]['schema_selectable']
        assert name not in scope['included_sheets']
    for name in ('chinese', 'single', 'dictionary'):
        assert sheets[name]['schema_selectable']
        assert any(i['severity'] == 'warning' for i in sheets[name]['issues'])
    assert sheets['valid']['issues'] == []
    with pytest.raises(WorkspaceError, match='重复'):
        manager.set_resource_directive(pid, rel, 'schema', schema_selection=manual_request(scope, ['duplicate']))
    assert manager.resource_directives(pid) == before
    manager.set_resource_directive(pid, rel, 'schema', schema_selection=manual_request(scope, ['chinese']))
    assert manager.resource_directives(pid)[rel]['schema_selection']['included_sheets'] == ['chinese']


def test_schema_preview_csv_and_unreadable_files(manager):
    pid = manager.create_project('project_alpha')['id']
    rel = manager.save_resource(pid, 'excel', 'sample.csv', b'id,,name\n1,,sample\n', purpose='evidence')['path']
    scope = manager.preview_schema(pid, rel)
    assert scope['sheets'][0]['issues'][0]['code'] == 'blank_columns'
    assert scope['included_sheets'] == []
    for filename, data in [('broken.xlsx', b'broken'), ('broken.csv', b'\xff\xfe'), ('legacy.xls', b'legacy')]:
        rel = manager.save_resource(pid, 'excel', filename, data, purpose='evidence')['path']
        with pytest.raises(WorkspaceError, match='另存|UTF-8'):
            manager.preview_schema(pid, rel)
        assert manager.resource_directives(pid)[rel]['purpose'] == 'evidence'
    with pytest.raises(WorkspaceError):
        manager.preview_schema(pid, '../outside.csv')


@pytest.mark.parametrize('suffix', ['xlsx', 'csv'])
@pytest.mark.parametrize('row_number', [2, 40])
def test_schema_preview_detects_trailing_unnamed_data_column(manager, suffix, row_number):
    pid = manager.create_project('project_alpha')['id']
    if suffix == 'xlsx':
        wb = openpyxl.Workbook()
        wb.active.append(['id', None])
        wb.active.cell(row_number, 2, 'synthetic value')
        out = io.BytesIO(); wb.save(out); wb.close()
        data = out.getvalue()
    else:
        data = ('id,\n' + '\n' * (row_number - 2) + 'sample_1,synthetic value\n').encode()
    rel = manager.save_resource(pid, 'excel', 'sample.' + suffix, data, purpose='evidence')['path']
    scope = manager.preview_schema(pid, rel)
    sheet = scope['sheets'][0]
    assert not sheet['schema_selectable']
    assert any(i['code'] == 'blank_columns' and '第 2 列' in i['message'] for i in sheet['issues'])
    assert not any(i['code'] == 'single_column' for i in sheet['issues'])
    with pytest.raises(WorkspaceError, match='第 2 列'):
        manager.set_resource_directive(pid, rel, 'schema', schema_selection=manual_request(scope, [sheet['name']]))
    assert manager.resource_directives(pid)[rel]['purpose'] == 'evidence'


def test_schema_preview_ignores_formatting_only_trailing_columns(manager):
    pid = manager.create_project('project_alpha')['id']
    wb = openpyxl.Workbook()
    wb.active.append(['id', 'name'])
    wb.active['Z40'].number_format = '0.00'
    out = io.BytesIO(); wb.save(out); wb.close()
    rel = manager.save_resource(pid, 'excel', 'sample.xlsx', out.getvalue(), purpose='evidence')['path']
    sheet = manager.preview_schema(pid, rel)['sheets'][0]
    assert sheet['schema_selectable']
    assert sheet['headers'] == ['id', 'name']
    assert sheet['issues'] == []


def test_schema_inspection_reused_for_preview_and_save_but_force_rescans(manager, monkeypatch):
    pid = manager.create_project('project_alpha')['id']
    rel = manager.save_resource(pid, 'excel', 'sample.xlsx', workbook_bytes(), purpose='evidence')['path']
    rules = material_detect._rules
    original = rules.inspect_sheets
    calls = []

    def counted(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(rules, 'inspect_sheets', counted)
    scope = manager.preview_schema(pid, rel)
    assert len(calls) == 1
    scope['sheets'][1]['headers'].append('must_not_poison_cache')
    again = manager.preview_schema(pid, rel)
    assert len(calls) == 1
    assert 'must_not_poison_cache' not in again['sheets'][1]['headers']
    manager.set_resource_directive(pid, rel, 'schema', schema_selection=manual_request(again, ['table_b']))
    assert len(calls) == 1
    assert manager.preview_schema(pid, rel)['included_sheets'] == ['table_b']
    assert len(calls) == 1
    manager.preview_schema(pid, rel, force=True)
    assert len(calls) == 2
    with pytest.raises(material_detect.SheetSelectionError):
        manager.set_resource_directive(pid, rel, 'schema', schema_selection=manual_request(again, ['missing']))
    assert len(calls) == 2


def test_schema_cache_detects_same_size_same_mtime_replacement(manager):
    import os
    pid = manager.create_project('project_alpha')['id']
    rel = manager.save_resource(pid, 'excel', 'sample.csv', b'id,name\n1,item\n', purpose='evidence')['path']
    before = manager.preview_schema(pid, rel)
    path = manager.project_root(pid) / rel
    stat = path.stat()
    path.write_bytes(b'id,code\n1,item\n')
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = manager.preview_schema(pid, rel)
    assert after['file_sha256'] != before['file_sha256']
    assert after['sheets'][0]['headers'] == ['id', 'code']
    with pytest.raises(WorkspaceError, match='尚未保存为项目版本'):
        manager.set_resource_directive(pid, rel, 'schema', schema_selection=manual_request(before, ['sample']))


def test_schema_inspection_changed_mid_scan_is_not_cached(tmp_path, monkeypatch):
    path = tmp_path / 'sample.csv'
    path.write_text('id,name\n')
    rules = material_detect._rules
    original = rules.inspect_sheets

    def changing(path):
        result = original(path)
        path.write_text('id,code\n')
        return result

    monkeypatch.setattr(rules, 'inspect_sheets', changing)
    with pytest.raises(material_detect.SheetSelectionError, match='检查期间'):
        material_detect.resolve_selection(path)
    monkeypatch.setattr(rules, 'inspect_sheets', original)
    assert material_detect.resolve_selection(path)['sheets'][0]['headers'] == ['id', 'code']
