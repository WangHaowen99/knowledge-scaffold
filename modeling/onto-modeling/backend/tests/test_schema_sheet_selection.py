"""Synthetic workbooks: shared Sheet scope for uploads, project tools and mapping."""

import io
import json
import posixpath
import subprocess
import sys
from pathlib import Path

import openpyxl
import pytest

from app import config
from app.services import eval_import, material_detect, package_mapping
from app.services.workspace import WorkspaceManager, WorkspaceError


@pytest.fixture
def manager(tmp_path):
    return WorkspaceManager(tmp_path / "projects", Path(config.ASSETS_DIR))


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


def cli_headers(root):
    script = (root / ".claude" / "scripts" / "check_eval_data.py").resolve()
    # 安全审查：解释器、项目工具脚本和参数均由测试固定；argv 不经 shell 解析，
    # 且工作目录仅为 tmp_path 下的合成项目。
    return subprocess.run(  # NOSONAR(S4721)
        [sys.executable, str(script), "--headers"],
        cwd=root,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
        timeout=60,
    )


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


def test_evaluation_excludes_reference_sheet_too(manager):
    _, root, rel = setup_project(manager)
    tables, warnings = eval_import.read_workbook(root / rel, "sample.xlsx")
    assert {t.name for t in tables} == {"table_a", "table_b"}
    assert any("总览" in w for w in warnings)


def test_real_table_named_overview_and_chinese_manual_headers(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.title = "总览"
    wb.active.append(["id", "name"])
    sheet = wb.create_sheet("table_b")
    sheet.append(["编号", "名称"])
    p = tmp_path / "sample.xlsx"; wb.save(p); wb.close()
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
    path = tmp_path / "sample.xlsx"
    wb.save(path)
    wb.close()
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
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from app.main import sheet_selection_error_handler, workspace_error_handler
    from app.routers import projects

    monkeypatch.setattr(projects, "workspaces", manager)
    pid, root, rel = setup_project(manager)
    manager._update_meta(pid, owner="operator_a", members={"reviewer_a": "viewer"})
    user = {"sub": "operator_a", "role": "user"}
    app = FastAPI()
    app.include_router(projects.router)
    app.add_exception_handler(WorkspaceError, workspace_error_handler)
    app.add_exception_handler(material_detect.SheetSelectionError, sheet_selection_error_handler)

    @app.middleware("http")
    async def synthetic_auth(request: Request, call_next):
        request.state.user = user
        return await call_next(request)

    with TestClient(app) as client:
        url = f"/api/projects/{pid}/resource-meta"
        preview_url = posixpath.join(url, "schema-preview")
        assert client.post(preview_url, json={'path': rel}).status_code == 200
        response = client.get(url)
        assert response.status_code == 200
        scope = response.json()["schema_scopes"][rel]
        body = {"path": rel, "purpose": "schema", "schema_selection": manual_request(scope, ["table_b"])}
        assert client.patch(url, json=body).status_code == 200
        assert package_mapping.read_source_tables(root) == {"table_b": ["id", "name"]}
        body["schema_selection"]["file_sha256"] = "0" * 64
        assert client.patch(url, json=body).status_code == 409
        user["sub"] = "reviewer_a"
        assert client.post(preview_url, json={'path': rel}).status_code == 403
        assert client.get(url).status_code == 200
        assert client.patch(url, json=body).status_code == 403
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
    assert json.loads(result.stdout) == {"sample": ["id", "name"]}


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
    with pytest.raises(WorkspaceError, match='文件已变化'):
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
