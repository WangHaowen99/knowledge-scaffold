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
5. ID 溯源：资料里的编号必须出现在声明的资料/锚点中，读不到明确为无法核验；资料里没有编号时助手按
   `ITEM-01`、`RULE-01` 自编，资料里查不到这个编号就算自编，不需要人确认（F4b）。出处读资料的文本版
   （多源连接器转出的 resource/text/…），旧报告指向 DOCX/XLSX 原件的锚点照常核对，只提示改指文本版；
6. ID 稳定：派遣前固化的既有清单里的 ID 必须仍在报告中，新增 ID 必须在
   「ID变更对照」给出依据——不允许静默改名；
7. 字段保真：「引用字段」列的每个字段必须是真实表头里的列（表名.列名 或列名）；
8. 来源可定位：「来源位置」非空，写成路径时该文件必须存在；「详细信息」里写到的
   `resource/` 路径与锚点同样必须存在，且每处出处写全路径（规则的「原始资料来源」取自这里）；
9. 人工待决：「缺口与待决」里未勾选的 `- [ ]` 条目转人工确认，不自动放行。

用法（工作区根目录执行）：
  python3 .claude/scripts/research_checker.py capture [--json]
      派遣资料分析子代理**之前**执行：把真实表头与既有规范清单固化成只读快照
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
import source_extract
from table_profile import load_directives, table_headers, within_workspace
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
# 「详细信息」里的资料出处：`路径` 或 `路径#锚点`（文本版写行号 L12、L12-L15，锚点写法见 source_extract）。
DETAIL_SOURCE = re.compile(r'(resource/[^\s|，。；：、（）()`#]+\.[A-Za-z0-9]{1,6})(?:#([^\s，。；、（）()`|]+))?')
# 省略路径的锚点（`#L12`、`#sheet:规则!C2`）：下游取不到它属于哪个文件。
BARE_ANCHOR = re.compile(r'(?<![\w./\-])#(?:L\d|p:\d|table:\d|sheet:)[^\s，。；、（）()`|]*')
# 来源位置可定位的查找范围：用户资料、设计文档与既有报告。
LOCATE_DIRS = ('resource', 'docs', 'research')
# 资料里没有自己的编号时，助手自编的业务项 / 规则编号（F4b：不需要人确认）；别的写法都按「资料里的编号」核对。
SELF_NUMBERED = re.compile(r'^(?:ITEM|RULE)-\d{1,4}$')


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
    """验收依赖的源材料：资料用途清单、schema/term 表格。"""
    rels = []
    if (root / 'resource' / 'materials.json').is_file():
        rels.append('resource/materials.json')
    for rel, directive in load_directives(root).items():
        if isinstance(directive, dict) and directive.get('purpose') in ('schema', 'term'):
            posix = str(rel).replace('\\', '/')
            if (root / posix).is_file():
                rels.append(posix)
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

    不读当前报告：报告可能没通过验收。基线只用于稳定性对比，不能免除当前来源核验。
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
        lines.append('- 本项目暂无可核对的数据表（没有 schema 资料）。'
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
        lines.append('- 首次资料分析，暂无既有清单。本轮产出的 ID 将成为后续轮次的基线。')
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
            # Ambiguous basenames must not silently select the first material.
            index[path.name] = rel if path.name not in index else ''
    return index


def _read_text(path: Path) -> str:
    """与 Agent 共用确定性抽取；读取失败不能伪装成来源为空。"""
    return source_extract.located_text(source_extract.extract(path))


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
        # 旧基线只保留作 ID 稳定性对比，不作为当前资料核验的白名单。
        self.declared_old: set[str] = set()
        self.declared_new: set[str] = set()
        self.manual_pending: list[dict] = []
        self.id_evidence: dict[str, list] = {}
        self._sources: dict[str, dict] = {}
        self._corpus = None

    def source_text(self, rel: str, anchor: str = '') -> str:
        """出处所在文本。资料区里的原件（PDF、Word、Excel 等）不解析（F4c，ADR-0076）：旧报告指向原件的，
        按它的文本版整篇核对；还没有文本版就核对不了。"""
        if rel not in self._sources:
            path = self.root / rel
            if not within_workspace(self.root, path):
                raise ValueError('来源必须位于当前工作区内')
            read = rel
            if path.suffix.lower() not in source_extract.TEXT_SUFFIXES and rel.startswith('resource/'):
                read = source_extract.text_version(rel)
                if not (self.root / read).is_file():
                    raise ValueError('出处指向资料原件，它还没有文本版（转换中、暂不支持或转换失败），这里不解析原件，核对不了')
            self._sources[rel] = {**source_extract.extract(self.root / read), 'read_rel': read}
        data = self._sources[rel]
        return source_extract.located_text(data, '' if data['read_rel'] != rel else anchor)

    def sources_read(self) -> list[str]:
        """ID 溯源时实际读过的来源文档（原件读的是文本版，记文本版）。

        回执要把它们记进输入指纹：规范 ID 就取自这些文档，文档里的 ID 改了而回执
        不失效的话，报告会带着已经对不上的 ID 继续算作已验收。
        """
        return sorted({data['read_rel'] for data in self._sources.values()})

    def corpus(self) -> str:
        """resource/ 下的全部文本资料。不含任何报告，避免报告自证。有文本版的原件读文本版，不解析原件。"""
        if self._corpus is None:
            parts = []
            resource = self.root / 'resource'
            if resource.is_dir():
                for path in sorted(resource.rglob('*')):
                    # 只读文本（含 resource/text 下的文本版），原件一律不解析
                    if path.is_file() and path.suffix.lower() in source_extract.TEXT_SUFFIXES:
                        try:
                            parts.append(self.source_text(path.relative_to(self.root).as_posix()))
                        except ValueError:
                            # Only cited/declared evidence can block the row. An unrelated
                            # unsupported material cannot prove absence or exempt an ID.
                            continue
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


def _contains_id(text: str, value: str) -> bool:
    return bool(re.search(r'(?<![A-Za-z0-9_-])' + re.escape(value) + r'(?![A-Za-z0-9_-])', text))


def _trace_ids(item_id: str, rule_id: str, source_rel: str,
               ctx: CheckContext, source_text: str | None = None, anchor: str = '') -> list[tuple[str, str, str]]:
    """返回来源不符的 ID。资料里的编号要在声明的位置找到；资料里查不到、按 ITEM-/RULE- 写的是助手自编的，
    直接通过，不转人工（F4b：编号是内部机制，不问人）。前后两轮不悄悄变化由 check_id_stability 管。"""
    found = []
    for label, value in (('业务项 ID', item_id), ('规则 ID', rule_id)):
        if _is_empty(value):
            continue
        text = source_text if source_text is not None else ctx.source_text(source_rel)
        if source_rel and _contains_id(text, value):
            ctx.id_evidence.setdefault(source_rel, []).append(
                {'id': value, 'kind': 'source_id', 'anchor': anchor or 'whole_file'})
            continue
        if _contains_id(ctx.corpus(), value):
            # A wrong anchor/source is a defect, not a successful provenance check.
            found.append((ERROR, label, value))
        elif SELF_NUMBERED.match(value):
            ctx.id_evidence.setdefault(source_rel, []).append(
                {'id': value, 'kind': 'assigned', 'anchor': anchor or 'whole_file'})
        else:
            found.append((ERROR, label, value))
    return found


def _check_rows(rel: str, rows: list[dict], ctx: CheckContext) -> list[dict]:
    """逐行检查 ID 是否填写、能否溯源，引用字段是否真实，来源位置能否定位。"""
    issues = []
    tables = ctx.tables
    columns, tables_lower = _column_index(tables)
    unknown_fields, unlocatable = [], []
    untraceable, misplaced, on_original = [], [], []

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
        if not source_rel or source_rel.startswith('research/'):
            unlocatable.append(f'{item_id}:{location[:40]}')
            continue
        anchor = location.partition('#')[2].strip().strip('`')
        if Path(source_rel).suffix.lower() not in source_extract.TEXT_SUFFIXES \
                and (ctx.root / source_extract.text_version(source_rel)).is_file():
            on_original.append(f'{item_id}:{source_rel}')
        try:
            source_text = ctx.source_text(source_rel, anchor)
        except ValueError as exc:
            issues.append(_issue(rel, ERROR, '来源无法核验', f'{item_id}：{exc}'))
            continue

        rule_id = (row.get('规则ID') or '').strip()
        for severity, label, value in _trace_ids(item_id, rule_id, source_rel, ctx, source_text, anchor):
            entry = f'{item_id}:{label} {value}'
            (misplaced if severity == WARNING else untraceable).append(entry)

    if rows and not tables:
        issues.append(_issue(rel, WARNING, '字段对账',
                             '项目里还没有可核对的数据表（schema 资料），'
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
                             f'{len(untraceable)} 个 ID 无法在所声明的来源位置核实：'
                             f'{_clip(untraceable, 5)}。资料里的编号逐字沿用、核对来源锚点；'
                             '资料里没有编号时自编为 ITEM-01、RULE-01 这种写法，不要编成像资料编号的样子'))
    if on_original:
        issues.append(_issue(rel, WARNING, '出处',
                             f'{len(on_original)} 条出处指向原件，这些资料已有文本版：{_clip(on_original, 5)}。'
                             '出处改指文本版 resource/text/…（带行号）'))
    if misplaced:
        issues.append(_issue(rel, WARNING, 'ID溯源',
                             f'{len(misplaced)} 个 ID 不在它自己声明的来源文件里'
                             f'（在别处找到）：{_clip(misplaced, 5)}。核对来源位置是否写错'))
    return issues


def _check_detail_sources(rel: str, raw: str, ctx: CheckContext) -> list[dict]:
    """「详细信息」的资料出处要能定位：它们会写进规则的「原始资料来源」，页面据此打开原文。

    条目写在「## 名称」小节里，extract_sections 会把小节拆成独立段，这里按一级标题整段截取。
    """
    match = re.search(r'^#[ \t]+详细信息[ \t]*$(.*?)(?=^#[ \t]|\Z)', raw, re.MULTILINE | re.DOTALL)
    body = match.group(1) if match else ''
    missing, broken = [], []
    for path, anchor in dict.fromkeys(DETAIL_SOURCE.findall(body)):
        source_rel = ctx.locatable.get(path)
        if not source_rel:
            missing.append(path)
        elif anchor:
            try:
                ctx.source_text(source_rel, anchor)
            except ValueError:
                broken.append(f'{path}#{anchor}')
    issues = []
    if missing:
        issues.append(_issue(rel, ERROR, '来源缺失',
                             f'「详细信息」有 {len(missing)} 处出处指向不存在的文件：{_clip(missing, 5)}'))
    if broken:
        issues.append(_issue(rel, ERROR, '来源无法核验',
                             f'「详细信息」有 {len(broken)} 处锚点在原文里找不到：{_clip(broken, 5)}。'
                             '逐字使用抽取器返回的位置'))
    bare = list(dict.fromkeys(BARE_ANCHOR.findall(body)))
    if bare:
        issues.append(_issue(rel, ERROR, '来源缺失',
                             f'「详细信息」有 {len(bare)} 处锚点省略了文件路径：{_clip(bare, 5)}。'
                             '每处出处都写完整的 路径#锚点'))
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
    issues += _check_detail_sources(rel, raw, ctx)
    return issues, pending


# ---------------------------------------------------------------------------
# 跨报告：ID 相对既有清单的稳定性
# ---------------------------------------------------------------------------

def _declared_changes(root: Path, reports, ctx: CheckContext) -> tuple[set[str], set[str], list]:
    """「ID变更对照」里给过依据的 (原ID, 新ID)；依据为空不算声明。"""
    old_ids, new_ids = set(), set()
    issues = []
    for rel in reports:
        try:
            raw = (root / rel).read_text(encoding='utf-8')
        except OSError:
            continue
        sections = extract_sections(raw)
        for row in parse_markdown_table(sections.get(ID_CHANGE_SECTION, '')):
            if _is_empty(row.get('依据')):
                continue
            evidence = row.get('依据', '').strip()
            _, source_rel = _resolve_source(evidence, ctx.locatable)
            kind = row.get('变更类型', '').strip()
            if kind not in {'新增', '删除', '改名', '归并', '沿用来源ID'} or not source_rel or source_rel.startswith('research/'):
                issues.append(_issue(rel, ERROR, 'ID变更依据', 'ID变更必须指定合法类型及原始资料路径，报告不能自证'))
                continue
            try:
                ctx.source_text(source_rel, evidence.partition('#')[2].strip().strip('`'))
            except ValueError as exc:
                issues.append(_issue(rel, ERROR, '来源无法核验', f'ID变更依据：{exc}'))
                continue
            new_values = [v for v in _cells(row.get('新ID', '')) if v not in EMPTY_MARKERS]
            if kind == '新增' and any(_contains_id(ctx.corpus(), value) for value in new_values):
                issues.append(_issue(rel, ERROR, 'ID变更类型', '来源中已存在的 ID 不能登记为新造，使用「沿用来源ID」并核对原始位置'))
                continue
            old_ids.update(v for v in _cells(row.get('原ID', '')) if v not in EMPTY_MARKERS)
            new_ids.update(v for v in _cells(row.get('新ID', '')) if v not in EMPTY_MARKERS)
    return old_ids, new_ids, issues


def check_id_stability(items: list[dict], ctx: CheckContext) -> list[dict]:
    """既有 ID 不得静默消失，新 ID 不得静默出现。首次资料分析无基线时跳过。

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
    # 先验证变更依据；声明自身不构成来源证据。
    ctx.declared_old, ctx.declared_new, issues = _declared_changes(root, reports, ctx)
    manual_pending: list[dict] = []
    for rel in reports:
        report_issues, pending = check_report(root, rel, ctx)
        issues += report_issues
        manual_pending += pending
    items = report_items(root, reports)
    issues += check_id_stability(items, ctx)
    manual_pending += ctx.manual_pending

    tracked = list(reports) + source_files(root) + ctx.sources_read()
    if snapshot:
        tracked.append(research_review.INVENTORY_FILE.as_posix())
    inputs = research_review.file_revisions(root, tracked)
    for rel, data in ctx._sources.items():
        if data['source_sha256'] and inputs.get(data['read_rel']) != data['source_sha256'][:16]:
            issues.append(_issue(rel, ERROR, '来源无法核验', '抽取后原文件修订发生变化，请重新核验'))
    errors = [issue for issue in issues if issue['severity'] == ERROR]
    status = (research_review.UNVERIFIABLE if any(i['category'] == '来源无法核验' for i in errors)
              else research_review.FAILED if errors else research_review.PASSED)
    evidence = {rel: {'source_sha256': data['source_sha256'], 'extractor_version': data['extractor_version'],
                      'status': data['status'], 'anchors': [s['anchor'] for s in data['segments']],
                      'ids': ctx.id_evidence.get(rel, [])}
                for rel, data in ctx._sources.items()}
    research_review.write_review(root, status, inputs, errors, manual_pending, evidence)
    # 只有完整验收通过的 ID 才能成为下一轮的基线；失败时保留上一份，不清空也不覆盖
    if status == research_review.PASSED and not manual_pending:
        research_review.write_baseline(root, {
            'accepted_from': reports,
            'item_ids': sorted({i['item_id'] for i in items if not _is_empty(i['item_id'])}),
            'rule_ids': sorted({i['rule_id'] for i in items if not _is_empty(i['rule_id'])}),
        })
    return {'status': status, 'issues': issues, 'manual_pending': manual_pending, 'inputs': inputs}


def check_exit_code(result: dict) -> int:
    if result['status'] in {NO_REPORT, research_review.UNVERIFIABLE}:
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
        _emit('research/ 下没有资料分析报告，无法核验（请先运行 /ontology-research）')
        return
    errors = [i for i in result['issues'] if i['severity'] == ERROR]
    warnings = [i for i in result['issues'] if i['severity'] == WARNING]
    # 只用中文方括号标记，不用 emoji：控制台编码不一定支持，打印失败会把"通过"
    # 变成异常退出。
    for issue in errors + warnings:
        _emit(f'[{issue["severity"]}][{issue["category"]}] {issue["path"]}：{issue["message"]}')
    for item in result['manual_pending']:
        _emit(f'[待人工确认] {item["path"]}：{item["item"]}')
    if result['status'] == research_review.UNVERIFIABLE:
        _emit('\n来源无法核验：先修正读取或锚点，不得登记新增 ID 绕过；本轮不放行。')
    elif errors:
        _emit(f'\n未通过：{len(errors)} 个确定性错误，{len(warnings)} 条警告。'
              '修正报告后重新运行本检查；验收未通过时不进入对象提取。')
    elif result['manual_pending']:
        _emit(f'\n待核验：确定性检查通过（{len(warnings)} 条警告），但有 '
              f'{len(result["manual_pending"])} 项需人工确认。'
              '确认后在报告中勾选并重新运行本检查。')
    else:
        _emit(f'\n已验收：确定性检查通过（{len(warnings)} 条警告），可进入对象提取。')


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
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
