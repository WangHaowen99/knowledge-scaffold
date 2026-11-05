"""The evaluation copy of the mapping rules derives the same relations as onto-modeling. Synthetic objects only.

Skipped when the modeling source is not beside eval-service, as in a standalone evaluation package."""
import importlib.util
from pathlib import Path

import pytest

from evaluation import mapping_rules

MODELING = Path(__file__).resolve().parents[2] / 'modeling' / 'onto-modeling' / 'backend' / 'app' / 'services' / 'package_mapping.py'


@pytest.fixture(scope='module')
def modeling():
    if not MODELING.is_file():
        pytest.skip('onto-modeling source is not available beside eval-service')
    spec = importlib.util.spec_from_file_location('modeling_package_mapping', MODELING)
    module = importlib.util.module_from_spec(spec)
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


def test_package_relations_match_modeling(modeling):
    texts = employee('- 当前对象 → references（manager_id → employee_id） → 当前对象\n'
                     '- 当前对象 → references（manager_id → employee_id） → 当前对象\n')
    for duplicates in (False, True):
        assert mapping_rules.package_relations(texts, reject_duplicate_relations=duplicates) == \
            modeling.package_relations(texts, reject_duplicate_relations=duplicates)
