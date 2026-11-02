#!/usr/bin/env python3
"""
全局一致性检查（详设 6.8 `core.consistency`、第 9 章建议 2「全部做完后再做一次全局一致性检查」）。

各片建完之后看三件事，确定性检查、不调用模型：

1. 关系：链接指不到任何条目；对象没有任何操作、规则、别的对象用到（孤立）；
2. 口径：建模蓝图里统一了叫法的术语，条目里还在用别的叫法；
3. 跨切片衔接（按片建模时）：一片的条目链接到另一片的对象，却没声明依赖那一片；条目不属于任何一片；
   规划在某一片里、主线上却没有的条目。

结果写进 `reports/consistency/consistency.md`（覆盖上一份；上一份里已勾掉的同一条结论保留勾选，便于你对某条
说「就这样」），每条一行 `- [ ] … (C-xxxxxxxx)`。没有未处理的条目时全局一致性这一步完成（flow.yaml 的
consistency_report、consistency_pending 探针）。

用法：python3 .claude/scripts/consistency_check.py [--json]
退出码：0 没有未处理的条目；1 有；2 没有可检查的条目。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import blueprint  # noqa: E402
import onto_metamodel  # noqa: E402

REPORT_REL = 'reports/consistency/consistency.md'
CHECKED_KINDS = ('object', 'operation', 'rule', 'metric')
KIND_LABELS = {'object': '对象', 'operation': '操作', 'rule': '规则', 'metric': '指标'}
_ID_RE = re.compile(r'\((C-[0-9a-f]{8})\)\s*$')


def _entries(root: Path) -> dict:
    """(类型, 名称) → 正文；操作含旧目录。"""
    found = {}
    for kind in CHECKED_KINDS:
        entity = onto_metamodel.BY_KIND[kind]
        for directory in entity.directories:
            base = root / directory
            if not base.is_dir():
                continue
            for path in sorted(base.glob(f'*{entity.suffix}')):
                if path.name.lower() == onto_metamodel.INDEX_FILE:
                    continue
                name = onto_metamodel.decode_filename(path.stem)
                if (kind, name) not in found:
                    found[(kind, name)] = path.read_text(encoding='utf-8', errors='replace')
    return found


def _finding(code: str, level: str, text: str, target: str) -> dict:
    digest = hashlib.sha256(f'{code}|{target}|{text}'.encode('utf-8')).hexdigest()[:8]
    return {'id': f'C-{digest}', 'code': code, 'level': level, 'text': text, 'target': target}


def _depends(doc: dict) -> dict:
    """每片（传递地）依赖的片。"""
    direct = {item['id']: set(item.get('depends_on') or []) for item in blueprint.live_slices(doc)}
    closure = {}
    for slice_id in direct:
        seen, stack = set(), list(direct[slice_id])
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack += list(direct.get(current, ()))
        closure[slice_id] = seen
    return closure


def check(root: Path) -> list:
    root = Path(root)
    entries = _entries(root)
    names = {name for _kind, name in entries}
    findings = []
    used = set()
    for (kind, name), text in entries.items():
        for link in onto_metamodel.unresolved(text, names):
            findings.append(_finding('dangling_link', 'error', f'「{name}」链接的「{link.full_name}」不存在', f'{kind}:{name}'))
        for target in onto_metamodel.link_names(text, names):
            if target != name:
                used.add(target)
    for (kind, name) in entries:
        if kind == 'object' and name not in used:
            findings.append(_finding('orphan_object', 'warning', f'对象「{name}」没有被任何条目用到', f'object:{name}'))
    doc = None
    try:
        doc = blueprint.load(root)
    except blueprint.BlueprintError:
        doc = None
    for term in ((doc or {}).get('terms') or {}).get('ambiguous') or []:
        chosen = term.get('chosen')
        if not chosen:
            continue
        for variant in term.get('variants') or []:
            if variant == chosen or not variant:
                continue
            for (kind, name), text in entries.items():
                if variant in onto_metamodel.strip_links(text) and chosen not in variant:
                    findings.append(_finding('term_variant', 'warning',
                                             f'「{name}」里还在用「{variant}」，口径统一为「{chosen}」', f'{kind}:{name}'))
    if doc is not None and blueprint.mode(doc) == 'sliced':
        findings += _cross_slice(doc, entries, names)
    return findings


def _cross_slice(doc: dict, entries: dict, names: set) -> list:
    findings = []
    owner = {}
    for item in blueprint.live_slices(doc):
        for kind, members in blueprint.members(item).items():
            for name in members:
                owner.setdefault((kind, name), item)
                if (kind, name) not in entries:
                    findings.append(_finding('planned_missing', 'warning',
                                             f'「{item["name"]}」规划了{KIND_LABELS[kind]}「{name}」，还没建',
                                             f'{kind}:{name}'))
    closure = _depends(doc)
    object_owner = {name: item for (kind, name), item in owner.items() if kind == 'object'}
    for (kind, name), text in entries.items():
        item = owner.get((kind, name))
        if item is None:
            if kind in blueprint.MEMBER_FIELDS:
                findings.append(_finding('unassigned', 'warning', f'「{name}」不属于任何一片', f'{kind}:{name}'))
            continue
        for target in onto_metamodel.link_names(text, names):
            other = object_owner.get(target)
            if other is None or other['id'] == item['id'] or other['id'] in closure.get(item['id'], set()):
                continue
            findings.append(_finding('undeclared_dependency', 'warning',
                                     f'「{item["name"]}」的「{name}」用到了「{other["name"]}」的对象「{target}」，没声明依赖这一片',
                                     f'{kind}:{name}'))
    return findings


def _previous_checked(path: Path) -> set:
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return set()
    checked = set()
    for line in lines:
        match = _ID_RE.search(line)
        if match and line.lstrip().startswith('- [x]'):
            checked.add(match.group(1))
    return checked


def write_report(root: Path, findings: list) -> tuple[Path, int]:
    path = Path(root) / REPORT_REL
    checked = _previous_checked(path)
    lines = ['# 全局一致性检查', '', f'检查时间：{time.strftime("%Y-%m-%d %H:%M:%S")}', '']
    pending = 0
    if not findings:
        lines.append('没有发现问题。')
    groups = (('关系', ('dangling_link', 'orphan_object')), ('口径', ('term_variant',)),
              ('跨切片衔接', ('undeclared_dependency', 'unassigned', 'planned_missing')))
    for title, codes in groups:
        items = [item for item in findings if item['code'] in codes]
        if not items:
            continue
        lines += [f'## {title}', '']
        for item in items:
            done = item['id'] in checked
            pending += 0 if done else 1
            lines.append(f"- [{'x' if done else ' '}] {item['text']} ({item['id']})")
        lines.append('')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines).rstrip() + '\n', encoding='utf-8')
    return path, pending


def main(argv: list) -> int:
    root = Path('.')
    if not _entries(root):
        print('error:还没有可检查的条目', file=sys.stderr)
        return 2
    findings = check(root)
    path, pending = write_report(root, findings)
    if '--json' in argv:
        print(json.dumps({'report': REPORT_REL, 'pending': pending, 'findings': findings}, ensure_ascii=False))
    else:
        print(f'{REPORT_REL}：{len(findings)} 条，未处理 {pending} 条')
    return 1 if pending else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
