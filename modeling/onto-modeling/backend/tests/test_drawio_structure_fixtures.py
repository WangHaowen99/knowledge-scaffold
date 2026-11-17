"""Synthetic drawio structure cases shared by the frontend and diagram checker tests."""
import importlib.util
import json
from pathlib import Path

import pytest


CHECKER = Path(__file__).resolve().parents[2] / 'assets/workspace-toolkit/scripts/check_diagrams.py'
FIXTURES = Path(__file__).resolve().parents[2] / 'fixtures/drawio-structure'
FALLBACK_CASES = [
    {'name': 'valid-wrapped', 'checker_issue': None},
    {'name': 'duplicate-id', 'checker_issue': 'id 重复'},
    {'name': 'dangling-edge', 'checker_issue': '不存在的端点'},
    {'name': 'duplicate-edge-id', 'checker_issue': 'id 重复'},
    {'name': 'missing-annotation-id', 'checker_issue': '缺少 id'},
]
CASES = (json.loads((FIXTURES / 'cases.json').read_text(encoding='utf-8'))
         if (FIXTURES / 'cases.json').is_file() else FALLBACK_CASES)


# The CI test runner copies test modules without the repository's fixtures directory.
def synthetic_diagram(case):
    work = '<mxCell id="work" value="Work" vertex="1" style="rounded=1" parent="1"><mxGeometry x="20" y="130" width="100" height="60" as="geometry"/></mxCell>'
    if case == 'valid-wrapped':
        work = '<UserObject id="work" label="Work" link="https://example.com"><mxCell vertex="1" style="rounded=1" parent="1"><mxGeometry x="20" y="130" width="100" height="60" as="geometry"/></mxCell></UserObject>'
    extra = {
        'duplicate-id': '<mxCell id="work" value="Work copy" vertex="1" style="rounded=1" parent="1"><mxGeometry x="220" y="130" width="100" height="60" as="geometry"/></mxCell>',
        'dangling-edge': '<mxCell id="e3" edge="1" source="work" target="missing" parent="1"><mxGeometry relative="1" as="geometry"/></mxCell>',
        'duplicate-edge-id': '<mxCell id="e1" edge="1" source="work" target="end" parent="1"><mxGeometry relative="1" as="geometry"/></mxCell>',
        'missing-annotation-id': '<mxCell value="Label" style="text;html=1;" vertex="1" parent="1" data-flowchart-annotation-edge="e1"><mxGeometry x="90" y="90" width="50" height="20" as="geometry"/></mxCell>',
    }.get(case, '')
    return (
        '<mxfile><diagram name="synthetic"><mxGraphModel><root>'
        '<mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="start" value="Start" vertex="1" style="shape=mxgraph.flowchart.terminator" parent="1"><mxGeometry x="20" y="20" width="100" height="60" as="geometry"/></mxCell>'
        f'{work}'
        '<mxCell id="end" value="End" vertex="1" style="shape=mxgraph.flowchart.terminator" parent="1"><mxGeometry x="20" y="240" width="100" height="60" as="geometry"/></mxCell>'
        '<mxCell id="e1" edge="1" source="start" target="work" parent="1"><mxGeometry relative="1" as="geometry"/></mxCell>'
        '<mxCell id="e2" edge="1" source="work" target="end" parent="1"><mxGeometry relative="1" as="geometry"/></mxCell>'
        f'{extra}'
        '</root></mxGraphModel></diagram></mxfile>'
    )


@pytest.fixture(scope='module')
def checker():
    spec = importlib.util.spec_from_file_location('shared_drawio_checker', CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['name'])
def test_shared_drawio_structure_case(checker, tmp_path, case):
    path = FIXTURES / f"{case['name']}.drawio"
    if not path.is_file():
        path = tmp_path / f"{case['name']}.drawio"
        path.write_text(synthetic_diagram(case['name']), encoding='utf-8')
    result = checker.check_diagram(path, legend_path=tmp_path / 'absent.drawio')
    expected = case['checker_issue']
    if expected is None:
        assert result['issues'] == []
    else:
        assert any(expected in issue for issue in result['issues'])
