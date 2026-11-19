"""采集结果 → 文本版（ADR-0104；design/data-sources.md 第 2、3 节）。在网关主进程里跑，插件绕不过。

插件发来的是原始的表（列 + 至多 200 行原始取值）和事实。这里：
1. 按 `mask-v1` 逐列识别、遮蔽，只留前 20 行样本，单元格截到 200 字，再整格扫一遍个人信息；
2. 低基数、不敏感的列自动补「码值」事实（插件自己给了的不重复）；
3. 渲染成和 Excel 文本版同一格式：一张表一个工作表（首行列名），后面是「库表事实」各节，末尾是锚点注释和
   给脚本读的 `onto:facts` 注释块。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ...evidence.convert import anchor_index, md_cell, md_table, sheet_marker
from . import masking

SAMPLE_ROWS = 20
CELL_CHARS = 200
MAX_TABLES = 500
MAX_FACTS = 5000
MAX_OUTPUT_CHARS = 16 * 1024 * 1024
DEFINITION_CHARS = 2000
VALUES_MAX = 20
MASKED = "«已遮蔽»"
FACTS_HEAD = "<!-- onto:facts 1"
FACTS_MARKER = "<!-- 库表事实 -->"

# 整数类型按词首匹配：interval、point 里的 int 不算
_NUMERIC = re.compile(r"^(?:tiny|small|medium|big)?int(?:eger)?\d*\b|decimal|numeric|float|double|real|number|serial|money",
                      re.IGNORECASE)
_TEMPORAL = re.compile(r"date|time|year|interval", re.IGNORECASE)
_BOOL = re.compile(r"^(bool|boolean|bit(\(1\))?|tinyint\(1\))$", re.IGNORECASE)
_BINARY = re.compile(r"blob|binary|bytea|image|varbinary|raw", re.IGNORECASE)

KEY_LABELS = {"pk": "主键", "fk": "外键", "uk": "唯一", "": ""}
SENSITIVITY_LABELS = {
    "secret": "丢弃", "phone": "手机号", "id_number": "证件号", "person_name": "姓名", "birth_date": "生日",
    "email": "邮箱", "bank_card": "银行卡", "address": "地址", "binary": "二进制", "text": "文本",
    "amount": "金额分桶", "join_key": "关联键哈希", "none": "",
}


def type_class(raw_type: str) -> str:
    text = str(raw_type or "").strip()
    if _BOOL.match(text):
        return "bool"
    if _BINARY.search(text):
        return "binary"
    if _TEMPORAL.search(text):
        return "temporal"
    if _NUMERIC.search(text):
        return "numeric"
    return "text"


@dataclass
class Collected:
    """runner 收齐的一次采集：表、事实、局限、错误。"""

    tables: list[dict] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


def _cell(value) -> str:
    text = "" if value is None else str(masking.plain(value))
    if len(text) > CELL_CHARS:
        text = text[:CELL_CHARS] + "…"
    if text and masking.has_pii([text]):
        return MASKED
    return text


def _mask_table(table: dict, masker: masking.Masker, join_keys: frozenset = frozenset()
                ) -> tuple[list[dict], list[list[str]], list[dict]]:
    """→ (带 sensitivity 的列, 遮蔽后的样本行, 自动识别的码值事实)。"""
    columns = table["columns"]
    rows = table["rows"]
    out_columns, auto_values = [], []
    categories = []
    for index, column in enumerate(columns):
        values = [row[index] for row in rows if index < len(row) and row[index] is not None]
        kind = type_class(column.get("type", ""))
        category = masking.classify(column["name"], table=table["name"], comment=column.get("comment", ""),
                                    type_class=kind, values=values,
                                    join_key=column.get("key") in ("pk", "fk") or (table["name"], column["name"]) in join_keys)
        categories.append(category)
        out_columns.append({**column, "sensitivity": category})
        texts = [str(masking.plain(value)) for value in values]
        if category == "none" and kind in ("text", "numeric", "bool") and masking.is_code_column(texts):
            counts: dict[str, int] = {}
            for text in texts:
                counts[text] = counts.get(text, 0) + 1
            ranked = sorted(counts, key=lambda item: (-counts[item], item))[:VALUES_MAX]
            auto_values.append({"kind": "values", "table": table["name"], "column": column["name"], "values": ranked})
    masked_rows = []
    for row in rows[:SAMPLE_ROWS]:
        masked_rows.append([_cell(masker.apply(categories[index], row[index] if index < len(row) else None))
                            for index in range(len(columns))])
    return out_columns, masked_rows, auto_values


def _md_lines(header: list[str], rows: list[list[str]]) -> list[str]:
    """表头 + 样本行 → Markdown 表格的行；全空的行也留着（行号和锚点一一对应）。"""
    lines = ["| " + " | ".join(md_cell(cell) for cell in header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(md_cell(cell) for cell in row) + " |" for row in rows]
    return lines


def _clip_fact(fact: dict) -> dict:
    out = {}
    for key, value in fact.items():
        if key in ("definition", "expression") and isinstance(value, str) and len(value) > DEFINITION_CHARS:
            value = value[:DEFINITION_CHARS] + "…"
        out[key] = value
    return out


def _join(values) -> str:
    return ", ".join(str(item) for item in values or [])


_SECTIONS = (
    # (节名, 事实类型, 表头, 取值)
    ("主键", ("primary_key",), ["表", "列"], lambda f: [f.get("table"), _join(f.get("columns"))]),
    ("外键", ("foreign_key",), ["表", "列", "引用表", "引用列", "名称"],
     lambda f: [f.get("table"), _join(f.get("columns")), f.get("ref_table"), _join(f.get("ref_columns")), f.get("name")]),
    ("推断关联", ("reference",), ["表", "列", "引用表", "引用列", "依据"],
     lambda f: [f.get("table"), _join(f.get("columns")), f.get("ref_table"), _join(f.get("ref_columns")), f.get("basis")]),
    ("唯一约束", ("unique",), ["表", "列", "名称"], lambda f: [f.get("table"), _join(f.get("columns")), f.get("name")]),
    ("检查约束", ("check",), ["表", "名称", "表达式"], lambda f: [f.get("table"), f.get("name"), f.get("expression")]),
    ("码值", ("values",), ["表", "列", "取值"], lambda f: [f.get("table"), f.get("column"), _join(f.get("values"))]),
    ("视图", ("view",), ["名称", "定义"], lambda f: [f.get("name"), f.get("definition")]),
    ("存储过程", ("routine",), ["名称", "类型", "签名", "定义"],
     lambda f: [f.get("name"), f.get("type"), f.get("signature"), f.get("definition")]),
    ("触发器", ("trigger",), ["表", "名称", "时机", "事件", "定义"],
     lambda f: [f.get("table"), f.get("name"), f.get("timing"), f.get("event"), f.get("definition")]),
)

_JSON_KEYS = {"primary_key": "primary_keys", "foreign_key": "foreign_keys", "reference": "references",
              "unique": "unique", "check": "checks", "values": "values"}


def render(collected: Collected, salt: str) -> tuple[str, dict, list[str]]:
    """→ (正文 Markdown, 统计, 局限)。"""
    limitations = list(collected.limitations)
    tables = collected.tables
    if len(tables) > MAX_TABLES:
        limitations.append(f"只采了前 {MAX_TABLES} 张表，共 {len(tables)} 张")
        tables = tables[:MAX_TABLES]
    facts = [_clip_fact(item) for item in collected.facts]
    if len(facts) > MAX_FACTS:
        limitations.append(f"库表事实只留前 {MAX_FACTS} 条，共 {len(facts)} 条")
        facts = facts[:MAX_FACTS]
    masker = masking.Masker(salt=salt)
    # 外键和推断关联两端的列都按关联键加盐哈希：同一个值在两张表里哈希相同，样本之间还能对上
    join_keys = frozenset((fact.get(table), column) for fact in facts if fact.get("kind") in ("foreign_key", "reference")
                          for table, columns in (("table", "columns"), ("ref_table", "ref_columns"))
                          for column in fact.get(columns) or [])
    given_values = {(f.get("table"), f.get("column")) for f in facts if f.get("kind") == "values"}
    parts, anchors, cols = [], [], {}
    meta: dict[str, dict] = {}
    rows_total = 0
    line = 0
    seen: set[str] = set()
    for table in tables:
        name = table["name"]
        if name in seen:
            limitations.append(f"表 {name} 重复，只留第一份")
            continue
        seen.add(name)
        columns, rows, auto_values = _mask_table(table, masker, join_keys)
        facts += [item for item in auto_values if (item["table"], item["column"]) not in given_values]
        rows_total += len(rows)
        header = [column["name"] for column in columns]
        body = _md_lines(header, rows)
        start = line + (1 if parts else 0)
        anchors.append((start + 1, f"sheet:{name}"))
        anchors.append((start + 4, "row:1"))
        anchors += [(start + 6 + offset, f"row:{offset + 2}") for offset in range(len(body) - 2)]
        cols[name] = len(header)
        part = "\n".join([sheet_marker(name), f"## {name}", "", *body])
        parts.append(part)
        line = start + part.count("\n") + 1
        meta[name] = {"comment": table.get("comment", ""),
                      "columns": [{key: column.get(key) for key in ("name", "type", "nullable", "key", "comment")}
                                  | {"masked": column["sensitivity"] != "none", "sensitivity": column["sensitivity"]}
                                  for column in columns]}

    fact_parts = [FACTS_MARKER]
    table_rows = [[name, info["comment"]] for name, info in meta.items() if info["comment"]]
    if table_rows:
        fact_parts.append("## 表\n\n" + md_table([["表", "注释"], *table_rows]))
    column_rows = [[name, column["name"], column.get("type") or "",
                    "" if column.get("nullable") is None else ("是" if column["nullable"] else "否"),
                    KEY_LABELS.get(column.get("key") or "", ""), column.get("comment") or "",
                    SENSITIVITY_LABELS.get(column["sensitivity"], "")]
                   for name, info in meta.items() for column in info["columns"]]
    if column_rows:
        fact_parts.append("## 列\n\n" + md_table([["表", "列", "类型", "可空", "键", "注释", "遮蔽"], *column_rows]))
    for title, kinds, head, pick in _SECTIONS:
        rows = [[md_cell(value) for value in pick(fact)] for fact in facts if fact.get("kind") in kinds]
        if rows:
            fact_parts.append(f"## {title}\n\n" + md_table([head, *rows]))
    if limitations:
        fact_parts.append("## 局限\n\n" + "\n".join(f"- {md_cell(item)}" for item in limitations))

    markdown = "\n\n".join(parts + (fact_parts if len(fact_parts) > 1 else [])) + "\n"
    if len(markdown) > MAX_OUTPUT_CHARS:
        markdown = markdown[:MAX_OUTPUT_CHARS].rsplit("\n", 1)[0] + "\n"
        limitations.append("内容太长，只留了前一部分")
    kept_lines = markdown.count("\n")
    index = anchor_index([(n, anchor) for n, anchor in anchors if n <= kept_lines], None, cols)
    document = {"tables": meta}
    for fact in facts:
        key = _JSON_KEYS.get(fact.get("kind"))
        if key:
            document.setdefault(key, []).append({k: v for k, v in fact.items() if k != "kind"})
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).replace(">", "\\u003e")
    markdown += ("\n" + index + "\n" if index else "") + f"\n{FACTS_HEAD}\n{payload}\n-->\n"
    stats = {"tables": len(meta), "facts": len(facts), "rows": rows_total}
    return markdown, stats, limitations


def parse_facts(markdown: str) -> dict | None:
    """文本版末尾的 `onto:facts` 注释块 → dict；没有或坏了时 None。工具箱的 table_profile 有同口径的一份。"""
    text = str(markdown or "")
    start = text.rfind("\n" + FACTS_HEAD + "\n")
    if start < 0:
        return None
    body = text[start + len(FACTS_HEAD) + 2:]
    end = body.find("\n-->")
    if end < 0:
        return None
    try:
        data = json.loads(body[:end])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
