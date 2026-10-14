#!/usr/bin/env python3
"""Research Checker — 资料分析报告的确定性内容验收。

和 ontology_checker 对本体文档的作用相同：报告写出来之后由脚本对账，而不是让
模型自己宣布"资料分析完成"。只做能确定判定的事，语义判断转人工待决。

检查项（全部不经模型）：
1. 结构：报告含「核心业务对象候选 / 核心业务逻辑候选 / 核心规则候选 / 核心指标候选 /
   详细信息 / 业务项清单 / 分类小计 / 合计 / ID变更对照 / 数据与术语来源 / 信息来源 /
   缺口与待决」十二段，清单表格列齐全，候选名按 `- **名称**：说明` 加粗
   （下游提取按加粗名取候选）；
2. 分类一致：「业务项清单」的分类列取值合法，且每个候选名所在的段落与它在清单里的
   分类一一对应——提取阶段按这一列把操作/规则/指标写进各自目录，分类错就落错目录；
3. 计数自洽：`合计` = 业务项清单的唯一业务项数；分类小计逐类条数 = 清单里该分类行数；
4. ID 唯一：同一报告内业务项 ID、规则 ID 均不重复；
5. ID 溯源：每个 ID 必须逐字出现在它声明的来源文件里（找不到则退而在 resource/
   与既有报告里找）；哪里都找不到就是改名或臆造，除非在「ID变更对照」声明为新增。
   首轮调研没有报告基线，这条是捉 ID 静默改名的唯一确定性信号；
6. ID 稳定：派遣前固化的既有清单里的 ID 必须仍在报告中，新增 ID 必须在
   「ID变更对照」给出依据——不允许静默改名；
7. 字段保真：「引用字段」列的每个字段必须是真实表头里的列（表名.列名 或列名）；
8. 来源可定位：「来源位置」非空，写成路径时该文件必须存在；
9. 人工待决：「缺口与待决」里未勾选的 `- [ ]` 条目转人工确认，不自动放行。

用法（工作区根目录执行）：
  python3 .claude/scripts/research_checker.py capture [--json]
      派遣调研子代理**之前**执行：把真实表头与既有规范清单固化成只读快照
      （.ontology-state/research-inventory.json），并打印可直接注入 prompt 的区块。
  python3 .claude/scripts/research_checker.py [check] [--json]
      报告落盘后执行：核验内容并写验收回执；通过才允许进入对象提取。
  python3 .claude/scripts/research_checker.py status [--json]
      只读当前验收状态，不重新核验、不改回执。

退出码：0=通过；1=存在确定性错误或人工待决未完成；2=无法核验（没有报告）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import research_review
from check_eval_data import load_directives, table_headers, uploaded_eval_files
from ontology_checker import extract_sections, parse_markdown_table

ERROR = '错误'
WARNING = '警告'

INVENTORY_SECTION = '业务项清单'
SUBTOTAL_SECTION = '分类小计'
TOTAL_SECTION = '合计'
ID_CHANGE_SECTION = 'ID变更对照'
GAP_SECTION = '缺口与待决'
# 分类 → 候选段。提取阶段按「业务项清单」的分类列分流（操作→ontology/operation、
# 规则→ontology/rule、指标→ontology/metric），候选段是同一分类的加粗索引；两者
# 不一致实体就会落错目录，故由 `_check_categories` 逐项对账。
CATEGORY_SECTIONS = {
    '对象': '核心业务对象候选',
    '操作': '核心业务逻辑候选',
    '规则': '核心规则候选',
    '指标': '核心指标候选',
}
VALID_CATEGORIES = tuple(CATEGORY_SECTIONS)
# 下游按段落名读报告：coverage-check-agent、extract-objects-agent、
# extract-operation-agent、rule-metric-extraction-agent 都从候选段与「详细信息」
# 取候选名。段落缺失即判结构错误，否则提取会静默漏项。
DOWNSTREAM_SECTIONS = tuple(CATEGORY_SECTIONS.values()) + ('详细信息',)
REQUIRED_SECTIONS = DOWNSTREAM_SECTIONS + (
    INVENTORY_SECTION,
    SUBTOTAL_SECTION,
    TOTAL_SECTION,
    ID_CHANGE_SECTION,
    '数据与术语来源',
    '信息来源',
    GAP_SECTION,
)

INVENTORY_COLUMNS = ('业务项ID', '业务项名称', '分类', '规则ID', '引用字段', '来源位置')
SUBTOTAL_COLUMNS = ('分类', '条数')

# 单元格里表示"没有"的写法，不参与对账。
EMPTY_MARKERS = {'', '无', '-', '—', '–', 'n/a', 'N/A'}
CELL_SEPARATORS = re.compile(r'[,，、;；]')
# 下游提取认的候选名格式：`- **名称**：说明`。只取列表项开头那个加粗，说明里
# 强调用的加粗词（如「主键为 **订单号**」）不是候选名。
BOLD_NAME = re.compile(r'^[ \t]*(?:[-*+]|\d+[.)])[ \t]+\*\*([^*\n]+)\*\*', re.MULTILINE)
# 来源位置里形如 `路径/文件.后缀` 的片段；用于判断这条来源能否定位到真实文件。
PATH_LIKE = re.compile(r'[^\s|，。；、]+\.[A-Za-z0-9]{1,6}')
# 来源位置可定位的查找范围：用户资料、设计文档与既有报告。
LOCATE_DIRS = ('resource', 'docs', 'research')
# ID 溯源时可直接读的文本资料后缀；二进制资料（Excel 等）不参与溯源。
TEXT_SUFFIXES = ('.md', '.txt', '.csv', '.json', '.yaml', '.yml')
MAX_SOURCE_TEXT_BYTES = 4 * 1024 * 1024


def _issue(path: str, severity: str, category: str, message: str) -> dict:
    return {'path': path, 'severity': severity, 'category': category, 'message': message}


def _is_empty(value: str) -> bool:
    return (value or '').strip() in EMPTY_MARKERS


def _cells(value: str) -> list[str]:
    return [token.strip() for token in CELL_SEPARATORS.split(value or '') if token.strip()]


def _clip(values, limit: int = 8) -> str:
    values = list(values)
    return '、'.join(values[:limit]) + ('…' if len(values) > limit else '')


# ---------------------------------------------------------------------------
# 只读快照：派遣前固化真实表头与既有清单
# ---------------------------------------------------------------------------

def source_files(root: Path) -> list[str]:
    """验收依赖的源材料：资料用途清单、schema/term 表格、评测数据表。"""
    rels = []
    if (root / 'resource' / 'materials.json').is_file():
        rels.append('resource/materials.json')
    for rel, directive in load_directives(root).items():
        if isinstance(directive, dict) and directive.get('purpose') in ('schema', 'term'):
            posix = str(rel).replace('\\', '/')
            if (root / posix).is_file():
                rels.append(posix)
    rels.extend(uploaded_eval_files(root))
    return sorted(set(rels))


def report_items(root: Path, rels) -> list[dict]:
    """给定报告的业务项清单行；解析不出来时为空，由结构检查报错。"""
    items = []
    for rel in rels:
        try:
            raw = (root / rel).read_text(encoding='utf-8')
        except OSError:
            continue
        sections = extract_sections(raw)
        for row in parse_markdown_table(sections.get(INVENTORY_SECTION, '')):
            items.append({
                'source': rel,
                'item_id': (row.get('业务项ID') or '').strip(),
                'rule_id': (row.get('规则ID') or '').strip(),
                'name': (row.get('业务项名称') or '').strip(),
                'category': (row.get('分类') or '').strip(),
            })
    return items


def capture(root: Path) -> dict:
    """固化派遣用的只读快照。既有清单取上一次完整验收通过时的 ID，表头取真实表格。

    不读当前报告：报告可能没通过验收，把它的 ID 当基线会让编造的 ID 在下一轮
    凭基线放行（`_trace_ids` 认基线为合法出处）。
    """
    baseline = research_review.load_baseline(root)
    snapshot = {
        'captured_from': baseline.get('accepted_from') or [],
        'tables': table_headers(root),
        'item_ids': sorted(baseline.get('item_ids') or []),
        'rule_ids': sorted(baseline.get('rule_ids') or []),
        'source_files': source_files(root),
    }
    return research_review.write_inventory(root, snapshot)


def inventory_block(snapshot: dict) -> str:
    """派遣 prompt 用的只读来源区块。"""
    lines = ['# 只读来源快照（自动加载，不可改写）', '', '## 真实表头（字段名的唯一依据）']
    tables = snapshot.get('tables') or {}
    if tables:
        for name, headers in tables.items():
            lines.append(f'- 表「{name}」：{"、".join(headers)}')
        lines.append('- 「引用字段」列只能写成 `表名.列名`，且逐字取自上面的表头；'
                     '核对不上的字段写进「缺口与待决」，不要改写或编造。')
    else:
        lines.append('- 本项目暂无可核对的数据表（既没有 schema 资料也没有评测数据表）。'
                     '「引用字段」列一律填「无」，不要编造表名或列名。')
    lines += ['', '## 既有规范清单（ID 必须沿用）']
    item_ids = snapshot.get('item_ids') or []
    rule_ids = snapshot.get('rule_ids') or []
    if item_ids or rule_ids:
        lines.append(f'- 业务项 ID：{"、".join(item_ids) or "无"}')
        lines.append(f'- 规则 ID：{"、".join(rule_ids) or "无"}')
        lines.append(f'- 来源报告：{"、".join(snapshot.get("captured_from") or []) or "无"}')
        lines.append('- 这些 ID 只能沿用。新增、删除、归并或改名必须在「ID变更对照」段'
                     '逐条写出原 ID、新 ID、变更类型与依据。')
    else:
        lines.append('- 首次调研，暂无既有清单。本轮产出的 ID 将成为后续轮次的基线。')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 单份报告：结构、计数、ID 与引用
# ---------------------------------------------------------------------------

def _check_sections(rel: str, sections: dict) -> list[dict]:
    missing = [name for name in REQUIRED_SECTIONS if name not in sections]
    if not missing:
        return []
    return [_issue(rel, ERROR, '结构缺失',
                   f'报告缺少必需段落：{"、".join(missing)}。'
                   '按 .claude/agents/research-agent.md 的 Step 4 模板补齐后重新核验')]


def _section_candidates(sections: dict, name: str) -> list[str]:
    """候选段里的加粗名；段落缺失或写「无」时为空。"""
    body = (sections.get(name) or '').strip()
    if not body or body in EMPTY_MARKERS:
        return []
    return [m.strip() for m in BOLD_NAME.findall(body) if m.strip()]


def _check_candidates(rel: str, sections: dict) -> list[dict]:
    """候选名必须加粗：下游提取按 `**名称**` 取候选，格式不符会静默漏项。"""
    issues = []
    for name in CATEGORY_SECTIONS.values():
        if name not in sections:
            continue  # 段落本身缺失已由 _check_sections 报出
        body = (sections[name] or '').strip()
        if not body:
            issues.append(_issue(rel, ERROR, '候选缺失',
                                 f'「{name}」是空段：本轮没有这一类就写一行「无」，'
                                 '留空无法区分"没有"与"漏写"'))
            continue
        if body in EMPTY_MARKERS:
            continue  # 本轮确实没有这一类
        if not BOLD_NAME.search(body):
            issues.append(_issue(rel, ERROR, '候选缺失',
                                 f'「{name}」里没有 `- **名称**：说明` 形式的加粗候选名：'
                                 '提取按加粗名称取候选，这样写会漏项'))
    return issues


def _check_categories(rel: str, sections: dict, rows: list[dict]) -> list[dict]:
    """分类列取值合法，且候选段归属与清单分类逐项一致。

    提取阶段按分类分流（操作→`ontology/operation`、规则→`ontology/rule`、
    指标→`ontology/metric`）。候选段与分类对不上实体就落错目录——规则被写成
    operation 文档正是这么来的，所以两个方向都查：候选必须在清单里有分类，
    清单里的项也必须出现在它那一类的候选段里。
    """
    issues = []
    by_name = {}
    for row in rows:
        name = (row.get('业务项名称') or '').strip()
        category = (row.get('分类') or '').strip()
        if not name:
            continue
        if category not in VALID_CATEGORIES:
            issues.append(_issue(rel, ERROR, '分类非法',
                                 f'业务项「{name[:40]}」的分类是「{category[:20] or "空"}」，'
                                 f'只能取 {" / ".join(VALID_CATEGORIES)}'))
            continue
        by_name[name] = category

    listed = {category: _section_candidates(sections, section)
              for category, section in CATEGORY_SECTIONS.items()}

    for category, candidates in listed.items():
        section = CATEGORY_SECTIONS[category]
        for candidate in candidates:
            actual = by_name.get(candidate)
            if actual is None:
                issues.append(_issue(rel, ERROR, '分类越界',
                                     f'「{section}」里的候选「{candidate[:40]}」不在'
                                     f'「{INVENTORY_SECTION}」里：提取按清单分类分流，'
                                     '没有分类的候选落不到正确目录'))
            elif actual != category:
                issues.append(_issue(rel, ERROR, '分类越界',
                                     f'候选「{candidate[:40]}」在清单里分类为「{actual}」，'
                                     f'却列在「{section}」下，应改列到'
                                     f'「{CATEGORY_SECTIONS[actual]}」'))

    for name, category in by_name.items():
        if name not in listed[category]:
            issues.append(_issue(rel, ERROR, '候选缺失',
                                 f'业务项「{name[:40]}」分类为「{category}」，但'
                                 f'「{CATEGORY_SECTIONS[category]}」里没有它的加粗候选名：'
                                 '提取按候选名取条目，漏列就会漏提取'))
    return issues


def _unique_item_ids(rows: list[dict]) -> set[str]:
    return {(row.get('业务项ID') or '').strip() for row in rows
            if not _is_empty(row.get('业务项ID'))}


def _check_counts(rel: str, sections: dict, rows: list[dict]) -> list[dict]:
    issues = []
    actual = len(_unique_item_ids(rows))
    declared = re.search(r'-?\d+', sections.get(TOTAL_SECTION, '') or '')
    if declared is None:
        issues.append(_issue(rel, ERROR, '计数', '「合计」段没有数字，无法与清单对账'))
    elif int(declared.group()) != actual:
        issues.append(_issue(rel, ERROR, '计数',
                             f'「合计」写的是 {int(declared.group())}，业务项清单实际有 '
                             f'{actual} 个不重复业务项：合计必须由清单逐项计数得出'))

    subtotals = parse_markdown_table(sections.get(SUBTOTAL_SECTION, ''))
    if not subtotals:
        issues.append(_issue(rel, ERROR, '结构缺失',
                             f'「{SUBTOTAL_SECTION}」没有可解析的表格'
                             f'（需要列：{"、".join(SUBTOTAL_COLUMNS)}）'))
        return issues

    declared_by_category, total = {}, 0
    for row in subtotals:
        category = (row.get('分类') or '').strip()
        number = re.search(r'-?\d+', row.get('条数', '') or '')
        if number is None:
            issues.append(_issue(rel, ERROR, '计数',
                                 f'分类「{category[:20]}」的条数不是数字'))
            return issues
        declared_by_category[category] = declared_by_category.get(category, 0) + int(number.group())
        total += int(number.group())
    if total != actual:
        issues.append(_issue(rel, ERROR, '计数',
                             f'分类小计之和是 {total}，业务项清单实际有 {actual} '
                             '个不重复业务项：两者必须相等'))

    # 逐类对账：总数对得上但分布错了，提取照样会把规则分到操作那一侧。
    # 口径与 `actual` 一致，按不重复业务项 ID 计，重复 ID 由 _check_duplicates 单独报。
    counted, seen_ids = {}, set()
    for row in rows:
        item_id = (row.get('业务项ID') or '').strip()
        if _is_empty(item_id) or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        category = (row.get('分类') or '').strip()
        counted[category] = counted.get(category, 0) + 1
    for category in sorted(set(declared_by_category) | set(counted)):
        if declared_by_category.get(category, 0) != counted.get(category, 0):
            issues.append(_issue(rel, ERROR, '计数',
                                 f'「{SUBTOTAL_SECTION}」里「{category[:20] or "空"}」写的是 '
                                 f'{declared_by_category.get(category, 0)}，清单里该分类实际有 '
                                 f'{counted.get(category, 0)} 行：小计必须逐类由清单计数得出'))
    return issues


def _check_duplicates(rel: str, rows: list[dict]) -> list[dict]:
    issues = []
    for column, label in (('业务项ID', '业务项 ID'), ('规则ID', '规则 ID')):
        seen, duplicated = set(), []
        for row in rows:
            value = (row.get(column) or '').strip()
            if _is_empty(value):
                continue
            if value in seen and value not in duplicated:
                duplicated.append(value)
            seen.add(value)
        if duplicated:
            issues.append(_issue(rel, ERROR, 'ID重复',
                                 f'{label}重复：{_clip(duplicated)}'))
    return issues


def _column_index(tables: dict) -> tuple[dict, dict]:
    """(小写列名 → 所属表名, 小写表名 → 原表名)。"""
    columns = {}
    for name, headers in tables.items():
        for header in headers:
            columns.setdefault(header.lower(), name)
    return columns, {name.lower(): name for name in tables}


def _locatable_index(root: Path) -> dict:
    """资料、文档与报告目录下的文件：相对路径与纯文件名都映射到相对路径。"""
    index: dict[str, str] = {}
    for base in LOCATE_DIRS:
        directory = root / base
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob('*')):
            if not path.is_file():
                continue
            rel = f'{base}/{path.relative_to(directory).as_posix()}'
            index[rel] = rel
            index.setdefault(path.name, rel)  # 同名文件以先读到的为准
    return index


def _read_text(path: Path) -> str:
    """文本资料内容；读不到、过大或二进制时为空串。"""
    try:
        if path.stat().st_size > MAX_SOURCE_TEXT_BYTES:
            return ''
        return path.read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return ''


class CheckContext:
    """一次核验共享的只读上下文：真实表头、可定位文件、ID 溯源语料。

    ID 溯源语料按需加载（只有某个 ID 在它自己声明的来源文件里找不到时才会用到），
    避免每次核验都把 resource/ 下的资料全读一遍。
    """

    def __init__(self, root: Path, snapshot: dict):
        self.root = root
        self.snapshot = snapshot
        self.tables = table_headers(root)
        self.locatable = _locatable_index(root)
        # 上一轮已验收的 ID 也是合法出处。用快照里的 ID 集合而不是去读既有报告正文：
        # 报告文件会被本轮覆盖，读它等于让报告自证。
        self.baseline_ids = set(snapshot.get('item_ids') or []) | set(snapshot.get('rule_ids') or [])
        self.declared_old: set[str] = set()
        self.declared_new: set[str] = set()
        self._text: dict[str, str] = {}
        self._corpus = None

    def source_text(self, rel: str) -> str:
        if rel not in self._text:
            self._text[rel] = _read_text(self.root / rel)
        return self._text[rel]

    def sources_read(self) -> list[str]:
        """ID 溯源时实际读过的来源文档。

        回执要把它们记进输入指纹：规范 ID 就取自这些文档，文档里的 ID 改了而回执
        不失效的话，报告会带着已经对不上的 ID 继续算作已验收。
        """
        return sorted(self._text)

    def corpus(self) -> str:
        """resource/ 下的全部文本资料。不含任何报告，避免报告自证。"""
        if self._corpus is None:
            parts = []
            resource = self.root / 'resource'
            if resource.is_dir():
                for path in sorted(resource.rglob('*')):
                    if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                        parts.append(_read_text(path))
            self._corpus = '\n'.join(parts)
        return self._corpus


def _resolve_source(location: str, locatable: dict) -> tuple[list[str], str]:
    """(来源位置里的路径片段, 第一个能定位到的相对路径)。"""
    hits = [hit.lstrip('./') for hit in PATH_LIKE.findall(location.split('#')[0])]
    for hit in hits:
        rel = locatable.get(hit)
        if rel:
            return hits, rel
    return hits, ''


def _trace_ids(item_id: str, rule_id: str, source_rel: str,
               ctx: CheckContext) -> list[tuple[str, str, str]]:
    """ID 溯源：返回 (严重度, 标签, ID)。

    规范 ID 来自用户上传的资料或上一轮已验收的报告。首轮调研没有报告基线，
    `check_id_stability` 无从比对，此时"ID 在任何来源里都找不到"就是改名或臆造的
    唯一确定性信号（issue #362 的现场正是首轮）。
    合法出处依次是：自己声明的来源文件 → resource/ 下的文本资料 → 快照里已验收的
    ID → 「ID变更对照」声明的新增 ID。
    """
    found = []
    for label, value in (('业务项 ID', item_id), ('规则 ID', rule_id)):
        if _is_empty(value):
            continue
        if source_rel and value in ctx.source_text(source_rel):
            continue
        if value in ctx.corpus():
            found.append((WARNING, label, value))
        elif value not in ctx.baseline_ids and value not in ctx.declared_new:
            found.append((ERROR, label, value))
    return found


def _check_rows(rel: str, rows: list[dict], ctx: CheckContext) -> list[dict]:
    """逐行检查 ID 是否填写、能否溯源，引用字段是否真实，来源位置能否定位。"""
    issues = []
    tables = ctx.tables
    columns, tables_lower = _column_index(tables)
    unknown_fields, unlocatable = [], []
    untraceable, misplaced = [], []

    for row in rows:
        item_id = (row.get('业务项ID') or '').strip()
        if _is_empty(item_id):
            issues.append(_issue(rel, ERROR, '清单缺项',
                                 '存在没有业务项 ID 的行（业务项名称：'
                                 f'{(row.get("业务项名称") or "")[:20] or "空"}）'))
            continue

        if tables:
            for field in _cells(row.get('引用字段', '')):
                if field in EMPTY_MARKERS:
                    continue
                table, _, column = field.rpartition('.')
                if table:
                    real = tables_lower.get(table.lower())
                    if real is None or column.lower() not in {h.lower() for h in tables[real]}:
                        unknown_fields.append(f'{item_id}:{field}')
                elif column.lower() not in columns:
                    unknown_fields.append(f'{item_id}:{field}')

        location = (row.get('来源位置') or '').strip()
        if _is_empty(location):
            issues.append(_issue(rel, ERROR, '来源缺失', f'业务项 {item_id} 没有填来源位置'))
            continue
        hits, source_rel = _resolve_source(location, ctx.locatable)
        if hits and not source_rel:
            unlocatable.append(f'{item_id}:{location[:40]}')

        rule_id = (row.get('规则ID') or '').strip()
        for severity, label, value in _trace_ids(item_id, rule_id, source_rel, ctx):
            entry = f'{item_id}:{label} {value}'
            (misplaced if severity == WARNING else untraceable).append(entry)

    if rows and not tables:
        issues.append(_issue(rel, WARNING, '字段对账',
                             '项目里还没有可核对的数据表（schema 资料或评测数据源的上传表），'
                             '报告中的字段名无从核对——给结构表打 schema 标签后重新核验'))
    if unknown_fields:
        issues.append(_issue(rel, ERROR, '字段对账',
                             f'{len(unknown_fields)} 个引用字段不在真实表头里：'
                             f'{_clip(unknown_fields)}。字段名必须逐字取自表头，'
                             '核对不上的写进「缺口与待决」，不要改写'))
    if unlocatable:
        issues.append(_issue(rel, ERROR, '来源缺失',
                             f'{len(unlocatable)} 条来源位置指向不存在的文件：'
                             f'{_clip(unlocatable, 5)}'))
    if untraceable:
        issues.append(_issue(rel, ERROR, 'ID溯源',
                             f'{len(untraceable)} 个 ID 在资料与既有报告里都找不到：'
                             f'{_clip(untraceable, 5)}。规范 ID 必须逐字沿用资料里的写法；'
                             '确实是本轮新造的，在「ID变更对照」写一行「无 → 新ID / 新增 / 依据」'))
    if misplaced:
        issues.append(_issue(rel, WARNING, 'ID溯源',
                             f'{len(misplaced)} 个 ID 不在它自己声明的来源文件里'
                             f'（在别处找到）：{_clip(misplaced, 5)}。核对来源位置是否写错'))
    return issues


def _manual_pending(rel: str, sections: dict) -> list[dict]:
    """「缺口与待决」里未勾选的条目——需人工确认，不由脚本自动放行。"""
    pending = []
    for line in (sections.get(GAP_SECTION, '') or '').splitlines():
        text = line.strip()
        if text.startswith('- [ ]') or text.startswith('* [ ]'):
            pending.append({'path': rel, 'item': text[5:].strip()[:120]})
    return pending


def check_report(root: Path, rel: str, ctx: CheckContext) -> tuple[list, list]:
    """单份报告 → (issues, manual_pending)。"""
    try:
        raw = (root / rel).read_text(encoding='utf-8')
    except OSError as exc:
        return [_issue(rel, ERROR, '读取失败', f'报告读不到：{exc}')], []
    sections = extract_sections(raw)
    pending = _manual_pending(rel, sections)
    issues = _check_sections(rel, sections) + _check_candidates(rel, sections)

    rows = parse_markdown_table(sections.get(INVENTORY_SECTION, ''))
    if not rows:
        issues.append(_issue(rel, ERROR, '结构缺失',
                             f'「{INVENTORY_SECTION}」没有可解析的表格'
                             f'（需要列：{"、".join(INVENTORY_COLUMNS)}）'))
        return issues, pending
    missing_columns = [name for name in INVENTORY_COLUMNS if name not in rows[0]]
    if missing_columns:
        issues.append(_issue(rel, ERROR, '结构缺失',
                             f'「{INVENTORY_SECTION}」缺少列：{"、".join(missing_columns)}'))
        return issues, pending

    issues += _check_counts(rel, sections, rows)
    issues += _check_categories(rel, sections, rows)
    issues += _check_duplicates(rel, rows)
    issues += _check_rows(rel, rows, ctx)
    return issues, pending


# ---------------------------------------------------------------------------
# 跨报告：ID 相对既有清单的稳定性
# ---------------------------------------------------------------------------

def _declared_changes(root: Path, reports) -> tuple[set[str], set[str]]:
    """「ID变更对照」里给过依据的 (原ID, 新ID)；依据为空不算声明。"""
    old_ids, new_ids = set(), set()
    for rel in reports:
        try:
            raw = (root / rel).read_text(encoding='utf-8')
        except OSError:
            continue
        for row in parse_markdown_table(extract_sections(raw).get(ID_CHANGE_SECTION, '')):
            if _is_empty(row.get('依据')):
                continue
            old_ids.update(v for v in _cells(row.get('原ID', '')) if v not in EMPTY_MARKERS)
            new_ids.update(v for v in _cells(row.get('新ID', '')) if v not in EMPTY_MARKERS)
    return old_ids, new_ids


def check_id_stability(items: list[dict], ctx: CheckContext) -> list[dict]:
    """既有 ID 不得静默消失，新 ID 不得静默出现。首次调研无基线时跳过。

    首轮没有基线时由 `_trace_ids` 的 ID 溯源兜住——那条检查不依赖既有报告。
    """
    snapshot = ctx.snapshot
    baseline = {
        '业务项 ID': (set(snapshot.get('item_ids') or []),
                      {i['item_id'] for i in items if not _is_empty(i['item_id'])}),
        '规则 ID': (set(snapshot.get('rule_ids') or []),
                    {i['rule_id'] for i in items if not _is_empty(i['rule_id'])}),
    }
    if not any(base for base, _ in baseline.values()):
        return []

    issues = []
    for label, (base, current) in baseline.items():
        dropped = sorted(base - current - ctx.declared_old)
        if dropped:
            issues.append(_issue('research/', ERROR, 'ID变更',
                                 f'既有{label}在报告中消失且没有变更对照：{_clip(dropped)}。'
                                 '沿用原 ID，或在「ID变更对照」逐条写出原 ID、新 ID 与依据'))
        added = sorted(current - base - ctx.declared_new)
        if added:
            issues.append(_issue('research/', ERROR, 'ID变更',
                                 f'新出现的{label}没有变更对照：{_clip(added)}。'
                                 '在「ID变更对照」写明变更类型与依据，不要静默替换原 ID'))
    return issues


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

NO_REPORT = 'no_report'


def run_check(root: Path) -> dict:
    """核验全部报告并写回执。返回 {status, issues, manual_pending, inputs}。"""
    reports = research_review.report_paths(root)
    if not reports:
        return {'status': NO_REPORT, 'issues': [], 'manual_pending': [], 'inputs': {}}

    snapshot = research_review.load_inventory(root)
    ctx = CheckContext(root, snapshot)
    # 「ID变更对照」要先全量扫出来：逐行的 ID 溯源要用它放行本轮新造的 ID
    ctx.declared_old, ctx.declared_new = _declared_changes(root, reports)

    issues: list[dict] = []
    manual_pending: list[dict] = []
    for rel in reports:
        report_issues, pending = check_report(root, rel, ctx)
        issues += report_issues
        manual_pending += pending
    items = report_items(root, reports)
    issues += check_id_stability(items, ctx)

    errors = [issue for issue in issues if issue['severity'] == ERROR]
    status = research_review.FAILED if errors else research_review.PASSED
    tracked = list(reports) + source_files(root) + ctx.sources_read()
    if snapshot:
        tracked.append(research_review.INVENTORY_FILE.as_posix())
    inputs = research_review.file_revisions(root, tracked)
    research_review.write_review(root, status, inputs, errors, manual_pending)
    # 只有完整验收通过的 ID 才能成为下一轮的基线；失败时保留上一份，不清空也不覆盖
    if status == research_review.PASSED and not manual_pending:
        research_review.write_baseline(root, {
            'accepted_from': reports,
            'item_ids': sorted({i['item_id'] for i in items if not _is_empty(i['item_id'])}),
            'rule_ids': sorted({i['rule_id'] for i in items if not _is_empty(i['rule_id'])}),
        })
    return {'status': status, 'issues': issues, 'manual_pending': manual_pending, 'inputs': inputs}


def check_exit_code(result: dict) -> int:
    if result['status'] == NO_REPORT:
        return 2
    if result['status'] == research_review.FAILED or result['manual_pending']:
        return 1
    return 0


def _emit(text: str, stream=None) -> None:
    """把命令结果写到标准输出（或指定的流）。

    这里写出的是命令的产物，不是运行日志：`capture` 的区块被原样注入子代理 prompt，
    `--json` 结果由评分脚本解析，`check` 的结论由提交钩子转给用户。所以直接写流、
    不走 logging——日志级别或格式一变，这些内容就可能被改写或吞掉。
    """
    target = sys.stdout if stream is None else stream
    target.write(f'{text}\n')


def _print_check(result: dict) -> None:
    if result['status'] == NO_REPORT:
        _emit('research/ 下没有调研报告，无法核验（请先运行 /ontology-research）')
        return
    errors = [i for i in result['issues'] if i['severity'] == ERROR]
    warnings = [i for i in result['issues'] if i['severity'] == WARNING]
    # 只用中文方括号标记，不用 emoji：控制台编码不一定支持，打印失败会把"通过"
    # 变成异常退出。
    for issue in errors + warnings:
        _emit(f'[{issue["severity"]}][{issue["category"]}] {issue["path"]}：{issue["message"]}')
    for item in result['manual_pending']:
        _emit(f'[待人工确认] {item["path"]}：{item["item"]}')
    if errors:
        _emit(f'\n未通过：{len(errors)} 个确定性错误，{len(warnings)} 条警告。'
              '修正报告后重新运行本检查；验收未通过时不进入对象提取。')
    elif result['manual_pending']:
        _emit(f'\n待核验：确定性检查通过（{len(warnings)} 条警告），但有 '
              f'{len(result["manual_pending"])} 项需人工确认。'
              '确认后在报告中勾选并重新运行本检查。')
    else:
        _emit(f'\n已验收：确定性检查通过（{len(warnings)} 条警告），可进入对象提取。')


def main() -> int:
    argv = [arg for arg in sys.argv[1:] if not arg.startswith('--')]
    as_json = '--json' in sys.argv
    command = argv[0] if argv else 'check'
    root = Path.cwd()

    if command == 'capture':
        snapshot = capture(root)
        _emit(json.dumps(snapshot, ensure_ascii=False) if as_json else inventory_block(snapshot))
        return 0

    if command == 'status':
        result = research_review.review_status(root)
        if as_json:
            _emit(json.dumps(result, ensure_ascii=False))
        else:
            _emit(f'{result["status"]}（{result["reason"]}）')
            tip = research_review.hint(result)
            if tip:
                _emit(tip)
        return 0 if result['status'] == research_review.PASSED else 1

    if command != 'check':
        _emit(f'unknown subcommand: {command}', sys.stderr)
        _emit(__doc__, sys.stderr)
        return 2

    result = run_check(root)
    if as_json:
        _emit(json.dumps(result, ensure_ascii=False))
    else:
        _print_check(result)
    return check_exit_code(result)


if __name__ == '__main__':
    sys.exit(main())
