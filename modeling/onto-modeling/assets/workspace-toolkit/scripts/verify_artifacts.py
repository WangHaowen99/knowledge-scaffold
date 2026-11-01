#!/usr/bin/env python3
"""轻量产物核验及修订回执；不调用模型、不修改业务文件。

生成收尾和提交 hook 共用此入口。stdout 默认仅摘要、最多 12 条截短定位，
完整问题保存在 .ontology-state/artifact-reviews/；--issues-json 仅供 hook 消费。
通过只表示指定产物的确定性检查通过，不代表业务覆盖、语义正确或子任务已结束。
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time

import check_diagrams
import table_profile
import ontology_checker

VERSION = 1
GROUPS = {
    'objects': ('object',),
    'operations': ('operation', 'logic', 'rule', 'metric'),
    'ontology': ('object', 'operation', 'logic', 'rule', 'metric', 'action'),
}
RECEIPT_DIR = Path('.ontology-state/artifact-reviews')


def _contained(root: Path, path: Path) -> Path:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('核验路径越出工作区')
    return path


def targets(root: Path, kind: str, names: list[str] | None = None) -> list[Path]:
    if kind == 'diagrams':
        candidates = sorted((root / 'ontology/diagrams').glob('*.drawio'))
    else:
        candidates = []
        for group in GROUPS[kind]:
            for path in (root / 'ontology' / group).rglob('*.md'):
                if path.name.lower() != 'index.md':
                    candidates.append(path)
        candidates.sort()
    candidates = [_contained(root, p) for p in candidates if p.is_file()]
    if names:
        selected = [p for p in candidates if p.stem in names or p.relative_to(root).as_posix() in names]
        found = set()
        for path in selected:
            found.update(set(names).intersection((path.stem, path.relative_to(root).as_posix())))
        if found != set(names):
            raise ValueError('部分指定产物不存在；不能把空检查当作通过')
        return selected
    return candidates


def input_digest(root: Path, mode: str) -> str:
    """包含全部交叉引用依赖、schema 和工具实现；不以大小/时间替代内容指纹。"""
    if mode == 'diagrams':
        paths = list((root / 'ontology/diagrams').glob('*.drawio'))
        paths.append(root / check_diagrams.DEFAULT_LEGEND)
    else:
        paths = list((root / 'ontology').rglob('*.md')) + list((root / 'research').glob('*.md'))
        materials = root / 'resource/materials.json'
        paths.append(materials)
        if materials.exists():
            directives = json.loads(materials.read_text(encoding='utf-8'))
            if not isinstance(directives, dict):
                raise ValueError('资料用途配置不可解析')
            paths += [root / rel for rel, value in directives.items()
                      if isinstance(value, dict) and value.get('purpose') == 'schema']
    digest = hashlib.sha256(f'{VERSION}:{mode}'.encode())
    for path in sorted(set(paths)):
        _contained(root, path)
        digest.update(path.relative_to(root).as_posix().encode() + b'\0')
        if path.is_file():
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
        else:
            digest.update(b'<missing>')
        digest.update(b'\0')
    # Checker upgrades invalidate earlier receipts without touching project data.
    for path in sorted(Path(__file__).parent.glob('*.py')):
        digest.update(path.name.encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


def _check(root: Path, mode: str, paths: list[Path]) -> list[dict]:
    if mode == 'diagrams':
        issues = []
        for path in paths:
            report = check_diagrams.check_diagram(path, root / check_diagrams.DEFAULT_LEGEND)
            for message in report['issues']:
                issues.append(dict(file=path.relative_to(root).as_posix(), severity='错误',
                                   category='流程图结构', message=message, source='规则检查'))
        return issues
    # All docs remain available as reference context; only the requested stems are checked.
    # The existing duplicate-stem check prevents ambiguous path/stem identities passing.
    # A supplied but unreadable table is not the same as having no table.
    # Load once (headers and value profiles) and reuse in the checker instead of silently skipping field checks.
    tables, profiles = table_profile.load_registered_tables(root)
    issues = ontology_checker.run_checks(
        root / 'ontology', target_files=[p.stem for p in paths], include_mock=True,
        tables=tables, strict_targets=True, profiles=profiles)
    return [dataclasses.asdict(issue) for issue in issues]


def _write_receipt(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.receipt-', delete=False) as stream:
            name = stream.name
            json.dump(data, stream, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


def verify(root: Path, kind: str, names: list[str] | None = None, *, status_only=False) -> dict:
    start = time.perf_counter()
    root = root.resolve()
    mode = 'diagrams' if kind == 'diagrams' else 'ontology'
    paths = targets(root, kind, names)
    result = dict(status='pending', reason='no_artifacts', checked_files=0, issues=[], cached=False)
    if not paths:
        return result
    inputs = input_digest(root, mode)
    relative_paths = [p.relative_to(root).as_posix() for p in paths]
    identity = hashlib.sha256(json.dumps([mode, inputs, relative_paths]).encode()).hexdigest()
    path = _contained(root, root / RECEIPT_DIR / f'{identity}.json')
    try:
        receipt = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        receipt = {}
    valid = (isinstance(receipt, dict) and receipt.get('version') == VERSION
             and receipt.get('inputs') == inputs and receipt.get('targets') == relative_paths
             and receipt.get('status') in ('passed', 'failed')
             and isinstance(receipt.get('issues'), list)
             and all(isinstance(i, dict) and i.get('severity') in ('错误', '警告', '建议')
                     for i in receipt.get('issues', [])))
    if valid:
        expected = 'failed' if any(i.get('severity') == '错误' for i in receipt.get('issues', [])) else 'passed'
        valid = receipt.get('status') == expected
    if valid:
        result = {**receipt, 'cached': True}
    elif not status_only:
        issues = _check(root, mode, paths)
        # Concurrent edits must never produce a passing receipt for a different revision.
        if inputs != input_digest(root, mode) or paths != targets(root, kind, names):
            result.update(reason='changed_during_check', issues=issues)
        else:
            result = dict(version=VERSION, status='failed' if any(i['severity'] == '错误' for i in issues)
                          else 'passed', reason='checked', inputs=inputs, targets=relative_paths,
                          checked_files=len(paths), checked_at=time.time(), issues=issues, cached=False)
            _write_receipt(path, result)
    else:
        result['reason'] = 'no_current_receipt'
    result.update(receipt=path.relative_to(root).as_posix(), elapsed_ms=round((time.perf_counter()-start)*1000, 3))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=('diagrams', *GROUPS))
    parser.add_argument('--files', nargs='+', help='本批产物的文件名 stem 或工作区相对路径')
    parser.add_argument('--status', action='store_true', help='只核对当前修订的回执，不重新检查')
    parser.add_argument('--issues-json', action='store_true', help='仅供提交 hook 读取完整问题数组')
    args = parser.parse_args()
    try:
        result = verify(Path.cwd(), args.kind, args.files, status_only=args.status)
    except Exception as exc:
        logging.error('轻量核验未完成：%s: %s', type(exc).__name__, str(exc)[:240])
        return 2
    output = logging.Logger('verify_artifacts.json', logging.INFO)
    output.addHandler(logging.StreamHandler(sys.stdout))
    if args.issues_json:
        output.info(json.dumps(result['issues'], ensure_ascii=False))
    else:
        summary = {k: result.get(k) for k in ('status', 'reason', 'checked_files', 'cached', 'elapsed_ms', 'receipt')}
        summary['errors'] = sum(i['severity'] == '错误' for i in result['issues'])
        summary['warnings'] = sum(i['severity'] == '警告' for i in result['issues'])
        by_category = {}
        for issue in result['issues']:
            if issue['severity'] in ('错误', '警告'):
                counts = by_category.setdefault(issue.get('category', ''), {'errors': 0, 'warnings': 0})
                counts['errors' if issue['severity'] == '错误' else 'warnings'] += 1
        summary['by_category'] = by_category
        # 错误排在前面：截断的 12 条定位里先看到会阻断的问题
        ordered = sorted(result['issues'], key=lambda i: {'错误': 0, '警告': 1}.get(i['severity'], 2))
        summary['details'] = [f"{i.get('file', '')}: {i.get('message', '')}"[:200] for i in ordered[:12]]
        output.info(json.dumps(summary, ensure_ascii=False))
    return {'passed': 0, 'failed': 1}.get(result['status'], 2)


if __name__ == '__main__':
    raise SystemExit(main())
