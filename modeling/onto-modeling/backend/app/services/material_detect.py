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
import functools
import re
from pathlib import Path

import openpyxl

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
    except Exception:  # noqa: BLE001 - 坏表（解析库各种异常）按无法识别，上传照常
        return []
    sheets = []
    try:
        for sheet in workbook.worksheets[:MAX_SHEETS]:
            merged = not read_only and any(
                rng.min_row <= 1 for rng in sheet.merged_cells.ranges
            )
            rows = [list(row) for row in sheet.iter_rows(max_row=SAMPLE_ROWS, values_only=True)]
            sheets.append((sheet.title, rows, merged))
    except Exception:  # noqa: BLE001 - 某张表的内容坏了：只用前面读出来的
        pass
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
