"""发布前结构自检：建模导出的包按 consumer 导入规则（副本）检查。全部为合成数据。"""
import io
import json
import zipfile
from pathlib import Path

import pytest

from app.services import consumer_package_rules, hub_packages, package_mapping
from app.services.workspace import WorkspaceManager
from tests import test_solver_contract_check as solver_fixtures

HEAD = '| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |\n| --- | --- | --- | --- | --- |\n'
ROW = '| 标识 | item_id | 合成字段 | 是 | 字符串 |\n'
RULES_DIR = Path(consumer_package_rules.__file__).parent


def _object(rows: str = ROW) -> str:
    return '# 对象描述\n合成示例。\n\n# 数据来源\n无\n\n# 对象属性清单\n' + HEAD + rows


def new_manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / 'assets'
    (assets / 'workspace-toolkit' / 'templates').mkdir(parents=True)
    (assets / 'workspace-rules.md').write_text('# rules', encoding='utf-8')
    (assets / 'resource-template' / 'others').mkdir(parents=True)
    return WorkspaceManager(tmp_path / 'data', assets)


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    return new_manager(tmp_path)


def _plain(manager: WorkspaceManager, build=None) -> str:
    pid = manager.create_project('结构自检合成项目')['id']
    root = manager.project_root(pid)
    (root / 'ontology/object/Item.md').write_text(_object(), encoding='utf-8')
    (root / 'ontology/operation/查询项.md').write_text('# 查询项\n\n查询[[Item]]\n', encoding='utf-8')
    if build:
        build(root)
    return pid


def _findings(manager: WorkspaceManager, pid: str) -> list[dict]:
    binding = package_mapping.resolve_object_tables(manager.project_root(pid), strict_declared=False)
    return manager.package_structure_findings(
        pid, binding=binding, allow_unbound=True, ontology_id=hub_packages.ontology_id(pid))


def _codes(findings: list[dict]) -> set[str]:
    return {item['code'] for item in findings}


def _many_objects(root):
    for index in range(201):
        (root / f'ontology/object/O{index}.md').write_text(_object(), encoding='utf-8')


def _rows(count):
    return ''.join(f'| 字段{i} | f{i} | 合成 | 否 | 字符串 |\n' for i in range(count))


ALIAS_TABLE = ('# 对象描述\n合成。\n\n# 对象属性清单\n| 中文名称 | 英文名称 | 属性别名 | 主键 | 类型 |\n'
               '| --- | --- | --- | --- | --- |\n| 标识 | item_id | 编号 | 是 | 字符串 |\n'
               '| 编码 | code_id | 编号 | 否 | 字符串 |\n')

REJECTED = {
    'over_object_limit': (_many_objects, 'package_limit_exceeded'),
    'over_attribute_limit': (
        lambda r: (r / 'ontology/object/Item.md').write_text(_object(_rows(101)), encoding='utf-8'),
        'package_limit_exceeded'),
    'over_operation_size': (
        lambda r: (r / 'ontology/operation/查询项.md').write_text('# 查询项\n\n查询[[Item]]\n' + '合成' * 7600,
                                                                encoding='utf-8'),
        'package_limit_exceeded'),
    'duplicate_field_key': (
        lambda r: (r / 'ontology/object/Item.md').write_text(
            _object('| 标识 | item_id | 合成 | 是 | 字符串 |\n| 编号 | item_id | 合成 | 否 | 字符串 |\n'), encoding='utf-8'),
        'field_aliases_invalid'),
    'field_name_two_fields': (
        lambda r: (r / 'ontology/object/Item.md').write_text(
            _object('| 标识 | item_id | 合成 | 是 | 字符串 |\n| 标识 | other_id | 合成 | 否 | 字符串 |\n'), encoding='utf-8'),
        'field_aliases_invalid'),
    'field_alias_two_fields': (
        lambda r: (r / 'ontology/object/Item.md').write_text(ALIAS_TABLE, encoding='utf-8'), 'field_aliases_invalid'),
    'object_keys_differ_by_case': (
        lambda r: (r / 'ontology/object/ITEM.md').write_text(_object(), encoding='utf-8'), 'package_archive_invalid'),
    'object_key_with_pipe': (
        lambda r: (r / 'ontology/object/A｜B|C.md').write_text(_object(), encoding='utf-8'), 'object_aliases_invalid'),
}


def test_plain_export_is_importable(manager):
    assert _findings(manager, _plain(manager)) == []


@pytest.mark.parametrize('case', sorted(REJECTED))
def test_consumer_rejections_are_reported(manager, case):
    build, code = REJECTED[case]
    findings = _findings(manager, _plain(manager, build))
    assert code in _codes(findings)
    assert all(len(item['message']) <= consumer_package_rules.MESSAGE_LIMIT for item in findings)


@pytest.mark.parametrize('build', [
    lambda m: solver_fixtures._with_contract(m)[0],
    lambda m: (lambda pid: m.save_solver_contract(pid, [solver_fixtures._template_item()], '') and pid)(
        solver_fixtures._project(m)[0]),
], ids=['contract', 'template_contract'])
def test_valid_solver_packages_pass(manager, build):
    assert _findings(manager, build(manager)) == []


def test_solver_package_over_operation_limit_is_reported(manager):
    pid, root = solver_fixtures._with_contract(manager)
    for index in range(101):
        (root / f'ontology/operation/合成操作{index}.md').write_text(f'# 合成操作{index}\n', encoding='utf-8')
    assert 'package_revision_invalid' in _codes(_findings(manager, pid))


def test_object_limit_follows_environment(manager, monkeypatch):
    pid = _plain(manager, lambda r: (r / 'ontology/object/Other.md').write_text(_object(), encoding='utf-8'))
    monkeypatch.setenv('ONTOLOGY_OBJECT_MAX_COUNT', '1')
    assert _codes(_findings(manager, pid)) == {'package_limit_exceeded'}
    monkeypatch.setenv('ONTOLOGY_OBJECT_MAX_COUNT', 'not-a-number')
    assert _findings(manager, pid) == []


# ---- 求解布局：篡改合法导出包，确认副本的布局规则生效 ----


def _rewrite(archive: Path, change) -> Path:
    with zipfile.ZipFile(archive) as source:
        files = {info.filename: source.read(info) for info in source.infolist() if not info.is_dir()}
    change(files)
    out = archive.with_name('tampered.zip')
    with zipfile.ZipFile(out, 'w') as target:
        for name, data in files.items():
            target.writestr(name, data)
    return out


def _solver_archive(manager, tmp_path) -> tuple[Path, str]:
    pid, _root = solver_fixtures._with_contract(manager)
    archive = manager.export_package_zip(pid, tmp_path / 'solver.zip', ontology_id=hub_packages.ontology_id(pid))
    return archive, hub_packages.ontology_id(pid)


def _without_solver_index_block(files, prefix):
    text = files[f'{prefix}/index.md'].decode('utf-8')
    files[f'{prefix}/index.md'] = text.split('<!--')[0].encode('utf-8')


SOLVER_TAMPERS = (
    lambda files, prefix: files.pop(f'{prefix}/operations/index.md'),
    _without_solver_index_block,
    lambda files, prefix: files.__setitem__(f'{prefix}/schemas/extra.json', b'{}'),
    lambda files, prefix: files.pop(f'{prefix}/operations/plan_route.fact-snapshot.schema.json'),
)


@pytest.mark.parametrize('tamper', SOLVER_TAMPERS, ids=[
    'operations_index_removed', 'root_index_block_removed', 'legacy_sidecar_directory', 'fact_sidecar_removed'])
def test_solver_layout_problems_are_reported(manager, tmp_path, tamper):
    archive, prefix = _solver_archive(manager, tmp_path)
    assert consumer_package_rules.check_package_zip(archive) == []
    tampered = _rewrite(archive, lambda files: tamper(files, prefix))
    assert 'solver_layout_invalid' in _codes(consumer_package_rules.check_package_zip(tampered))


# ---- 压缩包本身 ----


def _zip(path: Path, entries: dict[str, bytes], symlink: str = '') -> Path:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16
            archive.writestr(info, 'target')
    path.write_bytes(buffer.getvalue())
    return path


MANIFEST = b'kind: ontology-package\nschema_version: "0.1"\nid: pkg_demo\nname: demo\ndescription: synthetic\nversion: 0.1.0\n'


ARCHIVE_CASES = (
    ({'../escape.md': b'x'}, '', 'package_archive_invalid'),
    ({'pkg_demo/manifest.yaml': MANIFEST}, 'pkg_demo/link.md', 'package_archive_invalid'),
    ({'a/manifest.yaml': MANIFEST.replace(b'pkg_demo', b'a'), 'a/index.md': b'# a\n',
      'b/manifest.yaml': MANIFEST.replace(b'pkg_demo', b'b'), 'b/index.md': b'# b\n'}, '', 'package_manifest_invalid'),
    ({'pkg_demo/readme.txt': b'no manifest'}, '', 'package_manifest_invalid'),
    ({'pkg_demo/manifest.yaml': MANIFEST.replace(b'kind: ontology-package', b'kind: ontology'),
      'pkg_demo/index.md': b'# demo\n'}, '', 'package_manifest_invalid'),
)


@pytest.mark.parametrize('entries,symlink,expected', ARCHIVE_CASES,
                         ids=['path_escape', 'symlink', 'two_package_roots', 'no_manifest', 'legacy_kind'])
def test_archive_level_rejections(tmp_path, entries, symlink, expected):
    archive = _zip(tmp_path / 'synthetic.zip', entries, symlink)
    assert _codes(consumer_package_rules.check_package_zip(archive)) == {expected}


def test_empty_and_oversized_archives(tmp_path, monkeypatch):
    empty = tmp_path / 'empty.zip'
    empty.write_bytes(b'')
    assert _codes(consumer_package_rules.check_package_zip(empty)) == {'package_archive_invalid'}
    archive = _zip(tmp_path / 'small.zip', {'pkg_demo/manifest.yaml': MANIFEST, 'pkg_demo/index.md': b'# demo\n'})
    monkeypatch.setattr(consumer_package_rules, 'ONTOLOGY_MAX_ZIP_BYTES', archive.stat().st_size - 1)
    assert consumer_package_rules.check_package_zip(archive)[0]['message'] == 'Zip file too large'


# ---- 发布检查接口 ----


def test_binding_check_reports_structure(manager):
    pid = _plain(manager)
    assert manager.package_binding_check(pid)['structure'] == {
        'blocking': 0, 'findings': [], 'truncated': False, 'skipped': None}
    (manager.project_root(pid) / 'ontology/object/ITEM.md').write_text(_object(), encoding='utf-8')
    structure = manager.package_binding_check(pid)['structure']
    assert structure['blocking'] == 1 and structure['findings'][0]['code'] == 'package_archive_invalid'
    # 结构问题单列，不计入数据绑定的 blocking
    assert manager.package_binding_check(pid)['blocking'] == 0


def test_structure_check_defers_to_solver_findings(manager):
    pid, root = solver_fixtures._with_contract(manager)
    (root / 'ontology/solver/plan_route/input.schema.json').write_text('{"type": "string"}\n', encoding='utf-8')
    check = manager.package_binding_check(pid)
    assert check['solver']['findings']
    assert check['structure']['skipped'] == 'solver_findings' and check['structure']['blocking'] == 0


# ---- 副本与来源清单 ----


def test_copies_import_no_upstream_module():
    for path in RULES_DIR.glob('*.py'):
        text = path.read_text(encoding='utf-8')
        for package in ('ontology', 'config', 'plugin_api', 'solver_coordination', 'api', 'agent', 'integration'):
            assert f'\nfrom {package}.' not in text and f'\nfrom {package} import' not in text, path.name
            assert f'    from {package}.' not in text and f'\nimport {package}' not in text, path.name


def test_provenance_lists_every_generated_copy():
    manifest = json.loads((RULES_DIR / 'upstream.json').read_text(encoding='utf-8'))
    generated = {path.name for path in RULES_DIR.glob('*.py')} - {'__init__.py', 'limits.py'}
    assert set(manifest['files']) == generated
    assert manifest['upstream_commit']
    provenance = (RULES_DIR / 'PROVENANCE.md').read_text(encoding='utf-8')
    assert all(f'`{name}`' in provenance for name in generated)
