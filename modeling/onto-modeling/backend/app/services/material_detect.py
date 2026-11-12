"""表格资料的用途识别：判断一份 xlsx/csv 是「数据 Schema」还是「术语表」资料。

只看结构、不看业务语义，结果用于上传时预填用途与资料区的一键标注，用户可在资料区改用途。
判定逐 Sheet 进行：首行是不重复、不合并的英文标识符列名（≥80%）才算数据表；
中英混合（50%～80%）记为疑似；表头同时出现「字段名」类和「类型/说明」类词的是数据字典，
不是数据表；表头同时出现「术语」类列和「别名/英文/释义」类列的是术语表。
全部有内容的 Sheet 都是数据表或疑似、且至少一张数据表，整份判为数据表；
全部有内容的 Sheet 都是术语表，整份判为术语表。

判定规则与 Sheet 范围共用 table_sources（ADR-0096）。上传那一刻还没有文本版，这里仍读原件判用途——
这是 ADR-0076 记下的遗留点之一，不扩大：Sheet 范围、表头预检都只读文本版（Excel）或直接读 CSV。
"""

from __future__ import annotations

import csv
import functools
from pathlib import Path

import openpyxl

from .package_mapping import TABLE_SUFFIXES
from .table_sources import rules as _rules

# 与 develop 的 material_detect 同一组名字：判定规则与 Sheet 范围都来自 table_sources
TABLE, MAYBE, DICTIONARY, TERM, DOCUMENT, META, EMPTY = (
    _rules.TABLE, _rules.MAYBE, _rules.DICTIONARY, _rules.TERM, _rules.DOCUMENT, _rules.META, _rules.EMPTY,
)
classify_sheet = _rules.classify_sheet
resolve_selection = _rules.resolve_selection
selection_metadata = _rules.selection_metadata
read_selected_headers = _rules.read_selected_headers
evaluation_selection = _rules.evaluation_selection
SheetSelectionError = _rules.SheetSelectionError
TextPending = _rules.TextPending

SAMPLE_ROWS = 30
MAX_SHEETS = 200
# read_only 模式读不出合并单元格；与 Excel 预览同一阈值，超过时跳过合并检查
FULL_LOAD_MAX_BYTES = 8 * 1024 * 1024


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
