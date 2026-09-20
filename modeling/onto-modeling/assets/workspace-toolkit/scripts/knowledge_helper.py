#!/usr/bin/env python3
"""Knowledge helper — manages ontology/knowledge/*.md and INDEX.md.

Subcommands (added in subsequent tasks):
    add <type> <slug> <name> [--scope ...] [--confidence ...] [--source ...]
    list [--type ...] [--scope ...]
    remove <slug>
    show <slug>
    load [--scope ...] [--budget N]
    sync-index
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


KNOWLEDGE_DIR = Path('ontology/knowledge')
INDEX_PATH = KNOWLEDGE_DIR / 'INDEX.md'

VALID_TYPES = ('correction', 'convention', 'terminology', 'domain-rule')
VALID_CONFIDENCE = ('high', 'medium', 'low')
REQUIRED_FIELDS = ('name', 'type', 'slug', 'scopes', 'source', 'confidence', 'revisions', 'related')

CONFIDENCE_ALIAS = {
    '高': 'high', 'high': 'high', 'h': 'high',
    '中': 'medium', 'medium': 'medium', 'med': 'medium', 'm': 'medium',
    '低': 'low', 'low': 'low', 'l': 'low',
}

AFTER_CONFLICT_CHECK_CHOICES = (
    'none-found',
    'duplicate-suggested',
    'conflict-suggested',
    'relations-only',
    'skipped-with-reason',
)


@dataclass
class KnowledgeEntry:
    path: Path
    frontmatter: dict
    body: str

    @property
    def slug(self) -> str:
        return self.frontmatter['slug']


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML-ish frontmatter (restricted subset). Returns (fm_dict, body)."""
    if not text.startswith('---\n'):
        raise ValueError('文档缺少 frontmatter 起始 ---')
    end = text.find('\n---\n', 4)
    if end == -1:
        raise ValueError('文档缺少 frontmatter 结束 ---')
    fm_text = text[4:end]
    body = text[end + 5:]
    return _parse_yaml_subset(fm_text), body


def _parse_yaml_subset(text: str) -> dict:
    """Parse our restricted subset: scalars, lists of strings, lists of dicts."""
    out: dict[str, Any] = {}
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = re.match(r'^([A-Za-z_]+):\s*(.*)$', line)
        if not m:
            raise ValueError(f'frontmatter 解析失败：{line}')
        key, val = m.group(1), m.group(2).strip()
        if val:
            if val == '[]':
                out[key] = []
            else:
                out[key] = _strip_quotes(val)
            i += 1
            continue
        # multi-line: list of strings or list of dicts
        items: list = []
        i += 1
        while i < len(lines):
            sub = lines[i]
            if not sub.strip():
                i += 1
                continue
            # next top-level key starts with no leading whitespace and contains ':'
            if re.match(r'^[A-Za-z_]+:', sub):
                break
            sm = re.match(r'^\s+-\s+(.*)$', sub)
            if not sm:
                break
            entry = sm.group(1).strip()
            i += 1
            # peek for nested k: v lines (dict items)
            nested: dict[str, str] = {}
            if ':' in entry and not entry.startswith('"') and not entry.startswith('['):
                # could be inline `key: value` start of dict
                k, _, v = entry.partition(':')
                nested[k.strip()] = _strip_quotes(v.strip())
                while i < len(lines):
                    nxt = lines[i]
                    sm2 = re.match(r'^\s{4,}([A-Za-z_]+):\s*(.*)$', nxt)
                    if not sm2:
                        break
                    nested[sm2.group(1)] = _strip_quotes(sm2.group(2).strip())
                    i += 1
                items.append(nested)
            else:
                items.append(_strip_quotes(entry))
        out[key] = items
    return out


def _strip_quotes(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    return v


def dump_frontmatter(fm: dict, body: str) -> str:
    """Emit our restricted-YAML frontmatter. Stable key order = REQUIRED_FIELDS."""
    lines = ['---']
    for k in REQUIRED_FIELDS:
        v = fm.get(k)
        if isinstance(v, list):
            if not v:
                lines.append(f'{k}: []')
            else:
                lines.append(f'{k}:')
                for item in v:
                    if isinstance(item, dict):
                        first = True
                        for ik, iv in item.items():
                            prefix = '  - ' if first else '    '
                            lines.append(f'{prefix}{ik}: {_quote_if_needed(iv)}')
                            first = False
                    else:
                        lines.append(f'  - {_quote_if_needed(item)}')
        else:
            lines.append(f'{k}: {v}')
    lines.append('---')
    lines.append('')
    lines.append(body.rstrip() + '\n')
    return '\n'.join(lines)


def _quote_if_needed(v: str) -> str:
    if not isinstance(v, str):
        return str(v)
    if any(c in v for c in ':#[]{},&*!|>%@`') or v != v.strip():
        return f'"{v}"'
    return f'"{v}"' if (':' in v or v == '') else v


class KnowledgeError(Exception):
    """User-facing knowledge errors. Message is shown verbatim."""


def _emit_error(msg: str) -> None:
    """Print errors to BOTH stderr and stdout, defeating `2>/dev/null` redirects."""
    print(f'[knowledge_helper:error] {msg}', file=sys.stderr)
    print(f'[knowledge_helper:error] {msg}')


def _validate_inputs(type_: str, scopes: list, confidence: str) -> None:
    if type_ not in VALID_TYPES:
        raise KnowledgeError(
            f'类型必须是 {"/".join(VALID_TYPES)} 之一，收到：{type_}'
        )
    if not scopes:
        raise KnowledgeError('scopes 不能为空，至少写 "global"')
    if confidence not in VALID_CONFIDENCE:
        raise KnowledgeError(
            f'置信度必须是 {"/".join(VALID_CONFIDENCE)}，收到：{confidence}'
        )


def _entry_path(type_: str, slug: str) -> Path:
    return KNOWLEDGE_DIR / f'{type_}-{slug}.md'


INDEX_HEADER = (
    '# 业务知识索引\n\n'
    '> 由 /ontology-extract-object、/ontology-extract-operation、/ontology-trace 等命令在识别到专家经验/纠错时自动维护。\n'
    '> 每行一条：`- [标题](文件名.md) — 类型 | scopes: …`\n\n'
)


def _iter_entries() -> list:
    if not KNOWLEDGE_DIR.exists():
        return []
    out = []
    for f in KNOWLEDGE_DIR.glob('*.md'):
        if f.name == 'INDEX.md':
            continue
        try:
            fm, body = parse_frontmatter(f.read_text(encoding='utf-8'))
        except ValueError:
            continue
        out.append(KnowledgeEntry(path=f, frontmatter=fm, body=body))
    out.sort(key=lambda e: e.frontmatter.get('slug', e.path.name))
    return out


def sync_index() -> None:
    entries = _iter_entries()
    lines = [INDEX_HEADER.rstrip(), '']
    for e in entries:
        scopes = ', '.join(e.frontmatter.get('scopes', []))
        line = (
            f'- [{e.frontmatter["name"]}]({e.path.name}) — '
            f'{e.frontmatter["type"]} | scopes: {scopes}'
        )
        lines.append(line[:200])  # safety cap
    INDEX_PATH.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def add_entry(
    type_: str, slug: str, name: str, scopes: list,
    confidence: str, source: str, body: str,
) -> Path:
    _validate_inputs(type_, scopes, confidence)
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    path = _entry_path(type_, slug)
    if path.exists():
        raise KnowledgeError(
            f'业务知识 "{slug}" 已存在。如需修改，使用 revise 子命令。'
        )
    fm = {
        'name': name,
        'type': type_,
        'slug': slug,
        'scopes': scopes,
        'source': source,
        'confidence': confidence,
        'revisions': [],
        'related': [],
    }
    path.write_text(dump_frontmatter(fm, body), encoding='utf-8')
    sync_index()
    return path


def _load_by_slug(slug: str) -> KnowledgeEntry:
    for e in _iter_entries():
        if e.slug == slug:
            return e
    raise KnowledgeError(f'未找到业务知识：{slug}')


def list_entries(type_=None, scope=None) -> list:
    out = []
    for e in _iter_entries():
        if type_ and e.frontmatter['type'] != type_:
            continue
        if scope and scope not in e.frontmatter.get('scopes', []):
            continue
        out.append(e)
    return out


def remove_entry(slug: str) -> None:
    e = _load_by_slug(slug)
    e.path.unlink()
    sync_index()


def revise_entry(slug: str, new_body: str, summary: str, source: str) -> Path:
    e = _load_by_slug(slug)
    fm = dict(e.frontmatter)
    revisions = list(fm.get('revisions') or [])
    revisions.append({
        'at': time.strftime('%Y-%m-%dT%H:%M'),
        'summary': summary,
    })
    fm['revisions'] = revisions
    fm['source'] = source
    old_body = e.body.strip()
    merged = (
        new_body.rstrip()
        + '\n\n<details>\n<summary>历史版本</summary>\n\n'
        + old_body
        + '\n\n</details>\n'
    )
    e.path.write_text(dump_frontmatter(fm, merged), encoding='utf-8')
    sync_index()
    return e.path


def show_entry(slug: str) -> str:
    e = _load_by_slug(slug)
    return e.path.read_text(encoding='utf-8')


def load_for_scope(scopes, budget=10) -> str:
    """Return a markdown block compact enough to inject into a subagent prompt.

    Always includes all `global` entries; for non-global scopes, includes up to
    `budget` matched entries (most recently modified first).
    """
    all_entries = _iter_entries()
    if not all_entries:
        return '（暂无相关业务知识）\n'

    global_entries = [e for e in all_entries if 'global' in e.frontmatter.get('scopes', [])]
    targeted = []
    for e in all_entries:
        if 'global' in e.frontmatter.get('scopes', []):
            continue
        if any(s in e.frontmatter.get('scopes', []) for s in scopes):
            targeted.append(e)

    targeted.sort(key=lambda e: e.path.stat().st_mtime, reverse=True)
    targeted = targeted[:budget]

    if not global_entries and not targeted:
        return '（暂无相关业务知识）\n'

    confidence_label = {'high': '高', 'medium': '中', 'low': '低'}
    lines = ['# 已沉淀业务知识（自动加载）', '']
    for e in global_entries + targeted:
        lines.append(f'## {e.frontmatter["name"]} ({e.frontmatter["type"]})')
        scopes_str = ', '.join(e.frontmatter.get('scopes', []))
        conf = e.frontmatter.get('confidence', 'medium')
        lines.append(f'_scopes: {scopes_str} · 置信度: {confidence_label.get(conf, conf)}_')
        lines.append('')
        # Strip the <details> historical block when loading — historical noise.
        body = re.split(r'\n<details>', e.body, maxsplit=1)[0].strip()
        lines.append(body)
        lines.append('')
    return '\n'.join(lines) + '\n'


def main():
    import argparse

    class _DualWriteParser(argparse.ArgumentParser):
        """Override argparse to also echo errors on stdout so `2>/dev/null` cannot hide them."""

        def error(self, message):
            self.print_usage(sys.stderr)
            _emit_error(f'{self.prog}: {message}')
            sys.exit(2)

    p = _DualWriteParser(description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True, parser_class=_DualWriteParser)

    a = sub.add_parser('add')
    a.add_argument('--type', dest='type_', required=True, choices=VALID_TYPES)
    a.add_argument('--slug', required=True)
    a.add_argument('--name', '--title', dest='name', required=True,
                   help='一句话标题；--title 为兼容别名')
    a.add_argument('--scope', action='append', required=True, dest='scopes')
    a.add_argument('--confidence', default='medium',
                   help='high/medium/low（接受 高/中/低 中文别名）')
    a.add_argument('--source', required=True)
    a.add_argument('--body', '--content', dest='body', required=True,
                   help='知识正文；--content 为兼容别名')
    a.add_argument('--after-conflict-check', dest='after_conflict_check',
                   required=True, choices=AFTER_CONFLICT_CHECK_CHOICES,
                   help='必填：声明 knowledge-conflict-agent 的结论或为何跳过。'
                        'skipped-with-reason 需同时提供 --skip-reason。')
    a.add_argument('--skip-reason', dest='skip_reason', default='',
                   help='仅当 --after-conflict-check=skipped-with-reason 时必填')

    r = sub.add_parser('revise')
    r.add_argument('--slug', required=True)
    r.add_argument('--summary', required=True)
    r.add_argument('--source', required=True)
    r.add_argument('--body', '--content', dest='body', required=True,
                   help='新版正文；--content 为兼容别名')

    rm = sub.add_parser('remove')
    rm.add_argument('--slug', required=True)

    ls = sub.add_parser('list')
    ls.add_argument('--type', dest='type_', choices=VALID_TYPES)
    ls.add_argument('--scope')

    sh = sub.add_parser('show')
    sh.add_argument('--slug', required=True)

    ld = sub.add_parser('load')
    ld.add_argument('--scope', action='append', default=[], dest='scopes')
    ld.add_argument('--budget', type=int, default=10)

    sub.add_parser('sync-index')

    args = p.parse_args()
    try:
        if args.cmd == 'add':
            conf = CONFIDENCE_ALIAS.get(args.confidence.strip().lower(), args.confidence)
            if conf not in VALID_CONFIDENCE:
                raise KnowledgeError(
                    f'置信度必须是 {"/".join(VALID_CONFIDENCE)}（或中文别名 高/中/低），收到：{args.confidence}'
                )
            if args.after_conflict_check == 'skipped-with-reason' and not args.skip_reason.strip():
                raise KnowledgeError(
                    '--after-conflict-check=skipped-with-reason 时必须同时提供 --skip-reason，'
                    '解释为何在未经 knowledge-conflict-agent 审查的情况下直接落盘。'
                )
            path = add_entry(
                args.type_, args.slug, args.name, args.scopes,
                conf, args.source, args.body,
            )
            print(f'已沉淀业务知识：{path}')
            if args.after_conflict_check == 'skipped-with-reason':
                print(f'⚠ 未经 knowledge-conflict-agent 审查直接落盘，理由：{args.skip_reason}')
        elif args.cmd == 'revise':
            path = revise_entry(args.slug, args.body, args.summary, args.source)
            print(f'已更新业务知识：{path}')
        elif args.cmd == 'remove':
            remove_entry(args.slug)
            print('已删除')
        elif args.cmd == 'list':
            for e in list_entries(type_=args.type_, scope=args.scope):
                print(f'{e.slug}\t{e.frontmatter["type"]}\t{e.frontmatter["name"]}')
        elif args.cmd == 'show':
            print(show_entry(args.slug))
        elif args.cmd == 'load':
            sys.stdout.write(load_for_scope(args.scopes, budget=args.budget))
        elif args.cmd == 'sync-index':
            sync_index()
            print('ok')
    except KnowledgeError as e:
        _emit_error(str(e))
        sys.exit(1)


if __name__ == '__main__':
    main()
