#!/usr/bin/env python3
"""Ontology Checker — validates business knowledge ontology documents."""

import argparse
import ast
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPORT_TZ = ZoneInfo("Asia/Shanghai")

# Sibling script under .claude/scripts/; works when invoked as
# `python3 .claude/scripts/ontology_checker.py` (sys.path[0] is the script dir).
sys.path.append(str(Path(__file__).resolve().parent))
from knowledge_helper import parse_frontmatter as _hk_parse_frontmatter
from script_contract import ScriptContractError, parse_script_contract
from check_eval_data import table_headers as _read_table_headers

# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class OntologyDoc:
    path: Path
    doc_type: str          # "object" | "operation" | "action"
    raw: str
    sections: dict = field(default_factory=dict)
    wiki_links: list = field(default_factory=list)
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


def extract_wiki_links(raw: str) -> list:
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

# 知识包导出范围（与 backend export_package_zip 一致）：只有这些目录的内容
# 会送达 consumer。logic 为 operation 的旧目录兼容（ADR-0004），不再推荐新建。
PACKAGED_SUBDIRS = ('object', 'operation', 'logic', 'rule', 'metric')
RECOMMENDED_PACKAGED_SUBDIRS = ('object', 'operation', 'rule', 'metric')

# 按设计就不该进知识包的过程产物目录——不提示。
WORKING_ARTIFACT_SUBDIRS = ('data-mock', 'diagrams-summary', 'testcase')

# 求解契约受管目录：只由后端接口写入（守卫拒绝直接写），检查器只读取、提示断链。
SOLVER_SUBDIR = 'solver'


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
        elif parts[0] == SOLVER_SUBDIR:
            issues.append(Issue(
                file=str(rel), severity='错误', category='文件位置',
                message=f'ontology/solver/ 是平台受管的求解契约目录，不放知识文档；允许的子目录：{allowed_str}',
                source='规则检查',
            ))
        elif parts[0] not in ALLOWED_MD_SUBDIRS:
            issues.append(Issue(
                file=str(rel), severity='错误', category='文件位置',
                message=f'文件位于 ontology/{parts[0]}/ —— 非白名单目录；允许的子目录：{allowed_str}',
                source='规则检查',
            ))
    return issues


def check_unpackaged_knowledge(ontology_dir: Path) -> list:
    """白名单内但不进知识包的目录里存在知识文档 —— consumer 永远看不到。

    这类内容本身合法（建模代理的上下文沉淀），但常被误当作"补知识"的落点，
    导致改了不生效。此处只提示，不判错。
    """
    if not ontology_dir.exists():
        return []
    silent = set(PACKAGED_SUBDIRS) | set(WORKING_ARTIFACT_SUBDIRS)
    packaged_str = '、'.join(RECOMMENDED_PACKAGED_SUBDIRS)
    issues = []
    for subdir in ALLOWED_MD_SUBDIRS:
        if subdir in silent:
            continue
        d = ontology_dir / subdir
        if not d.is_dir():
            continue
        docs = [f for f in sorted(d.rglob('*.md')) if f.name.lower() != 'index.md']
        if not docs:
            continue
        issues.append(Issue(
            file=f'{subdir}/', severity='警告', category='打包范围',
            message=(
                f'ontology/{subdir}/ 有 {len(docs)} 个知识文档，但该目录不进知识包，'
                f'consumer 检索不到；若这些内容需要被问答使用，请建模为 {packaged_str} '
                f'中的条目（案例/方案类知识 → ontology/rule/ 的场景规则）。'
            ),
            source='规则检查',
        ))
    return issues


def _solver_bound_operation(binding: Path) -> str:
    """binding.yaml 顶层 `operation:` 标量；后端用 yaml.safe_dump 写出，逐行即可取到。"""
    for line in binding.read_text(encoding='utf-8').splitlines():
        m = re.match(r'^operation:\s*(.+?)\s*$', line)
        if m:
            return m.group(1).strip('\'"')
    return ''


def check_solver_bindings(ontology_dir: Path) -> list:
    """求解契约绑定的 Operation 被删除或改名后，发布会被求解门禁阻断。

    只提示断链（警告，不阻断提交）：契约由求解模型面板维护，模型不能改
    ontology/solver/，修复方式是恢复 Operation 原名，或请用户在面板里改绑定。
    """
    solver_dir = ontology_dir / SOLVER_SUBDIR
    if not solver_dir.is_dir():
        return []
    stems = {
        f.stem
        for subdir in ('operation', 'logic') if (ontology_dir / subdir).is_dir()
        for f in (ontology_dir / subdir).glob('*.md')
    }
    issues = []
    for binding in sorted(solver_dir.glob('*/binding.yaml')):
        operation = _solver_bound_operation(binding)
        if not operation or operation in stems:
            continue
        issues.append(Issue(
            file=str(binding.relative_to(ontology_dir)), severity='警告', category='求解契约',
            message=(f'求解契约「{binding.parent.name}」绑定的 Operation「{operation}」不存在，发布会被求解门禁阻断；'
                     '已绑定求解契约的 Operation 不要删除或改名，确需调整请恢复原名，或提示用户在求解模型面板中修改绑定。'),
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
    wiki_links = extract_wiki_links(raw)
    attribute_rows, input_rows, output_rows, action_rows = [], [], [], []
    if doc_type == 'object':
        attribute_rows = parse_markdown_table(sections.get('对象属性清单', ''))
    elif doc_type == 'operation':
        input_rows = parse_markdown_table(sections.get('输入', ''))
        output_rows = parse_markdown_table(sections.get('输出', ''))
        action_rows = parse_markdown_table(sections.get('与动作(action)的关联', ''))
    return OntologyDoc(
        path=path, doc_type=doc_type, raw=raw, sections=sections,
        wiki_links=wiki_links, attribute_rows=attribute_rows,
        input_rows=input_rows, output_rows=output_rows, action_rows=action_rows,
    )


# =============================================================================
# RULE CHECKER
# =============================================================================

OBJECT_REQUIRED_SECTIONS = ['对象描述', '对象别名', '英文名称', '对象属性清单']
# 数据来源：发布包「来源Sheet」与 object-data-mappings.json 的唯一声明口径，
# 缺它对象就绑不到物理表、数据问答查不到；纯概念对象填"无"即可，故列为建议而非必需
OBJECT_OPTIONAL_SECTIONS = ['数据来源', '关联对象']
OBJECT_REQUIRED_COLUMNS = ['中文名称', '英文名称', '描述', '主键', '类型']

OPERATION_REQUIRED_SECTIONS = ['业务描述', '与operation的关联', '与object的关联', '输入', '输出', '逻辑描述']
OPERATION_INPUT_REQUIRED_COLUMNS = ['参数名称', '描述']
OPERATION_OUTPUT_REQUIRED_COLUMNS = ['参数名称', '描述']
ACTION_REQUIRED_SECTIONS = ['动作描述']

# 范式升级（ADR-0004）：规则/指标为一等实体，元信息对齐 modeling 知识包模型
RULE_REQUIRED_SECTIONS = ['规则描述', '适用对象', '约束表达']
METRIC_REQUIRED_SECTIONS = ['指标描述', '计算公式', '数据来源']

_FIELD_ALIAS_SEPARATORS = re.compile(r'[,，、;；]')
_EMPTY_FIELD_ALIAS_MARKERS = {'无', '-', '—', '–', 'n/a'}


def _normalize_field_name(value: str) -> str:
    """与 modeling/src/workflow/field-alias.ts 保持一致的比较口径。"""
    text = unicodedata.normalize('NFC', (value or '').strip())
    return text.lower() if text.isascii() else text


def _field_alias_tokens(value: str) -> list[str]:
    text = (value or '').strip()
    if not text:
        return []
    return [
        token for token in (item.strip() for item in _FIELD_ALIAS_SEPARATORS.split(text))
        if token and unicodedata.normalize('NFKC', token).lower() not in _EMPTY_FIELD_ALIAS_MARKERS
    ]


def check_object_field_name_conflicts(doc: OntologyDoc) -> list:
    """检查对象内 key、中文名、别名是否指向多个不同字段。

    该规则与 modeling 的 findFieldNameConflicts 保持同一归一化口径，
    供质量检查提前发现评测启动时会阻断的问题。
    """
    if doc.doc_type != 'object' or not doc.attribute_rows:
        return []
    claims_by_name: dict[str, list[tuple[str, str, str]]] = {}
    for row in doc.attribute_rows:
        field_key = (row.get('英文名称') or '').strip()
        if not field_key:
            continue
        claims = [('key', field_key, field_key)]
        cn_name = (row.get('中文名称') or '').strip()
        if cn_name:
            claims.append(('中文名', cn_name, field_key))
        claims.extend(('别名', alias, field_key) for alias in _field_alias_tokens(row.get('属性别名', '')))
        for kind, value, owner in claims:
            normalized = _normalize_field_name(value)
            if normalized:
                claims_by_name.setdefault(normalized, []).append((kind, value, owner))

    issues = []
    for claims in claims_by_name.values():
        field_keys = sorted({claim[2] for claim in claims}, key=_normalize_field_name)
        if len(field_keys) <= 1:
            continue
        display = sorted(
            claims,
            key=lambda claim: ({'key': 0, '中文名': 1, '别名': 2}[claim[0]], _normalize_field_name(claim[1])),
        )[0]
        ordered_claims = sorted(
            claims,
            key=lambda claim: (
                _normalize_field_name(claim[2]),
                {'key': 0, '中文名': 1, '别名': 2}[claim[0]],
            ),
        )
        declaration_lines = []
        for kind, value, owner in ordered_claims:
            declaration_lines.append(f'字段“{owner}”的{kind}原值“{value}”')
        declarations = '，'.join(declaration_lines)
        issues.append(Issue(
            file=_rel(doc), severity='错误', category='字段名称冲突',
            message=(f'对象“{doc.path.stem}”的字段名称“{display[1]}”同时用于字段：{"、".join(field_keys)}；'
                     f'原始声明：{declarations}。请根据字段语义将冲突别名改为准确且唯一的名称，禁止删除别名。'),
            source='规则检查',
        ))
    return issues


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
            issues.append(Issue(
                file=_rel(doc), severity='错误', category='结构缺失',
                message=f'缺少必需章节「{section}」（{cn}模板要求）',
                source='规则检查',
            ))
    return issues


def check_entity_links(doc: OntologyDoc, all_docs: dict, cn: str) -> list:
    issues = []
    known = set(all_docs.keys())
    for link in doc.wiki_links:
        target = link.split('|')[0].split('#')[0].strip()
        if target and target not in known:
            issues.append(Issue(
                file=_rel(doc), severity='警告', category='链接失效',
                message=f'{cn}引用了不存在的文档 [[{target}]]',
                source='规则检查',
            ))
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


# 数据来源首行即表名（与后端 package_mapping 同一口径），写成说明句会绑不上表；
# 表名（Sheet 名）不含句读、反引号和 [[链接]]
_SOURCE_SENTENCE_RE = re.compile(r"[，。；：、`]|\[\[")
_SOURCE_NONE_RE = re.compile(r"无\s*[（(，,。：:；;]")
# 属性规则里带这些标记的属性本来就不在数据表里：operation 计算得到的、取自文档的常量
_NON_STORED_MARKERS = ('派生：', '派生:', '非库字段')


def _source_first_line(doc: OntologyDoc) -> str:
    body = re.sub(r"\{[^{}]*\}", " ", doc.sections.get('数据来源', ''), flags=re.S)
    for line in body.split('\n'):
        first = line.strip().lstrip('-*').strip()
        if first and not first.startswith(('|', '>', '{')):
            return first
    return ''


def _declares_none(first: str) -> bool:
    return first in ('无', '-', '—') or bool(_SOURCE_NONE_RE.match(first))


def _is_bare_name(first: str) -> bool:
    """首行是否只写了表名：整行一个 `表名`，或不含句读、反引号、[[链接]]。"""
    return bool(re.fullmatch(r"`[^`]+`", first)) or not _SOURCE_SENTENCE_RE.search(first)


def _declared_table(doc: OntologyDoc) -> str:
    """「# 数据来源」声明的表名；「无」、写成说明句（由数据来源格式检查提示）时返回空。"""
    first = _source_first_line(doc)
    quoted = re.fullmatch(r"`([^`]+)`", first)
    if quoted:
        return quoted.group(1).strip()
    if _declares_none(first):
        return ''
    return first if _is_bare_name(first) else ''


def check_object_data_source(doc: OntologyDoc) -> list:
    first = _source_first_line(doc)
    if not first:
        return []
    if _declares_none(first) or _is_bare_name(first):
        return []
    return [Issue(
        file=_rel(doc), severity='警告', category='数据来源格式',
        message=f'「# 数据来源」首行应只写表名（Sheet 名，逐字相同）或"无"，说明另起一行用 > 开头；'
                f'当前首行会被整行当作表名：{first[:40]}',
        source='规则检查',
    )]


def load_table_headers(ontology_dir: Path) -> dict:
    """工作区的 schema 资料与评测数据表表头 {表名: [列]}；读不到（如缺 openpyxl）时为空，不做列名对账。"""
    return _read_table_headers(ontology_dir.resolve().parent)


def _needs_column(row: dict, columns: dict) -> bool:
    """该属性应在表里有同名列：填了英文名、还没对上，且不是派生属性或非库字段。"""
    key = row.get('英文名称', '').strip()
    if key in ('', '无', '-') or key.lower() in columns:
        return False
    return not any(marker in row.get('属性规则', '') for marker in _NON_STORED_MARKERS)


def _source_declaration(doc: OntologyDoc, by_lower: dict) -> tuple:
    """(声明原文, 绑定到的表名)。首行写成说明句时与后端同口径：按句中反引号标出、
    且真实存在的第一个表名绑定；「无」或没有该段时声明原文为空。"""
    first = _source_first_line(doc)
    if not first or _declares_none(first):
        return '', ''
    declared = _declared_table(doc) or first
    table = by_lower.get(declared.lower(), '')
    if not table and not _is_bare_name(first):
        table = next((by_lower[n.strip().lower()] for n in re.findall(r"`([^`\n]+)`", first)
                      if n.strip().lower() in by_lower), '')
    return declared, table


def check_object_columns(doc: OntologyDoc, tables: dict) -> list:
    """存储属性的英文名称必须是所声明数据表的真实列名（与评测导入、发布包 mapping 同一口径）。

    声明的表不在现有数据表里是错误（发布时该对象会被阻断）；项目里还没有任何数据表时，
    声明无从核对，只提示——多半是凭空写的表名，发布包不会绑定它。
    """
    declared, table = _source_declaration(doc, {name.lower(): name for name in tables})
    if not declared:
        return []
    if not tables:
        return [Issue(
            file=_rel(doc), severity='警告', category='列名对账',
            message=f'「# 数据来源」声明了表「{declared[:40]}」，但项目里还没有可核对的数据表'
                    '（schema 表格资料或评测数据源的上传表），发布包不会绑定该对象。'
                    '表名来自数据表资料时，把该资料的用途标为 schema；不确定时首行填「无」，不要编造表名',
            source='规则检查',
        )]
    if not table:
        return [Issue(
            file=_rel(doc), severity='错误', category='列名对账',
            message=f'「# 数据来源」声明的表「{declared[:40]}」不在 schema 资料或评测数据表里'
                    f'（现有表：{"、".join(sorted(tables)[:10])}{"…" if len(tables) > 10 else ""}），'
                    '发布时会被阻断：首行改为现有表名（逐字相同），不落库的对象填「无」',
            source='规则检查',
        )]
    if not doc.attribute_rows:
        return []
    columns = {c.lower(): c for c in tables[table]}
    issues, missing = [], []
    if not any(row.get('英文名称', '').strip().lower() in columns for row in doc.attribute_rows):
        issues.append(Issue(
            file=_rel(doc), severity='错误', category='列名对账',
            message=f'表「{table}」没有一列能对上属性英文名称，发布时会被阻断：'
                    '核对「# 数据来源」是否选错了表，或把从库里读取的属性英文名称改为真实列名',
            source='规则检查',
        ))
    for row in doc.attribute_rows:
        if not _needs_column(row, columns):
            continue
        key = row.get('英文名称', '').strip()
        aliases = [a.strip() for a in re.split(r'[,，、]', row.get('属性别名', '')) if a.strip()]
        real = next((columns[a.lower()] for a in aliases if a.lower() in columns), '')
        if real:
            issues.append(Issue(
                file=_rel(doc), severity='错误', category='列名对账',
                message=f'属性「{row.get("中文名称") or key}」的英文名称 {key} 不是表「{table}」的列名，'
                        f'属性别名里的 {real} 才是：英文名称改为 {real}（旧名可留作别名）；'
                        f'若 {real} 其实不是同一字段，从属性别名里删掉它',
                source='规则检查',
            ))
        else:
            missing.append(key)
    if missing:
        issues.append(Issue(
            file=_rel(doc), severity='警告', category='列名对账',
            message=f'{len(missing)} 个属性在表「{table}」里没有同名列：{"、".join(missing[:8])}'
                    f'{"…" if len(missing) > 8 else ""}。从库里读取的属性改为真实列名；'
                    '计算得到的按「派生：由 [[operation名]] 写入」标注；取自文档的常量在属性规则里注明「非库字段」',
            source='规则检查',
        ))
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
              message=f'Wiki 链接 [[{link}]] 无对应文件', source='规则检查')
        for link in doc.wiki_links if link not in all_docs
    ]


# --- 关联对象的关联字段（与后端 package_mapping._relation_join、评测图导入同一口径）---

# 图关系名与属性英文名的字符集：从括注里取字段候选，再拿两端属性清单裁决
_REL_IDENTIFIER_RE = re.compile(r'[A-Za-z_一-鿿][A-Za-z0-9_一-鿿]*')
_REL_NOTE_RE = re.compile(r'[（(]([^()（）]*)[）)]')


def _relation_parts(value: str):
    """关系行 → (源, 关系名, 目标, 括注)；解析不出返回 None。

    括注里的箭头是关联字段声明，不是条目分隔符，所以先把括号段挖出来再按箭头切分。
    """
    notes = []

    def stash(match):
        notes.append(match.group(1).strip())
        return f'\x00{len(notes) - 1}\x00'

    parts = [p.strip() for p in re.split(r'→|->', _REL_NOTE_RE.sub(stash, value))]
    if len(parts) != 3:
        return None

    def restore(text: str) -> str:
        return re.sub(r'\x00(\d+)\x00', lambda m: f'（{notes[int(m.group(1))]}）', text)

    middle = re.fullmatch(r'([^\x00]*)\x00(\d+)\x00', parts[1])
    name = middle.group(1).strip() if middle else restore(parts[1])
    note = notes[int(middle.group(2))] if middle else ''
    return _relation_node(restore(parts[0])), name, _relation_node(restore(parts[2])), note


def _relation_node(value: str) -> str:
    link = re.fullmatch(r'\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]', value.strip())
    return (link.group(1) if link else value).strip()


def _field_keys(doc: OntologyDoc) -> set:
    """属性英文名清单：关联字段的判据（与发布包 mapping 的 field key 同一列）。"""
    keys = {row.get('英文名称', '').strip() for row in doc.attribute_rows}
    return {key for key in keys if key not in ('', '无', '-')}


def _single_primary_key(doc: OntologyDoc) -> str:
    """单字段主键的英文名；没标主键或是复合主键时为空——复合主键的一部分不是完整连接条件。"""
    keys = [row.get('英文名称', '').strip() for row in doc.attribute_rows
            if row.get('主键', '').strip() == '是']
    return keys[0] if len(keys) == 1 and keys[0] not in ('', '无', '-') else ''


def _parse_join_note(note: str, source_fields: set, target_fields: set) -> tuple:
    """括注 → (源对象字段, 目标对象字段, 错误说明)。判据是两端属性清单，不是括注的措辞。"""
    if not note:
        return '', '', ''
    if '→' in note or '->' in note:
        sides = re.split(r'→|->', note)
        names = [_REL_IDENTIFIER_RE.findall(side) for side in sides]
        if len(sides) != 2 or any(len(found) != 1 for found in names):
            return '', '', '的关联字段括注不明确，应写成「关系名（源对象字段 → 目标对象字段）」'
        source_join, target_join = names[0][0], names[1][0]
        absent = [name for name, fields in ((source_join, source_fields), (target_join, target_fields))
                  if fields and name not in fields]
        if absent:
            return '', '', (f'声明的关联字段 {source_join} → {target_join} 里，'
                            f'「{"、".join(absent)}」不在对应对象属性清单的「英文名称」列里')
        return source_join, target_join, ''
    # 没有箭头：只认两端清单都有的那一个标识符，取不到就是一句纯说明
    shared = [name for name in _REL_IDENTIFIER_RE.findall(note)
              if name in source_fields and name in target_fields]
    if len(shared) > 1:
        return '', '', '的关联字段括注不明确，应写成「关系名（源对象字段 → 目标对象字段）」'
    return (shared[0], shared[0], '') if shared else ('', '', '')


def _joinable_by_same_name(source_doc: OntologyDoc, target_doc: OntologyDoc) -> bool:
    """未声明关联字段时后端的兜底推断：一端的单字段主键在另一端有同名属性，且只命中一种。"""
    source_fields, target_fields = _field_keys(source_doc), _field_keys(target_doc)
    candidates = set()
    for key, left, right in (
        (_single_primary_key(target_doc), target_fields, source_fields),
        (_single_primary_key(source_doc), source_fields, target_fields),
    ):
        if key and key in left and key in right:
            candidates.add(key)
    return len(candidates) == 1


def check_object_relation_joins(doc: OntologyDoc, all_docs: dict, tables: dict) -> list:
    """「# 关联对象」的关联字段：跨表关系必须声明，声明的字段必须真实存在。

    两端落在同一张数据表时按同一数据行关联，不需要关联字段。跨表时后端只会去找两端同名的
    单字段主键，找不到就整条关系丢弃——图模式的数据评测据此报「找不到关联字段」并拦下本轮
    评测。这里按同一规则预判，把问题停在建模阶段，而不是等评测跑到一半。
    """
    section = doc.sections.get('关联对象', '')
    if not section:
        return []
    by_lower = {name.lower(): name for name in tables}
    stem = doc.path.stem
    issues = []
    for line in section.split('\n'):
        value = line.strip().lstrip('-*').strip()
        if not value or value.startswith(('>', '|', '{')) or value in ('无', '-', '—'):
            continue
        parsed = _relation_parts(value)
        if not parsed:
            continue  # 关系行格式问题由发布与评测侧报，这里只管关联字段
        source, name, target, note = parsed
        source = stem if source in ('当前对象', stem) else source
        target = stem if target in ('当前对象', stem) else target
        other = target if source == stem else source if target == stem else ''
        other_doc = all_docs.get(other)
        if not other or other_doc is None or other_doc.doc_type != 'object':
            continue  # 两端不含当前对象、链接失效：另有检查负责
        source_doc = doc if source == stem else other_doc
        target_doc = doc if target == stem else other_doc
        label = f'关系「{source} -{name}→ {target}」'
        source_join, target_join, error = _parse_join_note(
            note, _field_keys(source_doc), _field_keys(target_doc))
        if error:
            issues.append(Issue(file=_rel(doc), severity='警告', category='关联字段',
                                message=label + error, source='规则检查'))
            continue
        if source_join and target_join:
            continue
        source_table = _source_declaration(source_doc, by_lower)[0]
        target_table = _source_declaration(target_doc, by_lower)[0]
        if not source_table or not target_table or source_table == target_table:
            continue  # 同表按同一数据行关联；没声明数据来源由列名对账报
        if _joinable_by_same_name(source_doc, target_doc):
            continue
        issues.append(Issue(
            file=_rel(doc), severity='警告', category='关联字段',
            message=f'{label}跨表（{source_table} / {target_table}）且未声明关联字段，'
                    f'两端也没有同名的单字段主键可依据：图模式的数据评测会报「找不到关联字段」'
                    f'并拦下评测，发布包里这条关系也连不上。请补成「{name}（源对象字段 → '
                    f'目标对象字段）」，字段用两端属性清单的「英文名称」',
            source='规则检查'))
    return issues


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
              message=f'Wiki 链接 [[{link}]] 无对应文件', source='规则检查')
        for link in doc.wiki_links if link not in all_docs
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
        if other_doc.doc_type == 'operation' and action_stem in other_doc.wiki_links:
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


def check_scripts(ontology_dir: Path) -> list:
    """Validate executable scripts before a workspace can be published."""
    scripts_dir = ontology_dir / "scripts"
    issues = []
    if not scripts_dir.is_dir():
        return issues
    for path in sorted(scripts_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        rel = path.relative_to(ontology_dir).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            issues.append(Issue(str(rel), "错误", "脚本契约", f"Python 脚本无法解析：{exc}", "规则检查"))
            continue
        calls = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
            ):
                calls.append(node)
        executable = "__main__" in source
        if executable and ("argparse" not in source or not any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "ArgumentParser" for node in ast.walk(tree)
        )):
            issues.append(Issue(str(rel), "错误", "脚本契约", "可执行脚本必须使用 argparse.ArgumentParser", "规则检查"))
        doc = ast.get_docstring(tree) or ""
        if "输入参数" not in doc or "输出" not in doc:
            issues.append(Issue(str(rel), "错误", "脚本契约", "头部注释必须包含输入参数和输出字段", "规则检查"))
        for call in calls:
            flags = []
            for arg in call.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    flags.append(arg.value)
            if not any(flag.startswith("--") for flag in flags):
                issues.append(Issue(str(rel), "错误", "脚本契约", "argparse 参数必须声明长选项 flag", "规则检查"))
        if executable and "json.dumps" not in source:
            issues.append(Issue(str(rel), "错误", "脚本契约", "CLI stdout 必须输出单个 JSON 结果", "规则检查"))
        # 与知识包导出同一实现：头部「输入参数」与 argparse 的名称、类型、必填须一一对应
        try:
            parse_script_contract(source)
        except ScriptContractError as exc:
            issues.append(Issue(str(rel), "错误", "脚本契约", str(exc), "规则检查"))
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
               include_knowledge: bool = False, include_scripts: bool = False) -> list:
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
    issues += check_unpackaged_knowledge(ontology_dir)
    issues += check_solver_bindings(ontology_dir)
    term_map = build_term_map(all_docs)
    tables = load_table_headers(ontology_dir)

    print("Running rule checks...", file=sys.stderr)
    for doc in check_docs.values():
        issues += check_todo_placeholders(doc)
        if doc.doc_type == 'object':
            issues += check_object_structure(doc)
            issues += check_object_data_source(doc)
            issues += check_object_columns(doc, tables)
            issues += check_object_table(doc)
            issues += check_object_field_name_conflicts(doc)
            issues += check_object_primary_key(doc)
            issues += check_object_links(doc, all_docs)
            issues += check_object_relation_joins(doc, all_docs, tables)
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

    if include_scripts:
        issues += check_scripts(ontology_dir)

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
    parser.add_argument('--include-scripts', action='store_true',
                        help='Validate ontology/scripts Python syntax and argparse contracts')
    args = parser.parse_args()

    ontology_dir = Path(args.ontology_dir)
    if not ontology_dir.exists():
        print(f"Error: not found: {ontology_dir}", file=sys.stderr)
        sys.exit(1)

    issues = run_checks(ontology_dir, target_files=args.files, include_mock=args.include_mock,
                        include_knowledge=args.include_knowledge, include_scripts=args.include_scripts)

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
