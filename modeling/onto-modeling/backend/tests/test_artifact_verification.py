"""Synthetic-only regression for lightweight checks, receipts and save hooks."""
import importlib
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / 'assets/workspace-toolkit/scripts'


@pytest.fixture
def verifier(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module('verify_artifacts')


def object_doc(name='EntityA', key='ID', relation='无'):
    return f'''# 对象描述
合成对象。
# 对象别名
无
# 英文名称
{name}
# 数据来源
无
# 查询边界
只查询已声明字段。
# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
| --- | --- | --- | --- | --- | --- | --- |
| 标识 | {key} | 合成标识 | 是 | 字符串 | 无 | 无 |
# 关联对象
{relation}
'''


def operation_doc():
    return '''# 业务描述
合成操作。
# 与operation的关联
无
# 与object的关联
[[Entity0]]
# 输入
无
# 输出
无
# 逻辑描述
读取已声明属性。
'''


def rule_doc():
    return '# 规则描述\n合成规则。\n# 适用对象\n[[Entity0]]\n# 约束表达\nID 不为空。\n'


def metric_doc():
    return '# 指标描述\n合成指标。\n# 计算公式\nCOUNT(ID)\n# 数据来源\n[[Entity0]]\n'


def put(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def diagram_xml():
    return '''<mxfile><diagram><mxGraphModel><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="a" value="Start" vertex="1" style="shape=mxgraph.flowchart.terminator" parent="1"/>
<mxCell id="b" value="Work" vertex="1" style="rounded=1" parent="1"/>
<mxCell id="c" value="End" vertex="1" style="shape=mxgraph.flowchart.terminator" parent="1"/>
<mxCell id="ab" edge="1" source="a" target="b" parent="1"/>
<mxCell id="bc" edge="1" source="b" target="c" parent="1"/>
</root></mxGraphModel></diagram></mxfile>'''


def test_default_idle_ttl_and_explicit_override():
    env = dict(os.environ)
    env.pop('ONTO_WEB_SESSION_IDLE_TTL', None)
    code = 'from app import config; print(config.SESSION_IDLE_TTL_SECONDS)'
    assert subprocess.check_output([sys.executable, '-c', code], env=env, text=True).strip() == '600'
    env['ONTO_WEB_SESSION_IDLE_TTL'] = '0'
    assert subprocess.check_output([sys.executable, '-c', code], env=env, text=True).strip() == '0'


def test_idle_reaper_obeys_600_seconds(tmp_path, monkeypatch):
    from app import config
    from app.services import agent_sessions
    from types import SimpleNamespace
    manager = agent_sessions.SessionManager()
    session = SimpleNamespace(state='idle', engine=object(), pending_prompts=[],
                              pending_permission=None, sending=False, last_activity=1000)
    # Use the public reaper with only eligibility/recycle mocked; no real model or wait.
    manager._sessions['synthetic'] = session
    monkeypatch.setattr(config, 'SESSION_IDLE_TTL_SECONDS', 600)
    monkeypatch.setattr(agent_sessions, '_recyclable', lambda s: s.state == 'idle')
    recycled = []
    async def recycle(s):
        recycled.append(s)
    async def drain():
        pass
    monkeypatch.setattr(manager, '_recycle', recycle)
    monkeypatch.setattr(manager, '_drain_waiters', drain)
    for now in (1121, 1599, 1600):
        monkeypatch.setattr(agent_sessions.time, 'time', lambda: now)
        assert asyncio.run(manager.reap_idle()) == 0
    monkeypatch.setattr(agent_sessions.time, 'time', lambda: 1601)
    assert asyncio.run(manager.reap_idle()) == 1
    assert recycled == [session]


def test_empty_or_index_only_is_not_completed(tmp_path, verifier):
    assert verifier.verify(tmp_path, 'objects')['status'] == 'pending'
    put(tmp_path, 'ontology/object/index.md', '# Index')
    assert verifier.verify(tmp_path, 'objects')['status'] == 'pending'


def test_operation_rule_metric_closeout(tmp_path, verifier):
    put(tmp_path, 'ontology/object/Entity0.md', object_doc('Entity0'))
    put(tmp_path, 'ontology/operation/TaskA.md', operation_doc())
    rule = put(tmp_path, 'ontology/rule/RuleA.md', rule_doc())
    put(tmp_path, 'ontology/metric/MetricA.md', metric_doc())
    result = verifier.verify(tmp_path, 'operations')
    assert result['status'] == 'passed', result
    assert result['checked_files'] == 3
    rule.write_text('# 规则描述\n合成规则。')
    assert verifier.verify(tmp_path, 'operations')['status'] == 'failed'


def test_unloaded_nested_rule_is_not_a_passing_empty_check(tmp_path, verifier):
    put(tmp_path, 'ontology/object/Entity0.md', object_doc('Entity0'))
    put(tmp_path, 'ontology/rule/nested/RuleA.md', rule_doc())
    with pytest.raises(ValueError, match='未被检查器加载'):
        verifier.verify(tmp_path, 'operations')


def test_valid_receipt_reused_without_checker(tmp_path, verifier, monkeypatch):
    put(tmp_path, 'ontology/object/EntityA.md', object_doc())
    first = verifier.verify(tmp_path, 'objects')
    assert first['status'] == 'passed', first
    monkeypatch.setattr(verifier, '_check', lambda *a: pytest.fail('duplicate check'))
    cached = verifier.verify(tmp_path, 'ontology', ['ontology/object/EntityA.md'])
    assert cached['cached'] and cached['status'] == 'passed'
    assert cached['receipt'] == first['receipt']


@pytest.mark.parametrize('rel,text', [
    ('ontology/object/EntityA.md', object_doc().replace('合成对象', '变更对象')),
    ('ontology/object/EntityB.md', object_doc('EntityB')),
    ('resource/materials.json', '{}'),
    ('research/analysis.md', '# Synthetic report'),
])
def test_input_changes_invalidate_receipt(tmp_path, verifier, rel, text):
    put(tmp_path, 'ontology/object/EntityA.md', object_doc())
    first = verifier.verify(tmp_path, 'objects')
    put(tmp_path, rel, text)
    result = verifier.verify(tmp_path, 'objects', status_only=True)
    assert result['status'] == 'pending'
    assert (tmp_path / first['receipt']).exists()  # old evidence remains


def test_deleted_expected_output_is_not_ignored(tmp_path, verifier):
    put(tmp_path, 'ontology/object/EntityA.md', object_doc())
    with pytest.raises(ValueError, match='不存在'):
        verifier.verify(tmp_path, 'objects', ['EntityA', 'EntityMissing'])


def test_schema_same_size_edit_invalidates(tmp_path, verifier):
    put(tmp_path, 'ontology/object/EntityA.md', object_doc())
    table = put(tmp_path, 'resource/table.csv', 'ID\nx1\n')
    put(tmp_path, 'resource/materials.json', json.dumps({'resource/table.csv': {'purpose': 'schema'}}))
    assert verifier.verify(tmp_path, 'objects')['status'] == 'passed'
    table.write_text('NO\nx1\n')
    assert verifier.verify(tmp_path, 'objects', status_only=True)['status'] == 'pending'


@pytest.mark.parametrize('content', [None, '', 'not an Excel file'])
def test_unreadable_registered_table_cannot_pass(tmp_path, verifier, content):
    put(tmp_path, 'ontology/object/EntityA.md', object_doc())
    put(tmp_path, 'resource/materials.json', json.dumps({'resource/table.xlsx': {'purpose': 'schema'}}))
    if content is not None:
        put(tmp_path, 'resource/table.xlsx', content)
    with pytest.raises(ValueError):
        verifier.verify(tmp_path, 'objects')


def test_schema_field_mismatch_and_nested_file(tmp_path, verifier):
    put(tmp_path, 'ontology/object/group/Entity A.md', object_doc().replace('# 数据来源\n无', '# 数据来源\ntable'))
    table = put(tmp_path, 'resource/table.csv', 'ID\nx1\n')
    put(tmp_path, 'resource/materials.json', json.dumps({'resource/table.csv': {'purpose': 'schema'}}))
    assert verifier.verify(tmp_path, 'objects', ['ontology/object/group/Entity A.md'])['status'] == 'passed'
    table.write_text('OTHER\nx1\n')
    assert verifier.verify(tmp_path, 'objects')['status'] == 'failed'


def test_failed_receipt_and_corruption_fail_closed(tmp_path, verifier):
    put(tmp_path, 'ontology/object/EntityA.md', '')
    failed = verifier.verify(tmp_path, 'objects')
    assert failed['status'] == 'failed'
    assert verifier.verify(tmp_path, 'objects')['status'] == 'failed'
    receipt = tmp_path / failed['receipt']
    receipt.write_text('{broken')
    assert verifier.verify(tmp_path, 'objects', status_only=True)['status'] == 'pending'


def test_inconsistent_receipt_status_is_rejected(tmp_path, verifier):
    put(tmp_path, 'ontology/object/EntityA.md', '')
    failed = verifier.verify(tmp_path, 'objects')
    path = tmp_path / failed['receipt']
    data = json.loads(path.read_text())
    data['status'] = 'passed'
    path.write_text(json.dumps(data))
    assert verifier.verify(tmp_path, 'objects', status_only=True)['status'] == 'pending'


def test_checker_upgrade_invalidates_receipt(tmp_path, verifier, monkeypatch):
    put(tmp_path, 'ontology/object/EntityA.md', object_doc())
    copy = put(tmp_path, 'tool-copy/verify_artifacts.py', '# version 1')
    monkeypatch.setattr(verifier, '__file__', str(copy))
    assert verifier.verify(tmp_path, 'objects')['status'] == 'passed'
    copy.write_text('# version 2')
    assert verifier.verify(tmp_path, 'objects', status_only=True)['status'] == 'pending'


def test_changed_during_check_has_no_passing_receipt(tmp_path, verifier, monkeypatch):
    path = put(tmp_path, 'ontology/object/EntityA.md', object_doc())
    check = verifier._check
    def mutate(*args):
        issues = check(*args)
        path.write_text(object_doc().replace('合成对象', '变化对象'))
        return issues
    monkeypatch.setattr(verifier, '_check', mutate)
    result = verifier.verify(tmp_path, 'objects')
    assert result['status'] == 'pending' and result['reason'] == 'changed_during_check'
    assert not (tmp_path / result['receipt']).exists()


@pytest.mark.parametrize('transform,message', [
    (lambda s: s.replace('id="ab"', ''), '缺少 id'),
    (lambda s: s.replace('id="bc"', 'id="ab"'), 'id 重复'),
    (lambda s: s.replace('target="c"', 'target="missing"'), '不存在的端点'),
    (lambda s: s[:-10], '无法解析'),
    (lambda s: s.replace('source="b"', ''), '起点缺失'),
])
def test_diagram_errors_are_blocking(tmp_path, verifier, transform, message):
    put(tmp_path, 'ontology/diagrams/FlowA.drawio', transform(diagram_xml()))
    result = verifier.verify(tmp_path, 'diagrams')
    assert result['status'] == 'failed'
    assert any(message in i['message'] for i in result['issues'])


def test_valid_diagram_and_wrapped_cell(tmp_path, verifier):
    xml = diagram_xml().replace('<mxCell id="b"', '<object id="b"><mxCell')
    xml = xml.replace('style="rounded=1" parent="1"/>', 'style="rounded=1" parent="1"/></object>')
    put(tmp_path, 'ontology/diagrams/FlowA.drawio', xml)
    assert verifier.verify(tmp_path, 'diagrams')['status'] == 'passed'
    assert verifier.verify(tmp_path, 'diagrams', status_only=True)['cached']


def test_diagram_ids_are_page_scoped(tmp_path, verifier):
    page = diagram_xml().removeprefix('<mxfile>').removesuffix('</mxfile>')
    put(tmp_path, 'ontology/diagrams/FlowA.drawio', '<mxfile>' + page + page + '</mxfile>')
    assert verifier.verify(tmp_path, 'diagrams')['status'] == 'passed'


def test_diagram_import_without_timezone_database():
    code = '''import sys, zoneinfo
sys.path.insert(0, sys.argv[1])
def missing(name):
    raise zoneinfo.ZoneInfoNotFoundError(name)
zoneinfo.ZoneInfo = missing
import check_diagrams
assert check_diagrams.REPORT_TZ.utcoffset(None).total_seconds() == 28800
'''
    subprocess.run([sys.executable, '-c', code, str(SCRIPTS)], check=True)


def test_reverse_join_direction_and_one_sided_declaration(tmp_path, verifier):
    put(tmp_path, 'ontology/object/EntityA.md', object_doc('EntityA', 'A_ID',
        '- 当前对象 → links（A_ID → B_ID） → [[EntityB]]'))
    other = put(tmp_path, 'ontology/object/EntityB.md', object_doc('EntityB', 'B_ID',
        '- [[EntityA]] → links → 当前对象'))
    assert verifier.verify(tmp_path, 'objects')['status'] == 'passed'
    other.write_text(object_doc('EntityB', 'B_ID',
        '- [[EntityA]] → links（B_ID → A_ID） → 当前对象'))
    result = verifier.verify(tmp_path, 'objects')
    assert result['status'] == 'failed'
    assert any(i['category'] == '关联字段' and i['severity'] == '错误' for i in result['issues'])
    # A targeted check must also reject an invalid declaration on its referenced end.
    assert verifier.verify(tmp_path, 'objects', ['EntityA'])['status'] == 'failed'


def test_conflicting_valid_joins_are_blocking(tmp_path, verifier):
    def with_extra(name, relation):
        return object_doc(name, 'ID', relation).replace('# 关联对象',
            '| 备选标识 | ALT | 合成字段 | 否 | 字符串 | 无 | 无 |\n# 关联对象')
    put(tmp_path, 'ontology/object/EntityA.md', with_extra('EntityA',
        '- 当前对象 → links（ID → ID） → [[EntityB]]'))
    put(tmp_path, 'ontology/object/EntityB.md', with_extra('EntityB',
        '- [[EntityA]] → links（ALT → ALT） → 当前对象'))
    result = verifier.verify(tmp_path, 'objects')
    assert any('两端声明' in i['message'] for i in result['issues'])
    assert result['status'] == 'failed'


def test_external_symlink_rejected(tmp_path, verifier):
    outside = tmp_path / 'outside.md'
    outside.write_text(object_doc())
    root = tmp_path / 'workspace'
    folder = root / 'ontology/object'
    folder.mkdir(parents=True)
    (folder / 'EntityA.md').symlink_to(outside)
    with pytest.raises(ValueError, match='越出'):
        verifier.verify(root, 'objects')


@pytest.fixture
def git_workspace(tmp_path):
    shutil.copytree(SCRIPTS, tmp_path / '.claude/scripts', ignore=shutil.ignore_patterns('__pycache__'))
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, stderr=subprocess.STDOUT, text=True)
    git('init', '-q')
    git('config', 'user.name', 'Synthetic Test')
    git('config', 'user.email', 'test@example.invalid')
    git('commit', '--allow-empty', '-qm', 'init: synthetic')
    return tmp_path, git


def hook(root, name, command='git commit -m "extract: synthetic objects"'):
    env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
    return subprocess.run(['bash', str(root / '.claude/scripts' / name)], cwd=root, env=env,
                          input=json.dumps({'tool_input': {'command': command}, 'tool_response': {'exitCode': 0}}),
                          capture_output=True, text=True, check=True)


def test_save_hook_blocks_bad_diagram_and_checker_failure(git_workspace):
    root, git = git_workspace
    path = put(root, 'ontology/diagrams/FlowA.drawio', diagram_xml().replace('id="ab"', ''))
    git('add', 'ontology/diagrams')
    assert 'block' in hook(root, 'pre_commit_check.sh').stdout
    path.write_text(diagram_xml())
    git('add', 'ontology/diagrams')
    assert 'block' not in hook(root, 'pre_commit_check.sh').stdout
    (root / '.claude/scripts/verify_artifacts.py').unlink()
    assert 'block' in hook(root, 'pre_commit_check.sh').stdout


@pytest.mark.parametrize('body', ['', 'print("not json")', 'print("[]"); raise SystemExit(1)'])
def test_save_hook_blocks_missing_or_inconsistent_result(git_workspace, body):
    root, git = git_workspace
    put(root, 'ontology/object/EntityA.md', object_doc())
    git('add', 'ontology/object')
    (root / '.claude/scripts/verify_artifacts.py').write_text(body)
    assert 'block' in hook(root, 'pre_commit_check.sh').stdout


def test_save_hook_partial_stage_and_completion_hint(git_workspace):
    root, git = git_workspace
    path = put(root, 'ontology/object/EntityA.md', object_doc())
    git('add', 'ontology/object')
    path.write_text(object_doc().replace('合成对象', '修改对象'))
    assert 'block' in hook(root, 'pre_commit_check.sh').stdout
    git('add', 'ontology/object')
    assert 'block' not in hook(root, 'pre_commit_check.sh').stdout
    git('commit', '-qm', 'extract: synthetic objects')
    assert '产物轻量核验通过' in hook(root, 'post_commit_hook.sh').stdout
    path.write_text(object_doc())
    result = hook(root, 'post_commit_hook.sh')
    assert '仍待核验' in result.stdout and '产物轻量核验通过' not in result.stdout


def test_post_hook_index_only_does_not_claim_completion(git_workspace):
    root, git = git_workspace
    put(root, 'ontology/object/index.md', '# Index')
    git('add', 'ontology/object')
    git('commit', '-qm', 'extract: synthetic objects')
    assert '仍待核验' in hook(root, 'post_commit_hook.sh').stdout
