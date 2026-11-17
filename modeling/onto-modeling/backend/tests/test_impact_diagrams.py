"""Diagram impact regression cases. All documents and diagrams are synthetic."""

import importlib
import json
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / 'assets/workspace-toolkit/scripts'
OLD_FORMULA = 'max(0, desired_count - available_count)'


@pytest.fixture
def impact(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module('impact_analysis')


def put(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def diagram(label):
    root = ET.Element('mxfile')
    page = ET.SubElement(root, 'diagram', name='synthetic')
    cells = ET.SubElement(ET.SubElement(page, 'mxGraphModel'), 'root')
    ET.SubElement(cells, 'mxCell', id='synthetic_node', value=label)
    return ET.tostring(root, encoding='unicode')


def analyze(impact, root, **kwargs):
    return impact.analyze_impact(
        'calculate_quantity', 'operation', impact.load_ontology(root), root / 'data-mock',
        testcase_dir=root / 'testcase', **kwargs,
    )


def test_first_impact_lists_same_name_diagram_and_stale_formula_without_links(impact, tmp_path):
    put(tmp_path, 'operation/calculate_quantity.md', '# 逻辑描述\n' + OLD_FORMULA)
    put(tmp_path, 'diagrams/calculate_quantity.drawio', diagram(OLD_FORMULA))
    put(tmp_path, 'diagrams/another_flow.drawio', diagram('<div>max(0, desired_count<br>- available_count)</div>'))
    put(tmp_path, 'diagrams-summary/calculate_quantity.md', '# Synthetic summary\n' + OLD_FORMULA)
    put(tmp_path, 'diagrams/unrelated.drawio', diagram('Synthetic unrelated step'))
    result = analyze(impact, tmp_path)
    assert [entry['file'] for entry in result['diagram_refs']] == [
        'ontology/diagrams/another_flow.drawio',
        'ontology/diagrams/calculate_quantity.drawio',
        'ontology/diagrams-summary/calculate_quantity.md',
    ]
    assert '公式' in result['diagram_refs'][0]['reason']
    report = impact.format_report(result)
    assert '另行同步' in report
    assert '本次更新不会自动修改' in report
    assert '可以安全修改' not in report
    assert '无任何下游引用' not in report
    assert all(OLD_FORMULA not in entry['reason'] for entry in result['diagram_refs'])


def test_diagram_links_and_caller_resources_are_included(impact, tmp_path):
    put(tmp_path, 'operation/calculate_quantity.md', '# Synthetic target')
    put(tmp_path, 'operation/parent_flow.md', '# 逻辑描述\n调用[[calculate_quantity]]逻辑文档')
    put(tmp_path, 'diagrams/parent_flow.drawio', diagram('Synthetic parent step'))
    put(tmp_path, 'diagrams/nested/ref.drawio', diagram('[[calculate_quantity]]'))
    put(tmp_path, 'diagrams/wrapper.drawio', diagram('[[parent_flow]]'))
    put(tmp_path, 'diagrams-summary/nested/ref.md', '[[calculate_quantity]]')
    result = analyze(impact, tmp_path)
    assert len(result['diagram_refs']) == 4
    assert result['called_by'] == ['parent_flow']


@pytest.mark.parametrize('content', [
    '<mxfile>broken',
    '<mxfile><diagram>synthetic_unsupported_payload</diagram></mxfile>',
    '<mxfile/>',
])
def test_unreadable_or_unsupported_diagram_is_pending_review(impact, tmp_path, content):
    put(tmp_path, 'operation/calculate_quantity.md', '# Synthetic target')
    put(tmp_path, 'diagrams/unknown.drawio', content)
    result = analyze(impact, tmp_path)
    assert result['diagram_refs'] == [{
        'file': 'ontology/diagrams/unknown.drawio',
        'reason': '无法完整扫描，需人工复核并另行同步',
    }]
    assert '可以安全修改' not in impact.format_report(result)


def test_mixed_compressed_pages_cannot_silently_pass(impact, tmp_path):
    root = ET.fromstring(diagram('Synthetic unrelated step'))
    ET.SubElement(root, 'diagram').text = 'synthetic_unsupported_payload'
    put(tmp_path, 'diagrams/multiple_pages.drawio', ET.tostring(root, encoding='unicode'))
    result = analyze(impact, tmp_path)
    assert '部分图页无法完整扫描' in result['diagram_refs'][0]['reason']


@pytest.mark.parametrize('kind', ['oversized', 'encoding', 'read_error'])
def test_scan_limits_and_read_errors_remain_visible(impact, tmp_path, monkeypatch, kind):
    path = put(tmp_path, 'diagrams/unknown.drawio', diagram('Synthetic step'))
    if kind == 'oversized':
        monkeypatch.setattr(impact, 'MAX_DIAGRAM_BYTES', 1)
    elif kind == 'encoding':
        path.write_bytes(b'\xff')
    else:
        original = Path.read_text

        def read_text(self, *args, **kwargs):
            if self == path:
                raise PermissionError('Synthetic read failure')
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, 'read_text', read_text)
    result = analyze(impact, tmp_path)
    assert '无法完整扫描' in result['diagram_refs'][0]['reason']


def test_diagram_pages_are_complete_deterministic_and_bounded(impact, tmp_path):
    put(tmp_path, 'operation/calculate_quantity.md', '# 逻辑描述\n' + OLD_FORMULA)
    for index in range(105):
        put(tmp_path, f'diagrams/flow_{index:03}.drawio', diagram(OLD_FORMULA))
    first = analyze(impact, tmp_path, diagram_limit=500)
    assert len(first['diagram_refs']) == 100
    assert first['diagram_scan'] == {'total': 105, 'offset': 0, 'limit': 100, 'next_offset': 100}
    second = analyze(impact, tmp_path, diagram_offset=100, diagram_limit=500)
    paths = [entry['file'] for entry in first['diagram_refs'] + second['diagram_refs']]
    assert len(set(paths)) == 105
    assert second['diagram_scan']['next_offset'] is None
    assert '--diagram-offset 100' in impact.format_report(first)


def test_empty_scan_does_not_claim_semantic_safety(impact, tmp_path):
    put(tmp_path, 'operation/calculate_quantity.md', '# Synthetic target')
    put(tmp_path, 'diagrams/unrelated.drawio', diagram('Synthetic unrelated step'))
    result = analyze(impact, tmp_path)
    assert result['diagram_refs'] == []
    report = impact.format_report(result)
    assert '静态扫描未发现下游引用' in report
    assert '仍需复核' in report
    assert '可以安全修改' not in report


def test_existing_document_mock_and_testcase_references_are_preserved(impact, tmp_path):
    put(tmp_path, 'object/synthetic_item.md', '# 对象描述\nSynthetic item')
    put(tmp_path, 'operation/parent_flow.md', '# 逻辑描述\n[[synthetic_item]]')
    put(tmp_path, 'data-mock/sample.md', '# 正常数据\n| item |\n| --- |\n| synthetic_item |')
    put(tmp_path, 'testcase/parent_flow.md', '# Synthetic testcase')
    result = impact.analyze_impact(
        'synthetic_item', 'object', impact.load_ontology(tmp_path), tmp_path / 'data-mock',
        testcase_dir=tmp_path / 'testcase',
    )
    assert result['operation_refs'] == ['parent_flow']
    assert result['mock_refs'] == ['sample']
    assert result['testcase_refs'] == ['parent_flow']
    assert result['diagram_refs'] == []
    assert 'parent_flow' in impact.format_report(result)


def test_cli_returns_json_candidates_without_writing_resources(tmp_path):
    ontology = tmp_path / 'ontology'
    target = put(ontology, 'operation/calculate_quantity.md', '# 逻辑描述\n`' + OLD_FORMULA + '`')
    flow = put(ontology, 'diagrams/other_flow.drawio', diagram(OLD_FORMULA))
    before = {path: path.read_bytes() for path in (target, flow)}
    command = [
        sys.executable, '-B', '-X', 'utf8', str(SCRIPTS / 'impact_analysis.py'),
        'ontology/operation/calculate_quantity.md', '--json', '--diagram-limit', '1',
    ]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, encoding='utf-8', check=True)
    data = json.loads(result.stdout)
    assert data['category'] == 'operation'
    assert data['diagram_refs'][0]['file'] == 'ontology/diagrams/other_flow.drawio'
    assert data['diagram_scan']['limit'] == 1
    assert {path: path.read_bytes() for path in before} == before
