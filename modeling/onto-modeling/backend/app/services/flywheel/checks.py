"""Pre-publication checks of flywheel candidate content; local only, no credentials.

The web publication's gates (data binding, solver contract, publication readiness of ADR-0103) plus the
evolution command's own quality checks run again on the server: hard errors of the rule checker and the
generalization check of the revised documents. Modeling stages only require object and operation content: projects imported from
Hub have no research or validation reports. Each check keeps at most DETAIL_LIMIT messages.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from ... import config
from .. import hub_packages
from ..publish import readiness, service as publish_service
from ..solver import service as solver_service
from ..workspace import WorkspaceError, _git

DETAIL_LIMIT = 5
DETAIL_CHARS = 200
SCRIPT_TIMEOUT = 180
CHANGED_LIMIT = 50
# 发布必需的建模阶段，及各自产物所在的目录
CONTENT_STAGES = (('对象提取', ('ontology/object',)), ('操作提取', ('ontology/operation', 'ontology/logic')))


def _item(key, group, label, passed, message, details=()):
    return {'key': key, 'group': group, 'label': label, 'passed': passed, 'message': message,
            'details': [str(text)[:DETAIL_CHARS] for text in list(details)[:DETAIL_LIMIT]]}


def _script(root, name, *args):
    """Run a workspace-toolkit checker on the workspace; it only reads files and git."""
    script = Path(config.ASSETS_DIR) / 'workspace-toolkit' / 'scripts' / name
    # 只传必要的环境变量：检查脚本不需要服务凭据
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG') if key in os.environ}
    env['PYTHONIOENCODING'] = 'utf-8'
    result = subprocess.run(  # NOSONAR(S4721): fixed toolkit script and arguments, no shell
        [sys.executable, '-E', '-s', str(script), *args], cwd=root, env=env, shell=False,
        capture_output=True, text=True, encoding='utf-8', timeout=SCRIPT_TIMEOUT)
    return json.loads(result.stdout or 'null')


def changed_paths(root, base):
    """Package documents changed since the snapshot, committed or not."""
    tracked = _git(root, '-c', 'core.quotepath=false', 'diff', '--no-ext-diff', '--name-only', base, '--', 'ontology')
    untracked = _git(root, '-c', 'core.quotepath=false', 'ls-files', '--others', '--exclude-standard', '--', 'ontology')
    return sorted({path for path in (tracked + untracked).splitlines() if path.strip()})


def _documents(root, *dirs):
    return any(path.name != 'index.md' for directory in dirs for path in (root / directory).glob('*.md'))


def _content(root):
    missing = [label for label, dirs in CONTENT_STAGES if not _documents(root, *dirs)]
    return _item('content', 'stage', '对象与操作内容完整', not missing,
                 f"缺少{'、'.join(missing)}产物" if missing else '对象与操作文档齐全')


def _rules(root):
    try:
        issues = _script(root, 'ontology_checker.py', 'ontology', '--json', '--include-scripts')
    except (OSError, ValueError, subprocess.SubprocessError):
        return _item('rules', 'quality', '规则检查', False, '规则检查未能完成，请在助手中运行 /ontology-validate')
    if not isinstance(issues, list):
        return _item('rules', 'quality', '规则检查', False, '规则检查未能完成，请在助手中运行 /ontology-validate')
    errors = [item for item in issues if isinstance(item, dict) and item.get('severity') == '错误']
    return _item('rules', 'quality', '规则检查', not errors,
                 f'{len(errors)} 个硬错误' if errors else '无硬错误',
                 (f"{item.get('file', '')}：{item.get('message', '')}" for item in errors))


def _generalization(root, base, report):
    args = ['--workspace', '.', '--base', base, '--json'] + (['--report', f'reports/{report}'] if report else [])
    try:
        findings = _script(root, 'check_eval_generalization.py', *args)
    except (OSError, ValueError, subprocess.SubprocessError):
        return _item('generalization', 'quality', '泛化性检查', False, '泛化性检查未能完成')
    if isinstance(findings, dict):
        return _item('generalization', 'quality', '泛化性检查', True, findings.get('skipped') or '无需检查')
    if not isinstance(findings, list):
        return _item('generalization', 'quality', '泛化性检查', False, '泛化性检查未能完成')
    return _item('generalization', 'quality', '泛化性检查', not findings,
                 f'{len(findings)} 处应试痕迹' if findings else '修订内容无原文雷同与应试措辞',
                 (f"{item.get('file', '')}：{item.get('message', '')}" for item in findings if isinstance(item, dict)))


def _binding(manager, pid):
    check = publish_service.package_binding_check(manager, pid)
    blocking = [issue['message'] for issue in check['issues'] if issue['blocking']]
    return _item('binding', 'binding', '数据绑定', not check['blocking'],
                 f"{check['blocking']} 个对象的数据来源与数据表对不上" if check['blocking']
                 else f"对象 {check['objects']} 个，已绑定数据表 {check['bound_objects']} 个", blocking)


def _solver(manager, pid):
    status = solver_service.solver_release_status(manager, pid)
    if not status.get('enabled'):
        return _item('solver', 'binding', '求解契约', True, '未启用求解适配')
    # 只是契约静态检查；求解能力是否已验证在发布时另行核对（publish._verified_solver）
    blocking = status.get('findings') or []
    return _item('solver', 'binding', '求解契约', not blocking,
                 f'{len(blocking)} 项未通过' if blocking else '契约检查通过',
                 (item['message'] for item in blocking))


def _readiness(manager, pid):
    """发布就绪（ADR-0103）：主线最新提交上会挡发布、降级也绕不过的内容问题，与网页发布同一口径。"""
    findings = readiness.mainline_findings(manager, pid)
    if findings is None:
        return _item('readiness', 'binding', '发布就绪', False, '发布就绪检查未能完成')
    return _item('readiness', 'binding', '发布就绪', not findings,
                 f'{len(findings)} 个会挡发布的问题' if findings else '没有会挡发布的问题',
                 (readiness._line(item) for item in findings))


def quick(manager, pid):
    """The checks repeated right before publication; the slow quality checks ran on the candidate."""
    return [_binding(manager, pid), _solver(manager, pid), _readiness(manager, pid)]


def evaluate(manager, pid, base=None, report=None):
    """Check the workspace as a candidate. ``base`` is the run's snapshot; restored content has none."""
    root = manager.project_root(pid)
    changed = changed_paths(root, base) if base else []
    items = [_content(root), _rules(root)]
    if base:
        items.append(_generalization(root, base, report))
    items += quick(manager, pid)
    try:
        revision = hub_packages.content_revision(root)
    except WorkspaceError as exc:
        revision = ''
        items.append(_item('revision', 'stage', '内容一致性', False, str(exc)))
    return {'passed': all(item['passed'] for item in items), 'items': items, 'revision': revision,
            'head': _git(root, 'rev-parse', 'HEAD').strip(), 'changed': changed[:CHANGED_LIMIT],
            'changed_count': len(changed), 'checked_at': time.time()}


def failing(result):
    return [item['label'] for item in result['items'] if not item['passed']]


def groups(result):
    """The three check chips of a candidate (modeling stages, quality, binding) and the failing checks."""
    value = {group: all(item['passed'] for item in result['items'] if item['group'] == group)
             for group in ('stage', 'quality', 'binding')}
    return {**value, 'failing': failing(result)}
