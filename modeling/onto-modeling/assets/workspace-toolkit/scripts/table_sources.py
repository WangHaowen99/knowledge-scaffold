"""表格资料的用途识别与 Sheet 范围：判断每个 Sheet 是数据表还是参考资料，以及数据 Schema 选中哪些 Sheet。

后端（资料页、发布的数据绑定）和工作区脚本（table_profile）共用这一份，判定、范围、文字都与 develop 同口径（ADR-0096）。
差别只在读法：资料区的 Excel 读多源连接器转出的文本版（resource/text/<分组>/<原文件名>.md）和它带的表结构
（每行的原行号、第一行的合并区、整表有内容的最右一列），CSV 直接读，不解析 Excel 原件（ADR-0076）。只用标准库。

只看结构、不看业务语义。判定逐 Sheet 进行：首行是不重复、不合并的英文标识符列名（≥80%）才算数据表；
中英混合（50%～80%）记为疑似；表头同时出现「字段名」类和「类型/说明」类词的是数据字典，
不是数据表；表头同时出现「术语」类列和「别名/英文/释义」类列的是术语表。
"""

from __future__ import annotations

import copy
import csv
import hashlib
import re
import threading
from collections import OrderedDict
from pathlib import Path

# 数据源资料（.datasource，ADR-0103）和 Excel 一样读文本版
TABLE_SUFFIXES = (".xlsx", ".xlsm", ".csv", ".datasource")

SAMPLE_ROWS = 30
MAX_SHEETS = 200
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

_SHEET_RE = re.compile(r"^<!-- 工作表：(.+) -->$")
_DIVIDER_RE = re.compile(r":?-{3,}:?")


class SheetSelectionError(ValueError):
    """The selected workbook changed or its sheet scope cannot be resolved."""

    # 后端按契约码回 409（api/errors 认 code、status）
    code = "schema_selection_invalid"
    status = 409


class TextPending(SheetSelectionError):
    """资料区的 Excel 还没有文本版（转换中、暂不支持或转换失败），读不到 Sheet 与表头。"""


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


# ---------------------------------------------------------------------------
# 文本版：工作表、每行原行号、第一行合并区、整表列宽
# ---------------------------------------------------------------------------


def text_path(path: Path) -> Path | None:
    """资料区里的 Excel 原件 → 文本版路径；CSV、资料区之外的文件、文本版本身返回 None。"""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return None
    for parent in path.parents:
        if parent.name == "resource":
            rel = path.relative_to(parent)
            return None if rel.parts[:1] == ("text",) else parent / "text" / f"{rel.as_posix()}.md"
    return None


def _md_row(line: str) -> list[str]:
    """一行 Markdown 表格 → 单元格（转换器 md_cell 的逆：反斜杠转义、<br> 换行）。"""
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    cells, current, escaped = [], [], False
    for char in body:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current))
            current = []
        else:
            current.append(char)
    if current and "".join(current).strip():
        cells.append("".join(current))
    return [cell.strip().replace("<br>", "\n") for cell in cells]


ANCHORS_HEAD = "<!-- onto:anchors 1"
_REF_ROW = re.compile(r"^[A-Za-z]+(\d+)")


def _on_first_row(ref: str) -> bool:
    """合并区（A1:C1）是不是从第一行开始。"""
    match = _REF_ROW.match(ref)
    return bool(match) and int(match.group(1)) == 1


def _unescape(text: str) -> str:
    return text.replace("%20", " ").replace("%0D", "\r").replace("%0A", "\n").replace("%3E", ">").replace("%25", "%")


def anchor_block(lines: list[str]) -> dict | None:
    """文本版末尾的原件锚点注释（ADR-0093，与后端 convert.parse_anchor_index 同口径）→
    {lines: {文件行号: 锚点}, merges: {工作表: [A1:C1]}, cols: {工作表: 整表最右一列}, start: 注释所在的文件行号}；没有时 None。"""
    start = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip() == ANCHORS_HEAD), None)
    if start is None:
        return None
    found, merges, cols = {}, {}, {}
    for raw in lines[start + 1:]:
        line = raw.strip()
        if line == "-->":
            break
        head, _, rest = line.partition(" ")
        if head in ("merge", "cols"):
            sheet, _, value = rest.partition(" ")
            if head == "merge":
                merges.setdefault(_unescape(sheet), []).extend(value.split())
            elif value.strip().isdigit():
                cols[_unescape(sheet)] = int(value)
        elif head.isdigit() and rest:
            found[int(head)] = _unescape(rest)
    # 锚点行号从转换器正文数起；文件里前面还有内核写的文件头（和空行），按第一个工作表标记对齐
    first = min((n for n, anchor in found.items() if anchor.startswith("sheet:")), default=None)
    marker = next((i + 1 for i, line in enumerate(lines[:start]) if _SHEET_RE.match(line.strip())), None)
    offset = marker - first if first is not None and marker is not None else 0
    return {"lines": {n + offset: anchor for n, anchor in found.items()}, "merges": merges, "cols": cols,
            "start": start + 1}


def _new_sheet(name: str, block: dict | None) -> dict:
    # 有锚点时：每行的原行号、第一行是否在合并区里、整表最右一列；老文本版没有锚点时为 None，按表格本身推断
    merges = (block or {}).get("merges", {}).get(name, [])
    first_row_merged = any(_on_first_row(ref) for ref in merges)
    return {"name": name, "rows": [], "row_numbers": [] if block else None,
            "merged_header": first_row_merged if block else None,
            "width": (block or {}).get("cols", {}).get(name)}


def text_sheets(markdown: str) -> list[dict]:
    """文本版 → [{name, rows, row_numbers, merged_header, width}]，按工作表顺序，空表也列出。
    每个工作表只取标记后第一张表。"""
    lines = str(markdown or "").splitlines()
    block = anchor_block(lines)
    anchors = block["lines"] if block else {}
    body = lines[: block["start"] - 1] if block else lines
    sheets: list[dict] = []
    seen: set[str] = set()
    current: dict | None = None
    done = False
    for number, line in enumerate(body, 1):
        stripped = line.strip()
        marker = _SHEET_RE.match(stripped)
        if marker:
            name = marker.group(1)
            current, done = (None, True) if name in seen else (_new_sheet(name, block), False)
            if current is not None:
                seen.add(name)
                sheets.append(current)
            continue
        if current is None or done:
            continue
        if not stripped.startswith("|"):
            done = bool(current["rows"])
            continue
        cells = _md_row(stripped)
        if cells and all(_DIVIDER_RE.fullmatch(cell) for cell in cells):
            continue
        current["rows"].append(cells)
        if current["row_numbers"] is not None:
            anchor = anchors.get(number, "")
            current["row_numbers"].append(int(anchor[4:]) if anchor.startswith("row:") and anchor[4:].isdigit() else None)
    return sheets


def _width(rows) -> int:
    width = 0
    for row in rows:
        width = max(width, max((i + 1 for i, value in enumerate(row) if _clean(value)), default=0))
    return width


def _physical_rows(sheet: dict) -> list[list[str]]:
    """前两行（物理第 1、2 行）：有行号时按行号对位，第一行是空行就给空行；没有行号时按表格前两行。"""
    rows, numbers = sheet["rows"], sheet["row_numbers"]
    if numbers is None:
        return [list(row) for row in rows[:2]]
    by_number = dict(zip(numbers, rows))
    return [list(by_number.get(number, [])) for number in (1, 2)]


# ---------------------------------------------------------------------------
# Sheet 范围
# ---------------------------------------------------------------------------


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _text_stamp(path: Path) -> tuple:
    """文本版的修改时间与大小：原件没换、文本版重新转换后缓存也要失效。"""
    text = text_path(path)
    try:
        stat = text.stat() if text is not None else None
    except OSError:
        stat = None
    return (stat.st_mtime_ns, stat.st_size) if stat else ()


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
    if path.suffix.lower() not in (".xlsx", ".xlsm", ".datasource"):
        return []
    text = text_path(path)
    try:
        markdown = text.read_text(encoding="utf-8") if text is not None else None
    except (OSError, UnicodeDecodeError):
        markdown = None
    if markdown is None:
        raise TextPending("表格还没有转成文本版，暂时无法检查 Sheet 范围，请稍后重试"
                          if path.suffix.lower() != ".datasource" else "数据源还没有采集完，暂时无法检查 Sheet 范围，请稍后重试")
    sheets = text_sheets(markdown)
    if len(sheets) > MAX_SHEETS:
        raise SheetSelectionError(f"工作簿超过 {MAX_SHEETS} 个 Sheet，请拆分后使用")
    result = []
    for sheet in sheets:
        width = sheet["width"] if sheet["width"] is not None else _width(sheet["rows"])
        result.append(_sheet_details(sheet["name"], _physical_rows(sheet), bool(sheet["merged_header"]), width))
    return result


# Cache only structural inspection, never a user's selection or validation result.
# Hash the current bytes on every access; size/mtime alone cannot detect replacements.
_inspection_cache = OrderedDict()
_inspection_lock = threading.Lock()
_INSPECTION_CACHE_LIMIT = 64


def _inspect_current(path: Path, force: bool = False) -> tuple[str, list[dict]]:
    digest = file_digest(path)
    key = (str(path.resolve()), digest, _text_stamp(path))
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
    """范围内的 Sheet → 表头列。Excel 还没有文本版时为空（与没转出文本版的其他读法一致，由调用方列为未读）。"""
    try:
        scope = resolve_selection(path, directive)
    except TextPending:
        return {}
    selected = set(scope["included_sheets"])
    return {s["name"]: s["headers"] for s in scope["sheets"] if s["name"] in selected}


def evaluation_selection(path: Path) -> dict:
    """Keep ordinary data sheets (including Chinese headers), exclude reference tabs."""
    return resolve_selection(path, {"purpose": "schema"})
