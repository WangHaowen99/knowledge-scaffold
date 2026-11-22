#!/usr/bin/env python3
"""Impact analysis — given a file, list all downstream docs that reference it.

Reuses ontology_checker's link index to answer:
- Which operation docs reference this object?
- Which action docs / mock-data files are related to this object?
- Which operation docs call this sub-operation?
- Which operation docs invoke this action?
- Which diagrams/summaries need separate review (same name, links or literal formulas)?

Usage:
    python3 .claude/scripts/impact_analysis.py <file_path>
    python3 .claude/scripts/impact_analysis.py <stem>           # e.g. 订单
    python3 .claude/scripts/impact_analysis.py <file_path> --json
    python3 .claude/scripts/impact_analysis.py <file_path> --json --diagram-offset 50
"""

import argparse
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from ontology_checker import load_ontology, _parse_mock_tables, _extract_object_refs
import onto_metamodel as mm


MAX_DIAGRAM_BYTES = 2 * 1024 * 1024
MAX_DIAGRAM_PAGE = 100


def _normalized_text(text: str) -> str:
    return re.sub(r'\s+', '', html.unescape(re.sub(r'<[^>]+>', ' ', text)))


def _formula_fragments(text: str) -> set[str]:
    """Collect literal formula evidence, without evaluating expressions."""
    fragments = re.findall(r'`([^`\n]+)`', text)
    # Also cover plain-text formulas with nested calls, without requiring backticks.
    for match in re.finditer(r'\b(?:max|min|abs|round|ceil|floor)\s*\(', text):
        depth = 1
        end = match.end()
        while end < len(text) and depth:
            depth += (text[end] == '(') - (text[end] == ')')
            end += 1
        if depth == 0:
            fragments.append(text[match.start():end])
    return {
        _normalized_text(fragment) for fragment in fragments
        if re.search(r'[+*/<>=-]', fragment) and len(fragment.strip()) >= 7
    }


def _diagram_impact(related_stems: set[str], target_doc, ontology_dir: Path) -> list[dict]:
    """Return file handles and short reasons; never include diagram/source contents."""
    formulas = _formula_fragments(target_doc.raw) if target_doc else set()
    entries = []
    for subdir, suffix in (('diagrams', '*.drawio'), ('diagrams-summary', '*.md')):
        for path in sorted((ontology_dir / subdir).rglob(suffix)):
            reason = '同名流程图或摘要，需另行同步' if path.stem in related_stems else ''
            warning = ''
            try:
                if path.stat().st_size > MAX_DIAGRAM_BYTES:
                    raise ValueError('文件超过扫描上限')
                text = path.read_text(encoding='utf-8-sig')
                if subdir == 'diagrams':
                    root = ET.fromstring(text)
                    if not any(True for _ in root.iter('mxGraphModel')):
                        raise ValueError('压缩图或缺少可读取的图模型')
                    if any(not list(page) and (page.text or '').strip() for page in root.iter('diagram')):
                        warning = '部分图页无法完整扫描，需人工复核并另行同步'
                    text = '\n'.join(
                        node.get('value', '') or node.get('label', '') for node in root.iter()
                    )
                if not reason and any(
                    f'[[{name}]]' in html.unescape(text) for name in related_stems
                ):
                    reason = '引用受影响文档，需另行复核并同步'
                normalized = _normalized_text(text)
                if not reason and any(formula in normalized for formula in formulas):
                    reason = '包含目标文档中的公式，需另行复核并同步'
            except (OSError, ET.ParseError, ValueError):
                warning = '无法完整扫描，需人工复核并另行同步'
            if reason or warning:
                entries.append({
                    'file': (Path('ontology') / path.relative_to(ontology_dir)).as_posix(),
                    'reason': '；'.join(filter(None, (reason, warning))),
                })
    return entries


def resolve_target(input_arg: str, ontology_dir: Path) -> tuple:
    """Resolve user input to (stem, category) where category in {object, operation, action, mock, unknown}."""
    p = Path(input_arg)
    if p.exists() and p.suffix == '.md':
        parts = p.resolve().parts
        stem = p.stem
        if 'object' in parts:
            return stem, 'object'
        if 'operation' in parts or 'logic' in parts:
            return stem, 'operation'
        if 'action' in parts:
            return stem, 'action'
        if 'data-mock' in parts:
            return stem, 'mock'
        return stem, 'unknown'
    # Treat as stem — search ontology dirs
    for subdir, cat in [('object', 'object'), ('operation', 'operation'), ('logic', 'operation'),
                        ('action', 'action'), ('data-mock', 'mock')]:
        cand = ontology_dir / subdir / f'{input_arg}.md'
        if cand.exists():
            return input_arg, cat
    return input_arg, 'unknown'


def analyze_impact(stem: str, category: str, all_docs: dict, mock_dir: Path, testcase_dir: Optional[Path] = None,
                   *, diagram_offset: int = 0, diagram_limit: int = 50) -> dict:
    """Return a dict of downstream references grouped by type."""
    result = {
        'target': stem,
        'category': category,
        'operation_refs': [],       # operation docs that reference this
        'action_refs': [],      # action docs that reference this (via [[]])
        'object_refs': [],      # object docs that reference this (via 关联对象 / 对象引用)
        'mock_refs': [],        # mock-data files that reference this
        'called_by': [],        # for sub-operation: which operation calls this
        'invokes_action': [],   # for action: which operation invokes this
        'testcase_refs': [],    # operation stems whose saved testcase may need re-running
        'diagram_refs': [],     # read-only diagram/summary handles requiring separate review
        'notes': [],
    }

    for other_stem, other_doc in all_docs.items():
        if other_stem == stem:
            continue

        linked = any(mm.resolve(link, all_docs) == stem for link in other_doc.wiki_links)

        if other_doc.doc_type == 'operation':
            if linked:
                result['operation_refs'].append(other_stem)
            # sub-operation: invoked via 调用[[X]]逻辑文档
            operation_text = other_doc.sections.get('逻辑描述', '')
            sub_refs = [mm.resolve(raw, all_docs) or raw for raw in re.findall(r'调用\[\[([^\]]+)\]\]逻辑文档', operation_text)]
            if category == 'operation' and stem in sub_refs:
                result['called_by'].append(other_stem)
            # action invocation: action_rows
            if category == 'action':
                for row in other_doc.action_rows:
                    action_name = row.get('动作名称', '').strip()
                    link = mm.parse_link(action_name)
                    if link:
                        action_name = mm.resolve(link, all_docs) or link.full_name
                    if action_name == stem:
                        result['invokes_action'].append(other_stem)
                        break

        elif other_doc.doc_type == 'action':
            if linked:
                result['action_refs'].append(other_stem)

        elif other_doc.doc_type == 'object':
            # Only count as ref if this object doc mentions the target
            if linked:
                result['object_refs'].append(other_stem)
            # If target is an object and this object has ref attributes pointing at it
            if category == 'object':
                refs = _extract_object_refs(other_doc)
                for attr_name, targets in refs:
                    if stem in targets:
                        result['object_refs'].append(f'{other_stem} (字段: {attr_name})')

    # Dedup
    for k in ('operation_refs', 'action_refs', 'object_refs', 'called_by', 'invokes_action'):
        result[k] = sorted(set(result[k]))

    # Testcase scan — which saved trace scenarios may need re-running?
    # A testcase belongs to a operation document; so if the target is that operation
    # itself, or any operation that references the target, its testcase is at risk.
    if testcase_dir is not None and testcase_dir.exists():
        affected_operation_stems = set()
        if category == 'operation':
            affected_operation_stems.add(stem)
        affected_operation_stems.update(result['operation_refs'])
        affected_operation_stems.update(result['called_by'])
        affected_operation_stems.update(result['invokes_action'])
        for operation_stem in affected_operation_stems:
            if (testcase_dir / f'{operation_stem}.md').exists():
                result['testcase_refs'].append(operation_stem)
        result['testcase_refs'] = sorted(set(result['testcase_refs']))

    # Mock-data scan
    if mock_dir.exists():
        for f in sorted(mock_dir.glob('*.md')):
            if f.stem == stem:
                continue
            tables = _parse_mock_tables(f)
            hit = False
            for rows in tables.values():
                for row in rows:
                    for val in row.values():
                        if stem in val:
                            hit = True
                            break
                    if hit:
                        break
                if hit:
                    break
            if hit:
                result['mock_refs'].append(f.stem)

    related_stems = {stem, *result['operation_refs'], *result['called_by'], *result['invokes_action']}
    diagram_entries = _diagram_impact(related_stems, all_docs.get(stem), mock_dir.parent)
    diagram_offset = max(0, diagram_offset)
    diagram_limit = min(MAX_DIAGRAM_PAGE, max(1, diagram_limit))
    result['diagram_refs'] = diagram_entries[diagram_offset:diagram_offset + diagram_limit]
    result['diagram_scan'] = {
        'total': len(diagram_entries), 'offset': diagram_offset, 'limit': diagram_limit,
        'next_offset': diagram_offset + diagram_limit
        if diagram_offset + diagram_limit < len(diagram_entries) else None,
    }

    # Advisory notes based on category
    if category == 'object':
        if result['operation_refs'] or result['action_refs']:
            result['notes'].append(
                f'修改字段/主键时，需检查引用此对象的 {len(result["operation_refs"])} 个逻辑、'
                f'{len(result["action_refs"])} 个动作是否仍与定义一致'
            )
        if result['mock_refs']:
            result['notes'].append(
                f'修改主键或字段名后，{len(result["mock_refs"])} 个测试数据文件可能需重新生成'
            )
    elif category == 'operation':
        if result['called_by']:
            result['notes'].append(
                f'修改输入/输出后，{len(result["called_by"])} 个调用此子流程的父流程需复核参数契约'
            )
        if result['operation_refs'] and not result['called_by']:
            result['notes'].append(
                f'此流程在 {len(result["operation_refs"])} 个文档中被提及，请确认语义不变'
            )
    elif category == 'action':
        if result['invokes_action']:
            result['notes'].append(
                f'修改动作参数或返回后，{len(result["invokes_action"])} 个逻辑文档的调用处需复核'
            )
        elif not result['operation_refs']:
            result['notes'].append('该动作未被任何逻辑引用，若确认已废弃可删除')
    elif category == 'mock':
        result['notes'].append('测试数据变更只影响 /ontology-trace 的场景追踪结果，不影响其它文档')
    elif category == 'unknown':
        result['notes'].append('未识别的文件类型或未在 ontology/ 目录内，仅返回字面引用匹配结果')

    if result['testcase_refs']:
        result['notes'].append(
            f'以下 {len(result["testcase_refs"])} 个逻辑已保存追踪场景，修改后建议重新运行 /ontology-trace 验证：'
            + '、'.join(result['testcase_refs'])
        )

    return result


def format_report(result: dict) -> str:
    lines = [
        f"## 影响分析：{result['target']}（{result['category']}）",
        '',
    ]

    def section(title: str, items: list, desc: str):
        if not items:
            return
        lines.append(f'### {title}（{len(items)}）')
        lines.append(desc)
        for item in items:
            lines.append(f'- {item}')
        lines.append('')

    section('下游逻辑文档', result['operation_refs'],
            '这些逻辑文档中出现了对目标的引用。')
    section('调用此子流程的父流程', result['called_by'],
            '这些父流程通过"调用[[X]]逻辑文档"调用了目标；修改目标输入/输出会直接影响它们。')
    section('调用此动作的逻辑', result['invokes_action'],
            '这些逻辑在动作关联表中调用了目标动作。')
    section('引用此目标的对象', result['object_refs'],
            '这些对象通过 关联对象 或 对象引用 字段指向目标；主键/字段名修改会影响引用完整性。')
    section('引用此目标的动作', result['action_refs'],
            '这些动作在描述中引用了目标。')
    section('相关测试数据', result['mock_refs'],
            '这些测试数据文件字面出现了目标名，修改后可能需同步更新。')
    section('相关追踪场景', result['testcase_refs'],
            '这些逻辑已保存追踪场景（ontology/testcase/），修改目标后建议重新运行 /ontology-trace 验证场景仍然有效。')
    section('流程图及摘要（需另行同步）',
            [f"{entry['file']}：{entry['reason']}" for entry in result['diagram_refs']],
            '本次更新不会自动修改这些资源；应用文档变更前请确认单独同步安排。')
    scan = result['diagram_scan']
    if scan['next_offset'] is not None:
        lines.extend([
            f"流程图候选共 {scan['total']} 项，请使用 --diagram-offset {scan['next_offset']} "
            f"--diagram-limit {scan['limit']} 继续读取。", '',
        ])

    ref_keys = ('operation_refs', 'action_refs', 'object_refs', 'mock_refs',
                'called_by', 'invokes_action', 'testcase_refs')
    if not any(result[k] for k in ref_keys) and not scan['total']:
        lines.append('**静态扫描未发现下游引用。** 仍需复核流程图中的业务语义关联。')
        lines.append('')

    if result['notes']:
        lines.append('### 提示')
        for note in result['notes']:
            lines.append(f'- {note}')
        lines.append('')

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='Analyze downstream impact of modifying an ontology file.'
    )
    parser.add_argument('target', help='File path or stem (e.g. ontology/object/订单.md or 订单)')
    parser.add_argument('--ontology-dir', default='ontology', help='Ontology root directory')
    parser.add_argument('--json', action='store_true', help='Output JSON instead of markdown')
    parser.add_argument('--diagram-offset', type=int, default=0, help='Diagram candidate offset')
    parser.add_argument('--diagram-limit', type=int, default=50, help='Diagram page size (1-100)')
    args = parser.parse_args()

    ontology_dir = Path(args.ontology_dir)
    if not ontology_dir.exists():
        print(f'Error: ontology dir not found: {ontology_dir}', file=sys.stderr)
        sys.exit(1)

    stem, category = resolve_target(args.target, ontology_dir)
    all_docs = load_ontology(ontology_dir)
    mock_dir = ontology_dir / 'data-mock'
    testcase_dir = ontology_dir / 'testcase'

    result = analyze_impact(stem, category, all_docs, mock_dir, testcase_dir=testcase_dir,
                            diagram_offset=args.diagram_offset, diagram_limit=args.diagram_limit)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result))


if __name__ == '__main__':
    main()
