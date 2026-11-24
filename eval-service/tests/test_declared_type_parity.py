"""Declaration syntax stays compatible with modeling; all examples are synthetic."""
import importlib.util
from pathlib import Path
import sys

import pytest

from evaluation.core import Failure
from evaluation.declared_types import parse_declared_type
from evaluation.field_types import field_kind


MODELING = (Path(__file__).resolve().parents[2] / 'modeling/onto-modeling/assets'
            / 'workspace-toolkit/scripts/field_semantics.py')


@pytest.fixture(scope='module')
def modeling():
    if not MODELING.is_file():
        pytest.skip('modeling source is not available beside eval-service')
    spec = importlib.util.spec_from_file_location('modeling_field_semantics', MODELING)
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    return module


def test_every_modeling_alias_and_supported_annotation_matches(modeling):
    for canonical, aliases in modeling.TYPE_ALIASES.items():
        for alias in aliases:
            for declaration in (alias, alias.upper(), f'{alias}（合成单位）', f'{alias}(32)',
                                f'`{alias}`', f'{alias}：合成说明', f'{alias} synthetic',
                                f'{alias} [[synthetic]]', f'{alias}/{aliases[0]}'):
                expected = modeling.parse_declared_type(declaration)
                assert expected == (canonical, ''), declaration
                assert parse_declared_type(declaration) == expected, declaration
                assert field_kind(declaration, 'synthetic field'), declaration


@pytest.mark.parametrize('declaration', [
    None, '', ' ', '无', '-', '待填写', 'n/a', '{待填写：类型}', '金额',
    '字符串/整型/布尔', 'integer or string', '整数/unknown', '未知类型（合成说明）',
])
def test_invalid_declarations_match_modeling(modeling, declaration):
    assert parse_declared_type(declaration) == modeling.parse_declared_type(declaration)
    if declaration is None or declaration == '':
        assert field_kind(declaration, 'synthetic field') == ''
    else:
        with pytest.raises(Failure) as caught:
            field_kind(declaration, 'synthetic field')
        assert caught.value.code == 'graph_field_type_unsupported'


def test_int64_alias_keeps_existing_evaluation_compatibility():
    assert field_kind('int64', 'synthetic field') == 'integer'
