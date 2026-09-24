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


def _header_text(value) -> str:
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
    header = [_header_text(v) for v in rows[0]]
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
        table.name = parsed["declared_table"] or key
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


def prepare(root: Path, data_source: dict, graph: bool = False) -> dict:
    """编排入口：规范化表 + 绑定 + 工作簿字节；不合规直接抛 EvalImportError。"""
    plan = collect(root, data_source)
    if graph:
        plan["problems"] += graph_problems(plan["binding"])
    if plan["problems"]:
        raise EvalImportError(plan["problems"])
    plan["workbook"] = workbook_bytes(plan["tables"])
    return plan


def preview(root: Path, data_source: dict, graph: bool = False) -> dict:
    """向导预览：导入哪些表、对象绑到哪张表哪些列（即 mapping 内容）、有哪些问题。"""
    plan = collect(root, data_source)
    if graph:
        plan["problems"] += graph_problems(plan["binding"])
    mapping = package_mapping.object_data_mappings(plan["binding"], graph=graph)
    return {
        "tables": [t.summary() for t in plan["tables"]],
        "objects": {
            key: {
                "table": item["table"],
                "matched_by": item["matched_by"],
                "columns": {f["key"]: f["column"] for f in item["fields"] if f["column"]},
                "missing": item["missing_columns"],
            }
            for key, item in plan["binding"]["objects"].items()
        },
        "mapped_objects": sorted(mapping["objects"]),
        "problems": plan["problems"],
        "warnings": plan["warnings"],
    }


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
    """主键里第一个两端都有同名属性的键（外键约定）。"""
    for key in keys or []:
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
            continue
        base = {
            "sourceObject": rel["sourceObject"],
            "relationName": rel["relationName"],
            "targetObject": rel["targetObject"],
        }
        if source.get("defaultSheet") == target.get("defaultSheet"):
            relations.append({**base, "matchMode": "same_row"})
            continue
        source_props, target_props = props(source), props(target)
        join = _shared_key(target.get("primaryKey"), target_props, source_props) or _shared_key(
            source.get("primaryKey"), source_props, target_props
        )
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
