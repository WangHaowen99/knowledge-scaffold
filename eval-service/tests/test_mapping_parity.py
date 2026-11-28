"""The evaluation copy of the mapping rules derives the same relations as onto-modeling. Synthetic objects only.

Skipped when the modeling source is not beside eval-service, as in a standalone evaluation package.
"""
import importlib.util
from pathlib import Path
import sys
import types

import pytest

from evaluation import mapping_rules

ONTO = Path(__file__).resolve().parents[2] / 'modeling' / 'onto-modeling'
MODELING = ONTO / 'backend' / 'app' / 'services' / 'package_mapping.py'
# Link parsing and name resolution in the relation rules come from the metamodel; onto-modeling serves its
# image copy (the toolkit script) unless a project pins another one, and that copy needs only the standard library.
METAMODEL = ONTO / 'assets' / 'workspace-toolkit' / 'scripts' / 'onto_metamodel.py'


def _package(name):
    package = types.ModuleType(name)
    package.__path__ = []
    return package


@pytest.fixture(scope='module')
def modeling():
    if not MODELING.is_file() or not METAMODEL.is_file():
        pytest.skip('onto-modeling source is not available beside eval-service')
    # Other relative imports need the whole modeling app; the relation rules use only material_detect's name and the metamodel.
    metamodel_spec = importlib.util.spec_from_file_location('modeling_onto_metamodel', METAMODEL)
    onto_metamodel = importlib.util.module_from_spec(metamodel_spec)
    modules = {name: _package(name) for name in ('modeling_app', 'modeling_app.services', 'modeling_app.kernel')}
    modules['modeling_onto_metamodel'] = onto_metamodel  # its dataclasses look the module up while loading
    modules['modeling_app.services.material_detect'] = types.ModuleType('modeling_app.services.material_detect')
    kernel_metamodel = types.ModuleType('modeling_app.kernel.metamodel')
    kernel_metamodel.current = lambda: onto_metamodel
    modules['modeling_app.kernel.metamodel'] = kernel_metamodel
    modules['modeling_app.services'].material_detect = modules['modeling_app.services.material_detect']
    modules['modeling_app.kernel'].metamodel = kernel_metamodel
    spec = importlib.util.spec_from_file_location('modeling_app.services.package_mapping', MODELING)
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        for name, value in modules.items():
            patch.setitem(sys.modules, name, value)
        metamodel_spec.loader.exec_module(onto_metamodel)
        spec.loader.exec_module(module)
    return module


def employee(lines):
    return {'employee': (
        '# 对象属性清单\n| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |\n| --- | --- | --- | --- | --- |\n'
        '| 员工编号 | employee_id | 合成 | 是 | 字符串 |\n| 上级编号 | manager_id | 合成 | 否 | 字符串 |\n'
        '| 部门编号 | department_id | 合成 | 否 | 字符串 |\n'
        '\n# 关联对象\n' + lines),
        'department': ('# 对象属性清单\n| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |\n| --- | --- | --- | --- | --- |\n'
                       '| 部门编号 | department_id | 合成 | 是 | 字符串 |\n'
                       '\n# 关联对象\n- [[employee]] → belongs_to（department_id → department_id） → 当前对象\n')}


@pytest.mark.parametrize('lines', [
    # Self relations with different join fields are two relations.
    '- 当前对象 → references（manager_id → employee_id） → 当前对象\n'
    '- 当前对象 → references（employee_id → manager_id） → 当前对象\n',
    # The same relation declared on both ends with the same fields merges into one.
    '- 当前对象 → belongs_to（department_id → department_id） → [[department]]\n',
    # A relation written without join fields is kept apart from one with fields.
    '- 当前对象 → belongs_to → [[department]]\n',
    # Half-declared join fields are reported, not guessed.
    '- 当前对象 → reports_to（manager_id → ） → 当前对象\n',
])
def test_graph_relations_match_modeling(modeling, lines):
    texts = employee(lines)
    mapped = set(texts)
    assert mapping_rules.graph_relations(texts, mapped) == modeling.graph_relations(texts, mapped)


@pytest.mark.parametrize('lines', [
    '- 当前对象 → references（manager_id → employee_id） → 当前对象\n'
    '- 当前对象 → references（manager_id → employee_id） → 当前对象\n',
    # Declared once on each end: one relation, not a duplicate.
    '- 当前对象 → belongs_to（department_id → department_id） → [[department]]\n',
])
def test_package_relations_match_modeling(modeling, lines):
    texts = employee(lines)
    for duplicates in (False, True):
        assert mapping_rules.package_relations(texts, reject_duplicate_relations=duplicates) == \
            modeling.package_relations(texts, reject_duplicate_relations=duplicates)
