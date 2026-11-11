"""Readers for formats handled without docreader."""

from __future__ import annotations

import csv
import io
import json

from . import ParseError, ParseResult, SourceBlock

ROWS_PER_BLOCK = 20


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_text(data: bytes) -> ParseResult:
    return ParseResult(markdown=decode_text(data), engine="native-text")


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _table_markdown(
    rows: list[list[str]], *, sheet: str | None, offset: int
) -> tuple[str, list[SourceBlock]]:
    """Render rows as a GFM table; one source block per row group."""

    if not rows:
        return "", []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header, body = rows[0], rows[1:]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * width]
    blocks: list[SourceBlock] = []
    text = "\n".join(lines) + "\n"
    group_start = offset + len(text)
    group_first = 2  # 1-based data row number in the source (row 1 is the header)
    for index, row in enumerate(body):
        line = "| " + " | ".join(row) + " |\n"
        text += line
        last = index == len(body) - 1
        if (index + 1) % ROWS_PER_BLOCK == 0 or last:
            row_end = index + 2
            locator = {"type": "sheet", "row_start": group_first, "row_end": row_end}
            if sheet:
                locator["sheet"] = sheet
            blocks.append(SourceBlock(group_start, offset + len(text), locator))
            group_start = offset + len(text)
            group_first = row_end + 1
    return text, blocks


def read_delimited(data: bytes, *, delimiter: str) -> ParseResult:
    text = decode_text(data)
    rows = [[_cell(c) for c in row] for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        raise ParseError("empty_content", "表格没有数据")
    markdown, blocks = _table_markdown(rows, sheet=None, offset=0)
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
    for sheet in workbook.worksheets:
        rows = [[_cell(c) for c in row] for row in sheet.iter_rows(values_only=True)]
        rows = [r for r in rows if any(c for c in r)]
        if not rows:
            continue
        heading = f"## {sheet.title}\n\n"
        parts.append(heading)
        offset += len(heading)
        table, table_blocks = _table_markdown(rows, sheet=sheet.title, offset=offset)
        parts.append(table + "\n")
        blocks.extend(table_blocks)
        offset += len(table) + 1
        sheets += 1
    workbook.close()
    if not sheets:
        raise ParseError("empty_content", "Excel 没有数据")
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
