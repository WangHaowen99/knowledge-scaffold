"""对象 ↔ 真实数据表绑定：发布包「来源Sheet」与 object-data-mappings.json 的单一来源。

对象文档承载业务语义（「对象属性清单」的英文名称列就是真实列名），真实表名来自打了
eval_data / schema 标签的表格资料（一个 Sheet = 一张表）。两者在这里对齐一次，三处共用：

- 发布包导出：objects/index.md 的「来源Sheet」列、对象文档的 `# 字段` 段，以及
  mappings/object-data-mappings.json —— modeling 导入后据此生成 sourceRefs 与 sourceColumn，
  nl2sql 才有表名和列名可用；
- 评测前置门禁：数据集模式下没有任何对象绑定真实表时直接拦下，避免跑出"一次库都没查"的分数；
- 资料区对账：并进 /eval-data/alignment 的返回。

绑定优先级：对象文档「# 数据来源」的显式声明 > 属性英文名与 Sheet 列头的唯一最佳重合。
推断只在重合列数达标且候选唯一时成立，够不着就留空（来源Sheet 写 UNBOUND），不猜。

评测导入时数据表来源换成「本轮实际导入的工作簿」（eval_import），mapping 与导入库同源；
Neo4j 图导入的包按 modeling 图模式契约导出（object_data_mappings(graph=True)）。
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

# modeling 契约：来源Sheet 为 "—" 表示该对象不绑定物理表（package-import 按此跳过）
UNBOUND = "—"
# 包内部逻辑数据源别名，不是项目评测数据源的 provider；取 modeling 草稿同款默认值
PROVIDER_ALIAS = "data"
# 推断绑定的最低列命中数：低于此宁可不绑，错绑比不绑更难发现
MIN_INFERRED_COLUMNS = 2
# modeling 图导入对属性名与关系名的标识符约束（evaluation/data-source.ts GRAPH_IDENTIFIER）
GRAPH_IDENTIFIER = re.compile(r"^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*$")
RELATIONS_SECTION = "关联对象"
SOURCE_PURPOSES = ("eval_data", "schema")
TABLE_SUFFIXES = (".xlsx", ".xlsm", ".csv")

SOURCE_SECTION = "数据来源"
FIELDS_SECTION = "对象属性清单"

_EMPTY_MARKERS = {"", "无", "-", "—", "–", "n/a"}
# 模板占位符 `{待填写：…}` 可能跨行，只认起始花括号：真实表名和列名都不会以 { 开头
_PLACEHOLDER_RE = re.compile(r"^\{")
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


def parse_object_doc(text: str) -> dict:
    """对象文档 → {declared_table, fields[]}。fields 的 key 即属性英文名（真实列名）。"""
    declared = ""
    # 模板占位符 `{待填写：…}` 可能跨多行，先整块剔除再逐行找表名
    source_text = re.sub(r"\{[^{}]*\}", " ", _section(text, SOURCE_SECTION), flags=re.S)
    for line in source_text.split("\n"):
        candidate = line.strip().lstrip("-*").strip()
        if candidate and not candidate.startswith(("|", ">")) and not _is_empty(candidate):
            declared = candidate
            break

    headers, rows = _table(_section(text, FIELDS_SECTION))
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
    return {"declared_table": declared, "fields": fields}


def read_table_headers(path: Path) -> dict[str, list[str]]:
    """表格文件 → {Sheet 名: 表头列}。口径与工具箱 check_eval_data.py 一致：
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
    """打了 eval_data / schema 标签的表格资料 → {表名: 列头}。"""
    directives_path = root / "resource" / "materials.json"
    try:
        directives = json.loads(directives_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(directives, dict):
        return {}
    tables: dict[str, list[str]] = {}
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


def _match_table(field_keys: list[str], tables: dict[str, list[str]]) -> str:
    """属性英文名与各表列头的重合度 → 唯一最佳表名；并列第一或不达标时返回空，不猜。"""
    lowered = {k.lower() for k in field_keys}
    scores = sorted(
        ((len(lowered & {c.lower() for c in columns}), name) for name, columns in tables.items()),
        reverse=True,
    )
    if not scores or scores[0][0] < MIN_INFERRED_COLUMNS:
        return ""
    if len(scores) > 1 and scores[1][0] == scores[0][0]:
        return ""
    return scores[0][1]


def resolve_object_tables(
    root: Path,
    tables: dict[str, list[str]] | None = None,
    hints: dict[str, str] | None = None,
    strict_declared: bool = True,
) -> dict:
    """对象文档 × 数据表 → 绑定结果。problems 为阻断级问题，warnings 只提示。

    tables 缺省取资料区 eval_data/schema 表格；评测导入传入本轮实际导入的表，
    hints 为已知的对象→表对应（mock 种子按对象生成，一张表就是一个对象）。
    strict_declared=False 时，声明的表不在本轮数据里只提示不阻断——该对象不进 mapping，
    与导入库依然一致。
    """
    tables = read_source_tables(root) if tables is None else tables
    hints = hints or {}
    by_lower = {name.lower(): name for name in tables}
    objects: dict[str, dict] = {}
    problems: list[str] = []
    warnings: list[str] = []

    object_dir = root / "ontology" / "object"
    for doc in sorted(object_dir.glob("*.md")) if object_dir.is_dir() else []:
        if doc.name == "index.md":
            continue
        try:
            parsed = parse_object_doc(doc.read_text(encoding="utf-8"))
        except OSError:
            continue
        key = doc.stem
        fields = parsed["fields"]
        declared = parsed["declared_table"]
        table, matched_by = "", ""
        if declared:
            table = by_lower.get(declared.lower(), "")
            if table:
                matched_by = "declared"
            else:
                (problems if strict_declared else warnings).append(
                    f"对象「{key}」声明的来源表「{declared}」在评测数据表里不存在"
                    f"（现有表：{'、'.join(sorted(tables)) or '无'}）"
                )
        elif hints.get(key) in tables:
            table, matched_by = hints[key], "seed"
        elif tables and fields:
            table = _match_table([f["key"] for f in fields], tables)
            matched_by = "inferred" if table else ""

        columns = tables.get(table, [])
        lowered_columns = {c.lower(): c for c in columns}
        # fields 保留全部声明属性（发布包的 `# 字段` 段要完整），column 只在对上列时填
        fields = [{**f, "column": lowered_columns.get(f["key"].lower(), "")} for f in fields]
        matched = [f for f in fields if f["column"]]
        missing = [f["key"] for f in fields if table and not f["column"]]
        if table and not matched:
            problems.append(f"对象「{key}」绑定的表「{table}」没有一列能对上属性英文名")
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
        if not table:
            warnings.append(f"对象「{key}」没有绑定真实数据表，数据问答无法命中该对象")
        elif missing:
            warnings.append(
                f"对象「{key}」有 {len(missing)} 个属性在表「{table}」里没有同名列："
                f"{'、'.join(missing[:5])}{'…' if len(missing) > 5 else ''}"
            )

    bound_tables = {item["table"] for item in objects.values() if item["table"]}
    for name in sorted(set(tables) - bound_tables):
        warnings.append(f"数据表「{name}」没有任何对象绑定，评测时不会被查询")
    return {
        "tables": {name: list(columns) for name, columns in tables.items()},
        "objects": objects,
        "bound": len(bound_tables),
        "problems": problems,
        "warnings": warnings,
    }


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
            # 评测与问答只读，行数上限对齐 modeling 渲染器的保守默认
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


def parse_relations(text: str) -> list[tuple[str, str, str]]:
    """「# 关联对象」→ [(源, 关系名, 目标)]；「当前对象」按对象自身解析由调用方替换为 key。

    模板写法：`- 当前对象 → 关系名 → [[对象A]]` / `- [[对象B]] → 关系名 → 当前对象`。
    """
    out = []
    for line in _section(text, RELATIONS_SECTION).split("\n"):
        parts = [p.strip() for p in re.split(r"→|->", line.strip().lstrip("-*").strip())]
        if len(parts) != 3 or any(not p or _is_empty(p) for p in parts):
            continue

        def node(value: str) -> str:
            link = re.fullmatch(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]", value)
            return link.group(1).strip() if link else value

        out.append((node(parts[0]), parts[1], node(parts[2])))
    return out


def graph_relations(object_texts: dict[str, str], mapped: set[str]) -> tuple[dict[str, list], list[str]]:
    """对象文档的关联对象 → 图导入可用的出边：{源对象: [(关系名, 目标)]}，以及被跳过的原因。

    modeling 图导入校验包内全部关系（目标须存在、关系名须为合法标识符），不合规的一条
    就会让整包映射失败，所以这里只收两端都已绑定数据表、关系名合法的关系。
    """
    relations: dict[str, list] = {}
    skipped: list[str] = []
    seen = set()
    for key, text in object_texts.items():
        for source, name, target in parse_relations(text):
            source = key if source == "当前对象" else source
            target = key if target == "当前对象" else target
            if key not in (source, target) or (source, name, target) in seen:
                continue
            seen.add((source, name, target))
            if source not in mapped or target not in mapped:
                continue  # 一端没有数据表：图里没有该类节点，关系无从建立
            if not GRAPH_IDENTIFIER.match(name):
                skipped.append(f"关系「{source} -{name}→ {target}」名称不是合法的图关系名，未导入")
                continue
            relations.setdefault(source, []).append((name, target))
    return relations, skipped


def render_relations_section(relations: list[tuple[str, str]]) -> str:
    """出边 → modeling 包契约的 `# 关系` 表（关系 | 目标 | 说明）。"""
    rows = "".join(f"| {name} | {target} | |\n" for name, target in relations)
    return "# 关系\n\n| 关系 | 目标 | 说明 |\n| --- | --- | --- |\n" + rows
