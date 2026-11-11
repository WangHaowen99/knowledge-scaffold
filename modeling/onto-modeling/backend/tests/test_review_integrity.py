"""Synthetic regression cases for input and source integrity."""
import hashlib
import importlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from app import config
from artifact_process_policy import child_environment


@pytest.fixture
def scripts(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(config.ASSETS_DIR) / 'workspace-toolkit/scripts'))
    # trace_inputs 随场景模拟拆进了 trace 包（B4c）：单独跑这个文件时也要找得到，不靠别的用例先导入过
    monkeypatch.syspath_prepend(str(Path(config.ASSETS_DIR) / 'packs/trace/scripts'))
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


def test_docx_over_eight_mib_with_bounded_text_is_verifiable(tmp_path, scripts):
    _, source = scripts
    path = tmp_path / 'large-source.docx'
    docx(path)
    with zipfile.ZipFile(path, 'a', zipfile.ZIP_STORED) as archive:
        archive.writestr('word/media/large-image.bin', b'x' * (9 * 1024 * 1024))

    result = source.extract(path)
    assert path.stat().st_size > 8 * 1024 * 1024
    assert result['status'] == 'ok'
    assert result['source_size'] == path.stat().st_size
    assert result['binary_assets'] == 1
    assert result['binary_assets_extracted'] is False
    assert source.located_text(result, 'p:1') == '合成规则 RULE-01'


def test_docx_over_upload_limit_is_unverifiable(tmp_path, scripts):
    _, source = scripts
    path = tmp_path / 'too-large.docx'
    docx(path)
    with zipfile.ZipFile(path, 'a', zipfile.ZIP_STORED) as archive:
        archive.writestr('word/media/too-large.bin', b'x' * (21 * 1024 * 1024))

    result = source.extract(path)
    assert result['status'] == 'unverifiable'
    assert '原文件超出抽取上限' in result['reason']


def test_source_hash_and_content_use_same_snapshot_when_path_is_replaced(
        tmp_path, scripts, monkeypatch):
    _, source = scripts
    path = tmp_path / 'source.docx'
    replacement = tmp_path / 'replacement.docx'
    docx(path, '<w:p><w:r><w:t>OLD-ITEM</w:t></w:r></w:p>')
    docx(replacement, '<w:p><w:r><w:t>NEW-ITEM</w:t></w:r></w:p>')
    original_bytes = path.read_bytes()
    original_extract_docx = source._extract_docx

    def replace_before_extract(snapshot, add):
        replacement.replace(path)
        return original_extract_docx(snapshot, add)

    monkeypatch.setattr(source, '_extract_docx', replace_before_extract)
    result = source.extract(path)

    assert result['status'] == 'ok'
    assert result['source_sha256'] == hashlib.sha256(original_bytes).hexdigest()
    assert source.located_text(result, 'table:1/row:1/cell:1') == 'OLD-ITEM'
    with zipfile.ZipFile(path) as archive:
        assert b'NEW-ITEM' in archive.read('word/document.xml')


@pytest.mark.parametrize('error_type', [RuntimeError, NotImplementedError])
def test_archive_runtime_errors_are_unverifiable(tmp_path, scripts, monkeypatch, error_type):
    _, source = scripts
    path = tmp_path / 'source.docx'
    docx(path)

    def fail_extract(*args, **kwargs):
        raise error_type('Synthetic unreadable archive member')

    monkeypatch.setattr(source, '_extract_docx', fail_extract)
    result = source.extract(path)

    assert result['status'] == 'unverifiable'
    assert result['segments'] == []
    assert error_type.__name__ in result['reason']


def test_line_range_anchor_joins_lines_in_range(tmp_path, scripts):
    _, source = scripts
    path = tmp_path / 'notes.md'
    path.write_text('第一行\n\n第三行 RULE-01\n第四行\n第五行\n', encoding='utf-8')
    result = source.extract(path)
    assert source.located_text(result, 'L2-L4') == '第三行 RULE-01\n第四行'
    assert source.located_text(result, 'L3-4') == '第三行 RULE-01\n第四行'
    with pytest.raises(ValueError, match='锚点不存在'):
        source.located_text(result, 'L7-L9')


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
                                 shell=False, timeout=30, cwd=tmp_path, env=child_environment(tmp_path))
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
    import subprocess
    import sys
    trace, source = scripts
    # 命令行只读文本（F4b：Word、PDF 这类原件读多源连接器转出的文本版），用一份中文 md 测输出编码
    path = tmp_path / '说明.md'
    path.write_text('合成说明：门店每天提交补货申请。\n', encoding='utf-8')
    for module, args in [(source, [str(path)]), (trace, [str(scenario_file(tmp_path))])]:
        env = child_environment(tmp_path)
        env['PYTHONIOENCODING'] = 'ascii'
        # -I would ignore PYTHONIOENCODING and make the ASCII condition ineffective.
        # -s plus the allowlisted environment disables user startup imports while
        # preserving the real encoding condition exercised by these CLI tools.
        result = subprocess.run([sys.executable, '-B', '-s', module.__file__, *args],
                                capture_output=True, env=env, cwd=tmp_path,
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
