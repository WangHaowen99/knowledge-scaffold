"""人工同步用的对照测试：同一批合成包分别过规则副本与真实 consumer 上游，结论必须一致。

只在设置 CONSUMER_UPSTREAM_ROOT（上游 common 分支导出目录，含 consumer/ 与 plugins/）时运行，
单独执行本文件，见 app/services/consumer_package_rules/PROVENANCE.md。
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from app.services import consumer_package_rules, hub_packages
from tests import test_consumer_package_rules as cases
from tests import test_solver_contract_check as solver_fixtures

UPSTREAM = os.environ.get('CONSUMER_UPSTREAM_ROOT', '')
pytestmark = pytest.mark.skipif(not UPSTREAM, reason='set CONSUMER_UPSTREAM_ROOT to an upstream export to compare')
RULES_DIR = Path(consumer_package_rules.__file__).parent


@pytest.fixture()
def upstream(monkeypatch, tmp_path):
    root = Path(UPSTREAM)
    monkeypatch.syspath_prepend(str(root / 'plugins' / 'solver' / 'backend'))
    monkeypatch.syspath_prepend(str(root / 'consumer' / 'backend'))
    monkeypatch.delenv('CONSUMER_PLUGINS', raising=False)
    monkeypatch.chdir(tmp_path)  # 上游模块导入时会在当前目录建日志目录
    from api.routes import ontologies as routes
    from ontology.ontologies import is_valid_ontology_version
    from ontology.package_import import normalize_imported_package
    from ontology.package_validation import validate_import_rules

    def rejects(archive: Path, workdir: Path) -> bool:
        """按上游导入接口的顺序判断：导入被拒，或求解操作被准入判为不可用。"""
        data = archive.read_bytes()
        if not data or len(data) > routes.ONTOLOGY_MAX_ZIP_BYTES:
            return True
        extract = workdir / 'unzipped'
        extract.mkdir()
        if routes._safe_extract_zip(data, extract):
            return True
        try:
            found = routes._find_converted_ontology_root(extract)
        except routes.OntologyImportError:
            return True
        if found is None or not routes._is_valid_ontology_id(found.ontology_id):
            return True
        if found.version and not is_valid_ontology_version(found.version):
            return True
        try:
            normalized = normalize_imported_package(found.path, workdir / 'normalized')
        except ValueError:
            return True
        if validate_import_rules(normalized.root, normalized.snapshot).report.errors:
            return True
        return bool(normalized.admission.unavailable_operations())

    return rejects


def test_copies_match_recorded_upstream_sources():
    manifest = json.loads((RULES_DIR / 'upstream.json').read_text(encoding='utf-8'))
    changed = [name for name, item in manifest['files'].items()
               if hashlib.sha256((Path(UPSTREAM) / item['source']).read_bytes()).hexdigest() != item['source_sha256']]
    assert not changed, f'上游源文件已变化，请重新运行同步脚本：{changed}'


def _archives(manager, tmp_path) -> dict[str, Path]:
    def export(name, pid):
        return manager.export_package_zip(pid, tmp_path / f'{name}.zip', allow_unbound=True,
                                          ontology_id=hub_packages.ontology_id(pid))
    archives = {'plain': export('plain', cases._plain(manager))}
    for name, (build, _code) in cases.REJECTED.items():
        archives[name] = export(name, cases._plain(manager, build))
    archives['solver_contract'] = export('solver_contract', solver_fixtures._with_contract(manager)[0])
    pid, root = solver_fixtures._with_contract(manager)
    for index in range(501):
        (root / f'ontology/operation/合成操作{index}.md').write_text(f'# 合成操作{index}\n', encoding='utf-8')
    archives['solver_over_operation_limit'] = export('solver_over_operation_limit', pid)
    solver, prefix = cases._solver_archive(manager, tmp_path)
    for index, tamper in enumerate(cases.SOLVER_TAMPERS):
        target = tmp_path / f'tampered_{index}.zip'
        cases._rewrite(solver, lambda files: tamper(files, prefix)).rename(target)
        archives[f'solver_tampered_{index}'] = target
    for index, (entries, symlink, _code) in enumerate(cases.ARCHIVE_CASES):
        archives[f'archive_{index}'] = cases._zip(tmp_path / f'archive_{index}.zip', entries, symlink)
    return archives


def test_copy_and_upstream_agree(upstream, tmp_path):
    manager = cases.new_manager(tmp_path)
    mismatched = []
    for name, archive in _archives(manager, tmp_path).items():
        workdir = tmp_path / f'upstream_{name}'
        workdir.mkdir()
        copy = bool(consumer_package_rules.check_package_zip(archive))
        if copy != upstream(archive, workdir):
            mismatched.append(f'{name}: 副本{"拒绝" if copy else "通过"}，上游相反')
    assert not mismatched, mismatched
