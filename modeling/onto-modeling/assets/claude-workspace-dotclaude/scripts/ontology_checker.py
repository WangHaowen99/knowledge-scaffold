#!/usr/bin/env python3
"""Ontology Checker — validates business knowledge ontology documents."""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPORT_TZ = ZoneInfo("Asia/Shanghai")

# Sibling script under .claude/scripts/; works when invoked as
# `python3 .claude/scripts/ontology_checker.py` (sys.path[0] is the script dir).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from knowledge_helper import parse_frontmatter as _hk_parse_frontmatter

# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class OntologyDoc:
    path: Path
    doc_type: str          # "object" | "operation" | "action"
    raw: str
    sections: dict = field(default_factory=dict)
    obsidian_links: list = field(default_factory=list)
    attribute_rows: list = field(default_factory=list)   # object docs
    input_rows: list = field(default_factory=list)       # operation docs
    output_rows: list = field(default_factory=list)      # operation docs
    action_rows: list = field(default_factory=list)      # operation docs (empty when action phase disabled)

@dataclass
class Issue:
    file: str
    severity: str   # "错误" | "警告" | "建议"
    category: str
    message: str
    source: str     # "规则检查" | "LLM语义"


# =============================================================================
# PARSER
# =============================================================================

def extract_sections(raw: str) -> dict:
    """Extract heading sections. Keys have trailing colons stripped."""
    sections = {}
    current = None
    buffer = []
    for line in raw.split('\n'):
        m = re.match(r'^#{1,3}\s+(.+)', line)
        if m:
            if current is not None:
                sections[current] = '\n'.join(buffer).strip()
            current = m.group(1).strip().rstrip('：:').strip()
            buffer = []
        else:
            if current is not None:
                buffer.append(line)
    if current is not None:
        sections[current] = '\n'.join(buffer).strip()
    return sections


def extract_obsidian_links(raw: str) -> list:
    """Extract [[link]] targets as filename stems (no path, no alias, no extension)."""
    links = []
    for m in re.finditer(r'\[\[([^\]]+)\]\]', raw):
        target = m.group(1).split('|')[0].strip()   # drop alias
        stem = target.split('/')[-1].strip()          # drop path prefix
        stem = re.sub(r'\.md$', '', stem, flags=re.IGNORECASE)
        if stem and stem not in links:
            links.append(stem)
    return links


def parse_markdown_table(text: str) -> list:
    """Parse a markdown table into list of dicts. Returns [] if no valid table."""
    lines = [l for l in text.split('\n') if re.match(r'^\s*\|', l)]
    if len(lines) < 3:
        return []
    headers = [h.strip() for h in lines[0].split('|')[1:-1]]
    if not re.match(r'^\s*\|[\s\-:|]+\|', lines[1]):
        return []
    rows = []
    for line in lines[2:]:
        cells = [c.strip() for c in line.split('|')[1:-1]]
        if len(cells) >= len(headers):
            rows.append({headers[i]: cells[i] for i in range(len(headers))})
    return rows


def load_ontology(ontology_dir: Path) -> dict:
    """Load all object, operation, and action docs. Returns {stem: OntologyDoc}.

    Cross-category stem duplicates are silently overwritten here; they are surfaced
    as Issues via ``check_duplicate_stems`` during rule checks.
    """
    docs = {}
    # ontology/logic 是旧版目录，兼容读取为 operation（范式升级 ADR-0004）
    for subdir, dtype in [('object', 'object'), ('operation', 'operation'), ('logic', 'operation'), ('rule', 'rule'), ('metric', 'metric'), ('action', 'action')]:
        d = ontology_dir / subdir
        if d.exists():
            files = d.rglob('*.md') if subdir in ('object', 'operation', 'logic') else d.glob('*.md')
            for f in sorted(files):
                if f.name.lower() == 'index.md':
                    continue  # 目录索引不是实体文档
                docs[f.stem] = load_doc(f, dtype)
    return docs


KNOWLEDGE_REQUIRED_FIELDS = (
    'name', 'type', 'slug', 'scopes', 'source', 'confidence', 'revisions', 'related'
)
KNOWLEDGE_VALID_TYPES = ('correction', 'convention', 'terminology', 'domain-rule')
KNOWLEDGE_VALID_CONFIDENCE = ('high', 'medium', 'low')


def check_knowledge(ontology_dir):
    knowledge_dir = ontology_dir / 'knowledge'
    if not knowledge_dir.exists():
        return []
    issues = []

    seen_files = set()
    for f in sorted(knowledge_dir.glob('*.md')):
        if f.name == 'INDEX.md':
            continue
        seen_files.add(f.name)
        rel = f'knowledge/{f.name}'
        raw = f.read_text(encoding='utf-8')
        try:
            fm, _ = _hk_parse_frontmatter(raw)
        except ValueError:
            issues.append(Issue(file=rel, severity='错误', category='知识结构',
                                message='缺少或无法解析 frontmatter',
                                source='规则检查'))
            continue
        for field_ in KNOWLEDGE_REQUIRED_FIELDS:
            if field_ not in fm:
                issues.append(Issue(file=rel, severity='错误', category='知识字段',
                                    message=f'frontmatter 缺少必需字段：{field_}',
                                    source='规则检查'))
        if fm.get('type') and fm['type'] not in KNOWLEDGE_VALID_TYPES:
            issues.append(Issue(file=rel, severity='错误', category='知识类型',
                                message=f'非法类型：{fm["type"]}',
                                source='规则检查'))
        if fm.get('confidence') and fm['confidence'] not in KNOWLEDGE_VALID_CONFIDENCE:
            issues.append(Issue(file=rel, severity='错误', category='知识置信度',
                                message=f'非法置信度：{fm["confidence"]}',
                                source='规则检查'))
        # filename = {type}-{slug}.md
        expected = f'{fm.get("type", "")}-{fm.get("slug", "")}.md'
        if expected != f.name:
            issues.append(Issue(file=rel, severity='错误', category='知识文件名',
                                message=f'文件名应为 {expected}，实际 {f.name}',
                                source='规则检查'))

    index_path = knowledge_dir / 'INDEX.md'
    if index_path.exists():
        index_text = index_path.read_text(encoding='utf-8')
        for fname in seen_files:
            if fname not in index_text:
                issues.append(Issue(file='knowledge/INDEX.md', severity='警告',
                                    category='知识索引',
                                    message=f'INDEX.md 未收录 {fname}，请运行 knowledge_helper.py sync-index',
                                    source='规则检查'))

    return issues


# CLAUDE.md 规则 3：ontology/ 下允许放 .md 文件的子目录白名单。
# scripts/ 仅放 .py（codegen 输出），diagrams/ 仅放 .drawio，因此都不在 .md 白名单内。
ALLOWED_MD_SUBDIRS = (
    'object', 'operation', 'logic', 'rule', 'metric', 'action', 'data-mock',
    'diagrams-summary', 'testcase', 'knowledge',
)


def check_file_locations(ontology_dir: Path) -> list:
    """CLAUDE.md 规则 3：ontology/ 下的 .md 文件必须放在白名单子目录中。"""
    if not ontology_dir.exists():
        return []
    issues = []
    allowed_str = '、'.join(ALLOWED_MD_SUBDIRS)
    for f in sorted(ontology_dir.rglob('*.md')):
        rel = f.relative_to(ontology_dir)
        parts = rel.parts
        # 跳过隐藏目录里的文件（避免 .git/ 等的潜在干扰）
        if any(p.startswith('.') for p in parts[:-1]):
            continue
        # index.md 是平台维护的目录索引，允许出现在任意子目录
        if f.name.lower() == 'index.md':
            continue
        if len(parts) == 1:
            issues.append(Issue(
                file=str(rel), severity='错误', category='文件位置',
                message=f'ontology/ 根目录不允许直接放 .md 文件；请移至白名单子目录之一：{allowed_str}',
                source='规则检查',
            ))
        elif parts[0] not in ALLOWED_MD_SUBDIRS:
            issues.append(Issue(
                file=str(rel), severity='错误', category='文件位置',
                message=f'文件位于 ontology/{parts[0]}/ —— 非白名单目录；允许的子目录：{allowed_str}',
                source='规则检查',
            ))
    return issues


def check_duplicate_stems(ontology_dir: Path) -> list:
    """Detect files whose stem collides across object/operation/action — [[links]] become ambiguous."""
    by_stem: dict = {}
    for subdir in ('object', 'operation', 'logic', 'rule', 'metric', 'action'):
        d = ontology_dir / subdir
        if not d.exists():
            continue
        files = d.rglob('*.md') if subdir in ('object', 'operation', 'logic') else d.glob('*.md')
        for f in sorted(files):
            if f.name.lower() == 'index.md':
                continue  # 平台目录索引不是实体，不参与 [[链接]] 语义
            by_stem.setdefault(f.stem, []).append(f)
    issues = []
    for stem, paths in by_stem.items():
        if len(paths) <= 1:
            continue
        others = '、'.join(str(p.relative_to(ontology_dir)) for p in paths[1:])
        issues.append(Issue(
            file=str(paths[0].relative_to(ontology_dir)),
            severity='错误', category='重名',
            message=f'文件名 "{stem}.md" 在多个分类中重复（另见 {others}），[[链接]]会出现歧义',
            source='规则检查',
        ))
    return issues


def check_todo_placeholders(doc: OntologyDoc) -> list:
    """Flag TODO placeholders left in committed ontology docs.

    Canonical format is `TODO: 待补充{字段名}` per VALIDATION_BOUNDARIES.md「TODO 占位符」.
    Any line containing `TODO:` is reported as a 警告 / 未完成项.
    Non-canonical prefixes (e.g., `TODO: 需补充X`) are still caught and message hints
    the canonical form so authors fix the wording.
    """
    issues = []
    rel = _rel(doc)
    for lineno, line in enumerate(doc.raw.splitlines(), start=1):
        m = re.search(r'TODO:\s*(\S[^\n]{0,80})', line)
        if not m:
            continue
        snippet = m.group(0).strip()
        is_canonical = '待补充' in snippet
        hint = '' if is_canonical else '（建议改为 `TODO: 待补充{字段名}` 统一格式）'
        issues.append(Issue(
            file=rel, severity='警告', category='未完成项',
            message=f'第 {lineno} 行残留占位符 `{snippet}`{hint}',
            source='规则检查',
        ))
    return issues


def load_doc(path: Path, doc_type: str) -> OntologyDoc:
    raw = path.read_text(encoding='utf-8')
    sections = extract_sections(raw)
    # 章节名随范式升级迁移（ADR-0004）：旧文档章节读取时归一为新名
    if '与logic的关联' in sections and '与operation的关联' not in sections:
        sections['与operation的关联'] = sections.pop('与logic的关联')
    obsidian_links = extract_obsidian_links(raw)
    attribute_rows, input_rows, output_rows, action_rows = [], [], [], []
    if doc_type == 'object':
        attribute_rows = parse_markdown_table(sections.get('对象属性清单', ''))
    elif doc_type == 'operation':
        input_rows = parse_markdown_table(sections.get('输入', ''))
        output_rows = parse_markdown_table(sections.get('输出', ''))
        action_rows = parse_markdown_table(sections.get('与动作(action)的关联', ''))
    return OntologyDoc(
        path=path, doc_type=doc_type, raw=raw, sections=sections,
        obsidian_links=obsidian_links, attribute_rows=attribute_rows,
        input_rows=input_rows, output_rows=output_rows, action_rows=action_rows,
    )


# =============================================================================
# RULE CHECKER
# =============================================================================

OBJECT_REQUIRED_SECTIONS = ['对象描述', '对象别名', '英文名称', '对象属性清单']
OBJECT_OPTIONAL_SECTIONS = ['关联对象']
OBJECT_REQUIRED_COLUMNS = ['中文名称', '英文名称', '描述', '主键', '类型']

OPERATION_REQUIRED_SECTIONS = ['业务描述', '与operation的关联', '与object的关联', '输入', '输出', '逻辑描述']
OPERATION_INPUT_REQUIRED_COLUMNS = ['参数名称', '描述']
OPERATION_OUTPUT_REQUIRED_COLUMNS = ['参数名称', '描述']
ACTION_REQUIRED_SECTIONS = ['动作描述']

# 范式升级（ADR-0004）：规则/指标为一等实体，元信息对齐 modeling 知识包模型
RULE_REQUIRED_SECTIONS = ['规则描述', '适用对象', '约束表达']
METRIC_REQUIRED_SECTIONS = ['指标描述', '计算公式', '数据来源']


def _rel(doc: OntologyDoc) -> str:
    parts = doc.path.parts
    for i, p in enumerate(parts):
        if p in ('object', 'operation', 'logic', 'rule', 'metric', 'action'):
            return '/'.join(parts[i:])
    return doc.path.name


# --- Rule / Metric checks（通用结构 + 链接存在性）---

def check_entity_structure(doc: OntologyDoc, required_sections: list, cn: str) -> list:
    issues = []
    for section in required_sections:
        if section not in doc.sections or not doc.sections[section].strip():
            issues.append(f"错误 {_rel(doc)}: 缺少必需章节「{section}」（{cn}模板要求）")
    return issues


def check_entity_links(doc: OntologyDoc, all_docs: dict, cn: str) -> list:
    issues = []
    known = set(all_docs.keys())
    for link in doc.obsidian_links:
        target = link.split('|')[0].split('#')[0].strip()
        if target and target not in known:
            issues.append(f"警告 {_rel(doc)}: {cn}引用了不存在的文档 [[{target}]]")
    return issues


# --- Object checks ---

def check_object_structure(doc: OntologyDoc) -> list:
    issues = [
        Issue(file=_rel(doc), severity='错误', category='结构缺失',
              message=f'缺少必需章节：# {s}', source='规则检查')
        for s in OBJECT_REQUIRED_SECTIONS if s not in doc.sections
    ]
    for s in OBJECT_OPTIONAL_SECTIONS:
        if s not in doc.sections:
            issues.append(Issue(file=_rel(doc), severity='建议', category='结构缺失',
                                message=f'建议添加章节：# {s}', source='规则检查'))
    return issues


def check_object_table(doc: OntologyDoc) -> list:
    if not doc.attribute_rows:
        return [Issue(file=_rel(doc), severity='错误', category='表格缺失',
                      message='对象属性清单中未找到有效表格', source='规则检查')]
    present = set(doc.attribute_rows[0].keys())
    return [
        Issue(file=_rel(doc), severity='错误', category='表格格式',
              message=f'对象属性清单缺少必需列：{col}', source='规则检查')
        for col in OBJECT_REQUIRED_COLUMNS if col not in present
    ]


def check_object_primary_key(doc: OntologyDoc) -> list:
    if not doc.attribute_rows:
        return []
    if not any(row.get('主键', '').strip() == '是' for row in doc.attribute_rows):
        return [Issue(file=_rel(doc), severity='错误', category='主键缺失',
                      message='对象属性清单中没有标注主键=是的属性', source='规则检查')]
    return []


def check_object_links(doc: OntologyDoc, all_docs: dict) -> list:
    return [
        Issue(file=_rel(doc), severity='错误', category='链接失效',
              message=f'Obsidian 链接 [[{link}]] 无对应文件', source='规则检查')
        for link in doc.obsidian_links if link not in all_docs
    ]


# 派生属性强信号关键词：
# - 属性规则 / 描述任一列出现这些词,说明该属性"被某个 operation 写入"
# - 命中后,属性规则列必须使用规范格式 `派生：由 [[operation名]] 写入[；公式 = 表达式]`
_DERIVED_STRONG_KW = re.compile(
    r'派生|回写|自动生成|系统产出|自动写入'
    r'|由\s*\[\[[^\]]+\]\][^|]*(?:写入|回写|产出|生成)'
)
# 规范格式：派生：由 [[X]]（可顿号串联多个）写入
_DERIVED_CANONICAL = re.compile(
    r'派生[:：]\s*由\s*((?:\[\[[^\]]+\]\][、，,\s]*)+)\s*写入'
)


def check_derived_attribute_markers(doc: OntologyDoc, all_docs: dict) -> list:
    """对象的派生属性标记必须遵循规范格式（CLAUDE.md 派生属性约定）。

    形态级检查：当属性描述或规则中出现派生强信号关键词时，「属性规则」列必须含
    `派生：由 [[operation名]] 写入` 形式。规范命中后再校验括号内 stem 指向 operation 文档。
    跨文档完整性（operation 输出但 object 完全无痕迹）留给 semantic-check 处理。
    """
    if doc.doc_type != 'object':
        return []
    issues = []
    for row in doc.attribute_rows:
        name = row.get('中文名称', '').strip()
        if not name:
            continue
        rule_cell = row.get('属性规则', '').strip()
        desc_cell = row.get('描述', '').strip()
        combined = f'{desc_cell} || {rule_cell}'
        if not _DERIVED_STRONG_KW.search(combined):
            continue
        m = _DERIVED_CANONICAL.search(rule_cell)
        if not m:
            issues.append(Issue(
                file=_rel(doc), severity='警告', category='派生标记格式',
                message=(f'属性 "{name}" 在描述或规则中含派生信号（派生/回写/自动生成 等），'
                         f'但「属性规则」列未使用规范格式 `派生：由 [[operation名]] 写入[；公式 = 表达式]`'),
                source='规则检查',
            ))
            continue
        # 规范命中 → 校验每个 [[stem]] 指向 operation
        for link_match in re.finditer(r'\[\[([^\]]+)\]\]', m.group(1)):
            target = link_match.group(1).split('|')[0].strip()
            stem = target.split('/')[-1].strip()
            stem = re.sub(r'\.md$', '', stem, flags=re.IGNORECASE)
            target_doc = all_docs.get(stem)
            if target_doc is None:
                continue  # link integrity 已由 check_object_links 处理
            if target_doc.doc_type != 'operation':
                issues.append(Issue(
                    file=_rel(doc), severity='警告', category='派生标记格式',
                    message=(f'属性 "{name}" 派生标记引用 [[{stem}]]，'
                             f'但该文档类型为 {target_doc.doc_type}（应为 operation）'),
                    source='规则检查',
                ))
    return issues


# --- Operation checks ---

def _extract_sub_operation_refs(operation_text: str) -> list:
    """Extract sub-operation references from operation description."""
    refs = re.findall(r'调用\[\[([^\]]+)\]\]逻辑文档', operation_text)
    refs += re.findall(r'调用\s*"([^"]+)"\s*逻辑文档', operation_text)
    return refs


def check_operation_structure(doc: OntologyDoc) -> list:
    issues = []
    for s in OPERATION_REQUIRED_SECTIONS:
        if s not in doc.sections:
            issues.append(Issue(file=_rel(doc), severity='错误', category='结构缺失',
                                message=f'缺少必需章节：# {s}', source='规则检查'))
            continue
        content = doc.sections[s].strip()
        if '{待填写' in content:
            issues.append(Issue(file=_rel(doc), severity='警告', category='占位未替换',
                                message=f'章节 # {s} 仍含占位符 {{待填写...}}',
                                source='规则检查'))
    return issues


def check_operation_table(doc: OntologyDoc) -> list:
    issues = []
    if '输入' in doc.sections:
        if not doc.input_rows:
            # Check if it says '无'
            content = doc.sections.get('输入', '').strip()
            if content and content != '无' and '无' not in content.split('\n')[0]:
                issues.append(Issue(file=_rel(doc), severity='警告', category='表格缺失',
                                    message='输入章节中未找到有效表格', source='规则检查'))
        else:
            present = set(doc.input_rows[0].keys())
            for col in OPERATION_INPUT_REQUIRED_COLUMNS:
                if col not in present:
                    issues.append(Issue(file=_rel(doc), severity='警告', category='表格格式',
                                        message=f'输入表格缺少列：{col}', source='规则检查'))
    if '输出' in doc.sections:
        if not doc.output_rows:
            content = doc.sections.get('输出', '').strip()
            if content and content != '无' and '无' not in content.split('\n')[0]:
                issues.append(Issue(file=_rel(doc), severity='警告', category='表格缺失',
                                    message='输出章节中未找到有效表格', source='规则检查'))
        else:
            present = set(doc.output_rows[0].keys())
            for col in OPERATION_OUTPUT_REQUIRED_COLUMNS:
                if col not in present:
                    issues.append(Issue(file=_rel(doc), severity='警告', category='表格格式',
                                        message=f'输出表格缺少列：{col}', source='规则检查'))
    return issues


def check_operation_links(doc: OntologyDoc, all_docs: dict) -> list:
    return [
        Issue(file=_rel(doc), severity='错误', category='链接失效',
              message=f'Obsidian 链接 [[{link}]] 无对应文件', source='规则检查')
        for link in doc.obsidian_links if link not in all_docs
    ]


def _stems_in_section(section_text: str) -> set:
    """Return the set of [[stem]] targets in a section, alias- and path-aware."""
    stems = set()
    for m in re.finditer(r'\[\[([^\]]+)\]\]', section_text):
        target = m.group(1).split('|')[0].strip()      # drop alias
        stem = target.split('/')[-1].strip()            # drop path prefix
        stem = re.sub(r'\.md$', '', stem, flags=re.IGNORECASE)
        if stem and stem != '无':
            stems.add(stem)
    return stems


def _called_operation_stems(flow_section: str) -> set:
    """Extract sub-operation stems declared on the '调用operation' line(s) of 与operation的关联."""
    stems = set()
    for line in flow_section.split('\n'):
        if re.match(r'^\s*[-•]?\s*调用\s*operation\b', line, re.IGNORECASE):
            stems |= _stems_in_section(line)
    return stems


def _caller_operation_stems(flow_section: str) -> set:
    """Extract parent-operation stems declared on the '被operation调用' line(s) of 与operation的关联."""
    stems = set()
    for line in flow_section.split('\n'):
        if re.match(r'^\s*[-•]?\s*被\s*operation\s*调用\b', line, re.IGNORECASE):
            stems |= _stems_in_section(line)
    return stems


def check_operation_sub_refs(doc: OntologyDoc, all_docs: dict) -> list:
    """Check sub-operation calls and the bidirectional contract with 与operation的关联.

    Forward: 调用[[X]]逻辑文档 in 逻辑描述 → X 必须在 与operation的关联 → 调用operation 行
    Reverse: 调用operation 行声明的 [[X]] → 必须在 逻辑描述 中被实际 调用[[X]]逻辑文档
    """
    issues = []
    operation = doc.sections.get('逻辑描述', '')
    flow_section = doc.sections.get('与operation的关联', '')
    called_operation_stems = _called_operation_stems(flow_section)
    called_refs = set(_extract_sub_operation_refs(operation))

    # Forward
    for ref in _extract_sub_operation_refs(operation):
        if ref not in all_docs:
            issues.append(Issue(file=_rel(doc), severity='错误', category='引用失效',
                                message=f'逻辑描述中调用的子operation "{ref}" 无对应文件',
                                source='规则检查'))
        elif ref not in called_operation_stems:
            issues.append(Issue(file=_rel(doc), severity='警告', category='链接缺失',
                                message=f'子operation "{ref}" 在逻辑描述中被调用，但未在 与operation的关联 → 调用operation 中列出',
                                source='规则检查'))

    # Reverse
    for declared in called_operation_stems:
        if declared not in called_refs:
            issues.append(Issue(file=_rel(doc), severity='警告', category='链接冗余',
                                message=f'子operation "{declared}" 在 与operation的关联 → 调用operation 中声明，但逻辑描述中未实际调用（格式：调用[[{declared}]]逻辑文档）',
                                source='规则检查'))
    return issues


def check_operation_caller_refs(doc: OntologyDoc, all_docs: dict) -> list:
    """Check the bidirectional contract for 被operation调用.

    For each parent P declared in D.被operation调用, P must satisfy ONE of:
    - P.调用operation contains [[D]]
    - P.逻辑描述 contains 调用[[D]]逻辑文档

    若两者都不满足，本 operation 实际不是 P 的子例程，应改写成"无（…触发方式…）"。
    """
    issues = []
    flow_section = doc.sections.get('与operation的关联', '')
    declared_parents = _caller_operation_stems(flow_section)
    d_stem = doc.path.stem

    for parent_stem in declared_parents:
        parent_doc = all_docs.get(parent_stem)
        if parent_doc is None:
            # 链接失效已由 check_operation_flow_refs 报，不重复
            continue
        if parent_doc.doc_type != 'operation':
            issues.append(Issue(
                file=_rel(doc), severity='警告', category='流程关联不完整',
                message=f'被operation调用 中声明的父 [[{parent_stem}]] 不是 operation 文档',
                source='规则检查'))
            continue
        parent_flow = parent_doc.sections.get('与operation的关联', '')
        parent_operation = parent_doc.sections.get('逻辑描述', '')
        parent_called_stems = _called_operation_stems(parent_flow)
        parent_invocations = set(_extract_sub_operation_refs(parent_operation))
        if d_stem not in parent_called_stems and d_stem not in parent_invocations:
            issues.append(Issue(
                file=_rel(doc), severity='警告', category='流程关联不完整',
                message=(
                    f'被operation调用 声明父 [[{parent_stem}]]，但 [[{parent_stem}]] 既未在 调用operation 中列出本operation，'
                    f'也未在 逻辑描述 中出现 "调用[[{d_stem}]]逻辑文档"——若本operation由独立触发'
                    f'（异步回调 / 定时任务 / 用户直接发起 / 接口直接触发），应改填 "无（…触发方式…）"'
                ),
                source='规则检查'))
    return issues



def check_operation_flow_refs(doc: OntologyDoc, all_docs: dict) -> list:
    """Check that flow references in the flow association section have corresponding files."""
    issues = []
    flow_section = doc.sections.get('与operation的关联', '')
    for link in re.findall(r'\[\[([^\]]+)\]\]', flow_section):
        stem = link.split('|')[0].strip()
        if stem == '无' or not stem:
            continue
        if stem not in all_docs:
            issues.append(Issue(file=_rel(doc), severity='警告', category='链接失效',
                                message=f'流程关联中引用的流程 [[{stem}]] 无对应文件',
                                source='规则检查'))
    return issues


def check_branch_completeness(doc: OntologyDoc) -> list:
    """Check decision points in operation for missing branches."""
    issues = []
    operation = doc.sections.get('逻辑描述', '')
    if not operation:
        return []

    lines = operation.split('\n')
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Detect decision points: lines containing 判断/若...？
        if re.search(r'判断.*？', stripped) or stripped.endswith('？'):
            # Collect branches: look for subsequent lines starting with - 若
            branches = []
            for j in range(i + 1, min(i + 20, len(lines))):
                branch_match = re.match(r'\s*[-•]\s*若\s*(.+?)[:：]', lines[j])
                if branch_match:
                    branches.append(branch_match.group(1).strip())
                elif lines[j].strip() and not lines[j].strip().startswith('-') and not lines[j].strip().startswith('•'):
                    break  # no longer in branch block

            if len(branches) == 0:
                issues.append(Issue(
                    file=_rel(doc), severity='警告', category='分支缺失',
                    message=f'判断节点「{stripped.rstrip("？")}」未定义任何分支',
                    source='规则检查'))
            elif len(branches) == 1:
                issues.append(Issue(
                    file=_rel(doc), severity='警告', category='分支缺失',
                    message=f'判断节点「{stripped.rstrip("？")}」仅定义 1 个分支（{branches[0]}），可能缺少其他分支',
                    source='规则检查'))
            else:
                # Check for default/fallback branch
                has_default = any(re.search(r'其他|否则|默认|else|otherwise', b, re.IGNORECASE) for b in branches)
                if not has_default:
                    issues.append(Issue(
                        file=_rel(doc), severity='建议', category='分支缺失',
                        message=f'判断节点「{stripped.rstrip("？")}」有 {len(branches)} 个分支，但无兜底/默认分支',
                        source='规则检查'))
    return issues


def check_sub_operation_contract(doc: OntologyDoc, all_docs: dict) -> list:
    """Check if sub-operation calls satisfy input/output contracts."""
    issues = []
    operation = doc.sections.get('逻辑描述', '')
    if not operation:
        return []

    sub_refs = _extract_sub_operation_refs(operation)
    for ref in sub_refs:
        sub_doc = all_docs.get(ref)
        if sub_doc is None or sub_doc.doc_type != 'operation':
            continue  # broken link is caught by other checks

        caller_inputs = {row.get('参数名称', '').strip() for row in doc.input_rows if row.get('参数名称', '').strip()}
        caller_outputs = {row.get('参数名称', '').strip() for row in doc.output_rows if row.get('参数名称', '').strip()}
        caller_available = caller_inputs | caller_outputs

        sub_inputs = {row.get('参数名称', '').strip() for row in sub_doc.input_rows if row.get('参数名称', '').strip()}
        sub_outputs = {row.get('参数名称', '').strip() for row in sub_doc.output_rows if row.get('参数名称', '').strip()}

        missing = sub_inputs - caller_available
        if missing:
            issues.append(Issue(
                file=_rel(doc), severity='警告', category='契约不匹配',
                message=f'调用子流程[[{ref}]]，其需要输入 {sorted(missing)}，但当前流程的输入/输出中未定义这些参数',
                source='规则检查'))

        if sub_outputs and not (sub_outputs & caller_available):
            issues.append(Issue(
                file=_rel(doc), severity='建议', category='契约不匹配',
                message=f'子流程[[{ref}]]有输出 {sorted(sub_outputs)}，但当前流程未引用任何一个',
                source='规则检查'))

    return issues


# --- Action checks ---

def check_action_structure(doc: OntologyDoc) -> list:
    issues = []
    for s in ACTION_REQUIRED_SECTIONS:
        if s not in doc.sections:
            issues.append(Issue(file=_rel(doc), severity='错误', category='结构缺失',
                                message=f'缺少必需章节：# {s}', source='规则检查'))
            continue
        content = doc.sections[s].strip()
        if not content:
            issues.append(Issue(file=_rel(doc), severity='警告', category='内容为空',
                                message=f'章节 # {s} 内容为空', source='规则检查'))
        elif content == '{待补充}' or content.startswith('{待补充}'):
            issues.append(Issue(file=_rel(doc), severity='建议', category='待补充',
                                message=f'章节 # {s} 标注为待补充，请确认后填写',
                                source='规则检查'))
        elif '{待填写' in content:
            issues.append(Issue(file=_rel(doc), severity='警告', category='占位未替换',
                                message=f'章节 # {s} 仍含占位符 {{待填写...}}',
                                source='规则检查'))
    return issues


def check_action_orphan(doc: OntologyDoc, all_docs: dict) -> list:
    """Warn if an action file is not referenced by any operation document."""
    action_stem = doc.path.stem
    for other_doc in all_docs.values():
        if other_doc.doc_type == 'operation' and action_stem in other_doc.obsidian_links:
            return []
    return [Issue(file=_rel(doc), severity='建议', category='孤立动作',
                  message=f'动作 "{action_stem}" 未被任何逻辑文档引用', source='规则检查')]


# --- Cross-doc checks ---

def build_term_map(all_docs: dict) -> dict:
    """Build {object_stem: set(attribute_names)} from all object docs.

    Includes Chinese names, English names, and aliases so that operation docs
    referencing fields by English name (e.g. [[订单]].order_status) are
    correctly matched against the object's attribute table.
    """
    result = {}
    for stem, doc in all_docs.items():
        if doc.doc_type != 'object' or not doc.attribute_rows:
            continue
        names: set = set()
        for row in doc.attribute_rows:
            cn = row.get('中文名称', '').strip()
            en = row.get('英文名称', '').strip()
            alias = row.get('属性别名', '').strip()
            if cn:
                names.add(cn)
            if en:
                names.add(en)
            for a in alias.split(','):
                a = a.strip()
                if a and a != '无':
                    names.add(a)
        result[stem] = names
    return result


# --- Mock data checks ---

def _extract_object_refs(doc: OntologyDoc) -> list:
    """For an object, collect attribute names whose type is 对象引用 together with their target object(s).

    Returns list of (attribute_中文名称, [target_stem, ...]).
    Target stems are resolved by scanning 关联对象 section for [[X]] links and by matching the description.
    """
    refs = []
    if doc.doc_type != 'object':
        return refs
    for row in doc.attribute_rows:
        type_cell = row.get('类型', '').strip()
        if '对象引用' not in type_cell:
            continue
        name = row.get('中文名称', '').strip()
        desc = row.get('描述', '')
        # Extract target stems from description or type column
        candidates = re.findall(r'\[\[([^\]|/]+?)(?:\|[^\]]+)?\]\]', desc + ' ' + type_cell)
        if name and candidates:
            refs.append((name, [c.strip() for c in candidates]))
    # Also infer from 关联对象 lines when available — only when attribute name exactly
    # equals a linked stem (substring match caused "顾客" to wrongly match "顾客服务").
    relation_section = doc.sections.get('关联对象', '')
    linked_stems = set(re.findall(r'\[\[([^\]|/]+?)(?:\|[^\]]+)?\]\]', relation_section))
    for row in doc.attribute_rows:
        name = row.get('中文名称', '').strip()
        if not name or name not in linked_stems:
            continue
        if not any(r[0] == name for r in refs):
            refs.append((name, [name]))
    return refs


def _parse_mock_tables(path: Path) -> dict:
    """Parse a mock data file. Only 正常数据 participates in rule checks."""
    if not path.exists():
        return {}
    raw = path.read_text(encoding='utf-8')
    sections = extract_sections(raw)
    # 章节名随范式升级迁移（ADR-0004）：旧文档章节读取时归一为新名
    if '与logic的关联' in sections and '与operation的关联' not in sections:
        sections['与operation的关联'] = sections.pop('与logic的关联')
    result = {}
    table = parse_markdown_table(sections.get('正常数据', ''))
    if not table:
        body_lines = []
        in_body = False
        for line in raw.split('\n'):
            if re.match(r'^#\s+', line):
                in_body = True
                continue
            if in_body and re.match(r'^#{1,6}\s+', line):
                break
            if in_body:
                body_lines.append(line)
        table = parse_markdown_table('\n'.join(body_lines))
    if table:
        result['正常'] = table
    return result


def _find_primary_key(obj_doc: OntologyDoc) -> str:
    for row in obj_doc.attribute_rows:
        if row.get('主键', '').strip() == '是':
            return row.get('中文名称', '').strip()
    return ''


def check_mock_data(all_docs: dict, mock_dir: Path) -> list:
    """Rule-level mock data checks: primary-key uniqueness + foreign-key integrity.

    Mock data is normal-only. Older non-normal sections, if present, are ignored
    for compatibility instead of being treated as valid data.
    """
    issues = []
    if not mock_dir.exists():
        return issues

    mocks = {}
    for f in sorted(mock_dir.glob('*.md')):
        mocks[f.stem] = (f, _parse_mock_tables(f))

    # Primary-key uniqueness (per object, normal rows only)
    for stem, (path, tables) in mocks.items():
        obj_doc = all_docs.get(stem)
        if obj_doc is None or obj_doc.doc_type != 'object':
            continue
        pk = _find_primary_key(obj_doc)
        if not pk:
            continue
        seen = {}
        for label, rows in tables.items():
            for row in rows:
                val = row.get(pk, '').strip()
                if not val:
                    continue
                if val in seen:
                    issues.append(Issue(
                        file=f'data-mock/{stem}.md', severity='错误', category='主键重复',
                        message=f'主键 "{pk}" 的值 "{val}" 在 {seen[val]} 与 {label}数据 中同时出现',
                        source='规则检查'))
                else:
                    seen[val] = f'{label}数据'

    # Foreign-key integrity: for each object ref attribute, check normal rows point to valid target pk values
    for stem, (path, tables) in mocks.items():
        obj_doc = all_docs.get(stem)
        if obj_doc is None or obj_doc.doc_type != 'object':
            continue
        refs = _extract_object_refs(obj_doc)
        if not refs:
            continue
        for attr_name, target_stems in refs:
            for target_stem in target_stems:
                target_doc = all_docs.get(target_stem)
                if target_doc is None or target_doc.doc_type != 'object':
                    continue
                _, target_mock = mocks.get(target_stem, (None, {}))
                target_pk = _find_primary_key(target_doc)
                if not target_pk:
                    continue
                valid_values = set()
                for row in target_mock.get('正常', []):
                    v = row.get(target_pk, '').strip()
                    if v:
                        valid_values.add(v)
                if not valid_values:
                    continue  # no target data to check against
                for idx, row in enumerate(tables.get('正常', []), start=1):
                    val = row.get(attr_name, '').strip()
                    if not val or val == '无':
                        continue
                    if val not in valid_values:
                        issues.append(Issue(
                            file=f'data-mock/{stem}.md', severity='警告', category='外键缺失',
                            message=f'正常数据 第 {idx} 行 "{attr_name}"={val} 在 [[{target_stem}]] 的主键"{target_pk}"取值中不存在',
                            source='规则检查'))
    return issues


def check_terminology(operation_doc: OntologyDoc, term_map: dict) -> list:
    """Check field accesses `[[Object]].fieldName` reference attributes that exist on that object.

    Only `.fieldName` access patterns are validated. Quoted strings on the same line
    (typically enum values like "已下线" or "BEV") are ignored — they are values, not
    field names, and treating them as field-name references caused widespread false
    positives.
    """
    issues = []
    operation = operation_doc.sections.get('逻辑描述', '')
    for line in operation.split('\n'):
        obj_refs = re.findall(r'\[\[([^\]|/]+?)(?:\|[^\]]+)?\]\]', line)
        obj_refs += re.findall(r'\[\[[^\]]+/([^\]|/]+?)(?:\|[^\]]+)?\]\]', line)
        obj_refs = [r.strip() for r in obj_refs if r.strip() in term_map]

        for obj_name in obj_refs:
            known = term_map[obj_name]
            obj_field_refs = re.findall(
                r'\[\[' + re.escape(obj_name) + r'\]\]\.([^\s.=!<>;,()（）、，；：\[\]"]+)',
                line,
            )
            for field_name in obj_field_refs:
                if field_name not in known:
                    issues.append(Issue(
                        file=_rel(operation_doc), severity='警告', category='术语不一致',
                        message=f'逻辑描述访问字段 ".{field_name}" 在 [[{obj_name}]] 中未定义',
                        source='规则检查',
                    ))
    return issues




# =============================================================================
# REPORT GENERATOR
# =============================================================================

SEVERITY_ICON = {'错误': '❌', '警告': '⚠️', '建议': '💡'}


def generate_report(issues: list, ontology_dir: str) -> str:
    now = datetime.now(REPORT_TZ).strftime('%Y-%m-%d %H:%M:%S')

    def cnt(lst, sev): return sum(1 for i in lst if i.severity == sev)

    lines = [
        '# Ontology 检查报告', '',
        f'生成时间：{now} Asia/Shanghai',
        f'检查目录：{ontology_dir}',
        f'规则检查：✓  |  语义检查：由 Claude 执行（semantic-check-agent）',
        '', '---', '',
        '## 汇总', '',
        '| 类型 | 错误 | 警告 | 建议 |',
        '|------|------|------|------|',
        f'| 规则检查 | {cnt(issues,"错误")} | {cnt(issues,"警告")} | {cnt(issues,"建议")} |',
        '', '---', '',
    ]

    def render(section_issues: list, title: str):
        lines.append(f'## {title}')
        lines.append('')
        if not section_issues:
            lines.append('无问题。')
            lines.append('')
            return
        by_file: dict = {}
        for issue in section_issues:
            by_file.setdefault(issue.file, []).append(issue)
        for fname, file_issues in sorted(by_file.items()):
            lines.append(f'### {fname}')
            lines.append('')
            for issue in file_issues:
                icon = SEVERITY_ICON.get(issue.severity, '•')
                lines.append(f'- {icon} **[{issue.severity}][{issue.category}]** {issue.message}')
            lines.append('')

    render(issues, '规则检查结果')
    return '\n'.join(lines)


# =============================================================================
# CLI
# =============================================================================

def run_checks(ontology_dir: Path, target_files: list = None, include_mock: bool = False,
               include_knowledge: bool = False) -> list:
    print(f"Loading ontology from {ontology_dir}...", file=sys.stderr)
    all_docs = load_ontology(ontology_dir)
    print(f"  {len(all_docs)} documents loaded", file=sys.stderr)

    # Determine which docs to check
    if target_files:
        check_docs = {k: v for k, v in all_docs.items() if k in target_files}
        skipped = [f for f in target_files if f not in all_docs]
        if skipped:
            print(f"  WARNING: stems not found: {skipped}", file=sys.stderr)
        print(f"  Incremental mode: checking {len(check_docs)} of {len(all_docs)} docs", file=sys.stderr)
    else:
        check_docs = all_docs

    issues = list(check_duplicate_stems(ontology_dir))
    issues += check_file_locations(ontology_dir)
    term_map = build_term_map(all_docs)

    print("Running rule checks...", file=sys.stderr)
    for doc in check_docs.values():
        issues += check_todo_placeholders(doc)
        if doc.doc_type == 'object':
            issues += check_object_structure(doc)
            issues += check_object_table(doc)
            issues += check_object_primary_key(doc)
            issues += check_object_links(doc, all_docs)
            issues += check_derived_attribute_markers(doc, all_docs)
        elif doc.doc_type == 'operation':
            issues += check_operation_structure(doc)
            issues += check_operation_table(doc)
            issues += check_operation_links(doc, all_docs)
            issues += check_operation_sub_refs(doc, all_docs)
            issues += check_operation_caller_refs(doc, all_docs)
            issues += check_operation_flow_refs(doc, all_docs)
            issues += check_terminology(doc, term_map)
            issues += check_branch_completeness(doc)
            issues += check_sub_operation_contract(doc, all_docs)
        elif doc.doc_type == 'rule':
            issues += check_entity_structure(doc, RULE_REQUIRED_SECTIONS, '规则')
            issues += check_entity_links(doc, all_docs, '规则')
        elif doc.doc_type == 'metric':
            issues += check_entity_structure(doc, METRIC_REQUIRED_SECTIONS, '指标')
            issues += check_entity_links(doc, all_docs, '指标')
        elif doc.doc_type == 'action':
            issues += check_action_structure(doc)
            issues += check_action_orphan(doc, all_docs)

    if include_mock:
        mock_dir = ontology_dir / 'data-mock'
        mock_issues = check_mock_data(all_docs, mock_dir)
        print(f"  Mock-data checks done: {len(mock_issues)} issues", file=sys.stderr)
        issues += mock_issues

    if include_knowledge:
        knowledge_issues = check_knowledge(ontology_dir)
        print(f"  Knowledge checks done: {len(knowledge_issues)} issues", file=sys.stderr)
        issues += knowledge_issues

    print(f"  Rule checks done: {len(issues)} issues", file=sys.stderr)

    return issues


def main():
    parser = argparse.ArgumentParser(
        description='Check business knowledge ontology docs for quality issues.'
    )
    parser.add_argument('ontology_dir', help='Ontology root directory (contains object/, operation/, action/)')
    parser.add_argument('--output', default=None, help='Report output path (default: report_YYYYMMDD_HHMMSS.md)')
    parser.add_argument('--json', action='store_true', help='Output issues as JSON array to stdout, skip file report')
    parser.add_argument('--files', nargs='*', default=None,
                        help='Only check these file stems (e.g. 订单 创建订单). Loads all docs for cross-ref.')
    parser.add_argument('--include-mock', action='store_true',
                        help='Also check ontology/data-mock/ for primary-key uniqueness and foreign-key integrity')
    parser.add_argument('--include-knowledge', action='store_true',
                        help='Also validate ontology/knowledge/ entries')
    args = parser.parse_args()

    ontology_dir = Path(args.ontology_dir)
    if not ontology_dir.exists():
        print(f"Error: not found: {ontology_dir}", file=sys.stderr)
        sys.exit(1)

    issues = run_checks(ontology_dir, target_files=args.files, include_mock=args.include_mock,
                        include_knowledge=args.include_knowledge)

    if args.json:
        print(json.dumps(
            [{"severity": i.severity, "category": i.category, "file": i.file, "message": i.message} for i in issues],
            ensure_ascii=False
        ))
        return

    output_path = args.output or f"report_{datetime.now(REPORT_TZ).strftime('%Y%m%d_%H%M%S')}.md"
    Path(output_path).write_text(generate_report(issues, str(ontology_dir)), encoding='utf-8')

    rule_errors = sum(1 for i in issues if i.severity == '错误')
    print(f"\nReport saved to: {output_path}", file=sys.stderr)
    print(f"Total: {len(issues)} issues ({rule_errors} errors)", file=sys.stderr)


if __name__ == '__main__':
    main()
