"""Readers for formats handled without docreader."""

from __future__ import annotations

import csv
import io
import json
import re

from .. import text_decode
from . import ParseError, ParseResult, SourceBlock

ROWS_PER_BLOCK = 20


def decode_text(data: bytes) -> str:
    """Text of a plain-text file, without NUL characters (see :mod:`kb_service.text_decode`).

    A file whose encoding cannot be told fails to parse instead of being stored as garbage.
    """

    text = text_decode.decode(data)
    if text is None:
        raise ParseError(
            "bad_encoding",
            f"无法识别文件的文字编码（支持 {text_decode.SUPPORTED}），也可能是二进制文件；请另存为 UTF-8 后再上传",
        )
    return text


def read_text(data: bytes) -> ParseResult:
    return ParseResult(markdown=decode_text(data), engine="native-text")


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _table_markdown(
    rows: list[list[str]], *, sheet: str | None, offset: int, first_data_row: int = 2
) -> tuple[str, list[SourceBlock]]:
    """Render rows as a GFM table; one source block per row group.

    ``first_data_row`` is the 1-based source row of ``rows[1]``: 2 when the
    source has a header row, 1 when the header was made up (see :func:`_with_header`).
    """

    if not rows:
        return "", []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header, body = rows[0], rows[1:]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * width]
    blocks: list[SourceBlock] = []
    text = "\n".join(lines) + "\n"
    group_start = offset + len(text)
    group_first = first_data_row
    for index, row in enumerate(body):
        line = "| " + " | ".join(row) + " |\n"
        text += line
        last = index == len(body) - 1
        if (index + 1) % ROWS_PER_BLOCK == 0 or last:
            row_end = index + first_data_row
            locator = {"type": "sheet", "row_start": group_first, "row_end": row_end}
            if sheet:
                locator["sheet"] = sheet
            blocks.append(SourceBlock(group_start, offset + len(text), locator))
            group_start = offset + len(text)
            group_first = row_end + 1
    return text, blocks


# A lone row is a header unless one of its cells is a number or a date: a header names
# columns. A bare year (2024) can name a column, so it does not count as data.
_NUMBER = re.compile(r"[+-]?(?:\d+(?:[.,]\d+)*|\d*\.\d+)%?")
_DATE = re.compile(r"\d{4}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?")
_YEAR = re.compile(r"(?:19|20)\d{2}")


def _data_like(row: list[str]) -> bool:
    return any(
        cell and not _YEAR.fullmatch(cell) and (_NUMBER.fullmatch(cell) or _DATE.fullmatch(cell)) for cell in row
    )


def _with_header(rows: list[list[str]]) -> tuple[list[list[str]], int]:
    """(rows with a header row first, the source row of the first data row).

    A single row that looks like data gets numbered column names instead of
    being taken as a header with no data under it.
    """

    if len(rows) == 1 and _data_like(rows[0]):
        return [[f"列{i + 1}" for i in range(len(rows[0]))], rows[0]], 1
    return rows, 2


def read_delimited(data: bytes, *, delimiter: str) -> ParseResult:
    text = decode_text(data)
    rows = [[_cell(c) for c in row] for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        raise ParseError("empty_content", "表格没有数据")
    rows, first_data_row = _with_header(rows)
    if len(rows) == 1:
        raise ParseError("empty_content", "表格没有数据（只有表头，没有数据行）")
    markdown, blocks = _table_markdown(rows, sheet=None, offset=0, first_data_row=first_data_row)
    return ParseResult(
        markdown=markdown,
        engine="native-csv",
        source_blocks=blocks,
        metadata={"rows": str(len(rows) - 1)},
    )


def read_xlsx(data: bytes) -> ParseResult:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency is in the image
        raise ParseError("parser_unavailable", "openpyxl 未安装") from exc
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - corrupt or encrypted workbook
        raise ParseError("corrupt_file", f"Excel 文件无法读取: {exc}") from exc
    parts: list[str] = []
    blocks: list[SourceBlock] = []
    offset = 0
    sheets = 0
    data_rows = 0
    for sheet in workbook.worksheets:
        rows = [[_cell(c) for c in row] for row in sheet.iter_rows(values_only=True)]
        rows = [r for r in rows if any(c for c in r)]
        if not rows:
            continue
        rows, first_data_row = _with_header(rows)
        heading = f"## {sheet.title}\n\n"
        parts.append(heading)
        offset += len(heading)
        table, table_blocks = _table_markdown(rows, sheet=sheet.title, offset=offset, first_data_row=first_data_row)
        parts.append(table + "\n")
        blocks.extend(table_blocks)
        offset += len(table) + 1
        sheets += 1
        data_rows += len(rows) - 1
    workbook.close()
    if not sheets:
        raise ParseError("empty_content", "Excel 没有数据")
    if not data_rows:
        raise ParseError("empty_content", "Excel 没有数据（只有表头，没有数据行）")
    return ParseResult(
        markdown="".join(parts),
        engine="native-xlsx",
        source_blocks=blocks,
        metadata={"sheets": str(sheets)},
    )


def read_json(data: bytes) -> ParseResult:
    text = decode_text(data)
    try:
        pretty = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        pretty = text
    return ParseResult(markdown=f"```json\n{pretty}\n```\n", engine="native-json")
