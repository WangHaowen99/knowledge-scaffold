"""Excel（xlsx、xlsm）转 Markdown（ADR-0073）：每个工作表前一行位置标记加一个二级标题，下面一张 Markdown 表格。

第一行非空行当表头；大表只转前 max_rows 行、前 max_columns 列，表后写明截断。公式取缓存的计算结果，不自己算。
打开前先查 zip 成员数和解压后的大小（挡 zip 炸弹）。跑在连接器网关的子进程里，用 openpyxl（MIT）。
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


def _sheet(sheet, limits: ConvertLimits) -> tuple[str, str | None]:
    rows: list[list[str]] = []
    total = 0
    for raw in sheet.iter_rows(values_only=True):
        values = [_value(cell) for cell in raw]
        if not any(value.strip() for value in values):
            continue
        total += 1
        if len(rows) < limits.max_rows:
            rows.append(values)
        elif total >= COUNT_CAP:
            break
    if not rows:
        return "（空表）", None
    width = max((max((i for i, v in enumerate(row) if v.strip()), default=-1) + 1 for row in rows), default=0)
    columns = min(width, limits.max_columns)
    table = md_table([row[:columns] + [""] * (columns - len(row[:columns])) for row in rows])
    cuts = []
    if total > len(rows):
        cuts.append(f"只转了前 {len(rows)} 行，共 {'超过 ' if total >= COUNT_CAP else ''}{total} 行")
    if width > columns:
        cuts.append(f"只转了前 {columns} 列，共 {width} 列")
    note = "；".join(cuts) or None
    return table + (f"\n\n（{note}）" if note else ""), note


def convert(data: bytes, filename: str, limits: ConvertLimits) -> Conversion:
    check_ooxml(data, limits)
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - 坏文件抛 KeyError、InvalidFileException、zip 错误等
        raise ConvertError("corrupt") from exc
    parts, notes = [], []
    try:
        sheets = workbook.worksheets
        for sheet in sheets[: limits.max_sheets]:
            body, note = _sheet(sheet, limits)
            parts.append(f"{sheet_marker(sheet.title)}\n## {sheet.title}\n\n{body}")
            if note:
                notes.append(f"工作表 {sheet.title} {note}")
    finally:
        workbook.close()
    if len(sheets) > limits.max_sheets:
        notes.append(f"只转了前 {limits.max_sheets} 个工作表，共 {len(sheets)} 个")
    markdown, cut = cap_output("\n\n".join(parts) + "\n", limits)
    if cut:
        notes.append("内容太长，只转了前一部分")
    return Conversion(state="converted", markdown=markdown, locations=parse_locations(markdown),
                      note="；".join(notes) or None, stats={"sheets": len(sheets)})
