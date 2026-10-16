"""表格资料的数据表识别：判断一份 xlsx/csv 是否为「数据 Schema」资料。

只看结构、不看业务语义，结果用于上传时预填用途与资料区的一键标注，最终仍由用户确认。
判定逐 Sheet 进行：首行是不重复、不合并的英文标识符列名（≥80%）才算数据表；
中英混合（50%～80%）记为疑似；表头同时出现「字段名」类和「类型/说明」类词的是数据字典，
不是数据表。全部有内容的 Sheet 都是数据表或疑似、且至少一张数据表，整份才判为数据表。
"""

from __future__ import annotations

import csv
import functools
import re
import zipfile
from pathlib import Path

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException

from .package_mapping import TABLE_SUFFIXES

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

TABLE, MAYBE, DICTIONARY, DOCUMENT, META, EMPTY = (
    "table", "maybe", "dictionary", "document", "meta", "empty"
)


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def _identifier_ratio(cells: list[str]) -> float:
    return sum(1 for cell in cells if IDENTIFIER.match(cell)) / len(cells)


def _is_dictionary_header(header: list[str]) -> bool:
    lowered = {cell.lower() for cell in header}
    return bool(lowered & DICTIONARY_NAME_WORDS) and bool(lowered & DICTIONARY_ATTR_WORDS)


def _first_row(rows: list) -> list[str] | None:
    """首个非空行的非空单元格；整表为空时返回 None。"""
    for row in rows:
        cells = [_clean(cell) for cell in row]
        if any(cells):
            return [cell for cell in cells if cell]
    return None


def _classify_header(header: list[str], header_merged: bool) -> str:
    if len(header) < 2 or header_merged or len(set(header)) != len(header):
        return DOCUMENT
    if _is_dictionary_header(header):
        return DICTIONARY
    ratio = _identifier_ratio(header)
    if ratio >= IDENTIFIER_RATIO_TABLE:
        return TABLE
    return MAYBE if ratio >= IDENTIFIER_RATIO_MAYBE else DOCUMENT


def classify_sheet(name: str, rows: list, header_merged: bool = False) -> str:
    """单个 Sheet 的结构类别：table / maybe / dictionary / document / meta / empty。"""
    if META_SHEET.match(name.strip()):
        return META
    header = _first_row(rows)
    if header is None:
        return EMPTY
    return _classify_header(header, header_merged)


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
def _detect_cached(path_str: str, mtime_ns: int, size: int) -> tuple[bool, tuple[str, ...]]:
    """按 (路径, 修改时间, 大小) 缓存：资料区每次展开都要列候选，文件不变就不重读。"""
    del mtime_ns, size  # 只参与缓存键
    kinds = [
        (name, classify_sheet(name, rows, merged))
        for name, rows, merged in _read_sheets(Path(path_str))
    ]
    content = [(name, kind) for name, kind in kinds if kind not in (META, EMPTY)]
    tables = tuple(name for name, kind in content if kind == TABLE)
    is_table = bool(tables) and all(kind in (TABLE, MAYBE) for _, kind in content)
    return is_table, tables


def detect_data_table(path: Path) -> dict | None:
    """整份资料判为数据表时返回 {"tables": [数据表 Sheet 名]}，否则 None。"""
    if path.suffix.lower() not in TABLE_SUFFIXES:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    is_table, tables = _detect_cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    return {"tables": list(tables)} if is_table else None
