#!/usr/bin/env python3
"""资料分析报告与提取产物的对账：按「名称 + 分类」比对报告清单与 ontology 下的实际文档。

资料分析的检查只保证报告自己前后一致；报告里的操作、规则、指标最终有没有生成、生成的
东西是不是都有来源，要在提取前后各对一次账，差异才不会被埋掉。只比名称与分类，
特殊名称通过文档开头的 report_name 元数据映射到安全文件名；旧文档仍按文件名，不判断内容。

用法（工作区根目录执行）：
  python3 .claude/scripts/report_reconcile.py plan [--reports P ...] [--summaries P ...]
                                                   [--alias 文档名=流程图名 ...] [--json]
      提取之前：按「业务项清单」的分类列拆出操作 / 规则 / 指标候选，并与流程图摘要比对——
      同名的流程图就是报告里的操作（合成一份文档）；报告没收录的流程图是补充；与报告里的
      规则、指标、对象重名的流程图，要先由用户决定怎么处理。
  python3 .claude/scripts/report_reconcile.py check [--reports P ...] [--summaries P ...]
                                                    [--alias 文档名=流程图名 ...] [--json]
      提取之后：报告里有、没生成的；生成了、报告里没有的（标明来源）；放错目录的。
      --alias 声明"按用户决定改了名的流程图操作"，它们按流程图来源计；plan 也据此检查新名冲突。

--reports / --summaries 不给时取工作区里全部的资料分析报告与流程图摘要；给了但不带值表示本轮
没用这一类来源。

退出码：0 = 一致，或差异都有来源；1 = 有需要处理的差异；2 = 无法对账（没有报告，或报告
清单没有分类列）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import onto_metamodel as mm
import research_review
from ontology_checker import (
    ENTITY_TYPE_LABELS,
    REPORT_CATEGORY_TYPES,
    REPORT_INVENTORY_SECTION,
    extract_sections,
    parse_markdown_table,
)

# 实体类型 → ontology 下的目录；logic 是 operation 的旧目录（ADR-0004）。可嵌套子目录的
# 类型与 ontology_checker.load_ontology 保持一致。
ENTITY_DIRS = {
    'object': ('object',),
    'operation': ('operation', 'logic'),
    'rule': ('rule',),
    'metric': ('metric',),
}
NESTED_DIRS = ('object', 'operation', 'logic')
# 逻辑提取产出的三类；对象由对象提取负责，这里只用来判断重名
EXTRACTED_TYPES = ('operation', 'rule', 'metric')
SUMMARY_DIR = Path('ontology') / 'diagrams-summary'
LIST_LIMIT = 20

OK, DIFF, UNAVAILABLE = 0, 1, 2


def _write(text: str) -> None:
    """命令结果写到标准输出：提取命令会把它原样展示给用户并写进提交说明。

    与 research_checker._emit 同一写法，不走 logging：这是命令的产物而不是运行日志，
    日志级别或格式一变，提取命令读到的结果就可能被改写或吞掉。
    """
    stdout = sys.stdout
    stdout.write(f'{text}\n')


@dataclass
class Inventory:
    """报告「业务项清单」：名称 → 实体类型。"""

    by_name: dict = field(default_factory=dict)
    uncategorized: list = field(default_factory=list)  # 没有分类列、或分类取值非法的报告
    conflicting: list = field(default_factory=list)    # 多处给了不同分类的名称


def _report_rows(root: Path, rel: str) -> list[dict] | None:
    """一份报告的清单行；读不到或没有表格时为 None。"""
    try:
        raw = (root / rel).read_text(encoding='utf-8')
    except OSError:
        return None
    rows = parse_markdown_table(extract_sections(raw).get(REPORT_INVENTORY_SECTION, ''))
    return rows or None


def _typed_rows(rows: list[dict]) -> list[tuple]:
    """[(业务项名称, 实体类型)]；分类取值非法时实体类型为 None。"""
    typed = []
    for row in rows:
        name = row.get('业务项名称', '').strip()
        doc_type = REPORT_CATEGORY_TYPES.get(row.get('分类', '').strip())
        typed.append((name, doc_type))
    return typed


def load_inventory(root: Path, reports: list[str]) -> Inventory:
    inventory = Inventory()
    seen: dict[str, set] = {}
    for rel in reports:
        rows = _report_rows(root, rel)
        if rows is None or '分类' not in rows[0]:
            inventory.uncategorized.append(rel)
            continue
        typed = _typed_rows(rows)
        if any(name and doc_type is None for name, doc_type in typed):
            inventory.uncategorized.append(rel)  # 分类取值非法：research_checker 会报，这里不猜
            continue
        for name, doc_type in typed:
            if name:
                seen.setdefault(name, set()).add(doc_type)
    for name, types in sorted(seen.items()):
        if len(types) == 1:
            inventory.by_name[name] = next(iter(types))
        else:
            inventory.conflicting.append(name)
    return inventory


def names_of_type(inventory: Inventory, doc_type: str) -> list[str]:
    """报告里分类为某一实体类型的名称，按名称排序。"""
    return sorted(name for name, found in inventory.by_name.items() if found == doc_type)


def safe_document_stem(name: str) -> str:
    """业务名 → 文件名：编码规则在元模型（onto_metamodel.encode_filename，方案 C5），
    文件系统保留字符、与链接语法冲突的 `# | [ ] &` 和 `%` 本身都百分号编码。"""
    return mm.encode_filename(name)


def _document_name(path: Path) -> str:
    """只读取显式 report_name，不从正文标题或替换后的标点推断别名。"""
    raw = path.read_text(encoding='utf-8')
    if not raw.startswith('---\n'):
        return path.stem
    header, separator, _ = raw[4:].partition('\n---\n')
    fields = [line.partition(':')[2].strip() for line in header.splitlines()
              if line.startswith('report_name:')]
    if not fields:
        return path.stem
    if not separator or len(fields) != 1:
        raise ValueError('report_name 必须在完整的文档头中且只能声明一次')
    # JSON 字符串是 YAML 字符串的子集，可准确保留反斜杠与引号，无需引入 YAML 依赖。
    name = json.loads(fields[0])
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise ValueError('report_name 必须是非空 JSON 字符串，且不能包含首尾空白')
    return name


def _reserve_document(name: str, rel: str, occupied: dict, conflicts: list) -> None:
    """下游以 stem 为实体键；跨目录也不能重名，同一文档的增量规划可以复用。"""
    key = Path(rel).stem.casefold()
    identity = (name, rel)
    previous = occupied.get(key)
    if previous is not None and previous != identity:
        conflicts.append(f'实体文件名冲突：「{previous[0]}」({previous[1]}) 与「{name}」({rel})')
    occupied.setdefault(key, identity)


def entity_docs(root: Path, conflicts: list | None = None) -> dict[str, dict[str, str]]:
    """{实体类型: {报告原名或文件名: 相对路径}}；索引不算文档，旧文档保持原行为。"""
    docs: dict[str, dict[str, str]] = {doc_type: {} for doc_type in ENTITY_DIRS}
    conflicts = conflicts if conflicts is not None else []
    occupied = {}
    for doc_type, subdirs in ENTITY_DIRS.items():
        for subdir in subdirs:
            directory = root / 'ontology' / subdir
            if not directory.is_dir():
                continue
            files = directory.rglob('*.md') if subdir in NESTED_DIRS else directory.glob('*.md')
            for path in sorted(files):
                if path.name.lower() == 'index.md':
                    continue
                rel = path.relative_to(root).as_posix()
                try:
                    name = _document_name(path) if doc_type in EXTRACTED_TYPES else path.stem
                except (OSError, ValueError) as exc:
                    conflicts.append(f'{rel}：无法读取名称映射（{exc}）')
                    continue
                _reserve_document(name, rel, occupied, conflicts)
                previous = docs[doc_type].get(name)
                if previous and (name != path.stem or name != Path(previous).stem):
                    conflicts.append(f'「{name}」对应多个文档：{previous}、{rel}')
                docs[doc_type].setdefault(name, rel)
    return docs


def document_names(inventory: Inventory, docs: dict, supplements: list[str]) -> tuple[dict, list]:
    """提取前给出确定的路径及原名元数据；碰撞只报告，不重命名或覆盖文件。"""
    result, conflicts = {}, []
    occupied = {}
    for group in docs.values():
        for name, rel in group.items():
            _reserve_document(name, rel, occupied, conflicts)
    for kind in EXTRACTED_TYPES:
        result[kind] = []
        for name in names_of_type(inventory, kind):
            existing = docs.get(kind, {}).get(name)
            rel = existing or f'ontology/{kind}/{safe_document_stem(name)}.md'
            _reserve_document(name, rel, occupied, conflicts)
            metadata = ('---\nreport_name: ' + json.dumps(name, ensure_ascii=False) + '\n---\n'
                        if Path(rel).stem != name else '')
            result[kind].append({'name': name, 'path': rel, 'exists': bool(existing),
                                 'frontmatter': metadata})
    # 流程图补充沿用原文件名（包括字面的 %），但与报告产物共用实体键空间。
    for name in supplements:
        if name in inventory.by_name:
            conflicts.append(f'流程图补充「{name}」与报告候选同名，请明确合并或改名后重新规划')
        existing = docs.get('operation', {}).get(name)
        rel = existing or f'ontology/operation/{name}.md'
        if not existing and safe_document_stem(name).replace('%25', '%') != name:
            conflicts.append(f'流程图补充「{name}」不能直接作为文件名，请通过 --alias 指定安全名称')
        _reserve_document(name, rel, occupied, conflicts)
        result['operation'].append({'name': name, 'path': rel, 'exists': bool(existing),
                                    'frontmatter': '', 'source': 'flowchart'})
    return result, conflicts


def default_summaries(root: Path) -> list[str]:
    directory = root / SUMMARY_DIR
    if not directory.is_dir():
        return []
    summaries = []
    for path in directory.glob('*.md'):
        if path.name.lower() != 'index.md':
            summaries.append(path.relative_to(root).as_posix())
    return sorted(summaries)


def _git(root: Path, *args: str) -> bytes | None:
    """在工作区里跑一条 git 只读命令；git 不可用或命令失败时为 None。"""
    git = shutil.which('git')
    if git is None:
        return None
    try:
        # 安全审查：argv 是 shutil.which 解析出的 git 路径，加本模块里写死的只读子命令
        # （rev-parse / status）与参数；以序列传入、不经 shell 解析。
        result = subprocess.run(  # NOSONAR(S4721)
            [git, *args], cwd=root, capture_output=True, timeout=10, check=False, shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def changed_paths(root: Path) -> set[str] | None:
    """本轮新增或改动的 ontology 文件（相对路径）。不是 git 仓库时为 None。

    用来区分"本轮生成的报告外文档"和"之前就有的文档"（例如从知识包导入的规则）：
    后者每次都列出来只会淹没真正需要说明的差异。git 给出的路径相对仓库根目录，所以
    工作区不是仓库根目录时同样返回 None——调用方按"全部算本轮改动"处理，宁可多报。
    """
    toplevel = _git(root, 'rev-parse', '--show-toplevel')
    if toplevel is None:
        return None
    if Path(toplevel.decode('utf-8', errors='replace').strip()).resolve() != root.resolve():
        return None
    output = _git(root, 'status', '--porcelain', '-z', '--untracked-files=all', '--', 'ontology')
    if output is None:
        return None
    changed, skip_next = set(), False
    for token in output.decode('utf-8', errors='replace').split('\0'):
        if skip_next:  # 改名记录的第二段是原路径
            skip_next = False
            continue
        if len(token) < 4:
            continue
        status, path = token[:2], token[3:]
        skip_next = 'R' in status or 'C' in status
        if 'D' not in status:
            changed.add(path)
    return changed


# ---------------------------------------------------------------------------
# 提取之前：拆候选，比对流程图
# ---------------------------------------------------------------------------

def build_plan(inventory: Inventory, summary_names: list[str], docs: dict | None = None,
               aliases: dict | None = None) -> dict:
    candidates = {doc_type: names_of_type(inventory, doc_type) for doc_type in ENTITY_DIRS}
    matches, supplements, conflicts = [], [], []
    aliases = aliases or {}
    for name in summary_names:
        doc_type = inventory.by_name.get(name)
        renamed = [target for target, source in aliases.items() if source == name]
        if renamed:
            supplements.extend(renamed)
        elif name in inventory.conflicting:
            conflicts.append({'name': name, 'report_type': None})
        elif doc_type == 'operation':
            matches.append(name)
        elif doc_type is None:
            supplements.append(name)
        else:
            conflicts.append({'name': name, 'report_type': doc_type})
    filenames, naming_conflicts = document_names(inventory, docs or {}, supplements)
    return {
        'document_names': filenames,
        'naming_conflicts': naming_conflicts,
        'candidates': candidates,
        'flowchart_matches': matches,
        'flowchart_supplements': supplements,
        'flowchart_conflicts': conflicts,
        'category_conflicts': inventory.conflicting,
    }


def plan_exit_code(plan: dict) -> int:
    conflict_keys = ('flowchart_conflicts', 'category_conflicts', 'naming_conflicts')
    return DIFF if any(plan[key] for key in conflict_keys) else OK


# ---------------------------------------------------------------------------
# 提取之后：对账
# ---------------------------------------------------------------------------

def _report_side(inventory: Inventory, docs: dict) -> tuple[dict, list]:
    """报告里有的条目：(各类缺失, 放错目录)。"""
    missing = {doc_type: [] for doc_type in EXTRACTED_TYPES}
    misplaced = []
    for name, doc_type in inventory.by_name.items():
        if doc_type not in EXTRACTED_TYPES or name in docs[doc_type]:
            continue
        found = [other for other in EXTRACTED_TYPES if name in docs[other]]
        if found:
            misplaced.append({'name': name, 'expected': doc_type, 'found': found})
        else:
            missing[doc_type].append(name)
    return missing, misplaced


def _extra_docs(inventory: Inventory, docs: dict, flowchart_names: set,
                changed: set | None) -> list:
    """生成了、报告里没有的文档，标明来源；之前就有、本轮没动的单独标出。"""
    extra = []
    for doc_type in EXTRACTED_TYPES:
        for name, rel in docs[doc_type].items():
            if name in inventory.by_name or name in inventory.conflicting:
                continue  # 报告里有：要么对上了，要么已按放错目录报出
            from_flowchart = doc_type == 'operation' and name in flowchart_names
            extra.append({
                'name': name,
                'type': doc_type,
                'source': 'flowchart' if from_flowchart else 'unknown',
                'changed': changed is None or rel in changed,
            })
    return extra


def _matched_count(inventory: Inventory, docs: dict, doc_type: str) -> int:
    """落在正确目录里的报告条目数；放错目录的单独报，不计入。"""
    present = docs[doc_type]
    return sum(1 for name in names_of_type(inventory, doc_type) if name in present)


def _flowcharts_without_doc(inventory: Inventory, docs: dict, summary_names: list[str],
                            aliases: dict) -> list[str]:
    """报告没收录、也没有同名操作文档、也没有按用户决定改名生成的流程图。"""
    accounted = set(inventory.by_name) | set(inventory.conflicting)
    accounted |= set(docs['operation']) | set(aliases.values())
    return [name for name in summary_names if name not in accounted]


def build_check(inventory: Inventory, docs: dict, summary_names: list[str],
                aliases: dict, changed: set | None) -> dict:
    missing, misplaced = _report_side(inventory, docs)
    flowchart_names = set(summary_names) | set(aliases)
    return {
        'report_counts': {t: len(names_of_type(inventory, t)) for t in EXTRACTED_TYPES},
        'matched_counts': {t: _matched_count(inventory, docs, t) for t in EXTRACTED_TYPES},
        'missing': missing,
        'misplaced': misplaced,
        'extra': _extra_docs(inventory, docs, flowchart_names, changed),
        'aliases': aliases,
        'flowcharts_without_doc': _flowcharts_without_doc(inventory, docs, summary_names, aliases),
        'category_conflicts': inventory.conflicting,
    }


def _needs_explanation(item: dict) -> bool:
    """报告与流程图都没有、又是本轮新增或改动的文档，必须说明来源。"""
    return item['source'] == 'unknown' and item['changed']


def _is_earlier_doc(item: dict) -> bool:
    """报告外、本轮没改动的已有文档（如从知识包导入），不计入差异。"""
    return item['source'] == 'unknown' and not item['changed']


def check_exit_code(result: dict) -> int:
    has_missing = any(result['missing'].values())
    unexplained = [item for item in result['extra'] if _needs_explanation(item)]
    others = (result['misplaced'], unexplained, result['category_conflicts'])
    return DIFF if has_missing or any(others) else OK


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def _names(values) -> str:
    values = list(values)
    shown = '、'.join(values[:LIST_LIMIT])
    return shown + (f' …（共 {len(values)} 个）' if len(values) > LIST_LIMIT else '')


def _label(doc_type: str | None) -> str:
    return ENTITY_TYPE_LABELS.get(doc_type, '分类不一致')


def render_plan(plan: dict) -> list[str]:
    candidates = plan['candidates']
    lines = [f'报告候选：操作 {len(candidates["operation"])} 个、规则 {len(candidates["rule"])} 条、'
             f'指标 {len(candidates["metric"])} 个']
    if plan['flowchart_matches']:
        lines.append(f'与报告操作同名的流程图（合成一份文档）：{_names(plan["flowchart_matches"])}')
    if plan['flowchart_supplements']:
        lines.append(f'报告没收录的流程图（作为流程图补充单独成文）：{_names(plan["flowchart_supplements"])}')
    for conflict in plan['flowchart_conflicts']:
        lines.append(f'[需决定] 流程图「{conflict["name"]}」在报告里是{_label(conflict["report_type"])}，'
                     '不能直接按操作生成')
    for name in plan['category_conflicts']:
        lines.append(f'[需决定] 「{name}」在多份报告里分类不一致')
    for conflict in plan.get('naming_conflicts', []):
        lines.append(f'[需处理] 名称映射冲突：{conflict}')
    for group in plan.get('document_names', {}).values():
        for item in group:
            if item['frontmatter']:
                lines.append(f'安全文件名：「{item["name"]}」→ {item["path"]}（保留 report_name 原名）')
    return lines


def render_check(result: dict) -> list[str]:
    lines = []
    for doc_type in EXTRACTED_TYPES:
        missing = result['missing'][doc_type]
        line = (f'{ENTITY_TYPE_LABELS[doc_type]}：报告 {result["report_counts"][doc_type]} 个，'
                f'已生成 {result["matched_counts"][doc_type]} 个')
        lines.append(line + (f'；缺 {len(missing)} 个：{_names(missing)}' if missing else ''))
    for item in result['misplaced']:
        found = '、'.join(ENTITY_TYPE_LABELS[t] for t in item['found'])
        lines.append(f'[需处理] 「{item["name"]}」报告里是{ENTITY_TYPE_LABELS[item["expected"]]}，'
                     f'却生成在{found}目录')
    extra = result['extra']
    from_flowchart = [item['name'] for item in extra if item['source'] == 'flowchart']
    if from_flowchart:
        lines.append(f'流程图补充的操作 {len(from_flowchart)} 个：{_names(from_flowchart)}')
    unexplained = [item for item in extra if _needs_explanation(item)]
    if unexplained:
        labelled = [f'{item["name"]}（{ENTITY_TYPE_LABELS[item["type"]]}）' for item in unexplained]
        lines.append(f'[需说明] 报告与流程图都没有、本轮新增或改动的文档 {len(labelled)} 个：'
                     f'{_names(labelled)}')
    earlier = [item for item in extra if _is_earlier_doc(item)]
    if earlier:
        lines.append(f'报告外、本轮未改动的已有文档 {len(earlier)} 个（如从知识包导入），不计入差异')
    if result['flowcharts_without_doc']:
        lines.append(f'没有同名操作文档的流程图（应已并入报告里的某个操作）：'
                     f'{_names(result["flowcharts_without_doc"])}')
    for name in result['category_conflicts']:
        lines.append(f'[需处理] 「{name}」在多份报告里分类不一致，无法对账')
    return lines


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _parse_args(argv):
    parser = argparse.ArgumentParser(description='资料分析报告与提取产物的对账')
    parser.add_argument('command', choices=('plan', 'check'))
    parser.add_argument('--reports', nargs='*', default=None, help='本轮使用的资料分析报告')
    parser.add_argument('--summaries', nargs='*', default=None, help='本轮使用的流程图摘要')
    parser.add_argument('--alias', action='append', default=[],
                        help='改了名的流程图操作：文档名=流程图名，可重复')
    parser.add_argument('--json', action='store_true', help='输出 JSON')
    return parser.parse_args(argv)


def _unavailable(inventory: Inventory, reports: list[str]) -> str:
    if not reports:
        return '本轮没有使用资料分析报告，无从对账'
    if inventory.uncategorized:
        return (f'报告的「{REPORT_INVENTORY_SECTION}」没有分类列或分类取值非法：'
                f'{"、".join(inventory.uncategorized)}。'
                '先运行 python3 .claude/scripts/research_checker.py 核验并补齐分类')
    return ''


def main(argv=None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    root = Path.cwd()
    reports = research_review.report_paths(root) if args.reports is None else args.reports
    summaries = default_summaries(root) if args.summaries is None else args.summaries
    summary_names = sorted({Path(rel).stem for rel in summaries})
    inventory = load_inventory(root, reports)

    reason = _unavailable(inventory, reports)
    if reason:
        _write(json.dumps({'status': 'unavailable', 'reason': reason}, ensure_ascii=False)
               if args.json else reason)
        return UNAVAILABLE

    naming_conflicts = []
    docs = entity_docs(root, naming_conflicts)
    aliases = dict(item.split('=', 1) for item in args.alias if '=' in item)
    if args.command == 'plan':
        result = build_plan(inventory, summary_names, docs, aliases)
        code, lines = plan_exit_code(result), render_plan(result)
    else:
        result = build_check(inventory, docs, summary_names, aliases,
                             changed_paths(root))
        code, lines = check_exit_code(result), render_check(result)
    if naming_conflicts:
        result.setdefault('naming_conflicts', []).extend(naming_conflicts)
        lines.extend(f'[需处理] 名称映射冲突：{item}' for item in naming_conflicts)
        code = DIFF
    result['status'] = 'ok' if code == OK else 'diff'
    _write(json.dumps(result, ensure_ascii=False) if args.json else '\n'.join(lines))
    return code


if __name__ == '__main__':
    sys.exit(main())
