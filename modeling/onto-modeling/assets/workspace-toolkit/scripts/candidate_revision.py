#!/usr/bin/env python3
"""修正计划 → 候选 → 差异 → 整计划校验 → 接受 / 拒绝：已确认的本体文档不被直接覆盖。

修改已确认的 object / operation / rule / metric 文档时，先登记修正计划，在候选副本上改，
看差异、跑整计划校验，用户确认后才写回 ontology/；拒绝不碰工作区文件。
不调用模型。全部状态存放在 .ontology-state/revisions/<修订号>/：
  manifest.json（计划、基线指纹、状态）、base/（开始时的确认版本）、candidate/（候选，在这里改）、
  diff.json（完整差异）、check.json（校验结论与它绑定的指纹）。

用法（工作区根目录执行）：
  python3 .claude/scripts/candidate_revision.py start --plan ontology/operation/X.md ... [--note 修正说明]
  python3 .claude/scripts/candidate_revision.py diff   [--id 修订号]
  python3 .claude/scripts/candidate_revision.py check  [--id 修订号]
  python3 .claude/scripts/candidate_revision.py accept [--id 修订号]
  python3 .claude/scripts/candidate_revision.py reject [--id 修订号] [--reason 原因]
  python3 .claude/scripts/candidate_revision.py show   [--id 修订号]
  python3 .claude/scripts/candidate_revision.py list
不给 --id 时取最近一个未处理的修订。

check 在临时副本里叠加候选，对确认版本与叠加版本各跑一遍全量规则检查：候选引入新的「错误」
即不通过。结论绑定候选、确认版本和全部输入（资料、数据表、其余文档、检查脚本）的指纹，
accept 时任一变化都要求重新 check。不支持删除文档。

stdout 是单个 JSON 对象。最坏输出量：diff 最多 400 行差异（每行截到 300 字符，约 120KB 上限，
通常远小于此），check 各类问题最多 30 条（每条截到 200 字符，约 25KB），list 最多 50 条；
完整内容在 diff.json / check.json。
退出码：0 成功（check 通过、accept 完成）；1 校验未通过或被拒绝执行；2 用法错误或修订不存在。
"""
from __future__ import annotations

import argparse
import dataclasses
import difflib
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import check_eval_data
import ontology_checker
import verify_artifacts

REVISIONS_DIR = Path('.ontology-state/revisions')
ALLOWED_PATH_RE = re.compile(r'ontology/(?:object|operation|logic|rule|metric)/(?:[^/]+/)*[^/]+\.md')
REVISION_ID_RE = re.compile(r'\d{8}-\d{6}-[0-9a-f]{4}')
ISSUE_LIMIT = 30
MESSAGE_LIMIT = 200
DIFF_FILE_LINES = 150
DIFF_TOTAL_LINES = 400
DIFF_LINE_CHARS = 300
LIST_LIMIT = 50
_LINE_NUMBER_RE = re.compile(r'第 \d+ 行')


class RevisionError(Exception):
    """用法错误或修订不存在（退出码 2）。"""


# ---------------------------------------------------------------------------
# 路径与存储
# ---------------------------------------------------------------------------

def _no_symlink(base: Path, rel: str) -> None:
    current = base
    for part in rel.split('/'):
        current = current / part
        if current.is_symlink():
            raise RevisionError(f'路径含符号链接，不能修订：{rel}')


def plan_path(root: Path, value: str) -> str:
    """修正计划里的文档路径（工作区相对）；只允许会进知识包的本体文档，不含 index.md。"""
    rel = str(value).strip()
    rel = rel[2:] if rel.startswith('./') else rel
    parts = rel.split('/')
    unsafe = '\\' in rel or any(p in ('', '.', '..') for p in parts)
    if unsafe or not ALLOWED_PATH_RE.fullmatch(rel) or parts[-1].lower() == 'index.md':
        raise RevisionError(f'只能修订 ontology/object|operation|logic|rule|metric 下的 .md 文档：{value}')
    _no_symlink(root, rel)
    if not (root / rel).resolve().is_relative_to((root / 'ontology').resolve()):
        raise RevisionError(f'路径越出 ontology 目录：{value}')
    return rel


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=1)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _revision_dir(root: Path, revision_id: str) -> Path:
    if not REVISION_ID_RE.fullmatch(revision_id or ''):
        raise RevisionError(f'修订号格式不对：{revision_id}')
    folder = root / REVISIONS_DIR / revision_id
    if folder.is_symlink() or not (folder / 'manifest.json').is_file():
        raise RevisionError(f'修订不存在：{revision_id}')
    return folder


def _all_manifests(root: Path) -> list:
    folder = root / REVISIONS_DIR
    if not folder.is_dir():
        return []
    manifests = []
    for child in sorted(folder.iterdir(), reverse=True):
        if REVISION_ID_RE.fullmatch(child.name) and not child.is_symlink():
            manifest = _read_json(child / 'manifest.json')
            if manifest.get('id') == child.name:
                manifests.append(manifest)
    return manifests


def load_revision(root: Path, revision_id: str | None) -> tuple[Path, dict]:
    """(修订目录, manifest)；manifest 里的计划路径每次都重新校验，不信任磁盘上的内容。"""
    if not revision_id:
        open_ids = [m['id'] for m in _all_manifests(root) if m.get('status') == 'open']
        if not open_ids:
            raise RevisionError('没有未处理的修订；先用 start 登记修正计划')
        revision_id = open_ids[0]
    folder = _revision_dir(root, revision_id)
    manifest = _read_json(folder / 'manifest.json')
    plan = manifest.get('plan')
    if manifest.get('id') != revision_id or not isinstance(plan, list) or not plan:
        raise RevisionError(f'修订记录损坏：{revision_id}')
    for rel in plan:
        plan_path(root, rel)
        _no_symlink(folder / 'candidate', rel)
    return folder, manifest


def _files_digest(base: Path, plan: list) -> str:
    digest = hashlib.sha256()
    for rel in sorted(plan):
        digest.update(rel.encode() + b'\0')
        path = base / rel
        digest.update(path.read_bytes() if path.is_file() else b'<missing>')
        digest.update(b'\0')
    return digest.hexdigest()


def _read_text(path: Path) -> str | None:
    return path.read_text(encoding='utf-8') if path.is_file() else None


def _brief(issue: dict) -> dict:
    return {'file': issue['file'], 'severity': issue['severity'], 'category': issue['category'],
            'message': issue['message'][:MESSAGE_LIMIT]}


def _issue_key(issue: dict) -> tuple:
    # 行号随候选增删行漂移，比较时不计；同一问题挪了位置不算新问题
    return (issue['file'], issue['severity'], issue['category'],
            _LINE_NUMBER_RE.sub('第 N 行', issue['message']))


def _minus(issues: list, other: list) -> list:
    """issues 里多出 other 的部分（按问题计数比较）。"""
    remaining = Counter(map(_issue_key, issues)) - Counter(map(_issue_key, other))
    extra = []
    for issue in issues:
        key = _issue_key(issue)
        if remaining[key] > 0:
            remaining[key] -= 1
            extra.append(issue)
    return extra


def _run_checks(ontology_dir: Path, tables: dict, profiles: dict) -> list:
    issues = ontology_checker.run_checks(ontology_dir, include_mock=True, tables=tables, profiles=profiles)
    prefix = str(ontology_dir)
    return [{**item, 'message': item['message'].replace(prefix, 'ontology')}
            for item in map(dataclasses.asdict, issues)]


def _overlay(root: Path, folder: Path, plan: list, workdir: Path) -> Path:
    """临时副本：当前 ontology/ 与 research/ 报告，叠加候选。数据表仍读工作区原件。"""
    ontology = workdir / 'ontology'
    if (root / 'ontology').is_dir():
        shutil.copytree(root / 'ontology', ontology, symlinks=True)
    else:
        ontology.mkdir()
    if (root / 'research').is_dir():
        (workdir / 'research').mkdir()
        for path in (root / 'research').glob('*.md'):
            if path.is_file():
                shutil.copyfile(path, workdir / 'research' / path.name)
    for rel in plan:
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(folder / 'candidate' / rel, target)
    return ontology


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def start(root: Path, values: list, note: str = '') -> tuple[dict, int]:
    plan = list(dict.fromkeys(plan_path(root, value) for value in values))
    if not plan:
        raise RevisionError('修正计划为空：--plan 列出要修改的文档')
    _no_symlink(root, REVISIONS_DIR.as_posix())
    busy = [m['id'] for m in _all_manifests(root)
            if m.get('status') == 'open' and set(m.get('plan') or ()) & set(plan)]
    if busy:
        return {'status': 'refused', 'reason': f'修订 {busy[0]} 也涉及这些文档且尚未处理：先 accept 或 reject'}, 1
    revision_id = f'{time.strftime("%Y%m%d-%H%M%S")}-{secrets.token_hex(2)}'
    folder = root / REVISIONS_DIR / revision_id
    folder.mkdir(parents=True)
    for rel in plan:
        for side in ('base', 'candidate'):
            (folder / side / rel).parent.mkdir(parents=True, exist_ok=True)
            if (root / rel).is_file():
                shutil.copyfile(root / rel, folder / side / rel)
    new_files = [rel for rel in plan if not (folder / 'base' / rel).is_file()]
    _write_json(folder / 'manifest.json', {
        'version': 1, 'id': revision_id, 'status': 'open', 'created_at': time.time(),
        'note': (note or '')[:500], 'plan': plan, 'new_files': new_files,
        'base_digest': _files_digest(folder / 'base', plan),
    })
    candidate_root = (REVISIONS_DIR / revision_id / 'candidate').as_posix()
    return {
        'id': revision_id, 'status': 'open',
        'files': [{'path': rel, 'candidate': f'{candidate_root}/{rel}', 'new': rel in new_files} for rel in plan],
        'next': '只在 candidate 路径上修改（新文档在该路径新建），改完运行 diff 与 check；不要直接改 ontology/ 下的文件',
    }, 0


def _file_diff(folder: Path, rel: str) -> dict:
    before, after = _read_text(folder / 'base' / rel), _read_text(folder / 'candidate' / rel)
    if after is None:
        change = 'deleted' if before is not None else 'missing'
    elif before is None:
        change = 'added'
    else:
        change = 'unchanged' if before == after else 'modified'
    lines = list(difflib.unified_diff((before or '').splitlines(), (after or '').splitlines(),
                                      f'base/{rel}', f'candidate/{rel}', n=2, lineterm=''))
    return {
        'path': rel, 'change': change,
        'added_lines': sum(1 for line in lines if line.startswith('+') and not line.startswith('+++')),
        'removed_lines': sum(1 for line in lines if line.startswith('-') and not line.startswith('---')),
        'diff': lines,
    }


def diff(root: Path, revision_id: str | None) -> tuple[dict, int]:
    folder, manifest = load_revision(root, revision_id)
    files = [_file_diff(folder, rel) for rel in manifest['plan']]
    _write_json(folder / 'diff.json', {'id': manifest['id'], 'created_at': time.time(),
                                       'candidate_digest': _files_digest(folder / 'candidate', manifest['plan']),
                                       'files': files})
    shown, budget = [], DIFF_TOTAL_LINES
    for item in files:
        lines = item['diff'][:max(min(DIFF_FILE_LINES, budget), 0)]
        budget -= len(lines)
        shown.append({**item, 'diff': [line[:DIFF_LINE_CHARS] for line in lines],
                      'diff_truncated': len(lines) < len(item['diff'])})
    return {'id': manifest['id'], 'status': manifest['status'], 'files': shown,
            'diff_file': (REVISIONS_DIR / manifest['id'] / 'diff.json').as_posix()}, 0


def _plan_issues(issues: list, plan: list) -> list:
    files = {rel.removeprefix('ontology/') for rel in plan}
    return [issue for issue in issues if issue['file'] in files]


def check(root: Path, revision_id: str | None) -> tuple[dict, int]:
    """整计划校验：确认版本与叠加候选后的版本各跑一遍全量规则检查，候选不得引入新的错误。"""
    folder, manifest = load_revision(root, revision_id)
    plan = manifest['plan']
    if manifest.get('status') != 'open':
        return {'id': manifest['id'], 'status': 'refused', 'reason': f'修订已{manifest.get("status")}，不能再校验'}, 1
    inputs = verify_artifacts.input_digest(root, 'ontology')
    candidate_digest = _files_digest(folder / 'candidate', plan)
    report = {'id': manifest['id'], 'checked_at': time.time(), 'base_digest': manifest['base_digest'],
              'candidate_digest': candidate_digest, 'input_digest': inputs}
    missing = [rel for rel in plan if not (folder / 'candidate' / rel).is_file()]
    if _files_digest(root, plan) != manifest['base_digest']:
        report.update(status='stale', reason='确认版本在修订开始后被改动：reject 本修订后重新 start')
    elif missing:
        report.update(status='failed', reason=f'候选缺失（不支持删除文档）：{"、".join(missing[:10])}')
    elif candidate_digest == manifest['base_digest']:
        report.update(status='no_changes', reason='候选与确认版本相同，没有可接受的修改')
    else:
        try:
            tables, profiles = check_eval_data.load_registered_tables(root)
        except ValueError as error:
            report.update(status='unverifiable', reason=f'无法核验：{error}')
            _write_json(folder / 'check.json', report)
            return {key: report.get(key) for key in ('id', 'status', 'reason')}, 2
        before = _run_checks(root / 'ontology', tables, profiles)
        with tempfile.TemporaryDirectory(prefix='revision-check-') as workdir:
            after = _run_checks(_overlay(root, folder, plan, Path(workdir)), tables, profiles)
        new, resolved = _minus(after, before), _minus(before, after)
        new_errors = [i for i in new if i['severity'] == '错误']
        report.update(
            status='failed' if new_errors else 'passed',
            reason='候选引入了新的错误' if new_errors else '',
            new_errors=new_errors,
            new_warnings=[i for i in new if i['severity'] == '警告'],
            resolved_errors=[i for i in resolved if i['severity'] == '错误'],
            open_errors_in_plan=[i for i in _plan_issues(after, plan)
                                 if i['severity'] == '错误' and i not in new_errors],
        )
        if (verify_artifacts.input_digest(root, 'ontology') != inputs
                or _files_digest(folder / 'candidate', plan) != candidate_digest):
            report.update(status='stale', reason='校验期间候选或输入有变化：重新 check')
    _write_json(folder / 'check.json', report)
    shown = {key: report.get(key) for key in ('id', 'status', 'reason')}
    for key in ('new_errors', 'new_warnings', 'resolved_errors', 'open_errors_in_plan'):
        issues = report.get(key)
        if isinstance(issues, list):
            shown[key] = [_brief(issue) for issue in issues[:ISSUE_LIMIT]]
            shown[f'{key}_count'] = len(issues)
    shown['check_file'] = (REVISIONS_DIR / manifest['id'] / 'check.json').as_posix()
    return shown, 0 if report.get('status') == 'passed' else 1


def _write_atomic(target: Path, data: bytes) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix='.revision-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.chmod(name, target.stat().st_mode & 0o777 if target.exists() else 0o644)
    except BaseException:
        os.unlink(name)
        raise
    return name


def accept(root: Path, revision_id: str | None) -> tuple[dict, int]:
    """写回候选。只接受通过的整计划校验，且候选、确认版本、全部输入自校验后都没变。"""
    folder, manifest = load_revision(root, revision_id)
    plan = manifest['plan']
    report = _read_json(folder / 'check.json')
    if manifest.get('status') != 'open':
        reason = f'修订已{manifest.get("status")}'
    elif report.get('status') != 'passed':
        reason = '没有通过的整计划校验：先运行 check 并处理新增错误'
    elif any(not (folder / 'candidate' / rel).is_file() for rel in plan):
        reason = '候选缺失（不支持删除文档）'
    elif report.get('candidate_digest') != _files_digest(folder / 'candidate', plan):
        reason = '候选在校验后被修改：重新 check'
    elif (report.get('base_digest') != manifest['base_digest']
          or _files_digest(root, plan) != manifest['base_digest']):
        reason = '确认版本在修订开始后被改动：reject 本修订后重新 start'
    elif report.get('input_digest') != verify_artifacts.input_digest(root, 'ontology'):
        reason = '资料、数据表、其余文档或检查脚本在校验后有变化：重新 check'
    else:
        reason = ''
    if reason:
        return {'id': manifest['id'], 'status': 'refused', 'reason': reason}, 1
    staged = []
    try:
        for rel in plan:
            _no_symlink(root, rel)
            staged.append((_write_atomic(root / rel, (folder / 'candidate' / rel).read_bytes()), root / rel))
        for name, target in staged:
            os.replace(name, target)
    finally:
        for name, _ in staged:
            if os.path.exists(name):
                os.unlink(name)
    manifest.update(status='accepted', accepted_at=time.time(), accepted_digest=report['candidate_digest'])
    _write_json(folder / 'manifest.json', manifest)
    return {'id': manifest['id'], 'status': 'accepted', 'written': plan,
            'next': '已写回；确认版本保存在本修订的 base/。提交前仍需运行 verify_artifacts.py 收尾核验'}, 0


def reject(root: Path, revision_id: str | None, reason: str = '') -> tuple[dict, int]:
    """放弃候选：只改修订状态，工作区文件不动。"""
    folder, manifest = load_revision(root, revision_id)
    if manifest.get('status') != 'open':
        return {'id': manifest['id'], 'status': 'refused', 'reason': f'修订已{manifest.get("status")}'}, 1
    manifest.update(status='rejected', rejected_at=time.time(), reason=(reason or '')[:500])
    _write_json(folder / 'manifest.json', manifest)
    return {'id': manifest['id'], 'status': 'rejected', 'note': '工作区文件未改动'}, 0


def show(root: Path, revision_id: str | None) -> tuple[dict, int]:
    folder, manifest = load_revision(root, revision_id)
    report = _read_json(folder / 'check.json')
    check_state = {key: report.get(key) for key in ('status', 'reason') if key in report}
    for key in ('new_errors', 'new_warnings', 'resolved_errors', 'open_errors_in_plan'):
        if isinstance(report.get(key), list):
            check_state[f'{key}_count'] = len(report[key])
    if report and report.get('candidate_digest') != _files_digest(folder / 'candidate', manifest['plan']):
        check_state['outdated'] = True  # 候选在校验后又改过
    changes = [{key: item[key] for key in ('path', 'change', 'added_lines', 'removed_lines')}
               for item in (_file_diff(folder, rel) for rel in manifest['plan'])]
    return {**{key: manifest.get(key) for key in ('id', 'status', 'note', 'plan', 'new_files', 'reason')},
            'changes': changes, 'check': check_state or {'status': 'not_run'}}, 0


def list_revisions(root: Path) -> tuple[dict, int]:
    manifests = _all_manifests(root)
    return {'revisions': [{'id': m['id'], 'status': m.get('status'), 'files': len(m.get('plan') or ()),
                           'note': (m.get('note') or '')[:80]} for m in manifests[:LIST_LIMIT]],
            'total': len(manifests)}, 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description='候选修订：修正计划、差异、整计划校验、接受 / 拒绝（输出 JSON）')
    commands = parser.add_subparsers(dest='command', required=True)
    start_parser = commands.add_parser('start', help='登记修正计划并建立候选副本')
    start_parser.add_argument('--plan', nargs='+', required=True, help='要修改或新建的本体文档（工作区相对路径）')
    start_parser.add_argument('--note', default='', help='修正说明')
    for name in ('diff', 'check', 'accept', 'show'):
        commands.add_parser(name).add_argument('--id', default=None)
    reject_parser = commands.add_parser('reject')
    reject_parser.add_argument('--id', default=None)
    reject_parser.add_argument('--reason', default='')
    commands.add_parser('list')
    args = parser.parse_args(argv)
    root = Path.cwd().resolve()
    try:
        if args.command == 'start':
            result, code = start(root, args.plan, args.note)
        elif args.command == 'reject':
            result, code = reject(root, args.id, args.reason)
        elif args.command == 'list':
            result, code = list_revisions(root)
        else:
            result, code = {'diff': diff, 'check': check, 'accept': accept, 'show': show}[args.command](root, args.id)
    except (RevisionError, ValueError, OSError) as error:
        result, code = {'status': 'error', 'reason': str(error)[:300]}, 2
    output = logging.Logger('candidate_revision.json', logging.INFO)
    output.addHandler(logging.StreamHandler(sys.stdout))
    output.info(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
