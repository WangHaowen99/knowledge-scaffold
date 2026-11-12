"""Excel（xlsx、xlsm）转 Markdown（ADR-0073）：每个工作表前一行位置标记加一个二级标题，下面一张 Markdown 表格。

第一行非空行当表头。表头保留全部工作表、全部列（对象抽取按表头取表名与列名，少一列就对不上）；数据行每表最多
max_rows 行、max_sheet_cells 格（宽表少转几行），全文到 max_output_chars 时后面的工作表只留表头，表后写明截断。
最坏输出不超过 max_output_chars 个字符：只有表头本身就超过它（几千个工作表、上万列）时才截掉尾部。
公式取缓存的计算结果，不自己算。打开前先查 zip 成员数和解压后的大小（挡 zip 炸弹）。跑在连接器网关的子进程里，用 openpyxl（MIT）。
文末带原件锚点（ADR-0093）：每个工作表记 `sheet:<工作表名>`，每个表格行记它在 Excel 里的行号 `row:<n>`（表格从 A 列起，
第 n 列就是第 n 个列字母），合并区记 `merge <工作表名> A1:C1 …`，助手和主服务据此写出、找回 `sheet:表!B3`。
"""

from __future__ import annotations

import datetime as dt
import io
import posixpath
import zipfile
from xml.etree import ElementTree as ET

import openpyxl

from app.evidence.convert import Conversion, ConvertError, ConvertLimits, anchor_index, cap_output, check_ooxml, \
    md_table, parse_locations, sheet_marker

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


def _rows(sheet, limits: ConvertLimits) -> tuple[list[list[str]], list[int], int]:
    """工作表 → (表头加前面的数据行, 各行在 Excel 里的行号, 非空行总数)。表头整行留下；连表头到 max_rows 行或
    max_sheet_cells 格为止（按每行实际的列数算，渲染时再按补齐后的列数收一次）。从 A1 读起，行号、列序和 Excel 一致。"""
    rows: list[list[str]] = []
    numbers: list[int] = []
    total = cells = 0
    full = False
    for number, raw in enumerate(sheet.iter_rows(min_row=1, min_col=1, values_only=True), 1):
        values = [_value(cell) for cell in raw]
        width = _width(values)
        if not width:
            continue
        total += 1
        if not full and rows and (len(rows) >= limits.max_rows or cells + width > limits.max_sheet_cells):
            full = True
        if not full:
            rows.append(values[:width])
            numbers.append(number)
            cells += width
        elif total >= COUNT_CAP:
            break
    return rows, numbers, total


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
       "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}


def _merges(data: bytes) -> dict[str, list[str]]:
    """{工作表名: [合并区 A1:C1]}：只读模式不加载合并区，这里直接读各工作表 XML 里的 mergeCell。读不到就没有。"""
    out: dict[str, list[str]] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            book = ET.fromstring(archive.read("xl/workbook.xml"))
            rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {rel.get("Id"): rel.get("Target") for rel in rels.findall("rel:Relationship", _NS)}
            for sheet in book.findall("m:sheets/m:sheet", _NS):
                target = targets.get(sheet.get(f"{{{_NS['r']}}}id")) or ""
                name = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
                if name not in archive.namelist():
                    continue
                refs = []
                for _event, element in ET.iterparse(archive.open(name)):
                    if element.tag == f"{{{_NS['m']}}}mergeCell" and element.get("ref"):
                        refs.append(element.get("ref"))
                    element.clear()
                if refs:
                    out[sheet.get("name") or ""] = refs
    except (KeyError, ET.ParseError, zipfile.BadZipFile, OSError, ValueError):
        return {}
    return out


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
            rows, numbers, total = _rows(sheet, limits)
            # 每表读完就拼成文本行，不同时攒着全部工作表的单元格
            tables.append((sheet.title, *(_table(rows) if rows else ("", [], 0)), total, numbers))
    finally:
        workbook.close()
    # 表头优先：先给全部工作表的标题、表头和截断说明留出位置，剩下的按工作表顺序放数据行
    heads = [f"{sheet_marker(title)}\n## {title}\n\n{header or '（空表）'}" for title, header, *_ in tables]
    budget = limits.max_output_chars - sum(len(head) + 80 for head in heads)
    parts, notes, anchors = [], [], []
    line = 0  # 已经排下的正文行数（各工作表之间空一行）
    for head, (title, _header, lines, width, total, numbers) in zip(heads, tables, strict=True):
        kept = []
        for row in lines[: limits.max_sheet_cells // width - 1] if width else []:
            if len(row) + 1 > budget:
                break
            kept.append(row)
            budget -= len(row) + 1
        note = _note(len(kept) + 1, total) if width else None
        start = line + (1 if parts else 0)
        anchors.append((start + 1, f"sheet:{title}"))
        if width:  # 表头在标记、标题、空行之后的第 4 行，分隔行之后是数据行
            anchors.append((start + 4, f"row:{numbers[0]}"))
            anchors += [(start + 6 + k, f"row:{numbers[k + 1]}") for k in range(len(kept))]
        part = "\n".join([head, *kept]) + (f"\n\n（{note}）" if note else "")
        parts.append(part)
        line = start + part.count("\n") + 1
        if note:
            notes.append(f"工作表 {title} {note}")
    markdown, cut = cap_output("\n\n".join(parts) + "\n", limits)
    if cut:
        notes.append("内容太长，只转了前一部分")
    kept_lines = markdown.count("\n") + (0 if markdown.endswith("\n") else 1)
    index = anchor_index([(n, anchor) for n, anchor in anchors if n <= kept_lines], _merges(data))
    return Conversion(state="converted", markdown=markdown + ("\n" + index + "\n" if index else ""),
                      locations=parse_locations(markdown), note="；".join(notes) or None, stats={"sheets": len(tables)})
