#!/usr/bin/env python3
"""Reverse query / search over the ontology.

Query modes:
    --used-by <stem>        list all docs that reference [[stem]]
    --orphans               list docs that nothing references
    --search <keyword>      find any doc containing the keyword
    --unused-objects        objects not referenced by any operation
    --unused-actions        actions not referenced by any operation
    --dangling              broken [[link]] targets

Output: markdown table by default, --json for machine-readable.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from ontology_checker import load_ontology


def query_used_by(stem: str, all_docs: dict) -> list:
    """Return list of stems whose document contains [[stem]]."""
    return sorted(
        other_stem for other_stem, doc in all_docs.items()
        if other_stem != stem and stem in doc.obsidian_links
    )


def query_orphans(all_docs: dict) -> dict:
    """Return {doc_type: [stems not referenced by anyone]}."""
    referenced = set()
    for doc in all_docs.values():
        referenced.update(doc.obsidian_links)
    orphans = {'object': [], 'operation': [], 'action': []}
    for stem, doc in all_docs.items():
        if stem not in referenced:
            orphans.setdefault(doc.doc_type, []).append(stem)
    for k in orphans:
        orphans[k].sort()
    return orphans


def query_search(keyword: str, all_docs: dict) -> list:
    """Return list of (stem, doc_type, line_no, excerpt)."""
    hits = []
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    for stem, doc in sorted(all_docs.items()):
        for i, line in enumerate(doc.raw.split('\n'), start=1):
            if pattern.search(line):
                excerpt = line.strip()
                if len(excerpt) > 80:
                    excerpt = excerpt[:77] + '...'
                hits.append((stem, doc.doc_type, i, excerpt))
    return hits


def query_unused(all_docs: dict, target_type: str) -> list:
    """Objects/actions not referenced by any operation."""
    referenced = set()
    for doc in all_docs.values():
        if doc.doc_type == 'operation':
            referenced.update(doc.obsidian_links)
            # action invocations
            for row in doc.action_rows:
                action_name = row.get('动作名称', '').strip()
                m = re.match(r'\[\[([^\]]+)\]\]', action_name)
                if m:
                    referenced.add(m.group(1))
    return sorted(
        stem for stem, doc in all_docs.items()
        if doc.doc_type == target_type and stem not in referenced
    )


def query_dangling(all_docs: dict) -> list:
    """Return list of (source_stem, broken_link)."""
    dangling = []
    for stem, doc in sorted(all_docs.items()):
        for link in doc.obsidian_links:
            if link not in all_docs:
                dangling.append((stem, link))
    return dangling


def query_knowledge(keyword: str) -> list:
    """Search across ontology/knowledge/*.md (excluding INDEX). Match against
    name, body, scopes, type. Returns list of dicts.
    """
    knowledge_dir = Path('ontology/knowledge')
    if not knowledge_dir.exists():
        return []
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    hits = []
    for f in sorted(knowledge_dir.glob('*.md')):
        if f.name == 'INDEX.md':
            continue
        text = f.read_text(encoding='utf-8')
        if pattern.search(text):
            name_match = re.search(r'^name:\s*(.+)$', text, re.MULTILINE)
            type_match = re.search(r'^type:\s*(.+)$', text, re.MULTILINE)
            hits.append({
                'file': f.name,
                'name': name_match.group(1).strip() if name_match else f.stem,
                'type': type_match.group(1).strip() if type_match else 'unknown',
            })
    return hits


def format_knowledge(keyword: str, hits: list) -> str:
    if not hits:
        return f'## 业务知识搜索 `{keyword}`\n\n**未找到匹配条目。**'
    lines = [f'## 业务知识搜索 `{keyword}`', '', f'共 {len(hits)} 条匹配：', '']
    for h in hits:
        lines.append(f'- **{h["name"]}** ({h["type"]}) — `{h["file"]}`')
    return '\n'.join(lines)


def format_used_by(stem: str, results: list) -> str:
    if not results:
        return f'## 谁引用了 `{stem}`\n\n**没有任何文档引用。**'
    lines = [f'## 谁引用了 `{stem}`', '', f'共 {len(results)} 个文档：', '']
    for r in results:
        lines.append(f'- [[{r}]]')
    return '\n'.join(lines)


def format_orphans(orphans: dict) -> str:
    total = sum(len(v) for v in orphans.values())
    lines = [f'## 孤立文档（无任何引用）', '']
    if total == 0:
        lines.append('无孤立文档。')
        return '\n'.join(lines)
    cn = {'object': '对象', 'operation': '操作', 'action': '动作'}
    for dtype in ('object', 'operation', 'action'):
        items = orphans.get(dtype, [])
        if not items:
            continue
        lines.append(f'### {cn[dtype]}（{len(items)}）')
        for stem in items:
            lines.append(f'- [[{stem}]]')
        lines.append('')
    return '\n'.join(lines)


def format_search(keyword: str, hits: list) -> str:
    if not hits:
        return f'## 搜索 `{keyword}`\n\n**未找到任何匹配。**'
    lines = [f'## 搜索 `{keyword}`', '', f'共 {len(hits)} 处匹配：', '']
    current = None
    for stem, dtype, line_no, excerpt in hits:
        if stem != current:
            lines.append(f'### {stem}（{dtype}）')
            current = stem
        lines.append(f'- L{line_no}: {excerpt}')
    return '\n'.join(lines)


def format_unused(target_type: str, results: list) -> str:
    cn = {'object': '对象', 'action': '动作'}[target_type]
    if not results:
        return f'## 未被任何逻辑引用的{cn}\n\n无。'
    lines = [f'## 未被任何逻辑引用的{cn}（{len(results)}）', '']
    for stem in results:
        lines.append(f'- [[{stem}]]')
    return '\n'.join(lines)


def format_dangling(results: list) -> str:
    if not results:
        return '## 失效链接\n\n无失效链接。'
    lines = [f'## 失效链接（{len(results)}）', '']
    current = None
    for source, broken in results:
        if source != current:
            lines.append(f'### {source}')
            current = source
        lines.append(f'- 指向不存在的 [[{broken}]]')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Reverse query over ontology.')
    parser.add_argument('--ontology-dir', default='ontology')
    parser.add_argument('--json', action='store_true')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--used-by', metavar='STEM', help='List docs that reference [[STEM]]')
    group.add_argument('--orphans', action='store_true', help='List unreferenced docs')
    group.add_argument('--search', metavar='KEYWORD', help='Free-text search')
    group.add_argument('--unused-objects', action='store_true', help='Objects not used by any operation')
    group.add_argument('--unused-actions', action='store_true', help='Actions not invoked by any operation')
    group.add_argument('--dangling', action='store_true', help='Broken [[link]] targets')
    group.add_argument('--knowledge', metavar='KEYWORD',
                       help='Search business knowledge entries')
    args = parser.parse_args()

    ontology_dir = Path(args.ontology_dir)
    if not ontology_dir.exists():
        print(f'Error: ontology dir not found: {ontology_dir}', file=sys.stderr)
        sys.exit(1)

    all_docs = load_ontology(ontology_dir)

    if args.used_by:
        res = query_used_by(args.used_by, all_docs)
        out = res if args.json else format_used_by(args.used_by, res)
    elif args.orphans:
        res = query_orphans(all_docs)
        out = res if args.json else format_orphans(res)
    elif args.search:
        res = query_search(args.search, all_docs)
        out = [{'stem': s, 'type': t, 'line': l, 'excerpt': e} for s, t, l, e in res] if args.json else format_search(args.search, res)
    elif args.unused_objects:
        res = query_unused(all_docs, 'object')
        out = res if args.json else format_unused('object', res)
    elif args.unused_actions:
        res = query_unused(all_docs, 'action')
        out = res if args.json else format_unused('action', res)
    elif args.dangling:
        res = query_dangling(all_docs)
        out = [{'source': s, 'broken': b} for s, b in res] if args.json else format_dangling(res)
    elif args.knowledge:
        res = query_knowledge(args.knowledge)
        out = res if args.json else format_knowledge(args.knowledge, res)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(out)


if __name__ == '__main__':
    main()
