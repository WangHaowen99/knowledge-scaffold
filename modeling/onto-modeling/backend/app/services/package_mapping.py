"""对象 ↔ 真实数据表绑定：发布包「来源Sheet」与 object-data-mappings.json 的单一来源。

对象文档承载业务语义（「对象属性清单」的英文名称列就是真实列名），真实表名来自打了
schema 标签的表格资料（一个 Sheet = 一张表）。两者在这里对齐一次，供发布包导出使用：
objects/index.md 的「来源Sheet」列、对象文档的 `# 字段` 段，以及
mappings/object-data-mappings.json —— modeling 导入后据此生成 sourceRefs 与 sourceColumn，
nl2sql 才有表名和列名可用。

发布门禁只拦"有数据表可核对、但声明对不上"的对象（issues 里 blocking=True）；当前没有任何
可核对的数据表时，声明无从验证，只提示不阻断。阻断项可在发布前逐个修复（set_declared_source），
也可以降级发布（strict_declared=False）：对不上的对象来源Sheet 写 UNBOUND，不进 mapping。

绑定优先级：对象文档「# 数据来源」的显式声明 > 属性英文名与 Sheet 列头的唯一最佳重合。
推断只在重合列数达标且候选唯一时成立（声明了主键的对象，候选表还须含全部主键列），
够不着就留空（来源Sheet 写 UNBOUND），不猜。
首行写「无」是显式声明不落库（如只作为输出的单据），不再按重合度推断。

图模式变体按 modeling 图导入契约导出（object_data_mappings(graph=True)），留作发布适配器的变体。
"""

from __future__ import annotations

import csv
import difflib
import json
import re
import unicodedata
from pathlib import Path

from ..kernel.metamodel import mm

# modeling 契约：来源Sheet 为 "—" 表示该对象不绑定物理表（package-import 按此跳过）
UNBOUND = "—"
# 包内部逻辑数据源别名，不是外部数据源的 provider；取 modeling 草稿同款默认值
PROVIDER_ALIAS = "data"
# 推断绑定的最低列命中数：低于此宁可不绑，错绑比不绑更难发现
MIN_INFERRED_COLUMNS = 2
# 修复建议里推荐的候选表个数，以及只靠表名相似（列一个都对不上）时的最低相似度
MAX_CANDIDATES = 3
MIN_NAME_SIMILARITY = 0.5
# modeling 图导入对属性名与关系名的标识符约束（evaluation/data-source.ts GRAPH_IDENTIFIER）
GRAPH_IDENTIFIER = re.compile(r"^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*$")
# 同一字符集、去掉锚点：从关系括注里取出字段候选，再拿两端属性清单裁决
_IDENTIFIER_RE = re.compile(GRAPH_IDENTIFIER.pattern.strip("^$"))
RELATIONS_SECTION = "关联对象"
SOURCE_PURPOSES = ("schema",)
TABLE_SUFFIXES = (".xlsx", ".xlsm", ".csv")

SOURCE_SECTION = "数据来源"
FIELDS_SECTION = "对象属性清单"
# 知识包发布格式使用“字段”，旧版工作区模板仍使用“对象属性清单”。
# 绑定解析必须同时兼容两种章节名，否则重新导入知识包后会读不到任何字段。
FIELD_SECTION_NAMES = ("字段", "对象属性清单")

_EMPTY_MARKERS = {"", "无", "-", "—", "–", "n/a"}
# 模板占位符 `{待填写：…}` 可能跨行，只认起始花括号：真实表名和列名都不会以 { 开头
_PLACEHOLDER_RE = re.compile(r"^\{")
# 「无」后面带括注或标点的说明（如 `无（派生对象，不落库）`）同样表示不声明
_NONE_WITH_NOTE_RE = re.compile(r"^无\s*[（(，,。：:；;]")
_QUOTED_RE = re.compile(r"`([^`\n]+)`")
# 关系端点里指代"本文档对象"的写法；「当前对象名」是旧版抽取模板的字面占位，已写进文档的照认
CURRENT_OBJECT = "当前对象"
_CURRENT_OBJECT_ALIASES = (CURRENT_OBJECT, "当前对象名")
# 括注一侧写成 `表名.字段`（表名、字段可各带反引号）
_PREFIXED_FIELD_RE = re.compile(
    rf"`?({_IDENTIFIER_RE.pattern})`?\s*\.\s*`?({_IDENTIFIER_RE.pattern})`?"
)
# 属性规则带这些标记的属性本来就不在表里（operation 计算得到的、取自文档的常量），不算缺列；
# 与工具箱 ontology_checker 的列名对账同一口径
_NON_STORED_MARKERS = ("派生：", "派生:", "非库字段")
_HEADER_ALIASES = {
    "key": ("英文名称", "字段 key", "字段key", "field key", "key"),
    "cn": ("中文名称", "中文名", "字段名称"),
    "desc": ("描述", "说明", "业务说明"),
    "primary": ("主键",),
    "type": ("类型",),
    "alias": ("属性别名", "别名"),
    "rule": ("属性规则",),
}


def _is_empty(value: str) -> bool:
    text = (value or "").strip()
    return text.lower() in _EMPTY_MARKERS or bool(_PLACEHOLDER_RE.match(text))


def _section(text: str, name: str) -> str:
    """取 `# name` 到下一个一级标题之间的正文（与 modeling 包解析同一切分口径）。"""
    lines = text.replace("\r\n", "\n").split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == f"# {name}")
    except StopIteration:
        return ""
    out: list[str] = []
    for line in lines[start + 1:]:
        if re.match(r"^#\s+", line):
            break
        out.append(line)
    return "\n".join(out).strip()


def _fields_section(text: str) -> str:
    """读取新旧两种对象字段章节，优先使用知识包标准的 `# 字段`。"""
    for name in FIELD_SECTION_NAMES:
        value = _section(text, name)
        if value:
            return value
    return ""


def _table(body: str) -> tuple[list[str], list[list[str]]]:
    rows = [line.strip() for line in body.split("\n") if line.strip().startswith("|")]
    if len(rows) < 2:
        return [], []
    cells = [[c.strip() for c in row.strip().strip("|").split("|")] for row in rows]
    return cells[0], [r for r in cells[2:] if not all(set(c) <= set("-: ") for c in r)]


def _column_index(headers: list[str], kind: str) -> int:
    normalized = [h.replace(" ", "").lower() for h in headers]
    for name in _HEADER_ALIASES[kind]:
        target = name.replace(" ", "").lower()
        if target in normalized:
            return normalized.index(target)
    return -1


def _is_source_note(candidate: str) -> bool:
    """「# 数据来源」里不参与表名判定的行：空行、表格、引用说明、模板占位。"""
    if not candidate or candidate.startswith(("|", ">")):
        return True
    return bool(_PLACEHOLDER_RE.match(candidate))


def _is_non_stored(field: dict) -> bool:
    return any(marker in field["rule"] for marker in _NON_STORED_MARKERS)


def _first_existing(names: list[str], by_lower: dict[str, str]) -> str:
    """names 里第一个在 by_lower（小写表名 → 表名）中存在的表名；都不存在返回空。"""
    for name in names:
        if name.lower() in by_lower:
            return by_lower[name.lower()]
    return ""


def _declared_source(text: str) -> tuple[str, list[str], bool]:
    """「# 数据来源」首行 → (声明的表名, 首行里反引号标出的名字, 是否声明了「无」)。

    首行即表名；写成说明句时整句仍作为声明返回（找不到时报错要能看见原文），反引号里的
    名字留给绑定时兜底。首行是「无」则不声明，后面的说明行也不再当表名。
    """
    # 模板占位符 `{待填写：…}` 可能跨多行，先整块剔除再逐行找表名
    source_text = re.sub(r"\{[^{}]*\}", " ", _section(text, SOURCE_SECTION), flags=re.S)
    for line in source_text.split("\n"):
        candidate = line.strip().lstrip("-*").strip()
        if _is_source_note(candidate):
            continue
        if _is_empty(candidate) or _NONE_WITH_NOTE_RE.match(candidate):
            return "", [], True
        quoted = _QUOTED_RE.findall(candidate)
        if quoted == [candidate.strip("`")]:  # 整行就是一个 `表名`
            return quoted[0].strip(), [], False
        return candidate, [name.strip() for name in quoted if name.strip()], False
    return "", [], False


def parse_object_doc(text: str) -> dict:
    """对象文档 → {declared_table, declared_mentions, declared_none, fields[]}。

    fields 的 key 即属性英文名（真实列名）；declared_none 表示「# 数据来源」首行写了「无」。
    """
    declared, mentions, declared_none = _declared_source(text)

    headers, rows = _table(_fields_section(text))
    key_index = _column_index(headers, "key")
    fields: list[dict] = []
    if key_index >= 0:
        indexes = {kind: _column_index(headers, kind) for kind in _HEADER_ALIASES}

        def cell(row: list[str], kind: str) -> str:
            index = indexes[kind]
            return row[index].strip() if 0 <= index < len(row) else ""

        for row in rows:
            key = cell(row, "key")
            if _is_empty(key):
                continue
            alias, rule = cell(row, "alias"), cell(row, "rule")
            fields.append({
                "key": key,
                "cn": cell(row, "cn"),
                "desc": cell(row, "desc"),
                "type": cell(row, "type") or "字符串",
                "alias": "" if _is_empty(alias) else alias,
                "rule": "" if _is_empty(rule) else rule,
                "primary": cell(row, "primary").strip() in ("是", "true", "yes", "Y"),
            })
    return {
        "declared_table": declared, "declared_mentions": mentions,
        "declared_none": declared_none, "fields": fields,
    }


def read_table_headers(path: Path) -> dict[str, list[str]]:
    """表格文件 → {Sheet 名: 表头列}。口径与工具箱 table_profile.py 一致：
    一个 Sheet = 一张表，首行 = 字段名。"""
    if path.suffix.lower() == ".csv":
        try:
            with open(path, encoding="utf-8-sig", newline="") as handle:
                row = next(csv.reader(handle), [])
        except (OSError, UnicodeDecodeError, csv.Error):
            return {}
        headers = [str(c).strip() for c in row if str(c).strip()]
        return {path.stem: headers} if headers else {}
    try:
        import openpyxl
    except ImportError:  # pragma: no cover - 部署环境自带
        return {}
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return {}
    tables: dict[str, list[str]] = {}
    try:
        for sheet in workbook.worksheets:
            row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
            headers = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if headers and sheet.title not in tables:
                tables[sheet.title] = headers
    finally:
        workbook.close()
    return tables


def read_source_tables(root: Path) -> dict[str, list[str]]:
    """schema 标签的表格资料 → {表名: 列头}。同名 Sheet 以先读到的为准。"""
    tables: dict[str, list[str]] = {}
    directives_path = root / "resource" / "materials.json"
    try:
        directives = json.loads(directives_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        directives = {}
    if not isinstance(directives, dict):
        directives = {}
    for rel, directive in directives.items():
        if not isinstance(directive, dict) or directive.get("purpose") not in SOURCE_PURPOSES:
            continue
        if not str(rel).lower().endswith(TABLE_SUFFIXES):
            continue
        path = (root / str(rel)).resolve()
        if not str(path).startswith(str(root.resolve())) or not path.is_file():
            continue
        for name, headers in read_table_headers(path).items():
            tables.setdefault(name, headers)
    return tables


def _match_table(field_keys: list[str], tables: dict[str, list[str]], primary_keys: list[str]) -> str:
    """属性英文名与各表列头的重合度 → 唯一最佳表名；并列第一或不达标时返回空，不猜。

    声明了主键的对象只在含全部主键列的表里找：主键列不在表里，实例身份无从确定，
    绑上去只会造出身份错的节点（如未声明来源的对象与别的对象的表撞上两三列同名属性）。
    """
    lowered = {k.lower() for k in field_keys}
    required = {k.lower() for k in primary_keys}
    candidates = {name: {c.lower() for c in columns} for name, columns in tables.items()}
    scores = sorted(
        ((len(lowered & columns), name) for name, columns in candidates.items() if required <= columns),
        reverse=True,
    )
    if not scores or scores[0][0] < MIN_INFERRED_COLUMNS:
        return ""
    if len(scores) > 1 and scores[1][0] == scores[0][0]:
        return ""
    return scores[0][1]


def read_object_texts(root: Path, *, strict: bool = False) -> dict[str, str]:
    """对象文档原文：{对象 key: 正文}，绑定解析、关系解析与发布包导出共用同一来源。"""
    object_dir = root / "ontology" / "object"
    texts: dict[str, str] = {}
    for doc in sorted(object_dir.glob("*.md")) if object_dir.is_dir() else []:
        if doc.name == "index.md":
            continue
        try:
            texts[doc.stem] = doc.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            if strict:
                raise ValueError(f"ontology/object/{doc.name}：无法读取对象文档") from exc
            if isinstance(exc, UnicodeError):
                raise
            continue
    return texts


def _candidates(field_keys: list[str], name: str, tables: dict[str, list[str]], exclude: str = "") -> list[dict]:
    """修复建议：按列重合数、再按与声明名（或对象名）的相似度排序的候选表。"""
    lowered = {k.lower() for k in field_keys}
    ranked = []
    for table, columns in tables.items():
        if table == exclude:
            continue
        matched = len(lowered & {c.lower() for c in columns})
        similarity = difflib.SequenceMatcher(None, name.lower(), table.lower()).ratio()
        if matched or similarity >= MIN_NAME_SIMILARITY:
            ranked.append((-matched, -similarity, table, matched))
    return [{"table": table, "matched": matched} for _, _, table, matched in sorted(ranked)[:MAX_CANDIDATES]]


def _declared_binding(
    key: str, parsed: dict, field_keys: list[str], tables: dict[str, list[str]], by_lower: dict[str, str],
) -> tuple[str, str, dict | None]:
    """「# 数据来源」声明 → (绑定的表, 改写提示, 问题)。问题为 {kind, message, candidates}。"""
    declared = parsed["declared_table"]
    if not tables:
        return "", "", {
            "kind": "declared_unverifiable",
            "message": f"对象「{key}」声明的来源表「{declared}」无法核对：项目里还没有数据表"
                       "（schema 表格资料），发布包暂不绑定该对象",
            "candidates": [],
        }
    table = by_lower.get(declared.lower(), "")
    if table:
        return table, "", None
    table = _first_existing(parsed["declared_mentions"], by_lower)
    if table:
        return table, (
            f"对象「{key}」的「# 数据来源」首行不是表名，已按句中的「{table}」绑定；"
            "建议首行只写表名，说明另起一行"
        ), None
    return "", "", {
        "kind": "declared_missing",
        "message": f"对象「{key}」声明的来源表「{declared}」在数据表里不存在"
                   f"（现有表：{'、'.join(sorted(tables))}）；"
                   "「# 数据来源」首行只写表名（Sheet 名，逐字相同）或「无」，说明另起一行",
        "candidates": _candidates(field_keys, declared, tables),
    }


def _may_infer(tables: dict[str, list[str]], parsed: dict) -> bool:
    """未声明来源表时能否按重合度推断：「无」是显式不落库，没有数据表或属性也无从推断。"""
    if parsed["declared_none"]:
        return False
    return bool(tables and parsed["fields"])


def resolve_object_tables(
    root: Path,
    tables: dict[str, list[str]] | None = None,
    strict_declared: bool = True,
) -> dict:
    """对象文档 × 数据表 → 绑定结果。problems 为阻断级问题，warnings 只提示。

    tables 缺省取 schema 表格资料；发布按提交导出的只读树算好后传入。
    strict_declared=False 时，声明的表不在本轮数据里、或一列都对不上，只提示不阻断——
    该对象不进 mapping，与导入库依然一致。没有任何数据表时声明无从核对，恒为提示。
    「# 数据来源」首行写成说明句时，按句中反引号标出、且真实存在的第一个表名绑定并提示改写。

    issues 是 problems 与"声明无从核对"的结构化版本，供发布前逐对象修复：
    {object, kind: declared_missing|no_columns|declared_unverifiable, blocking, message,
    declared, table, candidates[{table, matched}]}。
    """
    tables = read_source_tables(root) if tables is None else tables
    by_lower = {name.lower(): name for name in tables}
    objects: dict[str, dict] = {}
    problems: list[str] = []
    warnings: list[str] = []
    issues: list[dict] = []

    def report(key: str, issue: dict, declared: str, table: str = "") -> None:
        blocking = strict_declared and issue["kind"] != "declared_unverifiable"
        (problems if blocking else warnings).append(issue["message"])
        issues.append({"object": key, **issue, "blocking": blocking, "declared": declared, "table": table})

    for key, text in read_object_texts(root).items():
        parsed = parse_object_doc(text)
        fields = parsed["fields"]
        field_keys = [f["key"] for f in fields]
        declared = parsed["declared_table"]
        table, matched_by = "", ""
        reported = False
        if declared:
            table, note, issue = _declared_binding(key, parsed, field_keys, tables, by_lower)
            matched_by = "declared" if table else ""
            if note:
                warnings.append(note)
            if issue:
                report(key, issue, declared)
                reported = True
        elif _may_infer(tables, parsed):
            table = _match_table(field_keys, tables, [f["key"] for f in fields if f["primary"]])
            matched_by = "inferred" if table else ""

        columns = tables.get(table, [])
        lowered_columns = {c.lower(): c for c in columns}
        # fields 保留全部声明属性（发布包的 `# 字段` 段要完整），column 只在对上列时填
        fields = [{**f, "column": lowered_columns.get(f["key"].lower(), "")} for f in fields]
        matched = [f for f in fields if f["column"]]
        missing = [f["key"] for f in fields if not f["column"] and not _is_non_stored(f)] if table else []
        if table and not matched:
            report(key, {
                "kind": "no_columns",
                "message": f"对象「{key}」绑定的表「{table}」没有一列能对上属性英文名",
                "candidates": _candidates(field_keys, declared or key, tables, exclude=table),
            }, declared, table)
            reported = True
            table, matched_by = "", ""
            fields = [{**f, "column": ""} for f in fields]
            matched, missing = [], []
        objects[key] = {
            "table": table,
            "matched_by": matched_by,
            "matched_columns": len(matched),
            "fields": fields,
            "missing_columns": missing,
            "primary_key": [f["key"] for f in matched if f["primary"]],
            "declared_table": declared,
        }
        if not table and not reported:
            warnings.append(f"对象「{key}」没有绑定真实数据表，数据问答无法命中该对象")
        elif missing:
            warnings.append(
                f"对象「{key}」有 {len(missing)} 个属性在表「{table}」里没有同名列："
                f"{'、'.join(missing[:5])}{'…' if len(missing) > 5 else ''}"
            )

    bound_tables = {item["table"] for item in objects.values() if item["table"]}
    for name in sorted(set(tables) - bound_tables):
        warnings.append(f"数据表「{name}」没有任何对象绑定，问答时不会被查询")
    return {
        "tables": {name: list(columns) for name, columns in tables.items()},
        "objects": objects,
        "bound": len(bound_tables),
        "problems": problems,
        "warnings": warnings,
        "issues": issues,
    }


def _bare_token(line: str) -> bool:
    return bool(re.fullmatch(r"`?[^\s`]+`?", line))


def set_declared_source(text: str, table: str) -> str:
    """把对象文档「# 数据来源」的声明行改成 table（空串写「无」）。

    原声明行是说明句时改成 `>` 引用说明留在段内，其余说明行与模板占位以外的内容原样保留；
    文档没有该段时插在「# 英文名称」段之后（没有则在「# 对象属性清单」之前，再没有则追加到末尾）。
    """
    value = table.strip() or "无"
    lines = text.replace("\r\n", "\n").split("\n")
    heading = f"# {SOURCE_SECTION}"
    start = next((i for i, line in enumerate(lines) if line.strip() == heading), -1)
    if start < 0:
        after = next((i for i, line in enumerate(lines) if line.strip() == "# 英文名称"), -1)
        if after >= 0:
            at = next((i for i in range(after + 1, len(lines)) if re.match(r"^#\s+", lines[i])), len(lines))
        else:
            at = next((i for i, line in enumerate(lines) if line.strip() == f"# {FIELDS_SECTION}"), len(lines))
        head = lines[:at]
        while head and not head[-1].strip():
            head.pop()
        block = [heading, value, ""]
        return "\n".join(head + ([""] if head else []) + block + lines[at:]).rstrip("\n") + "\n"

    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^#\s+", lines[i])), len(lines))
    body = re.sub(r"\{[^{}]*\}", "", "\n".join(lines[start + 1:end]), flags=re.S).split("\n")
    replaced = False
    for index, line in enumerate(body):
        candidate = line.strip().lstrip("-*").strip()
        if _is_source_note(candidate):
            continue
        keep = [] if _bare_token(candidate) else [f"> {candidate}"]
        body[index:index + 1] = [value, *keep]
        replaced = True
        break
    if not replaced:
        body.insert(0, value)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    tail = [""] if end < len(lines) else []
    return "\n".join(lines[:start + 1] + body + tail + lines[end:]).rstrip("\n") + "\n"


def object_data_mappings(resolution: dict, graph: bool = False) -> dict:
    """绑定结果 → mappings/object-data-mappings.json（modeling 包契约 §18）。

    graph=True 按 modeling 图模式（packageMappingResource）导出：resource 为对象 key
    （即 Neo4j Label），fields 覆盖对象 `# 字段` 的全部属性（图导入逐字段核对）；
    Sheet 与列仍由 sources[] 声明。NL2SQL 模式 resource 为表名，fields 只收对上列的属性。
    """
    objects: dict[str, dict] = {}
    for key, item in resolution["objects"].items():
        # 只有对上真实列的属性才进 mapping：写进去一个库里没有的列会让 nl2sql 生成废 SQL
        matched = [f for f in item["fields"] if f["column"]]
        if not item["table"] or not matched:
            continue
        fields = {
            field["key"]: {
                "source": field["key"],
                **({"source_column": field["column"]} if field["column"] else {}),
                "type": field["type"] or "string",
                "required": bool(field["primary"]),
                "readable": True,
            }
            for field in (item["fields"] if graph else matched)
        }
        primary_key = item["primary_key"] or [matched[0]["key"]]
        objects[key] = {
            "provider_alias": PROVIDER_ALIAS,
            "resource": key if graph else item["table"],
            "primary_key": primary_key,
            "fields": fields,
            "sources": [{
                "role": "primary",
                "resource": item["table"],
                # source_column 恒写：modeling 导入靠 sources[].fields 回填 sourceColumn，
                # 省略同名列会让 nl2sql 拿不到列绑定
                "fields": {f["key"]: {"source_column": f["column"]} for f in matched},
            }],
            # 问答只读，行数上限对齐 modeling 渲染器的保守默认
            "policy": {"read_only": True, "max_rows": 1000, "require_user_filter": []},
        }
    # schema_version 在契约里是字符串；写成数字会被 modeling 当成非法版本
    return {"schema_version": "0.1", "default_provider_alias": PROVIDER_ALIAS, "objects": objects}


def _cell(value: str) -> str:
    text = (value or "").strip().replace("|", "\\|")
    return text or " "


def render_fields_section(fields: list[dict]) -> str:
    """「对象属性清单」→ modeling 包契约的 `# 字段` 表（前六列按位解析，第七列留给属性规则）。"""
    rows = "\n".join(
        # 主键即必填：契约里没有独立的必填列，主键列是唯一可判定的来源
        f"| {_cell(f['key'])} | {_cell(f['cn'])} | {_cell(f['type'])} | "
        f"{'是' if f['primary'] else '否'} | {_cell(f['desc'])} | {_cell(f['alias'])} | {_cell(f['rule'])} |"
        for f in fields
    )
    return (
        "# 字段\n\n"
        "| 字段 key | 中文名 | 类型 | 必填 | 业务说明 | 别名 | 属性规则 |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"{rows}\n"
    )


def rewrite_object_body(body: str, fields: list[dict]) -> str:
    """发布时把「对象属性清单」整段换成 `# 字段` 段：同一份属性，两端都能解析。

    模板里的填写指引 blockquote 随原段一起去掉——发布包不该携带作者指引。
    """
    if not fields:
        return body
    lines = body.replace("\r\n", "\n").split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == f"# {FIELDS_SECTION}")
    except StopIteration:
        return body
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^#\s+", lines[i])),
        len(lines),
    )
    return "\n".join(lines[:start] + render_fields_section(fields).split("\n") + lines[end:])


def _relation_node(value: str) -> str:
    """关系端点：`[[对象]]`（可带别名）取对象全名，「当前对象名」归一为「当前对象」，其余原样。

    按元模型解析；端点带 `#` 时保留全名，存量文件名里带 `#` 的对象（如 `1#油泵`）才连得上。
    """
    link = mm.parse_link(value)
    name = link.full_name if link else value
    return CURRENT_OBJECT if name in _CURRENT_OBJECT_ALIASES else name


def _endpoint(name: str, object_texts) -> str:
    """关系端点全名 → 对象 key：先按全名，再去掉 `#锚点` 按名称（元模型 resolve）；都对不上时原样。"""
    return mm.resolve(name, object_texts) or name


def _split_relation_arrows(value: str) -> list[str]:
    """只拆分最外层箭头；字段说明括号里的箭头不是关系分隔符。"""
    parts: list[str] = []
    start = 0
    brackets: list[str] = []
    closing = {"(": ")", "（": "）", "[": "]", "【": "】"}
    index = 0
    while index < len(value):
        char = value[index]
        if char in closing:
            brackets.append(closing[char])
            index += 1
            continue
        if char in closing.values():
            if not brackets or brackets.pop() != char:
                raise ValueError("括号未配对")
            index += 1
            continue
        arrow = ""
        if not brackets and char == "→":
            arrow = char
        elif not brackets and value.startswith("->", index):
            arrow = "->"
        if arrow:
            parts.append(value[start:index].strip())
            index += len(arrow)
            start = index
            continue
        index += 1
    if brackets:
        raise ValueError("括号未配对")
    parts.append(value[start:].strip())
    return parts


def _relation_name_and_note(value: str) -> tuple[str, str]:
    """关系名与末尾括注分离；没有括注时括注为空。"""
    match = re.fullmatch(r"([^()（）]+)[（(]([^()（）]*)[）)]", value.strip())
    if not match:
        return value.strip(), ""
    name, note = (part.strip() for part in match.groups())
    return name, note


def _missing_field(name: str, fields: set[str]) -> bool:
    """清单为空说明这次拿不到该对象正文，此时不判缺失（发布包校验仍会核对字段存在）。"""
    return bool(fields) and name not in fields


def _join_side(side: str, own: set[str], other: set[str]) -> str:
    """括注箭头一侧 → 字段名。`表名.字段` 去掉前缀；前缀只认得是另一端的表时说明方向写反了，不认。"""
    names = _IDENTIFIER_RE.findall(side)
    if len(names) == 1:
        return names[0]
    prefixed = _PREFIXED_FIELD_RE.fullmatch(side.strip())
    if prefixed:
        prefix = prefixed.group(1).lower()
        if prefix in own or prefix not in other:
            return prefixed.group(2)
        raise ValueError(
            f"关联字段「{side.strip()}」的前缀是另一端对象的表，请按箭头方向写成：关系名（源字段 → 目标字段）"
        )
    raise ValueError("关联字段不明确，请写成：关系名（源字段 → 目标字段）")


def _relation_join(
    note: str, source_fields: set[str], target_fields: set[str],
    source_names: set[str] = frozenset(), target_names: set[str] = frozenset(),
) -> tuple[str | None, str | None]:
    """括注 → (源对象字段, 目标对象字段)。判据是两端的属性清单，不是括注的措辞。

    括注里的箭头是显式声明，按方向取两侧字段，属性清单只用于纠错。没有箭头时只认两端清单
    都有的那个标识符：取不到就是一句纯说明，取到多个则无从判断该连哪一对。
    箭头两侧可写成 `表名.字段`；*_names 是两端的对象名与声明表名（小写），用来核对前缀。
    """
    if not note:
        return None, None
    if "→" in note or "->" in note:
        sides = re.split(r"→|->", note)
        if len(sides) != 2:
            raise ValueError("关联字段不明确，请写成：关系名（源字段 → 目标字段）")
        source_join = _join_side(sides[0], source_names, target_names)
        target_join = _join_side(sides[1], target_names, source_names)
        if _missing_field(source_join, source_fields) or _missing_field(target_join, target_fields):
            raise ValueError(f"关联字段 {source_join} → {target_join} 不在两端对象的属性清单里")
        return source_join, target_join
    shared = [
        name for name in _IDENTIFIER_RE.findall(note)
        if name in source_fields and name in target_fields
    ]
    if len(shared) > 1:
        raise ValueError("关联字段不明确，请写成：关系名（源字段 → 目标字段）")
    return (shared[0], shared[0]) if shared else (None, None)


def _relation_table_cells(line: str) -> list[str]:
    value = line.strip().removeprefix("|").removesuffix("|")
    cells: list[str] = []
    current = ""
    escaped = False
    for char in value:
        if escaped:
            current += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append(current.strip())
            current = ""
        else:
            current += char
    cells.append(current.strip())
    return cells


def _relation_table_cell(headers: list[str], cells: list[str], *names: str) -> str:
    for name in names:
        if name in headers:
            index = headers.index(name)
            return cells[index] if index < len(cells) else ""
    return ""


def _parse_relation_table(text: str, diagnostics: list[str] | None) -> list[dict]:
    """读取标准包的关系表，保留行号、方向及可选的关联字段。"""
    out: list[dict] = []
    active = False
    headers: list[str] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if line.strip() == "# 关系":
            active, headers = True, []
            continue
        if re.match(r"^#\s+", line):
            active = False
        if not active or not line.strip().startswith("|"):
            continue
        cells = _relation_table_cells(line)
        if not headers:
            headers = cells
            if not ({"关系", "关系名"} & set(headers) and {"目标对象", "目标"} & set(headers)):
                problem = f"第 {line_number} 行：关系表缺少关系名或目标对象列"
                if diagnostics is None:
                    raise ValueError(problem)
                diagnostics.append(problem)
                active = False
            continue
        if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue

        name = _relation_table_cell(headers, cells, "关系", "关系名")
        target = _relation_table_cell(headers, cells, "目标对象", "目标")
        if _is_empty(name) and _is_empty(target):
            continue
        if _is_empty(name) or _is_empty(target):
            problem = f"第 {line_number} 行：关系表缺少关系名或目标对象"
            if diagnostics is None:
                raise ValueError(problem)
            diagnostics.append(problem)
            continue
        out.append({
            "line": line_number, "source": CURRENT_OBJECT,
            "relation": name, "target": _relation_node(target),
            "note": _relation_table_cell(headers, cells, "说明"),
            "evidence": _relation_table_cell(headers, cells, "证据"),
            "sourceJoinProperty": _relation_table_cell(headers, cells, "源关联字段") or None,
            "targetJoinProperty": _relation_table_cell(headers, cells, "目标关联字段") or None,
        })
    return out


def parse_relation_definitions(text: str, diagnostics: list[str] | None = None) -> list[dict]:
    """解析工作区箭头声明及标准关系表；物理关联字段由 graph_relations 裁决。"""
    out: list[dict] = []
    active = False
    for line_number, line in enumerate(text.splitlines(), 1):
        if line.strip() == f"# {RELATIONS_SECTION}":
            active = True
            continue
        if re.match(r"^#\s+", line):
            active = False
        if not active:
            continue
        value = line.strip().lstrip("-*").strip()
        if _is_empty(value) or _NONE_WITH_NOTE_RE.match(value) or value.startswith(">"):
            continue
        try:
            parts = _split_relation_arrows(value)
            if len(parts) != 3:
                raise ValueError("关系格式应为：源对象 → 关系名 → 目标对象")
            if any(_PLACEHOLDER_RE.match(part) for part in parts):
                continue  # 模板占位符：这一行还没填写，不是被丢掉的声明
            if any(map(_is_empty, parts)):
                raise ValueError("关系格式应为：源对象 → 关系名 → 目标对象")
            name, note = _relation_name_and_note(parts[1])
            if _is_empty(name):
                raise ValueError("缺少关系名")
        except ValueError as exc:
            if diagnostics is None:
                raise ValueError(f"第 {line_number} 行：{exc}") from exc
            diagnostics.append(f"第 {line_number} 行：{exc}")
            continue
        out.append({
            "line": line_number,
            "source": _relation_node(parts[0]),
            "relation": name,
            "target": _relation_node(parts[2]),
            "note": note,
        })
    return sorted(out + _parse_relation_table(text, diagnostics), key=lambda item: item["line"])


def _relation_identity(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _relation_object_context(object_texts: dict[str, str]) -> tuple[dict, dict]:
    fields, names = {}, {}
    for key, text in object_texts.items():
        doc = parse_object_doc(text)
        fields[key] = {_relation_identity(field["key"]) for field in doc["fields"]}
        names[key] = _relation_prefixes(key, doc)
    return fields, names


def _resolved_relation_fields(item: dict, source: str, target: str, fields: dict, names: dict) -> tuple:
    source_join, target_join = item.get("sourceJoinProperty"), item.get("targetJoinProperty")
    if source_join or target_join:
        if not (source_join and target_join):
            raise ValueError("关联字段必须同时声明源字段和目标字段")
        source_join = _join_side(_relation_identity(source_join), names.get(source, set()), names.get(target, set()))
        target_join = _join_side(_relation_identity(target_join), names.get(target, set()), names.get(source, set()))
        if _missing_field(source_join, fields.get(source, set())) or _missing_field(target_join, fields.get(target, set())):
            raise ValueError(f"关联字段 {source_join} → {target_join} 不在两端对象的属性清单里")
    else:
        source_join, target_join = _relation_join(
            _relation_identity(item.get("note") or ""), fields.get(source, set()), fields.get(target, set()),
            names.get(source, set()), names.get(target, set()),
        )
    return source_join, target_join


def package_relations(
    object_texts: dict[str, str], *, allow_self_relations: bool = True,
    reject_duplicate_relations: bool = False,
) -> tuple[dict[str, list], list[str]]:
    """检查包内关系结构；严格模式拒绝规范化后的重复声明，默认归并双端声明。"""
    relations: dict[str, list] = {}
    problems: list[str] = []
    seen: dict[tuple, dict] = {}
    first_declarations: dict[tuple, str] = {}
    objects_by_identity = {_relation_identity(key): key for key in object_texts}
    fields, names = _relation_object_context(object_texts)
    for key, text in object_texts.items():
        diagnostics: list[str] = []
        items = parse_relation_definitions(text, diagnostics)
        path = f"ontology/object/{key}.md"
        problems.extend(f"{path} {problem}" for problem in diagnostics)
        for item in items:
            source_id = _relation_identity(key if item["source"] == CURRENT_OBJECT else _endpoint(item["source"], object_texts))
            target_id = _relation_identity(key if item["target"] == CURRENT_OBJECT else _endpoint(item["target"], object_texts))
            label = f"{path} 第 {item['line']} 行"
            missing = sorted({source_id, target_id} - objects_by_identity.keys())
            if missing:
                problems.append(f"{label}：关系端点「{'、'.join(missing)}」不存在")
                continue
            if _relation_identity(key) not in (source_id, target_id):
                problems.append(f"{label}：关系两端不包含当前对象")
                continue
            if not allow_self_relations and source_id == target_id:
                problems.append(f"{label}：不允许自关联「{item['relation']}」")
                continue
            source, target = objects_by_identity[source_id], objects_by_identity[target_id]
            relation = {field: value for field, value in item.items() if field not in ("line", "source")}
            relation["target"] = target
            try:
                source_join, target_join = _resolved_relation_fields(item, source, target, fields, names)
            except ValueError as exc:
                problems.append(f"{label}：{exc}")
                continue
            if source_join and target_join:
                relation.update(sourceJoinProperty=source_join, targetJoinProperty=target_join)
            triple = (source_id, _relation_identity(item["relation"]), target_id, source_join, target_join)
            if triple in seen:
                if reject_duplicate_relations:
                    problems.append(
                        f"{label}：重复关系「{source} -{item['relation']}→ {target}」；"
                        f"关联字段：{_join_text(relation)}；"
                        f"首次声明于 {first_declarations[triple]}"
                    )
                    continue
                previous = seen[triple]
                for field in ("note", "evidence"):
                    if not previous.get(field) and relation.get(field):
                        previous[field] = relation[field]
                continue
            seen[triple] = relation
            first_declarations[triple] = label
            relations.setdefault(source, []).append(relation)
    return relations, problems


def _join_text(relation: dict) -> str:
    source, target = relation.get("sourceJoinProperty"), relation.get("targetJoinProperty")
    return f"{source} → {target}" if source and target else "未声明关联字段"


def _relation_prefixes(key: str, doc: dict) -> set[str]:
    """括注 `表名.字段` 可用的前缀：对象名与「# 数据来源」声明的表名（小写）。"""
    candidates = (key, doc["declared_table"], *doc["declared_mentions"])
    return {name.lower() for name in candidates if name}


def graph_relations(
    object_texts: dict[str, str], mapped: set[str], report: dict | None = None,
) -> tuple[dict[str, list], list[str]]:
    """生成可执行关系及逐条诊断。返回的 skipped 是没导入的关系及原因，都不阻断。

    关联字段取自关系括注，判据是两端对象的属性清单（见 _relation_join）：写法不固定，但字段
    名必须在清单里，所以不靠措辞也能认出来。

    同源、同名、同目标且字段对相同的声明合并；字段对不同则保留为不同关系。
    未声明字段视为空字段对，不与显式字段对混同，也不从其他声明猜字段。

    每条关系只影响自己：写错或端点没绑数据表（概念对象数据来源写「无」是正常结论）时只跳过
    这一条并记原因，发布照常进行，原因进 report["notes"] 给用户看。
    """
    fields, names = _relation_object_context(object_texts)
    # 概念对象（数据来源写「无」）：端点未绑定时提示里点明原因，免得被当成绑定出错
    concepts: set[str] = set()
    for key, text in object_texts.items():
        doc = parse_object_doc(text)
        if doc["declared_none"]:
            concepts.add(key)
    relations: dict[str, list] = {}
    skipped: list[str] = []
    unbound = 0
    seen: dict[tuple, dict] = {}
    triples: set[tuple] = set()
    declared = parsed = duplicates = 0

    for key, text in object_texts.items():
        problems: list[str] = []
        items = parse_relation_definitions(text, problems)
        declared += len(items) + len(problems)
        parsed += len(items)
        skipped.extend(f"对象「{key}」{problem}" for problem in problems)
        for item in items:
            source = item["source"] or ""
            name = item["relation"] or ""
            target = item["target"] or ""
            source = key if source == CURRENT_OBJECT else _endpoint(source, object_texts)
            target = key if target == CURRENT_OBJECT else _endpoint(target, object_texts)
            triple = (source, name, target)
            label = f"对象「{key}」第 {item['line']} 行"
            if key not in (source, target):
                triples.add(triple)
                skipped.append(f"{label}：关系两端不包含当前对象")
                continue
            if not {source, target} <= mapped:
                triples.add(triple)
                missing = sorted({source, target} - mapped)
                absent = [item for item in missing if item not in object_texts]
                if absent:
                    skipped.append(f"{label}：关系端点「{'、'.join(absent)}」不存在")
                elif set(missing) <= concepts:
                    unbound += 1
                    skipped.append(
                        f"{label}：关系端点「{'、'.join(missing)}」的数据来源为「无」（概念对象，"
                        "图里没有该类节点），本条关系不导入"
                    )
                else:
                    unbound += 1
                    skipped.append(f"{label}：关系端点「{'、'.join(missing)}」未绑定数据表，本条关系不导入")
                continue
            if not GRAPH_IDENTIFIER.fullmatch(name):
                triples.add(triple)
                skipped.append(f"{label}：关系名「{name}」不是合法的图关系名，请确认名称")
                continue
            try:
                source_join, target_join = _resolved_relation_fields(item, source, target, fields, names)
            except ValueError as exc:
                triples.add(triple)
                skipped.append(f"{label}：{exc}")
                continue
            triple = tuple(map(_relation_identity, (source, name, target))) + (source_join, target_join)
            triples.add(triple)
            relation = {
                "relation": name,
                "target": target,
                "sourceJoinProperty": source_join,
                "targetJoinProperty": target_join,
            }
            if item["note"]:
                relation["note"] = item["note"]
            if item.get("evidence"):
                relation["evidence"] = item["evidence"]
            if triple in seen:
                duplicates += 1
                continue
            seen[triple] = {"edge": relation, "label": label}
            relations.setdefault(source, []).append(relation)
    if report is not None:
        report.update(
            relations_declared=declared, relations_parsed=parsed,
            # 去重后的关系条数：两端各声明一次只算一条，和 relations_exported 同一量纲
            relations_distinct=len(triples),
            relations_exported=sum(map(len, relations.values())),
            # relations_skipped 是没导出的总数；其中端点未绑定数据表的另记一个数，便于对账
            relations_skipped=len(skipped), relations_unbound=unbound,
            relations_duplicates=duplicates, notes=skipped,
        )
    return relations, skipped


def render_relations_section(relations: list[dict | tuple[str, str]]) -> str:
    """保留原四列的位置，在末尾增加可选的关联字段。"""
    rows = []
    for relation in relations:
        if isinstance(relation, dict):
            cells = (
                relation.get("relation", ""),
                relation.get("target", ""),
                relation.get("note") or "",
                relation.get("evidence") or "",
                relation.get("sourceJoinProperty") or "",
                relation.get("targetJoinProperty") or "",
            )
            rows.append("| " + " | ".join(map(_cell, cells)) + " |\n")
        else:
            name, target = relation
            rows.append(f"| {name} | {target} | | | | |\n")
    return (
        "# 关系\n\n"
        "| 关系 | 目标 | 说明 | 证据 | 源关联字段 | 目标关联字段 |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        + "".join(rows)
    )


def rewrite_relations_section(body: str, relations: list[dict]) -> str:
    """把箭头声明及旧标准表替换成同一份关系表，避免旧表遮住新表。"""
    lines: list[str] = []
    active = False
    for line in body.splitlines():
        if line.strip() in ("# 关系", "# 关联对象"):
            active = True
            continue
        if re.match(r"^#\s+", line):
            active = False
        if not active:
            lines.append(line)
    result = "\n".join(lines).rstrip("\n")
    return result + ("\n\n" + render_relations_section(relations) if relations else "\n")
