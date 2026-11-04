#!/usr/bin/env python3
"""字段语义核验：属性类型的合法性与取值证据、标识混合、跨对象字段等价关联。

纯规则、只用标准库，不调用模型，不执行导入。ontology_checker（规则检查）、
table_profile（--profile 取值画像）和抽取命令（确认前展示）共用这里的口径。

用法（工作区根目录执行）：
  python3 .claude/scripts/field_semantics.py types [--files 对象名 ...]
      逐字段给出「声明类型 / 数据推断 / 结论 / 依据」，并分开报告字段覆盖与导入验证状态。
      schema 字段：ok 有登记的数据表；no_schema 项目里没有 schema 表格资料。
      退出码：0 无冲突；1 有冲突或非法类型；2 无法核验（登记的数据表缺失、无法解析、缺依赖，
      或还没有文本版——missing_text 列出这些资料）。
  python3 .claude/scripts/field_semantics.py joins [--objects 对象名 ...]
      列出已声明的关联字段对和各对象的标识字段，供操作抽取引用；--objects 只列涉及这些对象的部分。
      退出码 0。

输出是单个 JSON 对象，只含字段名、类型、计数与结论，不含任何单元格原值。
最坏输出量：types 最多 80 条明细加覆盖摘要（约 40KB）；joins 最多 300 条关联加 100 个对象、
每个对象 30 个标识字段（约 100KB，抽取时应带 --objects 收窄）。超出部分以 omitted / truncated 标注。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import decimal
import json
import logging
import math
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

try:
    import onto_metamodel as mm
except ModuleNotFoundError:  # 后端按文件加载本脚本时，同目录不在 sys.path 上
    sys.path.append(str(Path(__file__).resolve().parent))
    import onto_metamodel as mm

# ---------------------------------------------------------------------------
# 类型词表
# ---------------------------------------------------------------------------

CANONICAL_TYPES = ('字符串', '整数', '小数', '布尔', '日期', '枚举', '对象引用')
# 存量文档常写的「数字/数值」不区分整数与小数，照认，按可含小数核对
LEGACY_TYPES = ('数字',)
TYPE_HINT = '/'.join(CANONICAL_TYPES)
NUMERIC_TYPES = frozenset({'整数', '小数', '数字'})

TYPE_ALIASES = {
    '字符串': ('字符串', '文本', '字符', '字符型', 'string', 'str', 'text', 'varchar', 'char', 'nvarchar'),
    '整数': ('整数', '整型', '整数型', '长整型', 'int', 'integer', 'bigint', 'smallint', 'tinyint', 'long'),
    '小数': ('小数', '浮点', '浮点型', '浮点数', '双精度', 'float', 'double', 'decimal', 'real', 'numeric'),
    '数字': ('数字', '数值', '数值型', 'number'),
    '布尔': ('布尔', '布尔型', '布尔值', 'bool', 'boolean'),
    '日期': ('日期', '日期时间', '时间', '时间戳', 'date', 'datetime', 'timestamp', 'time'),
    '枚举': ('枚举', 'enum'),
    '对象引用': ('对象引用', '引用', 'reference', 'ref'),
}
_ALIAS_INDEX = {alias.lower(): canonical for canonical, aliases in TYPE_ALIASES.items() for alias in aliases}
_TYPE_PLACEHOLDERS = {'', '无', '-', '—', '待填写', '待定', '待确认', '未知', 'todo', 'tbd', 'n/a'}


def parse_declared_type(cell) -> tuple[str, str]:
    """类型单元格 → (规范类型, 问题)。问题为 '' / 'empty' / 'multiple' / 'unknown'。

    括注、[[链接]]、冒号后的说明不参与判断；「字符串/整型/布尔」这类照抄的候选列表是 multiple。
    """
    text = '' if cell is None else str(cell).strip()
    if re.search(r'\{[^{}]*\}', text):
        return '', 'empty'
    text = re.sub(r'\[\[[^\]]*\]\]|（[^）]*）|\([^)]*\)|<[^>]*>|`', ' ', text)
    text = re.split(r'[:：]', text, maxsplit=1)[0]
    text = re.sub(r'→|->', ' ', text).strip()
    if text.lower() in _TYPE_PLACEHOLDERS:
        return '', 'empty'
    tokens = [t.strip() for t in re.split(r'[/|、,，;；]|或|\bor\b', text, flags=re.I) if t.strip()]
    if not tokens:
        return '', 'empty'
    found = [_ALIAS_INDEX.get(t.lower()) for t in tokens]
    if len(tokens) > 1:
        if None in found or len(set(found)) > 1:
            return '', 'multiple'
        return found[0], ''
    if found[0]:
        return found[0], ''
    canonical = _ALIAS_INDEX.get(tokens[0].split()[0].lower())
    return (canonical, '') if canonical else ('', 'unknown')


# ---------------------------------------------------------------------------
# 单元格取值分类与列画像（只留计数，不留原值）
# ---------------------------------------------------------------------------

PROFILE_ROW_LIMIT = 2000
LONG_INTEGER_DIGITS = 15  # 超过即超出双精度可精确表示的位数，按数值导入会丢精度

_ZERO_PADDED = re.compile(r'^-?0\d+(?:\.\d+)?$')
_INTEGER = re.compile(r'^-?\d+$')
_INTEGRAL_DECIMAL = re.compile(r'^-?\d+\.0+$')
_DECIMAL = re.compile(r'^-?\d+\.\d+$|^-?\d+(?:\.\d+)?[eE][+-]?\d+$')
_BOOLEAN_WORDS = {'true': 'true', 'false': 'false', '是': 'true', '否': 'false', 'yes': 'true', 'no': 'false'}
_BOOLEAN_SHORT = frozenset({'t', 'f', 'y', 'n'})
_DATE_TEXT = re.compile(r'^\d{4}\s*[-/.年]\s*\d{1,2}(?!\d)|^\d{1,2}[:：]\d{2}(?:[:：]\d{2})?$')
# 与 modeling parseExcel 的注释行判定同一口径
_NOTE_NUMERIC = re.compile(r'^-?\d+(\.\d+)?$')
_NOTE_CJK = re.compile(r'[一-龥]')


def classify_value(value) -> tuple[str, str]:
    """单元格 → (类别, 规范形)。类别：empty / integer / decimal / zero_padded / boolean /
    boolean_short / date / text。规范形只用于同值异形比对，不对外输出。"""
    if value is None:
        return 'empty', ''
    if isinstance(value, bool):
        return 'boolean', 'true' if value else 'false'
    if isinstance(value, int):
        return 'integer', str(value)
    if isinstance(value, (float, decimal.Decimal)):
        if isinstance(value, float) and not math.isfinite(value):
            return 'text', str(value)
        if isinstance(value, decimal.Decimal) and not value.is_finite():
            return 'text', str(value)
        if value == int(value):
            return 'integer', str(int(value))
        return 'decimal', str(value)
    if isinstance(value, (_dt.date, _dt.time)):
        return 'date', value.isoformat()
    text = str(value).strip()
    if not text:
        return 'empty', ''
    if _ZERO_PADDED.match(text):
        sign = '-' if text.startswith('-') else ''
        return 'zero_padded', sign + (text.lstrip('-').lstrip('0') or '0')
    if _INTEGER.match(text):
        return 'integer', text
    if _INTEGRAL_DECIMAL.match(text):
        return 'integer', text.split('.')[0]
    if _DECIMAL.match(text):
        return 'decimal', text
    lower = text.lower()
    if lower in _BOOLEAN_WORDS:
        return 'boolean', _BOOLEAN_WORDS[lower]
    if lower in _BOOLEAN_SHORT:
        return 'boolean_short', lower
    if _DATE_TEXT.match(text):
        return 'date', text
    return 'text', text


class _ColumnStats:
    def __init__(self):
        self.filled = 0
        self.empty = 0
        self.kinds: Counter = Counter()
        self.long_integers = 0
        self.integers_01 = 0
        self.short_booleans: set = set()
        self.forms: dict = {}  # 规范形大写 → 出现过的原样写法

    def add(self, value) -> None:
        kind, normalized = classify_value(value)
        if kind == 'empty':
            self.empty += 1
            return
        self.filled += 1
        self.kinds[kind] += 1
        if kind == 'integer':
            if len(normalized.lstrip('-')) > LONG_INTEGER_DIGITS:
                self.long_integers += 1
            if normalized in ('0', '1'):
                self.integers_01 += 1
        elif kind == 'boolean_short':
            self.short_booleans.add(normalized)
        raw = value.strip() if isinstance(value, str) else normalized
        self.forms.setdefault(normalized.upper(), set()).add(raw)

    def summary(self, source_type: str = '') -> dict:
        column = {
            'filled': self.filled,
            'empty': self.empty,
            'kinds': dict(sorted(self.kinds.items())),
            'long_integers': self.long_integers,
            'short_booleans': sorted(self.short_booleans),
            'binary01': bool(self.kinds.get('integer')) and self.integers_01 == self.kinds['integer'],
            'collisions': sum(1 for forms in self.forms.values() if len(forms) > 1),
            'source_type': source_type,
        }
        column['inferred'] = infer_type(column)
        return column


def infer_type(column: dict) -> str:
    """列画像 → 数据推断类型；没有取值时为空（未验证，不能当作字符串的证据）。"""
    kinds = set(column.get('kinds') or {})
    if not column.get('filled') or not kinds:
        return ''
    if kinds <= {'boolean', 'boolean_short'}:
        return '布尔'
    if kinds == {'date'}:
        return '日期'
    if 'text' in kinds:
        return '字符串'
    if kinds == {'integer'}:
        return '字符串' if column.get('long_integers') else '整数'
    if kinds <= {'integer', 'zero_padded'}:
        return '字符串'
    if kinds <= {'integer', 'decimal'}:
        return '小数'
    return '混合'


def _cell_text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _filled(value) -> bool:
    return value is not None and str(value).strip() != ''


def _note_class(text: str) -> str:
    if not text:
        return 'empty'
    if _NOTE_NUMERIC.match(text):
        return 'num'
    return 'cjk' if _NOTE_CJK.search(text) else 'code'


def _raw_length(row) -> int:
    filled = [i for i, v in enumerate(row) if _filled(v)]
    return filled[-1] + 1 if filled else 0


def is_note_row(header: list, second, third) -> bool:
    """表头下一行是否注释行：与 modeling parseExcel 同一判定，命中的行不算数据。"""
    if _raw_length(second) != len(header):
        return False
    texts = ['' if v is None else str(v).strip() for v in list(second)[:len(header)]]
    heavy = sum(1 for v in texts if v and not _NOTE_NUMERIC.match(v)) >= len(header) / 2
    if not heavy or not any(re.search(r'[一-龥：:（(]', v) for v in texts):
        return False
    third = list(third)
    similar = sum(
        1 for c in range(len(header))
        if _note_class(texts[c]) == _note_class(_cell_text(third[c]) if c < len(third) else '')
    ) / max(len(header), 1)
    return similar < 0.6


def _type_row(row, positions) -> dict:
    """表头下的类型说明行（如「字符串 | 整数 | 日期」）→ {列位置: 规范类型}；不是类型行时为空。"""
    found, filled = {}, 0
    for index, _ in positions:
        cell = row[index] if index < len(row) else None
        if not _filled(cell):
            continue
        filled += 1
        canonical, problem = parse_declared_type(cell)
        if canonical and not problem:
            found[index] = canonical
    if len(positions) > 1 and len(found) >= 2 and len(found) >= 0.6 * filled:
        return found
    return {}


def profile_table(rows, limit: int = PROFILE_ROW_LIMIT) -> dict | None:
    """表格行（首个非空行为表头）→ 取值画像；最多读 limit 行数据，只保留计数。"""
    iterator = iter(rows)
    header = None
    for raw in iterator:
        row = list(raw or ())
        if any(map(_filled, row)):
            header = [_cell_text(v) for v in row]
            break
    if header is None:
        return None
    while header and not header[-1]:
        header.pop()
    positions, seen = [], set()
    for index, name in enumerate(header):
        if name and name not in seen:  # 重名列以先出现的为准，与表头读取一致
            seen.add(name)
            positions.append((index, name))
    if not positions:
        return None
    data, truncated = [], False
    for raw in iterator:
        row = list(raw or ())
        if not any(map(_filled, row)):
            continue
        if len(data) >= limit + 2:  # 多留两行给注释行与类型说明行
            truncated = True
            break
        data.append(row)
    skipped = []
    # 类型说明行先判：紧跟表头的中文类型行同样满足注释行特征，先判注释会丢掉类型证据
    source_types = _type_row(data[0], positions) if data else {}
    if not source_types and len(data) >= 2 and is_note_row(header, data[0], data[1]):
        data = data[1:]
        skipped.append('note')
        source_types = _type_row(data[0], positions) if data else {}
    if source_types:
        data = data[1:]
        skipped.append('type')
    if len(data) > limit:
        data, truncated = data[:limit], True
    stats = {name: _ColumnStats() for _, name in positions}
    for row in data:
        for index, name in positions:
            stats[name].add(row[index] if index < len(row) else None)
    return {
        'headers': [name for _, name in positions],
        'rows': len(data),
        'truncated': truncated,
        'skipped_rows': skipped,
        'columns': {name: stats[name].summary(source_types.get(index, ''))
                    for index, name in positions},
    }


# ---------------------------------------------------------------------------
# 类型验收：声明类型 × 取值证据
# ---------------------------------------------------------------------------

VERDICT_SEVERITY = {'非法类型': '错误', '冲突': '错误', '待确认': '警告'}
# 属性规则里的业务确认标记：数值形态的取值确属文本 / 标识混合的归一化口径已确认
TEXT_KEEP_MARKER = '文本保留'
MIXED_ID_MARKER = '混合标识'

_IDENTITY_EN_RE = re.compile(r'(?:^|_)(?:id|code|no|key|uuid|guid|sn)$', re.I)
_IDENTITY_CAMEL_RE = re.compile(r'[a-z0-9](?:Id|ID|Code|No|Key)$')
_IDENTITY_CN_RE = re.compile(r'(?:编号|编码|代码|标识|代号|号|码|ID|Id|id)$')
_KIND_LABELS = {'integer': '整数', 'decimal': '小数', 'zero_padded': '前导零数字', 'boolean': '布尔词',
                'boolean_short': '单字母布尔', 'date': '日期', 'text': '文本'}


def is_identity_field(row: dict) -> bool:
    """主键、对象引用，或按命名是编号 / 编码类的字段：取值是标识，不参与数值运算。"""
    if (row.get('主键') or '').strip() == '是':
        return True
    if parse_declared_type(row.get('类型', ''))[0] == '对象引用':
        return True
    english = (row.get('英文名称') or '').strip()
    chinese = (row.get('中文名称') or '').strip()
    return bool(_IDENTITY_EN_RE.search(english) or _IDENTITY_CAMEL_RE.search(english)
                or _IDENTITY_CN_RE.search(chinese))


def _kinds_text(kinds: dict) -> str:
    return '、'.join(f'{_KIND_LABELS.get(k, k)} {n}' for k, n in sorted(kinds.items()) if n)


def _mentions_token(rule: str, token: str) -> bool:
    return bool(re.search(rf'(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])', rule, re.I))


def _assess_text(kinds, column, rule, identity, evidence):
    if not set(kinds) <= {'integer', 'decimal'}:
        return '一致', evidence
    if column.get('long_integers'):
        return '一致', f'{evidence}；{column["long_integers"]} 个超过 {LONG_INTEGER_DIGITS} 位，按文本保留精度'
    if TEXT_KEEP_MARKER in rule:
        return '一致', evidence + '；属性规则已注明文本保留'
    if identity:
        return '一致', evidence + '；标识字段按文本保存'
    if column.get('source_type') == '字符串':
        return '一致', evidence + '；数据表类型说明行为字符串'
    suggestion = '整数' if set(kinds) == {'integer'} else '小数'
    return '待确认', (f'{evidence}；取值全是数值，声明为字符串却没有依据：改为{suggestion}，'
                      f'确属文本时在属性规则注明「{TEXT_KEEP_MARKER}：原因」')


def _assess_numeric(canonical, kinds, column, evidence):
    allowed = {'integer'} if canonical == '整数' else {'integer', 'decimal'}
    bad = {k: n for k, n in kinds.items() if k not in allowed}
    if bad:
        hints = []
        if 'zero_padded' in bad:
            hints.append('有前导零取值，按数值导入会丢掉前导零')
        if 'decimal' in bad:
            hints.append('有小数取值')
        if set(bad) - {'zero_padded', 'decimal'}:
            hints.append('有非数值取值')
        return '冲突', f'{evidence}；声明为{canonical}，但{"、".join(hints)}（{_kinds_text(bad)}）'
    if column.get('long_integers'):
        return '待确认', (f'{evidence}；{column["long_integers"]} 个超过 {LONG_INTEGER_DIGITS} 位，'
                          '按数值导入会丢精度：确认是标识时改为字符串')
    if column.get('source_type') == '字符串':
        return '待确认', evidence + f'；数据表类型说明行为字符串，与声明的{canonical}不一致'
    if canonical == '数字':
        return '一致', evidence + '；「数字」是存量写法，可明确为整数或小数'
    return '一致', evidence


def _assess_boolean(kinds, column, rule, evidence):
    bad = {k: n for k, n in kinds.items() if k in ('decimal', 'zero_padded', 'date', 'text')}
    integers = kinds.get('integer', 0)
    if integers and not column.get('binary01'):
        bad['integer'] = integers
    if bad:
        return '冲突', f'{evidence}；声明为布尔，但有非布尔取值（{_kinds_text(bad)}）'
    tokens = list(column.get('short_booleans') or []) + (['0', '1'] if integers else [])
    if not tokens:
        return '一致', evidence
    if re.search(r'转换|映射', rule) or all(_mentions_token(rule, t) for t in tokens):
        return '一致', evidence + '；属性规则已写明取值含义'
    return '待确认', (f'{evidence}；取值是 {" / ".join(tokens)}，导入时不会自动当作布尔：'
                      '在属性规则写明取值含义（如「取值：1=是 / 0=否」）或转换方式')


def _assess_date(kinds, rule, evidence):
    bad = {k: n for k, n in kinds.items() if k in ('boolean', 'boolean_short', 'text')}
    if bad:
        return '冲突', f'{evidence}；声明为日期，但有非日期取值（{_kinds_text(bad)}）'
    numeric = {k: n for k, n in kinds.items() if k in ('integer', 'decimal', 'zero_padded')}
    if numeric and not re.search(r'格式|转换', rule):
        return '待确认', (f'{evidence}；有纯数字取值（可能是表格日期序列号或 yyyymmdd），'
                          '在属性规则写明日期格式或转换方式')
    return '一致', evidence


def assess_type(type_cell, rule: str, identity: bool, column: dict | None) -> tuple[str, str, str]:
    """(规范类型, 结论, 依据)。结论：一致 / 待确认 / 冲突 / 非法类型 / 未验证。

    先判类型写法是否合法，再用列画像核对；column 为 None 时返回「未验证」且依据留空，
    由调用方说明为什么没有取值（未绑定表、列缺失、表未读取）。
    """
    canonical, problem = parse_declared_type(type_cell)
    raw = '' if type_cell is None else str(type_cell).strip()
    if problem == 'empty':
        return '', '非法类型', f'类型未填写，应为 {TYPE_HINT} 之一'
    if problem == 'multiple':
        return '', '非法类型', f'类型「{raw[:30]}」含多个候选，只能填一个：{TYPE_HINT}'
    if problem == 'unknown':
        return '', '待确认', f'类型「{raw[:30]}」不在类型词表里，改为 {TYPE_HINT} 之一'
    if column is None:
        return canonical, '未验证', ''
    if not column.get('filled'):
        return canonical, '未验证', f'整列为空（{column.get("empty", 0)} 行），取值无从核对，有数据后再验收'
    kinds = dict(column.get('kinds') or {})
    rule = rule or ''
    evidence = f'取值 {column["filled"]} 个：{_kinds_text(kinds)}'
    if canonical == '字符串':
        verdict, reason = _assess_text(kinds, column, rule, identity, evidence)
    elif canonical in NUMERIC_TYPES:
        verdict, reason = _assess_numeric(canonical, kinds, column, evidence)
    elif canonical == '布尔':
        verdict, reason = _assess_boolean(kinds, column, rule, evidence)
    elif canonical == '日期':
        verdict, reason = _assess_date(kinds, rule, evidence)
    else:  # 枚举 / 对象引用：取值集合由枚举与关联检查负责
        verdict, reason = '一致', evidence
    return canonical, verdict, reason


def identity_mixing(column: dict | None, rule: str = '') -> str:
    """标识列里数字与文本混用、前导零与无前导零并存、同值异形：归一化口径需要业务确认。"""
    if not column or not column.get('filled') or MIXED_ID_MARKER in (rule or ''):
        return ''
    kinds = column.get('kinds') or {}
    findings = []
    numeric = kinds.get('integer', 0) + kinds.get('decimal', 0)
    if numeric and kinds.get('text'):
        findings.append(f'数字 {numeric} 个与文本 {kinds["text"]} 个混用')
    if kinds.get('zero_padded') and kinds.get('integer'):
        findings.append(f'有前导零 {kinds["zero_padded"]} 个与无前导零 {kinds["integer"]} 个并存')
    if column.get('collisions'):
        findings.append(f'{column["collisions"]} 组取值只差大小写或前导零')
    return '；'.join(findings)


# ---------------------------------------------------------------------------
# 跨对象字段等价：把两个不同名的标识字段当作同一个值来核对 / 关联
# ---------------------------------------------------------------------------

_ASCII_IDENT_RE = re.compile(r'(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*(?![A-Za-z0-9_])')
_ACCESSOR_RE = re.compile(r'\s*(?:\.|(?:中|里)?的)\s*`?')
_CLAUSE_SEPARATOR_RE = re.compile(r'[，,。；;！!？?]')
_EQUAL_MID_RE = re.compile(
    r'(?:是否|必须|应当|应|须|需|不)?\s*(?:==|=|!=|<>|≠|≡|↔|⇔|<->|<=>|等于|不等于|等同于|即为|就是|'
    r'对应(?:于|到)?|映射(?:到|为|至)|相当于)')
_CONNECTOR_MID_RE = re.compile(r'(?:无法|不能|未能|需|须|应|可)?\s*(?:与|和|跟|同|及|对|对比|vs|VS|、|/)')
_HEAD_VERB_RE = re.compile(r'(?:核对|比对|核验|校验|比较|对比|匹配|对齐|对应)\s*[:：]?\s*$')
_TAIL_VERB_RE = re.compile(
    r'[^\[\]]{0,8}?(?:一致|相同|相等|等同|等价|匹配|核对|核验|比对|校验|对应|对齐|吻合|对上|关联|映射)')
_PENDING_RE = re.compile(r'待确认|待业务确认|需业务确认|待核实|TODO', re.I)
_QUOTES = ' \t`\'"“”‘’「」『』'
_EMPTY_ALIASES = {'无', '-', '—', '–', 'n/a'}


def _norm(value: str) -> str:
    """与 ontology_checker._normalize_field_name 同一比较口径。"""
    text = unicodedata.normalize('NFC', (value or '').strip())
    return text.lower() if text.isascii() else text


def _alias_tokens(value: str) -> list:
    tokens = (t.strip() for t in re.split(r'[,，、;；]', value or ''))
    return [t for t in tokens if t and t.lower() not in _EMPTY_ALIASES]


@dataclass(frozen=True)
class FieldRef:
    start: int
    end: int
    candidates: frozenset  # {(对象 stem, 英文名称)}；不带限定的字段名可能同时属于多个对象


@dataclass
class FieldIndex:
    names: dict      # stem → {规范化名: 英文名称}，含中文名与属性别名
    ordered: dict    # stem → [(规范化名, 英文名称)]，长名在前
    identity: dict   # stem → {标识字段英文名小写}
    qualifiers: dict  # 规范化限定词（对象名、对象英文名、表名）→ stem
    bare: dict       # 不带限定也能认出的字段名 → {(stem, 英文名称)}
    qualifier_re: re.Pattern | None
    bare_cjk_re: re.Pattern | None
    alternatives_re: re.Pattern | None  # 字段引用前用「或 / 和 / 、」并列的其他对象限定


def _alternation(words) -> str:
    return '|'.join(map(re.escape, sorted(words, key=len, reverse=True)))


def build_field_index(objects: dict) -> FieldIndex:
    """objects：{stem: {'qualifiers': [对象英文名, 表名, ...], 'rows': 属性清单行}}。

    不带限定时只认英文标识符和 3 字以上的中文名，「编号」「名称」这类短名太常见，
    只在「[[对象]]的编号」这种带限定的写法里认。
    """
    names, ordered, identity, qualifiers, bare = {}, {}, {}, {}, {}
    for stem, info in objects.items():
        fields, ids = {}, set()
        for row in info.get('rows') or []:
            english = (row.get('英文名称') or '').strip()
            if english in ('', '无', '-'):
                continue
            if is_identity_field(row):
                ids.add(english.lower())
            for name in (english, (row.get('中文名称') or '').strip(), *_alias_tokens(row.get('属性别名', ''))):
                if not name or name.lower() in _EMPTY_ALIASES:
                    continue
                key = _norm(name)
                fields.setdefault(key, english)
                if _ASCII_IDENT_RE.fullmatch(name) if name.isascii() else len(name) >= 3:
                    bare.setdefault(key, set()).add((stem, english))
        names[stem] = fields
        ordered[stem] = sorted(fields.items(), key=lambda item: -len(item[0]))
        identity[stem] = ids
        for qualifier in (stem, *(info.get('qualifiers') or [])):
            if qualifier and qualifier.strip():
                qualifiers.setdefault(_norm(qualifier), stem)
    qualifier_words = _alternation(qualifiers)
    cjk_words = _alternation(k for k in bare if not k.isascii())
    alternative = rf'\[\[[^\]]+\]\]|(?<![A-Za-z0-9_])(?:{qualifier_words})' if qualifier_words else r'\[\[[^\]]+\]\]'
    return FieldIndex(
        names, ordered, identity, qualifiers, bare,
        re.compile(rf'(?<![A-Za-z0-9_])(?:{qualifier_words})', re.I) if qualifier_words else None,
        re.compile(cjk_words) if cjk_words else None,
        re.compile(rf'(?:(?:{alternative})\s*(?:或者|或|和|及|、|/)\s*)+$', re.I),
    )


def _field_at(text: str, pos: int, stem: str, index: FieldIndex):
    for key, english in index.ordered.get(stem, ()):
        piece = text[pos:pos + len(key)]
        if (piece.lower() if key.isascii() else piece) != key:
            continue
        end = pos + len(key)
        if key.isascii() and re.match(r'[A-Za-z0-9_]', text[end:end + 1]):
            continue
        return end, english
    return None


def _overlaps(start: int, end: int, spans) -> bool:
    return any(start < e and s < end for s, e in spans)


def _find_refs(line: str, index: FieldIndex) -> list:
    """一行里的字段引用：[[对象]]的字段 / 对象名.字段 / 表名.字段 / 不带限定的字段名。"""
    refs, masked = [], []

    def qualified(start, after, stem):
        accessor = _ACCESSOR_RE.match(line, after)
        hit = _field_at(line, accessor.end(), stem, index) if accessor else None
        if not hit:
            return None
        end, english = hit
        end += 1 if line[end:end + 1] == '`' else 0
        ref = FieldRef(start, end, frozenset({(stem, english)}))
        refs.append(ref)
        masked.append((start, end))
        return ref

    for link in mm.parse_links(line):
        masked.append(link.span)
        stem = index.qualifiers.get(_norm(link.full_name)) or index.qualifiers.get(_norm(link.name))
        if stem:
            qualified(link.span[0], link.span[1], stem)
    if index.qualifier_re:
        for match in index.qualifier_re.finditer(line):
            stem = index.qualifiers.get(_norm(match.group(0)))
            if stem and not _overlaps(*match.span(), masked):
                qualified(match.start(), match.end(), stem)
    for match in _ASCII_IDENT_RE.finditer(line):
        found = index.bare.get(match.group(0).lower())
        if found and not _overlaps(*match.span(), masked):
            refs.append(FieldRef(match.start(), match.end(), frozenset(found)))
    if index.bare_cjk_re:
        taken = masked + [(r.start, r.end) for r in refs]
        for match in index.bare_cjk_re.finditer(line):
            if not _overlaps(*match.span(), taken):
                refs.append(FieldRef(match.start(), match.end(), frozenset(index.bare[match.group(0)])))
    refs.sort(key=lambda r: (r.start, -r.end))
    kept = []
    for ref in refs:
        if not kept or ref.start >= kept[-1].end:
            kept.append(ref)
    return kept


def _claims_equivalence(line: str, clause: tuple, left: FieldRef, right: FieldRef,
                        next_start: int, index: FieldIndex) -> bool:
    """两个相邻字段引用之间是否断言「同一个值」：A = B、A 对应 B、核对 A 与 B、A 和 B 一致。

    「按 A 和 B 匹配 [[表]]」是按组合键去另一张表查找，不算两字段等价。
    """
    mid = line[left.end:right.start].strip(_QUOTES)
    # SQL 写法 `a.credit_code = b.customer_id`：右侧字段前的别名前缀不算中间词
    mid = re.sub(r'(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*\.$', '', mid).strip(_QUOTES)
    # 「A 与X或Y的 B」：B 前并列的对象是同一字段的备选限定，同样不算中间词
    mid = index.alternatives_re.sub('', mid).strip(_QUOTES)
    if _EQUAL_MID_RE.fullmatch(mid):
        return True
    if not _CONNECTOR_MID_RE.fullmatch(mid):
        return False
    start, end = clause
    if _HEAD_VERB_RE.search(line[start:left.start]):
        return True
    tail = line[right.end:end]
    verb = _TAIL_VERB_RE.match(tail)
    if not verb or right.end + verb.end() > next_start:
        return False
    rest = tail[verb.end():].lstrip(' \t到至上与和')
    if rest.startswith('[['):
        return False
    return not (index.qualifier_re and index.qualifier_re.match(rest))


def _proven(left: FieldRef, right: FieldRef, index: FieldIndex, joins: set) -> bool:
    """这一对已有依据或不在检查范围：不是两个标识字段、同一对象内比较、同名或互为别名、声明过关联。"""
    def is_identity(ref):
        return any(english.lower() in index.identity.get(stem, ()) for stem, english in ref.candidates)

    if not (is_identity(left) and is_identity(right)):
        return True
    if len({stem for stem, _ in left.candidates | right.candidates}) == 1:
        return True
    for source, source_field in left.candidates:
        for target, target_field in right.candidates:
            if source_field.lower() == target_field.lower():
                return True
            if (index.names.get(source, {}).get(_norm(target_field)) == source_field
                    or index.names.get(target, {}).get(_norm(source_field)) == target_field):
                return True
            if (source, source_field.lower(), target, target_field.lower()) in joins:
                return True
    return False


def _ref_label(ref: FieldRef) -> str:
    if len(ref.candidates) == 1:
        stem, english = next(iter(ref.candidates))
        return f'{stem}.{english}'
    return sorted(english for _, english in ref.candidates)[0]


def find_unproven_equivalences(lines, index: FieldIndex, joins: set) -> list:
    """[(行号, 文本)] → 没有关联声明支撑的跨对象标识字段等价断言。

    joins：{(源对象, 源字段小写, 目标对象, 目标字段小写)}，两个方向都要在内。
    行内写了「待确认」等标记的记为 pending，由调用方降为提示。
    """
    findings = []
    for lineno, raw in lines:
        line = unicodedata.normalize('NFC', raw)
        refs = _find_refs(line, index)
        if len(refs) < 2:
            continue
        separators = [m.start() for m in _CLAUSE_SEPARATOR_RE.finditer(line)]
        for position, (left, right) in enumerate(zip(refs, refs[1:])):
            start = max((s + 1 for s in separators if s < left.start), default=0)
            end = min((s for s in separators if s >= left.end), default=len(line))
            if right.start >= end:
                continue
            next_start = refs[position + 2].start if position + 2 < len(refs) else len(line)
            if not _claims_equivalence(line, (start, end), left, right, next_start, index):
                continue
            if _proven(left, right, index, joins):
                continue
            findings.append({
                'line': lineno,
                'left': _ref_label(left),
                'right': _ref_label(right),
                'pending': bool(_PENDING_RE.search(line)),
                'text': line.strip()[:80],
            })
    return findings


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

ITEM_LIMIT = 80
JOIN_LIMIT = 300
LIST_LIMIT = 30
IDENTITY_OBJECT_LIMIT = 100
IMPORT_NOT_RUN = {'status': 'not_run',
                  'note': '本脚本不执行导入；导入验证以下游（modeling）的导入结果为准，没有导入记录时显示「未执行」'}


def _item_rank(item: dict) -> int:
    if item['verdict'] in ('冲突', '非法类型'):
        return 0
    if item['verdict'] == '待确认' or item.get('mixing'):
        return 1
    return {'未验证': 2, '不适用': 3}.get(item['verdict'], 4)


def _field_coverage(items: list, tables: dict) -> dict:
    mapped: dict = {}
    for item in items:
        if item.get('table'):
            mapped.setdefault(item['table'], set()).add((item.get('column') or '').lower())
    coverage = []
    for table, mapped_columns in sorted(mapped.items())[:LIST_LIMIT]:
        columns = tables.get(table, [])
        unmapped = [c for c in columns if c.lower() not in mapped_columns]
        coverage.append({'table': table, 'columns': len(columns), 'mapped': len(columns) - len(unmapped),
                         'unmapped': unmapped[:20], 'unmapped_count': len(unmapped)})
    missing = [f'{i["object"]}.{i["field"]}' for i in items if i.get('column_missing')]
    unbound = sorted({i['object'] for i in items if not i.get('table') and i['verdict'] != '不适用'})
    return {
        'tables': coverage,
        'tables_omitted': max(len(mapped) - LIST_LIMIT, 0),
        'attributes_without_column': missing[:LIST_LIMIT],
        'attributes_without_column_count': len(missing),
        'objects_without_table': unbound[:LIST_LIMIT],
        'objects_without_table_count': len(unbound),
    }


def types_report(root: Path, files: list | None = None) -> tuple[dict, int]:
    """逐字段类型验收报告与退出码。登记的数据表读不了时整体「无法核验」，不给通过。"""
    import table_profile
    import ontology_checker

    missing_text = table_profile.missing_text_versions(root)
    if missing_text:
        # 资料已标为 schema、只是还没转成文本：一次列全，不按「没有数据表」处理
        names = '、'.join(Path(rel).name for rel in missing_text[:10])
        return {'status': 'unverifiable', 'reason': f'{len(missing_text)} 份 schema 资料还没有文本版（转换中、暂不支持'
                f'或转换失败），字段核验未完成：{names}', 'missing_text': missing_text[:LIST_LIMIT],
                'import_validation': IMPORT_NOT_RUN}, 2
    try:
        tables, profiles = table_profile.load_registered_tables(root)
    except ValueError as error:
        return {'status': 'unverifiable', 'reason': str(error), 'import_validation': IMPORT_NOT_RUN}, 2
    docs = ontology_checker.load_ontology(root / 'ontology')
    objects = {stem: doc for stem, doc in docs.items() if doc.doc_type == 'object'}
    wanted = list(dict.fromkeys(Path(f).stem for f in files or ()))
    missing = [stem for stem in wanted if stem not in objects]
    if missing:
        return {'status': 'unverifiable', 'reason': f'指定对象不存在：{"、".join(missing[:10])}',
                'import_validation': IMPORT_NOT_RUN}, 2
    all_items = [item for doc in objects.values()
                 for item in ontology_checker.assess_object_types(doc, tables, profiles)]
    items = [item for item in all_items if not wanted or item['object'] in wanted]
    counts = Counter(item['verdict'] for item in items)
    mixing = sum(1 for item in items if item.get('mixing'))
    if counts['冲突'] or counts['非法类型']:
        status = 'failed'
    elif counts['待确认'] or mixing:
        status = 'pending'
    elif counts['未验证'] or not items:
        status = 'unverified'
    else:
        status = 'passed'
    ordered = sorted(items, key=lambda i: (_item_rank(i), i['object'], i['field']))
    shown = [{k: v for k, v in item.items() if v not in ('', None, False)} for item in ordered[:ITEM_LIMIT]]
    sampled = sorted(name for name, profile in profiles.items() if profile.get('truncated'))
    report = {
        'status': status,
        'counts': {**dict(sorted(counts.items())), '标识混合': mixing},
        'items': shown,
        'omitted': len(ordered) - len(shown),
        'field_coverage': _field_coverage(all_items, tables),
        'import_validation': IMPORT_NOT_RUN,
        'profile': {'tables_read': len(profiles), 'row_limit': PROFILE_ROW_LIMIT,
                    'sampled_tables': sampled[:LIST_LIMIT]},
        'schema': 'ok' if tables else 'no_schema',
    }
    return report, 1 if status == 'failed' else 0


def joins_report(root: Path, objects: list | None = None) -> dict:
    """已声明的关联字段对与各对象的标识字段；objects 只保留涉及这些对象的部分。"""
    import table_profile
    import ontology_checker

    tables = table_profile.table_headers(root)
    docs = ontology_checker.load_ontology(root / 'ontology')
    wanted = {Path(name).stem for name in objects or ()}
    joins = [join for join in ontology_checker.declared_join_list(docs, tables)
             if not wanted or {join['source'], join['target']} & wanted]
    identity = {}
    for stem, doc in sorted(docs.items()):
        if doc.doc_type != 'object' or (wanted and stem not in wanted):
            continue
        fields = [row['英文名称'].strip() for row in doc.attribute_rows
                  if (row.get('英文名称') or '').strip() not in ('', '无', '-') and is_identity_field(row)]
        if fields:
            identity[stem] = fields[:LIST_LIMIT]
    truncated = (len(joins) > JOIN_LIMIT or len(identity) > IDENTITY_OBJECT_LIMIT
                 or any(len(fields) >= LIST_LIMIT for fields in identity.values()))
    return {
        'joins': joins[:JOIN_LIMIT],
        'join_count': len(joins),
        'identity_fields': dict(list(identity.items())[:IDENTITY_OBJECT_LIMIT]),
        'truncated': truncated,
        'note': '跨对象字段只能按这里的关联字段对或同名 / 别名字段关联；其余等价关系写进注意事项并标「待确认」',
    }


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description='字段类型验收与关联字段清单（只读，输出 JSON）')
    commands = parser.add_subparsers(dest='command', required=True)
    types_parser = commands.add_parser('types', help='逐字段类型验收')
    types_parser.add_argument('--files', nargs='*', default=[], help='只报告这些对象（文件名或路径）')
    joins_parser = commands.add_parser('joins', help='已声明的关联字段对与标识字段')
    joins_parser.add_argument('--objects', nargs='*', default=[], help='只列涉及这些对象的部分')
    args = parser.parse_args(argv)
    root = Path.cwd()
    if args.command == 'types':
        report, code = types_report(root, args.files)
    else:
        report, code = joins_report(root, args.objects), 0
    output = logging.Logger('field_semantics.json', logging.INFO)
    output.addHandler(logging.StreamHandler(sys.stdout))
    output.info(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
