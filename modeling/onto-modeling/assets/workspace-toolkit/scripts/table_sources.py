"""表格资料的 Sheet 范围：哪些 Sheet 是数据表（对象字段与数据绑定的依据），哪些只作参考。

后端（资料页、发布的数据绑定）和工作区脚本（table_profile）共用这一份，口径一致（ADR-0096）。
资料区的 Excel 只读多源连接器转出的文本版（resource/text/<分组>/<原文件名>.md），CSV 按 UTF-8 直接读，
不解析 Excel 原件（ADR-0076）。只用标准库。

表头是每个工作表的第一个非空行（与 Excel 转换器、后端 md_tables 同口径）。按结构判定 Sheet 类别，不看业务语义：
首行是不重复的英文标识符列名（≥80%）才算数据表，中英混合（50%～80%）记为疑似；表头同时出现「字段名」类和
「类型/说明」类词的是数据字典；同时出现「术语」类列和「别名/英文/释义」类列的是术语表。

范围有两种：auto 按类别现算（文件换了自动跟着变）；manual 是用户在资料区勾选的，绑定确认时的文件修订，
文件换了就要重新确认，不悄悄换成现算的结果。
"""

from __future__ import annotations

import copy
import csv
import functools
import hashlib
import re
from pathlib import Path

TABLE_SUFFIXES = (".xlsx", ".xlsm", ".csv")
SCOPE_VERSION = 1
SAMPLE_ROWS = 30
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
REFERENCE_LABELS = {DICTIONARY: "字段字典", TERM: "术语表", META: "说明页"}

_SHEET_RE = re.compile(r"^<!-- 工作表：(.+) -->$")
_DIVIDER_RE = re.compile(r":?-{3,}:?")


class SheetSelectionError(ValueError):
    """Sheet 范围对不上当前文件（文件换了、选的 Sheet 没了、配置坏了），要在资料区重新确认。"""


# ---------------------------------------------------------------------------
# 结构判定
# ---------------------------------------------------------------------------


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def _identifier_ratio(cells: list[str]) -> float:
    return sum(1 for cell in cells if IDENTIFIER.match(cell)) / len(cells)


def _has_words(header: list[str], names: frozenset, attrs: frozenset) -> bool:
    lowered = {cell.lower() for cell in header}
    return bool(lowered & names) and bool(lowered & attrs)


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
    if _has_words(header, DICTIONARY_NAME_WORDS, DICTIONARY_ATTR_WORDS):
        return DICTIONARY
    if _has_words(header, TERM_NAME_WORDS, TERM_ATTR_WORDS):
        return TERM
    ratio = _identifier_ratio(header)
    if ratio >= IDENTIFIER_RATIO_TABLE:
        return TABLE
    return MAYBE if ratio >= IDENTIFIER_RATIO_MAYBE else DOCUMENT


def classify_sheet(name: str, rows: list, header_merged: bool = False) -> str:
    """单个 Sheet 的结构类别：table / maybe / dictionary / term / document / meta / empty。

    名字像说明页（总览、目录……）但表头是数据表的，按数据表算。"""
    header = _first_row(rows)
    if header is None:
        return EMPTY
    kind = _classify_header(header, header_merged)
    if META_SHEET.match(name.strip()) and kind not in (TABLE, MAYBE):
        return META
    return kind


# ---------------------------------------------------------------------------
# 读表：Excel 读文本版，CSV 直接读
# ---------------------------------------------------------------------------


def file_revision(path: Path) -> str:
    """文件修订（sha256 前 16 位，与资料的 revision、文本版文件头同口径）。只算摘要，不解析内容。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


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


def text_sheets(markdown: str) -> list[tuple[str, list[list[str]]]]:
    """文本版 → [(工作表名, 行)]，按工作表顺序，空表也列出（行为空）。每个工作表只取标记后第一张表，表头在第一行。"""
    sheets: list[tuple[str, list[list[str]]]] = []
    seen: set[str] = set()
    current: list[list[str]] | None = None
    done = False
    for line in str(markdown or "").splitlines():
        stripped = line.strip()
        marker = _SHEET_RE.match(stripped)
        if marker:
            name = marker.group(1)
            current, done = ([], False) if name not in seen else (None, True)
            if name not in seen:
                seen.add(name)
                sheets.append((name, current))
            continue
        if current is None or done:
            continue
        if not stripped.startswith("|"):
            done = done or bool(current)
            continue
        cells = _md_row(stripped)
        if cells and all(_DIVIDER_RE.fullmatch(cell) for cell in cells):
            continue
        current.append(cells)
    return sheets


def _width(rows) -> int:
    """有内容的最右一列（1 起）：表头右边没有列名、下面却有数据的列也要算进来。"""
    width = 0
    for row in rows:
        width = max(width, max((i + 1 for i, value in enumerate(row) if _clean(value)), default=0))
    return width


def _csv_sheet(path: Path) -> list[tuple[str, list, int]]:
    rows: list[list[str]] = []
    width = 0
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.reader(handle):
                if len(rows) < SAMPLE_ROWS:
                    rows.append(row)
                width = max(width, _width([row]))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise SheetSelectionError("CSV 要用 UTF-8 编码") from exc
    return [(path.stem, rows, width)]


@functools.lru_cache(maxsize=64)
def _text_cached(text: str, mtime_ns: int, size: int, revision: str) -> tuple:
    del mtime_ns, size, revision  # 只参与缓存键：原件换了、文本版重新转换后都重读
    markdown = Path(text).read_text(encoding="utf-8")
    return tuple((name, rows[:SAMPLE_ROWS], _width(rows)) for name, rows in text_sheets(markdown))


def read_sheets(path: Path, revision: str = "") -> list[tuple[str, list, int]] | None:
    """表格 → [(Sheet 名, 前几行, 有内容的列数)]。资料区的 Excel 还没有文本版时返回 None。"""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return _csv_sheet(path)
    text = text_path(path)
    if text is None or not text.is_file():
        return None
    try:
        stat = text.stat()
        return copy.deepcopy(list(_text_cached(str(text), stat.st_mtime_ns, stat.st_size, revision)))
    except (OSError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# 表头预检与 Sheet 范围
# ---------------------------------------------------------------------------


def sheet_details(name: str, rows: list, width: int = 0) -> dict:
    """一个 Sheet 的表头预检：类别、表头、问题（error 不能作数据表，warning 只提醒）。"""
    header = [_clean(cell) for cell in (rows[0] if rows else [])]
    while header and not header[-1]:
        header.pop()
    header.extend([""] * max(0, width - len(header)))
    kind = classify_sheet(name, rows)
    issues = []

    def issue(code, severity, message):
        issues.append({"code": code, "severity": severity, "message": message})

    if not any(header):
        issue("empty_header", "error", "没有表头")
    blanks = [str(i + 1) for i, value in enumerate(header) if not value]
    if any(header) and blanks:
        issue("blank_columns", "error", f"第 {'、'.join(blanks)} 列没有列名")
    positions: dict[str, list[int]] = {}
    for i, value in enumerate(header):
        if value:
            positions.setdefault(value.casefold(), []).append(i + 1)
    for columns in positions.values():
        if len(columns) > 1:
            issue("duplicate_columns", "error",
                  f"第 {'、'.join(map(str, columns))} 列列名「{header[columns[0] - 1]}」重复")
    if kind in REFERENCE_LABELS:
        issue("reference_sheet", "warning", f"像{REFERENCE_LABELS[kind]}，建议留作参考")
    elif any(header):
        named = [value for value in header if value]
        if len(header) == 1:
            issue("single_column", "warning", "只有一列")
        if any(not IDENTIFIER.match(value) for value in named):
            issue("non_identifier_header", "warning", "列名不全是英文标识符，按原样使用")
    return {
        "name": name, "kind": kind, "headers": [cell for cell in header if cell],
        "selectable": any(header),
        "schema_selectable": not any(item["severity"] == "error" for item in issues),
        "issues": issues,
    }


def inspect(path: Path, revision: str = "") -> list[dict] | None:
    """全部 Sheet 的表头预检；Excel 还没有文本版时返回 None。"""
    sheets = read_sheets(path, revision)
    if sheets is None:
        return None
    return [sheet_details(name, rows, width) for name, rows, width in sheets]


def stored_selection(directive: dict | None) -> dict | None:
    """materials.json 里存的人工范围；没存返回 None，存坏了抛 SheetSelectionError。"""
    stored = (directive or {}).get("schema_selection") if isinstance(directive, dict) else None
    if stored is None:
        return None
    included = stored.get("included_sheets") if isinstance(stored, dict) else None
    if not (isinstance(stored, dict) and stored.get("version") == SCOPE_VERSION
            and stored.get("mode") in ("auto", "manual") and isinstance(included, list)
            and all(isinstance(name, str) for name in included)):
        raise SheetSelectionError("Sheet 范围配置无效，要重新确认")
    return stored if stored["mode"] == "manual" else None


def resolve_selection(path: Path, directive: dict | None = None) -> dict:
    """当前文件的 Sheet 范围：{version, mode, revision, pending, included_sheets, reference_sheets, sheets}。

    pending：Excel 还没有文本版，Sheet 和表头都还不知道（人工范围照旧给出所选 Sheet）。
    人工范围对不上当前文件（换过文件、Sheet 没了）抛 SheetSelectionError，不退回现算。
    没有人工范围时现算：用户手标的 schema 收数据表、疑似和文档形式的表（中文表头的手标表照旧可用），
    上传时自动识别的只收数据表和疑似；字典、术语表、说明页、空表都只作参考。
    """
    directive = directive if isinstance(directive, dict) else {}
    manual = stored_selection(directive)
    try:
        revision = file_revision(path)
    except OSError as exc:
        raise SheetSelectionError("读不到文件") from exc
    if manual and manual.get("revision") != revision:
        raise SheetSelectionError("文件已替换，要重新确认 Sheet 范围")
    sheets = inspect(path, revision)
    base = {"version": SCOPE_VERSION, "mode": "manual" if manual else "auto", "revision": revision}
    if sheets is None:
        included = list(manual["included_sheets"]) if manual else []
        return {**base, "pending": True, "included_sheets": included, "reference_sheets": [], "sheets": []}
    if not sheets:
        raise SheetSelectionError("没有可读的 Sheet")
    by_name = {sheet["name"]: sheet for sheet in sheets}
    if manual:
        included = list(manual["included_sheets"])
        if len(included) != len(set(included)) or any(name not in by_name for name in included):
            raise SheetSelectionError("选的 Sheet 已不存在，要重新确认 Sheet 范围")
        if any(not by_name[name]["selectable"] for name in included):
            raise SheetSelectionError("选的 Sheet 没有表头，要重新确认 Sheet 范围")
    else:
        allowed = (TABLE, MAYBE) if directive.get("auto_detected") else (TABLE, MAYBE, DOCUMENT)
        included = [sheet["name"] for sheet in sheets if sheet["kind"] in allowed and sheet["selectable"]]
    reference = [sheet["name"] for sheet in sheets if sheet["name"] not in included]
    return {**base, "pending": False, "included_sheets": included, "reference_sheets": reference, "sheets": sheets}


def manual_selection(included: list[str], revision: str) -> dict:
    """存进 materials.json 的人工范围。"""
    return {"version": SCOPE_VERSION, "mode": "manual", "included_sheets": list(included), "revision": revision}


def read_selected_headers(path: Path, directive: dict | None = None) -> dict[str, list[str]]:
    """范围内的 Sheet → 表头列；Excel 还没有文本版时为空。"""
    scope = resolve_selection(path, directive if directive is not None else {"purpose": "schema"})
    selected = set(scope["included_sheets"])
    return {sheet["name"]: sheet["headers"] for sheet in scope["sheets"]
            if sheet["name"] in selected and sheet["headers"]}


def explicitly_empty(directive: dict | None) -> bool:
    """用户确认过「一个数据 Sheet 都不选」：没有表头不算读失败。"""
    try:
        manual = stored_selection(directive)
    except SheetSelectionError:
        return False
    return bool(manual) and manual["included_sheets"] == []
