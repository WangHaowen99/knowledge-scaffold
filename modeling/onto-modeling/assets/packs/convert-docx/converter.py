"""Word（docx）转 Markdown（ADR-0073）：按正文顺序转段落和表格，标题样式转成标题层级，列表项转成「- 」。

Word 没有页的概念，不加位置标记。文末带原件锚点（ADR-0093）：每个段落记 `p:<序号>`（正文里的段落都算，空段也占号），
每个表格行记 `table:<序号>/row:<行号>`，第 n 列就是 `cell:n`（按 Word 的单元格元素数，合并单元格只占一列），
和工具箱 source_extract 直接读 Word 的编号一致。打开前先查 zip 成员数和解压后的大小（挡 zip 炸弹）。
跑在连接器网关的子进程里，用 python-docx（MIT）。
"""

from __future__ import annotations

import io
import re

import docx
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from app.evidence.convert import Conversion, ConvertError, ConvertLimits, anchor_index, cap_output, check_ooxml, md_cell

_HEADING = re.compile(r"^(?:heading|标题)\s*(\d)$", re.IGNORECASE)


def _paragraph(paragraph: Paragraph) -> str:
    text = re.sub(r"[ \t]+", " ", paragraph.text.replace("\x0b", " ")).strip()
    if not text:
        return ""
    style = (paragraph.style.name if paragraph.style is not None else "") or ""
    heading = _HEADING.match(style.strip())
    if heading:
        return "#" * min(int(heading.group(1)), 6) + " " + text
    if style.strip().lower() in ("title", "标题"):
        return "# " + text
    if _numbered(paragraph) or style.strip().lower().startswith("list") or "列表" in style:
        return "- " + text
    return text


def _numbered(paragraph: Paragraph) -> bool:
    """列表项：段落自己或它的样式带编号。"""
    for properties in (paragraph._p.pPr, getattr(getattr(paragraph.style, "element", None), "pPr", None)):
        if properties is not None and properties.numPr is not None:
            return True
    return False


def _cell_text(cell: _Cell) -> str:
    return " ".join(part.text.strip() for part in cell.paragraphs if part.text.strip())


def _table(table: Table) -> list[tuple[str, int]]:
    """表格 → [(Markdown 行, Word 行号)]（表头行的行号是第一行有字的行，分隔行记 0）。按单元格元素逐个取
    （和 source_extract 同口径），整行没字的不出行但照样占行号；列数按最宽的一行补齐。"""
    rows = []
    for number, tr in enumerate(table._tbl.findall(qn("w:tr")), 1):
        cells = [md_cell(_cell_text(_Cell(tc, table))) for tc in tr.findall(qn("w:tc"))]
        if any(cells):
            rows.append((cells, number))
    if not rows:
        return []
    width = max(len(cells) for cells, _ in rows)
    lines = [("| " + " | ".join(cells + [""] * (width - len(cells))) + " |", number) for cells, number in rows]
    return [lines[0], ("|" + "---|" * width, 0), *lines[1:]]


def convert(data: bytes, filename: str, limits: ConvertLimits) -> Conversion:
    check_ooxml(data, limits)
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - 坏文件抛 KeyError、ValueError、lxml 错误等
        raise ConvertError("corrupt") from exc
    parts: list[list[tuple[str, str | None]]] = []   # 每块：[(Markdown 行, 原件锚点)]
    paragraph = table_number = paragraphs = tables = 0
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph += 1
            text = _paragraph(Paragraph(child, document))
            if text:
                parts.append([(text, f"p:{paragraph}")])
                paragraphs += 1
        elif tag == "tbl":
            table_number += 1
            lines = _table(Table(child, document))
            if lines:
                parts.append([(line, f"table:{table_number}/row:{row}" if row else None) for line, row in lines])
                tables += 1
    if not parts:
        return Conversion(state="unsupported", reason="Word 里没有文字", stats={"paragraphs": 0, "tables": 0})
    body, anchors = [], []
    for block in parts:
        if body:
            body.append("")
        for line, anchor in block:
            # 段落里的换行（软回车）展开成几行时，锚点记在第一行
            for k, piece in enumerate(line.split("\n")):
                body.append(piece)
                if anchor and k == 0:
                    anchors.append((len(body), anchor))
    markdown, cut = cap_output("\n".join(body) + "\n", limits)
    kept = markdown.count("\n") + (0 if markdown.endswith("\n") else 1)
    index = anchor_index([(line, anchor) for line, anchor in anchors if line <= kept])
    return Conversion(state="converted", markdown=markdown + ("\n" + index + "\n" if index else ""),
                      note="内容太长，只转了前一部分" if cut else None,
                      stats={"paragraphs": paragraphs, "tables": tables})
