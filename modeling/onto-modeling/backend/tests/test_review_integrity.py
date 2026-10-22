"""Synthetic regression cases for input, source and evaluation integrity."""
import importlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from app import config
from app.services import modeling_eval


@pytest.fixture
def scripts(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(config.ASSETS_DIR) / 'workspace-toolkit/scripts'))
    return importlib.import_module('trace_inputs'), importlib.import_module('source_extract')


def scenario_file(tmp_path):
    path = tmp_path / 'cases.md'
    path.write_text('# 测试场景：合成订单\n\n## 场景 1：边界\n\n- **输入**：\n'
                    '  - `订单.状态` = `待处理`（用于边界分支）\n'
                    '  - `数量` = `0`\n  - `允许` = `false`\n'
                    '  - `空串` = `""`\n  - `缺省` = `null`\n'
                    '  - `项目` = `[1, {"a": "中文"}]`\n'
                    '- **预期路径**：停止\n- **数据来源**：自定义\n', encoding='utf-8')
    return path


def test_trace_preserves_annotated_and_typed_fields(tmp_path, scripts):
    trace, _ = scripts
    path = scenario_file(tmp_path)
    payload = trace.parse_source(path)
    assert payload['scenarios'][0]['inputs'] == {
        '订单.状态': '待处理', '数量': 0, '允许': False, '空串': '', '缺省': None,
        '项目': [1, {'a': '中文'}]}
    assert trace.verify(path, payload) == payload


@pytest.mark.parametrize('change', ['drop', 'type', 'stale', 'rename', 'extra'])
def test_trace_rejects_changed_handoff(tmp_path, scripts, change):
    trace, _ = scripts
    path = scenario_file(tmp_path)
    payload = trace.parse_source(path)
    if change == 'drop':
        del payload['scenarios'][0]['inputs']['订单.状态']
    elif change == 'type':
        payload['scenarios'][0]['inputs']['允许'] = 0
    elif change == 'rename':
        payload['scenarios'][0]['name'] = '其他'
    elif change == 'extra':
        payload['scenarios'][0]['inputs']['多余'] = '伪造'
    else:
        path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='不一致'):
        trace.verify(path, payload)


@pytest.mark.parametrize('line', ['  - `数量` = `2`\n  - `数量` = `3`',
                                    '  - 字段没有等号', '  - `数量` = `2` 未知后缀',
                                    '  - `数量` = `2`\n- **输入**：\n  - `另一个` = `1`'])
def test_trace_never_silently_skips_lines(tmp_path, scripts, line):
    trace, _ = scripts
    path = scenario_file(tmp_path)
    path.write_text(path.read_text(encoding='utf-8').replace('  - `数量` = `0`', line), encoding='utf-8')
    with pytest.raises(ValueError):
        trace.parse_source(path)


def docx(path, cell_xml='<w:p><w:r><w:t>ITEM-01</w:t></w:r></w:p>'):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   '<w:body><w:p><w:r><w:t>合成规则 RULE-01</w:t></w:r></w:p>'
                   f'<w:tbl><w:tr><w:tc>{cell_xml}</w:tc></w:tr></w:tbl>'
                   '</w:body></w:document>')


def test_office_extraction_has_original_hash_and_positions(tmp_path, scripts):
    _, source = scripts
    path = tmp_path / 'source.docx'
    docx(path)
    result = source.extract(path)
    assert result['status'] == 'ok'
    assert len(result['source_sha256']) == 64
    assert source.located_text(result, 'p:1') == '合成规则 RULE-01'
    assert source.located_text(result, 'table:1/row:1/cell:1') == 'ITEM-01'
    with pytest.raises(ValueError, match='锚点不存在'):
        source.located_text(result, 'p:99')
    import openpyxl
    book = openpyxl.Workbook()
    book.active.title = '合成规则'
    book.active.append(['规则', 'RULE-01'])
    path = tmp_path / 'source.xlsx'
    book.save(path)
    assert source.located_text(source.extract(path), 'sheet:合成规则!B1') == 'RULE-01'


@pytest.mark.parametrize('name,data', [('bad.docx', b'not a zip'), ('bad.txt', b'\xff'),
                                      ('empty.txt', b''), ('unsupported.bin', b'RULE-01')])
def test_bad_sources_are_unverifiable(tmp_path, scripts, name, data):
    _, source = scripts
    path = tmp_path / name
    path.write_bytes(data)
    result = source.extract(path)
    assert result['status'] == 'unverifiable'
    assert result['segments'] == []


def test_docx_cell_paragraphs_do_not_merge_rule_ids(tmp_path, scripts):
    _, source = scripts
    path = tmp_path / 'source.docx'
    docx(path, '<w:p><w:r><w:t>RULE-01</w:t></w:r></w:p>'
               '<w:p><w:r><w:t>RULE-02</w:t></w:r></w:p>')
    assert source.located_text(source.extract(path), 'table:1/row:1/cell:1') == 'RULE-01\nRULE-02'


def test_source_cli_long_segment_continuation_is_lossless(tmp_path, scripts):
    import subprocess
    import sys
    _, source = scripts
    path = tmp_path / 'source.txt'
    original = '合成说明' * 620 + ' RULE-01'
    path.write_text(original, encoding='utf-8')
    offset, parts, hashes = 0, [], set()
    for _ in range(3):
        process = subprocess.run([sys.executable, '-I', source.__file__, str(path),
                                  '--anchor', 'L1', '--text-offset', str(offset)], capture_output=True,
                                 shell=False, timeout=30)
        assert process.returncode == 0, process.stderr
        page = json.loads(process.stdout.decode('utf-8'))
        hashes.add(page['source_sha256'])
        assert page['total'] == 1
        segment = page['segments'][0]
        parts.append(segment['text'])
        offset = segment['next_text_offset']
        if offset is None:
            break
    assert offset is None
    assert len(hashes) == 1
    assert ''.join(parts) == original


@pytest.mark.parametrize('results,score,valid', [
    ([{'verdict': 'error', 'finalAnswer': ''}], None, 0),
    ([{'verdict': 'pass', 'finalAnswer': '正常'}, {'verdict': 'error'}], 100, 1),
    ([{'verdict': 'pass', 'finalAnswer': ''}], None, 0),
    ([{'verdict': 'unknown', 'finalAnswer': '异常'}], None, 0),
    ([], None, 0),
])
def test_technical_errors_are_not_business_zero(tmp_path, results, score, valid):
    rel = modeling_eval._write_report(tmp_path, {'id': 'synthetic'}, results)
    state = modeling_eval.read_state(tmp_path)
    assert state['status'] == 'failed'
    assert state['summary']['score'] == score
    assert state['summary']['valid'] == valid
    assert state['summary']['result_status'] == ('partial' if valid else 'unavailable')
    assert '技术异常不计为业务答错' in (tmp_path / rel).read_text(encoding='utf-8')


def test_real_wrong_answer_still_scores_zero(tmp_path):
    modeling_eval._write_report(tmp_path, {'id': 'synthetic'}, [
        {'verdict': 'fail', 'finalAnswer': '不符合标准的合成回答'}])
    state = modeling_eval.read_state(tmp_path)
    assert state['status'] == 'done'
    assert state['summary']['score'] == 0


def test_missing_result_is_not_silently_completed(tmp_path):
    modeling_eval._write_report(tmp_path, {'id': 'synthetic'}, [
        {'question': 'Q1', 'verdict': 'pass', 'finalAnswer': 'A1'}],
        cases=[{'question': 'Q1'}, {'question': 'Q2'}])
    state = modeling_eval.read_state(tmp_path)
    assert state['status'] == 'failed'
    assert state['summary']['total'] == 2
    assert state['summary']['error'] == 1


def test_trace_verifies_actual_result_receipts(tmp_path, scripts):
    trace, _ = scripts
    payload = trace.parse_source(scenario_file(tmp_path))
    results = [{'scenario_id': '1', 'source_sha256': payload['source_sha256'],
                'initial_inputs': dict(payload['scenarios'][0]['inputs'])}]
    trace.verify_results(payload, results)
    del results[0]['initial_inputs']['订单.状态']
    with pytest.raises(ValueError, match='实际初始输入不一致'):
        trace.verify_results(payload, results)
    with pytest.raises(ValueError, match='场景数'):
        trace.verify_results(payload, [])


def test_utf8_cli_in_isolated_ascii_environment(tmp_path, scripts):
    import os
    import subprocess
    import sys
    trace, source = scripts
    path = tmp_path / '说明.docx'
    docx(path)
    for module, args in [(source, [str(path)]), (trace, [str(scenario_file(tmp_path))])]:
        result = subprocess.run([sys.executable, '-I', module.__file__, *args],
                                capture_output=True, env={**os.environ, 'PYTHONIOENCODING': 'ascii'},
                                shell=False, timeout=30)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.decode('utf-8'))['status'] in {'ok', 'passed'}


def test_source_limits_never_return_partial_success(tmp_path, scripts, monkeypatch):
    _, source = scripts
    path = tmp_path / 'source.txt'
    path.write_text('RULE-01\nRULE-02', encoding='utf-8')
    monkeypatch.setattr(source, 'MAX_SEGMENTS', 1)
    assert source.extract(path)['status'] == 'unverifiable'
    monkeypatch.setattr(source, 'MAX_BYTES', 1)
    assert source.extract(path)['status'] == 'unverifiable'


def test_done_poll_with_missing_results_is_technical_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(modeling_eval, 'POLL_INTERVAL_SECONDS', 0)
    monkeypatch.setattr(modeling_eval, '_http', lambda *a, **k: {
        'round': {'id': 'r1', 'status': 'done'},
        'results': [{'question': 'Q10', 'finalAnswer': 'A', 'verdict': 'pass'}]})
    monkeypatch.setattr(modeling_eval, '_git_commit', lambda *a: None)
    modeling_eval._poll_round('synthetic', tmp_path, '', 'p', 'r1', [{'question': 'Q1'}, {'question': 'Q10'}])
    state = modeling_eval.read_state(tmp_path)
    assert state['summary']['total'] == 2
    assert state['summary']['error'] == 1
    assert state['status'] == 'failed'


def test_collected_technical_failure_does_not_trap_rerun(tmp_path, monkeypatch):
    modeling_eval._write_state(tmp_path, modeling_project='p', round_id='r1', report_round='r1', status='failed')
    def unexpected(*args, **kwargs):
        pytest.fail('不能再次收取已完成失败轮代替重跑')
    monkeypatch.setattr(modeling_eval, '_http', unexpected)
    assert not modeling_eval._try_resume('synthetic', tmp_path, '', [{'question': 'Q1'}])
