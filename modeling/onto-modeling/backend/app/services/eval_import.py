"""评测数据一键导入：把选中的数据整理成一份规范化工作簿，MySQL 评测库与 Neo4j 图库共用。

数据来源是数据源里手动上传的业务数据表（Excel，可多份）或 mock 示例数据种子。同一份表结构同时决定：

- 导入库里建什么表、什么列（modeling quick-import / graph-import 按 Sheet 名与表头建表建节点）；
- 发布包 mappings 里写什么表、什么列（package_mapping 以它为数据表来源重新绑定）。

两者同源，mapping 与库字段一致由构造保证；导出后 verify_mapping 再逐项核对一遍兜底。

整理规则（都在进 modeling 之前完成，避免导入失败或列名漂移）：

- 表头去首尾空白；空表头列丢弃；大小写不敏感重名、超 64 字符（MySQL 标识符上限）阻断；
- 没有数据行的 Sheet 不导入（modeling 也不会建表），绑定到它的对象随之不进 mapping；
- 日期：整列都是完整且合法的日期/日期时间（Excel 日期单元格或 2024/1/5、2024年1月5日
  之类文本）时统一写成 `YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM:SS`；年月、带文字后缀、非法日期
  等保持文本原样——MySQL 严格模式下 DATETIME 列对这些值整批拒绝；
- mock 种子的中文表头按对象文档换成属性英文名，Sheet 名换成对象声明的来源表名。
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import zipfile
from pathlib import Path

from .. import config
from . import eval_data_files, package_mapping

WORKBOOK_NAME = "eval-data.xlsx"
MAX_FILES = 20
MAX_IDENTIFIER_LENGTH = 64  # MySQL 表名/列名上限
MAX_SHEET_NAME_LENGTH = 31  # Excel Sheet 名上限
_SHEET_FORBIDDEN = re.compile(r"[\[\]:*?/\\]")
_DATE_TEXT = re.compile(
    r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"
    r"(?:(?:\s+|T)(\d{1,2})[:：](\d{1,2})(?:[:：](\d{1,2})(?:\.\d{1,6})?)?)?$"
)
# modeling 解析层判定日期列的前缀规则：整列都命中时会被建成 DATETIME
_MODELING_DATE_PREFIX = re.compile(r"^\d{4}[-/]\d{1,2}")
_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")
_CJK = re.compile(r"[一-龥]")


class EvalImportError(ValueError):
    """导入数据不合规（接口层映射为 400），problems 逐条列出。"""

    def __init__(self, problems: list[str]):
        super().__init__("；".join(problems[:5]))
        self.problems = problems


class Table:
    def __init__(self, name: str, columns: list[str], rows: list[list], source: str):
        self.name = name
        self.columns = columns
        self.rows = rows
        self.source = source
        self.date_columns: list[str] = []
        self.text_date_columns: list[str] = []

    def summary(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "columns": self.columns,
            "rows": len(self.rows),
            "date_columns": self.date_columns,
            "text_date_columns": self.text_date_columns,
        }


# ---- 读取 ----


def _filled(value) -> bool:
    return value is not None and str(value).strip() != ""


def _cell_text(value) -> str:
    """单元格 → modeling 解析后同形的文本：整数值的浮点不留小数尾巴（JS 数字字符串化口径）。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _cell_class(value) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return "empty"
    if _NUMERIC.match(text):
        return "num"
    return "cjk" if _CJK.search(text) else "code"


def _raw_length(row: list) -> int:
    """行的有效宽度（到最后一个非空单元格），对应 modeling 读到的行数组长度。"""
    filled = [i for i, v in enumerate(row) if _filled(v)]
    return filled[-1] + 1 if filled else 0


def _is_note_row(header: list[str], second: list, third: list) -> bool:
    """次行注释判定，与 modeling parseExcel 同一口径：多数非数字且含中文、且与第三行形态迥异。"""
    if _raw_length(second) != len(header):
        return False
    texts = ["" if v is None else str(v).strip() for v in second[: len(header)]]
    heavy = sum(1 for v in texts if v and not _NUMERIC.match(v)) >= len(header) / 2
    if not heavy or not any(re.search(r"[一-龥：:（(]", v) for v in texts):
        return False
    similar = sum(
        1 for c in range(len(header))
        if _cell_class(texts[c]) == _cell_class(third[c] if c < len(third) else None)
    ) / max(len(header), 1)
    return similar < 0.6


def _sheet_table(sheet_name: str, raw_rows: list[tuple], source: str) -> tuple[Table | None, list[str]]:
    rows = [list(r) for r in raw_rows if any(map(_filled, r))]
    if not rows:
        return None, []
    header = [_cell_text(v) for v in rows[0]]
    while header and not header[-1]:
        header.pop()
    keep = [i for i, name in enumerate(header) if name]
    warnings = []
    if len(keep) < len(header):
        warnings.append(f"Sheet「{sheet_name}」有 {len(header) - len(keep)} 列没有列名，已忽略")
    if len(rows) > 2 and _is_note_row(header, rows[1], rows[2]):
        rows = rows[:1] + rows[2:]  # modeling 同样会跳过这行注释，不算数据
    data = [[r[i] if i < len(r) else None for i in range(len(header))] for r in rows[1:]]
    columns = [header[i] for i in keep]
    table_rows = [[row[i] for i in keep] for row in data]
    table_rows = [r for r in table_rows if any(map(_filled, r))]
    return Table(sheet_name, columns, table_rows, source), warnings


def read_workbook(path: Path, source: str) -> tuple[list[Table], list[str]]:
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    tables, warnings = [], []
    try:
        for sheet in workbook.worksheets:
            table, sheet_warnings = _sheet_table(
                sheet.title, list(sheet.iter_rows(values_only=True)), source
            )
            warnings += sheet_warnings
            if table is not None:
                tables.append(table)
    finally:
        workbook.close()
    return tables, warnings


def _mock_seed(root: Path, out: Path) -> None:
    """ontology/data-mock → Excel 种子。

    工具箱脚本是单一实现，在进程内加载后直接调用 build_seed，不起子进程。
    """
    import importlib.util

    script = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts" / "mock_to_eval_seed.py"
    spec = importlib.util.spec_from_file_location("onto_mock_to_eval_seed", script)
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    try:
        seed.build_seed(root, out, log=lambda _line: None)
    except seed.SeedError as exc:
        raise RuntimeError(f"示例数据种子生成失败：{exc}") from exc


def _mock_tables(root: Path) -> tuple[list[Table], dict[str, str], list[str]]:
    """mock 种子 → 表：中文表头换属性英文名，Sheet 名换对象声明的来源表。"""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="onto-eval-seed-") as tmp:
        seed = Path(tmp) / "mock-seed.xlsx"
        _mock_seed(root, seed)
        tables, warnings = read_workbook(seed, "示例数据")
    docs = {}
    object_dir = root / "ontology" / "object"
    for doc in sorted(object_dir.glob("*.md")) if object_dir.is_dir() else []:
        if doc.name != "index.md":
            docs[doc.stem] = package_mapping.parse_object_doc(doc.read_text(encoding="utf-8"))
    hints: dict[str, str] = {}
    for table in tables:
        # 脚本按 Excel 上限截断 Sheet 名，按前缀认回对象
        key = table.name if table.name in docs else next(
            (k for k in docs if k[:MAX_SHEET_NAME_LENGTH] == table.name), ""
        )
        parsed = docs.get(key)
        if parsed is None:
            warnings.append(f"示例数据「{table.name}」找不到对应的对象文档，按原表头导入")
            continue
        by_cn = {f["cn"]: f["key"] for f in parsed["fields"] if f["cn"]}
        by_key = {f["key"].lower(): f["key"] for f in parsed["fields"]}
        renamed = [by_cn.get(c) or by_key.get(c.lower()) or c for c in table.columns]
        untranslated = [c for c in table.columns if c not in by_cn and c.lower() not in by_key]
        if untranslated:
            warnings.append(
                f"示例数据「{key}」有列对不上对象属性：{'、'.join(untranslated[:5])}（按原列名导入，不进 mapping）"
            )
        table.columns = renamed
        # 首行是说明句时取句中第一个反引号名字，与 resolve_object_tables 的兜底同口径
        table.name = next(iter(parsed["declared_mentions"]), parsed["declared_table"]) or key
        hints[key] = table.name
    return tables, hints, warnings


# ---- 日期规范化 ----


def _as_datetime(value) -> tuple[dt.datetime, bool] | None:
    """值 → (时间点, 是否带时刻)；不是完整合法日期返回 None。"""
    if isinstance(value, dt.datetime):
        return value, bool(value.hour or value.minute or value.second)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day), False
    if not isinstance(value, str):
        return None
    match = _DATE_TEXT.match(value.strip())
    if not match:
        return None
    parts = [int(g) if g is not None else None for g in match.groups()]
    try:
        stamp = dt.datetime(parts[0], parts[1], parts[2], parts[3] or 0, parts[4] or 0, parts[5] or 0)
    except ValueError:
        return None
    return stamp, parts[3] is not None


def _plain(value):
    """非日期列里的日期/时间对象转文本，其余原样。"""
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S" if value.time() != dt.time() else "%Y-%m-%d")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, dt.time):
        return value.strftime("%H:%M:%S")
    if isinstance(value, str):
        return value.strip()
    return value


def normalize_dates(table: Table) -> list[str]:
    warnings = []
    for index, column in enumerate(table.columns):
        values = [row[index] for row in table.rows if _filled(row[index])]
        parsed = [_as_datetime(v) for v in values]
        if values and all(parsed):
            with_time = any(p[1] for p in parsed)
            pattern = "%Y-%m-%d %H:%M:%S" if with_time else "%Y-%m-%d"
            for row in table.rows:
                stamp = _as_datetime(row[index]) if row[index] is not None else None
                row[index] = stamp[0].strftime(pattern) if stamp else None
            table.date_columns.append(column)
            continue
        for row in table.rows:
            row[index] = _plain(row[index])
        texts = [str(v).strip() for v in values]
        if texts and all(_MODELING_DATE_PREFIX.match(t) for t in texts):
            table.text_date_columns.append(column)
            warnings.append(
                f"表「{table.name}」列「{column}」含年月或非标准日期（如 {texts[0]}），按文本导入"
            )
    return warnings


# ---- 组装 ----


def _check_tables(tables: list[Table]) -> list[str]:
    problems, seen = [], {}
    for table in tables:
        if table.name in seen:
            problems.append(f"多份数据存在同名表「{table.name}」：{seen[table.name]}、{table.source}")
        seen[table.name] = table.source
        if len(table.name) > MAX_SHEET_NAME_LENGTH or _SHEET_FORBIDDEN.search(table.name):
            problems.append(f"表名「{table.name}」不能作为 Excel Sheet 名（≤31 字符，不含 []:*?/\\）")
        lowered: dict[str, str] = {}
        for column in table.columns:
            if column.lower() in lowered:
                problems.append(f"表「{table.name}」存在重复列名：{lowered[column.lower()]} / {column}")
            lowered[column.lower()] = column
            if len(column) > MAX_IDENTIFIER_LENGTH:
                problems.append(f"表「{table.name}」列名超过 64 字符：{column[:20]}…")
    return problems


def collect(root: Path, data_source: dict) -> dict:
    """选中的数据 → 规范化后的表 + 对象绑定。problems 非空时不可导入。"""
    tables: list[Table] = []
    warnings: list[str] = []
    problems: list[str] = []
    hints: dict[str, str] = {}
    for item in (data_source.get("files") or [])[:MAX_FILES]:
        file_id = str(item.get("id") if isinstance(item, dict) else item).strip()
        label = str(item.get("name") or file_id) if isinstance(item, dict) else file_id
        path = eval_data_files.path_of(root, file_id)
        if path is None:
            problems.append(f"评测数据文件不存在或已被删除，请重新上传：{label}")
            continue
        try:
            file_tables, file_warnings = read_workbook(path, path.name)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            # 损坏或伪装成 Excel 的文件：openpyxl 抛 InvalidFileException（ValueError 子类）等
            problems.append(f"评测数据文件无法读取：{path.name}（{str(exc)[:80]}）")
            continue
        tables += file_tables
        warnings += file_warnings
    if data_source.get("include_mock"):
        try:
            mock, hints, mock_warnings = _mock_tables(root)
        except RuntimeError as exc:
            problems.append(str(exc))
        else:
            tables += mock
            warnings += mock_warnings
    for table in [t for t in tables if not t.rows]:
        warnings.append(f"表「{table.name}」没有数据行，不导入（绑定到它的对象不进 mapping）")
    tables = [t for t in tables if t.rows]
    if not tables and not problems:
        problems.append("选中的数据里没有可导入的数据行")
    problems += _check_tables(tables)
    for table in tables:
        warnings += normalize_dates(table)

    binding = package_mapping.resolve_object_tables(
        root, tables={t.name: t.columns for t in tables}, hints=hints, strict_declared=False
    )
    problems += binding["problems"]
    warnings += binding["warnings"]
    if tables and not binding["bound"]:
        problems.append(
            "没有任何对象绑定到导入的数据表，数据问答会全部落空：请在对象文档「# 数据来源」"
            "声明表名，或确认「对象属性清单」的英文名称与表头一致"
        )
    return {"tables": tables, "binding": binding, "problems": problems, "warnings": warnings}


def graph_problems(binding: dict) -> list[str]:
    """图导入额外约束：已绑定对象的全部属性名都须是合法图标识符（modeling 逐字段校验）。"""
    problems = []
    for key, item in binding["objects"].items():
        if not item["table"]:
            continue
        bad = [f["key"] for f in item["fields"] if not package_mapping.GRAPH_IDENTIFIER.match(f["key"])]
        if bad:
            problems.append(
                f"对象「{key}」的属性英文名不能作为 Neo4j 属性名：{'、'.join(bad[:5])}"
                "（只能用字母、数字、下划线或中文，且不以数字开头）"
            )
    return problems


# ---- 图导入预测：实体、实例、关系边（规则逐条对齐 modeling 的 graph-import validator）----


def _primary_source(entry: dict) -> tuple[str, dict]:
    """mapping 条目 → (来源表名, {属性: {source_column}})；评测导入恒为单一来源表。"""
    source = (entry.get("sources") or [{}])[0]
    return source.get("resource") or "", source.get("fields") or {}


def _column_position(table: Table | None, column: str) -> int:
    if table is None or not column or column not in table.columns:
        return -1
    return table.columns.index(column)


def _node_rows(
    tables: list[Table], mapping: dict
) -> tuple[dict[str, dict[int, tuple]], list[str], list[str]]:
    """每个实体会建成节点的行：{对象: {行号: 主键取值}}。

    modeling 按主键建节点：主键列任一为空的行跳过（非空单元格经类型归一不会变空，故只判空），
    同一张表里主键重复时 modeling 直接报错，所以这里按阻断问题报出来。
    """
    by_name = {t.name: t for t in tables}
    nodes: dict[str, dict[int, tuple]] = {}
    problems: list[str] = []
    warnings: list[str] = []
    for key, entry in (mapping.get("objects") or {}).items():
        table_name, fields = _primary_source(entry)
        table = by_name.get(table_name)
        if table is None:
            continue
        indexes = [
            _column_position(table, ((fields.get(prop) or {}).get("source_column") or ""))
            for prop in entry.get("primary_key") or []
        ]
        if not indexes or -1 in indexes:
            problems.append(f"实体「{key}」的主键属性在表「{table.name}」里没有对应列，图导入会失败")
            continue
        rows: dict[int, tuple] = {}
        identities: set[tuple] = set()
        blank = duplicated = 0
        for index, row in enumerate(table.rows):
            values = tuple(_cell_text(row[i]) for i in indexes)
            if not all(values):
                blank += 1
                continue
            if values in identities:
                duplicated += 1
                continue
            identities.add(values)
            rows[index] = values
        nodes[key] = rows
        if duplicated:
            problems.append(
                f"实体「{key}」在表「{table.name}」里有 {duplicated} 行主键重复，图导入会直接失败："
                "该表不是一对一扩展表，请拆成独立实体或关系"
            )
        if blank:
            warnings.append(f"实体「{key}」在表「{table.name}」有 {blank} 行主键为空，不会建成实例")
        if not rows:
            warnings.append(f"实体「{key}」在表「{table.name}」里没有一行主键完整，图里不会有这类节点")
    return nodes, problems, warnings


def _edge_counts(
    tables: list[Table], mapping: dict, relations: list[dict], nodes: dict[str, dict[int, tuple]],
) -> tuple[list[dict], list[str], list[str]]:
    """每条关系会写入的边数（modeling 的 relationPlan 同一规则）。

    同 Sheet 按行号配对（same_row）；跨 Sheet 按关联字段取值配对（field_match），关联值按
    去空白后的文本比较，一个源行匹配到 N 个目标实例就记 N 条边。只统计已建成节点的行，
    源行关联值为空或匹配不到目标都不建边。
    """
    by_name = {t.name: t for t in tables}

    def join_column(key: str, prop: str) -> tuple[Table | None, int]:
        table_name, fields = _primary_source(mapping["objects"][key])
        table = by_name.get(table_name)
        column = ((fields.get(prop) or {}).get("source_column") or "")
        return table, _column_position(table, column)

    items: list[dict] = []
    problems: list[str] = []
    warnings: list[str] = []
    for relation in relations:
        source_key, target_key = relation["sourceObject"], relation["targetObject"]
        source_rows, target_rows = nodes.get(source_key) or {}, nodes.get(target_key) or {}
        label = f"关系「{source_key} -{relation['relationName']}→ {target_key}」"
        blank = unmatched = 0
        if relation.get("matchMode") == "same_row":
            count = len(set(source_rows) & set(target_rows))
        else:
            source_table, source_index = join_column(source_key, relation["sourceJoinProperty"])
            target_table, target_index = join_column(target_key, relation["targetJoinProperty"])
            if source_index < 0 or target_index < 0:
                problems.append(f"{label}的关联字段在数据表里没有对应列，图导入会失败")
                continue
            targets: dict[str, int] = {}
            for index in target_rows:
                value = _cell_text(target_table.rows[index][target_index])
                if value:
                    targets[value] = targets.get(value, 0) + 1
            count = 0
            for index in source_rows:
                value = _cell_text(source_table.rows[index][source_index])
                if not value:
                    blank += 1
                    continue
                matched = targets.get(value, 0)
                if not matched:
                    unmatched += 1
                    continue
                count += matched
        items.append({
            "source": source_key, "relation": relation["relationName"], "target": target_key,
            "instances": count,
        })
        if blank:
            warnings.append(f"{label}有 {blank} 行关联字段为空，不建关系")
        if unmatched:
            warnings.append(f"{label}有 {unmatched} 行关联值匹配不到目标实例，不建关系")
        if not count:
            warnings.append(f"{label}没有匹配出任何关系，图里不会有这类关系")
    return items, problems, warnings


def _upload_shape(tables: list[Table], mapping: dict, declared: dict[str, list]) -> dict:
    """合成与 modeling 图导入上传预览同形的结构，让关联推断只保留一份实现。"""
    objects = []
    for key, entry in (mapping.get("objects") or {}).items():
        table_name, fields = _primary_source(entry)
        objects.append({
            "key": key,
            "defaultSheet": table_name,
            "primaryKey": list(entry.get("primary_key") or []),
            "propertyMappings": [
                {"property": prop, "sourceColumn": (value or {}).get("source_column")}
                for prop, value in fields.items()
            ],
        })
    relations = [
        {
            "sourceObject": source,
            "relationName": item["relation"],
            "targetObject": item["target"],
            "sourceJoinProperty": item.get("sourceJoinProperty"),
            "targetJoinProperty": item.get("targetJoinProperty"),
        }
        for source, items in declared.items() for item in items
    ]
    return {
        "sheets": [{"name": t.name, "columns": list(t.columns)} for t in tables],
        "schema": {"objects": objects, "relations": relations},
    }


def graph_plan(root: Path, tables: list[Table], binding: dict) -> dict:
    """图导入的本地预测（预览与运行共用）：实体、实例、关系边 + 阻断问题 + 提示。

    关联推断复用 graph_relation_mapping：这里喂本地合成的上传结构，运行时喂 modeling 真正的
    上传预览，两处只有一份推断实现。实例与边数在写库前由 modeling_eval 与 modeling 的导入
    预检逐项核对，复刻的规则一旦漂移会让评测停下，不会只在预览里留一个对不上的数字。

    关系转换不出来（声明写错、端点没绑数据表、找不到关联字段）只跳过那一条并提示，
    不影响这轮评测能不能跑；实体与主键的问题会让图导入失败，才是阻断项。
    """
    mapping = package_mapping.object_data_mappings(binding, graph=True)
    declared, relation_notes = package_mapping.graph_relations(
        package_mapping.read_object_texts(root), set(mapping["objects"]),
    )
    relations, unusable = graph_relation_mapping(_upload_shape(tables, mapping, declared))
    nodes, node_problems, node_warnings = _node_rows(tables, mapping)
    edges, edge_problems, edge_warnings = _edge_counts(tables, mapping, relations, nodes)
    instances = {key: len(rows) for key, rows in nodes.items()}
    problems = node_problems + edge_problems
    warnings = relation_notes + unusable + node_warnings + edge_warnings
    if mapping["objects"] and not sum(instances.values()):
        problems.append("导入数据里没有一行主键完整，图里不会有任何实例")
    if edges and not sum(item["instances"] for item in edges):
        warnings.append("关系都已转换，但本轮数据匹配不出任何关系，图里只会有节点")
    return {
        "mapping": mapping,
        "instances": instances,
        "edges": edges,
        "problems": problems,
        "warnings": warnings,
    }


def prepare(root: Path, data_source: dict, graph: bool = False) -> dict:
    """编排入口：规范化表 + 绑定 + 工作簿字节；不合规直接抛 EvalImportError。"""
    plan = collect(root, data_source)
    if graph:
        plan["problems"] += graph_problems(plan["binding"])
        view = graph_plan(root, plan["tables"], plan["binding"])
        plan["problems"] += view["problems"]
        plan["warnings"] += view["warnings"]
        # 写库前与 modeling 的导入预检核对：nodeCount / relationshipCount 必须等于这两个数
        plan["expected_instances"] = sum(view["instances"].values())
        plan["expected_edges"] = sum(item["instances"] for item in view["edges"])
    if plan["problems"]:
        raise EvalImportError(plan["problems"])
    plan["workbook"] = workbook_bytes(plan["tables"])
    return plan


def preview(root: Path, data_source: dict, graph: bool = False) -> dict:
    """向导预览：导入哪些表、对象绑到哪张表哪些列（即 mapping 内容）、有哪些问题。

    图模式额外给出 entities 与 relations：实体数、按主键预测的实例数、按关联字段匹配出的
    关系条数，三个数都与图里真正写入的内容同一口径（graph_plan）。
    """
    plan = collect(root, data_source)
    view: dict = {}
    if graph:
        plan["problems"] += graph_problems(plan["binding"])
        view = graph_plan(root, plan["tables"], plan["binding"])
        plan["problems"] += view["problems"]
        plan["warnings"] += view["warnings"]
        mapping = view["mapping"]
    else:
        mapping = package_mapping.object_data_mappings(plan["binding"], graph=False)
    instances = view.get("instances") or {}
    edges = view.get("edges") or []
    out = {
        "tables": [t.summary() for t in plan["tables"]],
        "objects": {
            key: {
                "table": item["table"],
                "matched_by": item["matched_by"],
                "columns": {f["key"]: f["column"] for f in item["fields"] if f["column"]},
                "missing": item["missing_columns"],
                **({"instances": instances[key]} if key in instances else {}),
            }
            for key, item in plan["binding"]["objects"].items()
        },
        "mapped_objects": sorted(mapping["objects"]),
        "problems": plan["problems"],
        "warnings": plan["warnings"],
    }
    if graph:
        out["entities"] = {"count": len(mapping["objects"]), "instances": sum(instances.values())}
        out["relations"] = {
            "count": len(edges),
            "instances": sum(item["instances"] for item in edges),
            "items": edges,
        }
    return out


def _text_cells(sheet, values: list) -> list:
    """一行值 → 单元格；字符串一律写成文本单元格，以 = 开头的值不能被当成公式。"""
    from openpyxl.cell import WriteOnlyCell

    out = []
    for value in values:
        cell = WriteOnlyCell(sheet, value=value)
        if isinstance(value, str):
            cell.data_type = "s"
        out.append(cell)
    return out


def workbook_bytes(tables: list[Table]) -> bytes:
    """规范化表 → xlsx 字节。"""
    import openpyxl

    workbook = openpyxl.Workbook(write_only=True)
    for table in tables:
        sheet = workbook.create_sheet(title=table.name)
        sheet.append(_text_cells(sheet, table.columns))
        for row in table.rows:
            sheet.append(_text_cells(sheet, row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ---- 一致性核对 ----


def verify_mapping(package_zip: Path, tables: list[Table]) -> list[str]:
    """发布包 mappings ↔ 导入工作簿逐项核对：每个 Sheet 与每个 source_column 都必须存在。"""
    columns = {t.name: set(t.columns) for t in tables}
    with zipfile.ZipFile(package_zip) as archive:
        name = next((n for n in archive.namelist() if n.endswith("mappings/object-data-mappings.json")), "")
        if not name:
            return []
        mappings = json.loads(archive.read(name).decode("utf-8"))
    problems = []
    for key, entry in (mappings.get("objects") or {}).items():
        for source in entry.get("sources") or []:
            sheet = source.get("resource")
            if sheet not in columns:
                problems.append(f"mapping 中对象「{key}」的表「{sheet}」不在导入数据里")
                continue
            missing = [
                v.get("source_column") for v in (source.get("fields") or {}).values()
                if v.get("source_column") not in columns[sheet]
            ]
            if missing:
                problems.append(f"mapping 中对象「{key}」的列不在表「{sheet}」里：{'、'.join(missing[:5])}")
    return problems


# ---- Neo4j 图导入：关系关联推断 ----


def _shared_key(keys: list, left: set, right: set) -> str:
    """只接受单字段主键，不能把复合主键的一部分当作完整连接条件。"""
    for key in keys if len(keys or []) == 1 else []:
        if key in left and key in right:
            return key
    return ""


def graph_relation_mapping(upload: dict) -> tuple[list[dict], list[str]]:
    """modeling 图导入上传预览 → 关系映射。

    同一 Sheet 的两类对象按同行关联；跨 Sheet 时找「一端的主键在另一端有同名属性」的
    外键约定按字段值关联；找不到可靠关联的关系跳过并说明（不猜：错连比不连更难发现）。
    """
    schema = upload.get("schema") or {}
    objects = {o["key"]: o for o in schema.get("objects") or []}
    sheet_columns = {s["name"]: set(s.get("columns") or []) for s in upload.get("sheets") or []}

    def props(obj: dict) -> set[str]:
        columns = sheet_columns.get(obj.get("defaultSheet"), set())
        return {m["property"] for m in obj.get("propertyMappings") or [] if m.get("sourceColumn") in columns}

    relations, skipped = [], []
    for rel in schema.get("relations") or []:
        source, target = objects.get(rel["sourceObject"]), objects.get(rel["targetObject"])
        if not source or not target:
            skipped.append(f"关系「{rel['relationName']}」端点不存在或未绑定数据表，未导入")
            continue
        base = {
            "sourceObject": rel["sourceObject"],
            "relationName": rel["relationName"],
            "targetObject": rel["targetObject"],
        }
        explicit_source = str(rel.get("sourceJoinProperty") or "").strip()
        explicit_target = str(rel.get("targetJoinProperty") or "").strip()
        if explicit_source or explicit_target:
            if not explicit_source or not explicit_target:
                skipped.append(
                    f"关系「{rel['sourceObject']} -{rel['relationName']}→ {rel['targetObject']}」"
                    "只声明了一个关联字段，未导入"
                )
                continue
            if explicit_source not in props(source) or explicit_target not in props(target):
                skipped.append(
                    f"关系「{rel['sourceObject']} -{rel['relationName']}→ {rel['targetObject']}」"
                    f"声明的关联字段不存在（{explicit_source} → {explicit_target}），未导入"
                )
                continue
            relations.append({
                **base,
                "matchMode": "field_match",
                "sourceJoinProperty": explicit_source,
                "targetJoinProperty": explicit_target,
            })
            continue
        if source.get("defaultSheet") == target.get("defaultSheet"):
            relations.append({**base, "matchMode": "same_row"})
            continue
        source_props, target_props = props(source), props(target)
        candidates = {
            _shared_key(target.get("primaryKey"), target_props, source_props),
            _shared_key(source.get("primaryKey"), source_props, target_props),
        } - {""}
        join = next(iter(candidates)) if len(candidates) == 1 else ""
        if join:
            relations.append({
                **base, "matchMode": "field_match",
                "sourceJoinProperty": join, "targetJoinProperty": join,
            })
        else:
            skipped.append(
                f"关系「{rel['sourceObject']} -{rel['relationName']}→ {rel['targetObject']}」"
                "找不到关联字段（两端没有同名的主键/外键属性），未导入"
            )
    return relations, skipped
