"""Excel（xlsx、xlsm）转 Markdown（ADR-0073）：每个工作表前一行位置标记加一个二级标题，下面一张 Markdown 表格。

第一行非空行当表头。表头保留全部工作表、全部列（对象抽取按表头取表名与列名，少一列就对不上）；数据行每表最多
max_rows 行、max_sheet_cells 格（宽表少转几行），全文到 max_output_chars 时后面的工作表只留表头，表后写明截断。
最坏输出不超过 max_output_chars 个字符：只有表头本身就超过它（几千个工作表、上万列）时才截掉尾部。
公式取缓存的计算结果，不自己算。打开前先查 zip 成员数和解压后的大小（挡 zip 炸弹）。跑在连接器网关的子进程里，用 openpyxl（MIT）。
"""

from __future__ import annotations

import datetime as dt
import io

import openpyxl

from app.evidence.convert import Conversion, ConvertError, ConvertLimits, cap_output, check_ooxml, md_table, \
    parse_locations, sheet_marker

COUNT_CAP = 1_000_000        # 截断后接着数总行数，最多数到这里


def _value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time(0) else value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    return str(value)


def _width(values: list[str]) -> int:
    return max((i for i, v in enumerate(values) if v.strip()), default=-1) + 1


def _rows(sheet, limits: ConvertLimits) -> tuple[list[list[str]], int]:
    """工作表 → (表头加前面的数据行, 非空行总数)。表头整行留下；连表头到 max_rows 行或 max_sheet_cells 格为止
    （按每行实际的列数算，渲染时再按补齐后的列数收一次）。"""
    rows: list[list[str]] = []
    total = cells = 0
    full = False
    for raw in sheet.iter_rows(values_only=True):
        values = [_value(cell) for cell in raw]
        width = _width(values)
        if not width:
            continue
        total += 1
        if not full and rows and (len(rows) >= limits.max_rows or cells + width > limits.max_sheet_cells):
            full = True
        if not full:
            rows.append(values[:width])
            cells += width
        elif total >= COUNT_CAP:
            break
    return rows, total


def _table(rows: list[list[str]]) -> tuple[str, list[str], int]:
    """行 → (表头两行, 数据行, 列数)；列数按最宽的一行补齐。md_cell 把换行写成 <br>，一行数据就是一行文本。"""
    width = max(len(row) for row in rows)
    lines = md_table([row + [""] * (width - len(row)) for row in rows]).split("\n")
    return "\n".join(lines[:2]), lines[2:], width


def _note(kept: int, total: int) -> str | None:
    if total <= kept:
        return None
    count = f"{'超过 ' if total >= COUNT_CAP else ''}{total}"
    return f"只转了前 {kept} 行，共 {count} 行" if kept > 1 else f"只转了表头，共 {count} 行"


def convert(data: bytes, filename: str, limits: ConvertLimits) -> Conversion:
    check_ooxml(data, limits)
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - 坏文件抛 KeyError、InvalidFileException、zip 错误等
        raise ConvertError("corrupt") from exc
    tables = []
    try:
        for sheet in workbook.worksheets:
            rows, total = _rows(sheet, limits)
            # 每表读完就拼成文本行，不同时攒着全部工作表的单元格
            tables.append((sheet.title, *(_table(rows) if rows else ("", [], 0)), total))
    finally:
        workbook.close()
    # 表头优先：先给全部工作表的标题、表头和截断说明留出位置，剩下的按工作表顺序放数据行
    heads = [f"{sheet_marker(title)}\n## {title}\n\n{header or '（空表）'}" for title, header, *_ in tables]
    budget = limits.max_output_chars - sum(len(head) + 80 for head in heads)
    parts, notes = [], []
    for head, (title, _header, lines, width, total) in zip(heads, tables, strict=True):
        kept = []
        for line in lines[: limits.max_sheet_cells // width - 1] if width else []:
            if len(line) + 1 > budget:
                break
            kept.append(line)
            budget -= len(line) + 1
        note = _note(len(kept) + 1, total) if width else None
        parts.append("\n".join([head, *kept]) + (f"\n\n（{note}）" if note else ""))
        if note:
            notes.append(f"工作表 {title} {note}")
    markdown, cut = cap_output("\n\n".join(parts) + "\n", limits)
    if cut:
        notes.append("内容太长，只转了前一部分")
    return Conversion(state="converted", markdown=markdown, locations=parse_locations(markdown),
                      note="；".join(notes) or None, stats={"sheets": len(tables)})
