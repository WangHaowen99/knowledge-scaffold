"""表格资料的用途识别：判断一份 xlsx/csv 是「数据 Schema」还是「术语表」资料。

只看结构、不看业务语义，结果用于上传时预填用途与资料区的一键标注，用户可在资料区改用途。
判定逐 Sheet 进行：首行是不重复、不合并的英文标识符列名（≥80%）才算数据表；
中英混合（50%～80%）记为疑似；表头同时出现「字段名」类和「类型/说明」类词的是数据字典，
不是数据表；表头同时出现「术语」类列和「别名/英文/释义」类列的是术语表。
全部有内容的 Sheet 都是数据表或疑似、且至少一张数据表，整份判为数据表；
全部有内容的 Sheet 都是术语表，整份判为术语表。
"""

from __future__ import annotations

import csv
import copy
from collections import OrderedDict
import functools
import hashlib
import re
import threading
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import openpyxl
    from openpyxl.utils.exceptions import InvalidFileException
except ImportError:  # CSV and non-table project checks remain usable without Excel support.
    openpyxl = None
    InvalidFileException = ValueError

TABLE_SUFFIXES = (".xlsx", ".xlsm", ".csv")

SAMPLE_ROWS = 30
MAX_SHEETS = 200
# read_only 模式读不出合并单元格；与 Excel 预览同一阈值，超过时跳过合并检查
FULL_LOAD_MAX_BYTES = 8 * 1024 * 1024
IDENTIFIER_RATIO_TABLE = 0.8
IDENTIFIER_RATIO_MAYBE = 0.5

IDENTIFIER = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9_]+$")
META_SHEET = re.compile(r"^(__.*__|总览|说明|目录|封面|readme|填写说明|使用说明)$", re.IGNORECASE)
DICTIONARY_NAME_WORDS = frozenset({
    "字段名", "字段", "字段名称", "列名", "字段英文名", "英文名", "英文名称",
    "field", "field_name", "column", "column_name",
})
DICTIONARY_ATTR_WORDS = frozenset({
    "类型", "数据类型", "字段类型", "说明", "描述", "字段说明",
    "type", "data_type", "description", "comment",
})

# 术语表：一列术语本身，另有别名、英文或释义列。单独的「名称」列不算——中文表头的
# 数据表也常有，误标成术语表会把数据表当成命名依据
TERM_NAME_WORDS = frozenset({"术语", "术语名称", "业务术语", "名词", "概念", "term"})
TERM_ATTR_WORDS = frozenset({
    "别名", "同义词", "近义词", "缩写", "英文", "英文名", "英文名称", "英文术语",
    "释义", "定义", "含义", "解释", "alias", "synonym", "english", "definition", "meaning",
})

TABLE, MAYBE, DICTIONARY, TERM, DOCUMENT, META, EMPTY = (
    "table", "maybe", "dictionary", "term", "document", "meta", "empty"
)


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def _identifier_ratio(cells: list[str]) -> float:
    return sum(1 for cell in cells if IDENTIFIER.match(cell)) / len(cells)


def _is_dictionary_header(header: list[str]) -> bool:
    lowered = {cell.lower() for cell in header}
    return bool(lowered & DICTIONARY_NAME_WORDS) and bool(lowered & DICTIONARY_ATTR_WORDS)


def _is_term_header(header: list[str]) -> bool:
    lowered = {cell.lower() for cell in header}
    return bool(lowered & TERM_NAME_WORDS) and bool(lowered & TERM_ATTR_WORDS)


def _first_row(rows: list) -> list[str] | None:
    """首个非空行的非空单元格；整表为空时返回 None。"""
    for row in rows:
        cells = [_clean(cell) for cell in row]
        if any(cells):
            return [cell for cell in cells if cell]
    return None


def _classify_header(header: list[str], header_merged: bool) -> str:
    if len(header) < 2 or header_merged:
        return DOCUMENT
    if len(set(header)) != len(header):  # 列名重复
        return DOCUMENT
    if _is_dictionary_header(header):
        return DICTIONARY
    if _is_term_header(header):
        return TERM
    ratio = _identifier_ratio(header)
    if ratio >= IDENTIFIER_RATIO_TABLE:
        return TABLE
    return MAYBE if ratio >= IDENTIFIER_RATIO_MAYBE else DOCUMENT


def classify_sheet(name: str, rows: list, header_merged: bool = False) -> str:
    """单个 Sheet 的结构类别：table / maybe / dictionary / term / document / meta / empty。"""
    header = _first_row(rows)
    if header is None:
        return EMPTY
    kind = _classify_header(header, header_merged)
    if META_SHEET.match(name.strip()) and kind not in (TABLE, MAYBE):
        return META
    return kind


def _csv_sheets(path: Path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            rows = []
            for row in csv.reader(handle):
                rows.append(row)
                if len(rows) >= SAMPLE_ROWS:
                    break
    except (OSError, UnicodeDecodeError, csv.Error):
        return []
    return [(path.stem, rows, False)]


def _workbook_sheets(path: Path):
    if openpyxl is None:
        return []
    read_only = path.stat().st_size > FULL_LOAD_MAX_BYTES
    try:
        workbook = openpyxl.load_workbook(path, read_only=read_only, data_only=True)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, InvalidFileException):
        return []
    sheets = []
    try:
        for sheet in workbook.worksheets[:MAX_SHEETS]:
            merged = not read_only and any(
                rng.min_row <= 1 for rng in sheet.merged_cells.ranges
            )
            rows = [list(row) for row in sheet.iter_rows(max_row=SAMPLE_ROWS, values_only=True)]
            sheets.append((sheet.title, rows, merged))
    finally:
        workbook.close()
    return sheets


def _read_sheets(path: Path):
    if path.suffix.lower() == ".csv":
        return _csv_sheets(path)
    return _workbook_sheets(path)


@functools.lru_cache(maxsize=256)
def _detect_cached(path_str: str, mtime_ns: int, size: int) -> tuple[str, tuple[str, ...]]:
    """按 (路径, 修改时间, 大小) 缓存：资料区每次展开都要列候选，文件不变就不重读。

    返回（整份类别, 相关 Sheet 名）：类别为 "schema"、"term" 或 ""（不预填）。
    """
    del mtime_ns, size  # 只参与缓存键
    kinds = [
        (name, classify_sheet(name, rows, merged))
        for name, rows, merged in _read_sheets(Path(path_str))
    ]
    content = [(name, kind) for name, kind in kinds if kind not in (META, EMPTY)]
    tables = tuple(name for name, kind in content if kind == TABLE)
    if tables and all(kind in (TABLE, MAYBE) for _, kind in content):
        return "schema", tables
    if content and all(kind == TERM for _, kind in content):
        return "term", tuple(name for name, _ in content)
    return "", ()


def detect_material(path: Path) -> dict | None:
    """整份表格资料可预填用途时返回 {"purpose": "schema"|"term", "tables": [Sheet 名]}，否则 None。"""
    if path.suffix.lower() not in TABLE_SUFFIXES:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    purpose, sheets = _detect_cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    return {"purpose": purpose, "tables": list(sheets)} if purpose else None


def detect_data_table(path: Path) -> dict | None:
    """整份资料判为数据表时返回 {"tables": [数据表 Sheet 名]}，否则 None。"""
    detected = detect_material(path)
    if detected is None or detected["purpose"] != "schema":
        return None
    return {"tables": detected["tables"]}


class SheetSelectionError(ValueError):
    """The selected workbook changed or its sheet scope cannot be resolved."""


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sheet_structure(archive: zipfile.ZipFile, sheet_path: str) -> tuple[bool, int]:
    """Scan actual cells, not formatting dimensions, without retaining data rows."""
    merged, width = False, 0
    with archive.open(sheet_path) as handle:
        parents = []
        cell_ref, cell_has_value = "", False
        for event, element in ET.iterparse(handle, events=("start", "end")):
            tag = element.tag.rsplit("}", 1)[-1]
            if event == "start":
                parents.append(element)
                if tag == "c":
                    cell_ref, cell_has_value = element.attrib.get("r", ""), False
                continue
            if tag in ("v", "t", "f") and _clean(element.text):
                cell_has_value = True
            if tag == "c" and cell_ref and cell_has_value:
                column, _ = openpyxl.utils.cell.coordinate_from_string(cell_ref)
                width = max(width, openpyxl.utils.column_index_from_string(column))
            if tag == "mergeCell":
                _, min_row, _, _ = openpyxl.utils.range_boundaries(element.attrib["ref"])
                if min_row == 1:
                    merged = True
            parents.pop()
            if parents:
                parents[-1].remove(element)
            element.clear()
    return merged, width


def _sheet_details(name: str, rows: list, merged: bool = False, data_width: int = 0) -> dict:
    header = [_clean(c) for c in (rows[0] if rows else [])]
    while header and not header[-1]:
        header.pop()
    header.extend([""] * max(0, data_width - len(header)))
    kind = classify_sheet(name, rows, merged)
    issues = []

    def issue(code, severity, message):
        issues.append({"code": code, "severity": severity, "message": message})

    if not any(header):
        issue("empty_header", "error", "第一行为空，无法确定字段名。请把字段名移到第一行，删除其上方的空行或标题行。")
    if merged:
        issue("merged_header", "error", "第一行包含合并单元格，无法逐列对应字段。请取消第一行的合并，并为每列填写独立字段名。")
    blanks = [str(i + 1) for i, value in enumerate(header) if not value]
    if blanks:
        issue("blank_columns", "error", f"第一行第 {'、'.join(blanks)} 列缺少字段名。请补齐这些列名，或删除不需要的整列。")
    positions = {}
    for i, value in enumerate(header):
        if value:
            positions.setdefault(value.casefold(), []).append(i + 1)
    for columns in positions.values():
        if len(columns) > 1:
            issue("duplicate_columns", "error", f"第一行第 {'、'.join(map(str, columns))} 列的字段名「{header[columns[0] - 1]}」重复（忽略大小写）。请按实际字段含义改为唯一名称。")
    if kind in (DICTIONARY, TERM, META):
        label = {DICTIONARY: "字段字典", TERM: "术语表", META: "说明或封面页"}[kind]
        issue("reference_sheet", "warning", f"内容类似{label}，建议保留为参考资料。若勾选，系统会直接把第一行作为字段名，不会自动把字典或说明转换成数据表。")
    elif header and not merged:
        if len(header) == 1:
            issue("single_column", "warning", "只有一列字段，请确认这确实是一张单列表，而不是标题或说明；单列表可以继续使用。")
        if any(not IDENTIFIER.match(value) for value in header if value):
            issue("non_identifier_header", "warning", "表头含中文或非英文标识符。系统会原样使用这些字段名；请核对是否与实际数据源列名一致，不要为通过识别而随意改名。")
    return {"name": name, "kind": kind, "headers": [c for c in header if c],
            "selectable": any(header) and not merged,
            "schema_selectable": not any(i["severity"] == "error" for i in issues),
            "issues": issues}


def inspect_sheets(path: Path) -> list[dict]:
    """Inspect all sheets; first-row headers match downstream mapping readers."""
    if path.suffix.lower() == ".csv":
        rows, width = [], 0
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.reader(handle):
                if len(rows) < 2:
                    rows.append(row)
                width = max(width, max((i + 1 for i, value in enumerate(row) if _clean(value)), default=0))
        return [_sheet_details(path.stem, rows, data_width=width)]
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        return []
    if openpyxl is None:
        raise SheetSelectionError("需要 openpyxl 解析 Excel（pip install openpyxl）")
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if len(workbook.worksheets) > MAX_SHEETS:
            raise SheetSelectionError(f"工作簿超过 {MAX_SHEETS} 个 Sheet，请拆分后使用")
        result = []
        with zipfile.ZipFile(path) as archive:
            for sheet in workbook.worksheets:
                rows = [list(row) for row in sheet.iter_rows(max_row=2, values_only=True)]
                # openpyxl's read-only worksheet omits merged_cells; its XML path
                # lets us inspect merge declarations while retaining streaming reads.
                merged, width = _sheet_structure(archive, sheet._worksheet_path)
                result.append(_sheet_details(sheet.title, rows, merged, width))
        return result
    finally:
        workbook.close()


# Cache only structural inspection, never a user's selection or validation result.
# Hash the current bytes on every access; size/mtime alone cannot detect replacements.
_inspection_cache = OrderedDict()
_inspection_lock = threading.Lock()
_INSPECTION_CACHE_LIMIT = 64


def _inspect_current(path: Path, force: bool = False) -> tuple[str, list[dict]]:
    digest = file_digest(path)
    key = (str(path.resolve()), digest)
    with _inspection_lock:
        if force:
            _inspection_cache.pop(key, None)
        elif key in _inspection_cache:
            _inspection_cache.move_to_end(key)
            return digest, copy.deepcopy(_inspection_cache[key])
    sheets = inspect_sheets(path)
    if file_digest(path) != digest:
        raise SheetSelectionError("检查期间文件发生变化，请重新检查后确认 Sheet 范围")
    with _inspection_lock:
        _inspection_cache[key] = copy.deepcopy(sheets)
        _inspection_cache.move_to_end(key)
        while len(_inspection_cache) > _INSPECTION_CACHE_LIMIT:
            _inspection_cache.popitem(last=False)
    return digest, sheets


def resolve_selection(path: Path, directive: dict | None = None, *, force: bool = False) -> dict:
    """Read-only fallback for legacy projects; never interpret [] as all sheets."""
    directive = directive or {}
    stored = directive.get("schema_selection")
    try:
        digest, sheets = _inspect_current(path, force=force)
    except SheetSelectionError:
        raise
    except Exception as exc:
        raise SheetSelectionError("工作簿无法读取，请检查文件或重新上传") from exc
    if not sheets:
        raise SheetSelectionError("未找到可读取的 Sheet，请使用 xlsx/xlsm/csv")
    by_name = {s["name"]: s for s in sheets}
    manual = isinstance(stored, dict) and stored.get("mode") == "manual"
    if stored is not None:
        valid_type = isinstance(stored, dict)
        valid_version = valid_type and stored.get("version") == 1
        valid_mode = valid_type and stored.get("mode") in ("auto", "manual")
        if not (valid_type and valid_version and valid_mode):
            raise SheetSelectionError("Sheet 范围配置无效，请在资料区重新确认")
    if manual and stored.get("file_sha256") != digest:
        raise SheetSelectionError("文件已替换，原人工选择的 Sheet 范围需要重新确认")
    if stored is not None and stored.get("file_sha256") == digest:
        # Keep the resolved scope across saves (including manually tagged files
        # with Chinese headers); only automatic selections refresh on replacement.
        included = stored.get("included_sheets")
        if not isinstance(included, list) or any(not isinstance(n, str) for n in included):
            raise SheetSelectionError("Sheet 范围配置无效，请重新确认")
        if len(included) != len(set(included)) or any(n not in by_name for n in included):
            raise SheetSelectionError("所选 Sheet 已不存在或重复，请重新确认")
        if any(not by_name[n].get("selectable", bool(by_name[n]["headers"])) for n in included):
            raise SheetSelectionError("所选 Sheet 的首行无法作为表头，请检查合并单元格或空列名")
    else:
        # Existing manual schema tags may use Chinese/one-column headers. Preserve
        # those, but never promote a cover sheet or dictionary to a physical table.
        legacy_manual = not directive.get("auto_detected")
        allowed = (TABLE, MAYBE, DOCUMENT) if legacy_manual else (TABLE, MAYBE)
        included = []
        for sheet in sheets:
            selectable = sheet.get("selectable", bool(sheet["headers"]))
            if sheet["kind"] in allowed and selectable:
                included.append(sheet["name"])
    reference = [s["name"] for s in sheets if s["name"] not in included]
    warnings = []
    if not included:
        warnings.append("没有选中数据 Sheet；文件仍可作为参考资料阅读")
    if reference:
        warnings.append("未选中的 Sheet 仅作参考，不参与字段提取与数据映射")
    return {"version": 1, "mode": "manual" if manual else "auto",
            "included_sheets": list(included), "reference_sheets": reference,
            "file_sha256": digest, "sheets": sheets, "warnings": warnings}


def selection_metadata(scope: dict) -> dict:
    return {
        "version": scope["version"],
        "mode": scope["mode"],
        "included_sheets": scope["included_sheets"],
        "reference_sheets": scope["reference_sheets"],
        "file_sha256": scope["file_sha256"],
    }


def read_selected_headers(path: Path, directive: dict | None = None) -> dict[str, list[str]]:
    scope = resolve_selection(path, directive)
    selected = set(scope["included_sheets"])
    return {s["name"]: s["headers"] for s in scope["sheets"] if s["name"] in selected}


def evaluation_selection(path: Path) -> dict:
    """Keep ordinary data sheets (including Chinese headers), exclude reference tabs."""
    return resolve_selection(path, {"purpose": "schema"})
