"""Word（docx）转 Markdown（ADR-0073）：按正文顺序转段落和表格，标题样式转成标题层级，列表项转成「- 」。

Word 没有页的概念，不加位置标记。打开前先查 zip 成员数和解压后的大小（挡 zip 炸弹）。
跑在连接器网关的子进程里，用 python-docx（MIT）。
"""

from __future__ import annotations

import io
import re

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.evidence.convert import Conversion, ConvertError, ConvertLimits, cap_output, check_ooxml, md_table

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


def _table(table: Table) -> str:
    rows = []
    for row in table.rows:
        cells, previous = [], None
        for cell in row.cells:
            if cell._tc is previous:  # 合并单元格：python-docx 每格都返回同一个
                continue
            previous = cell._tc
            cells.append(" ".join(part.text.strip() for part in cell.paragraphs if part.text.strip()))
        rows.append(cells)
    return md_table(rows)


def convert(data: bytes, filename: str, limits: ConvertLimits) -> Conversion:
    check_ooxml(data, limits)
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - 坏文件抛 KeyError、ValueError、lxml 错误等
        raise ConvertError("corrupt") from exc
    parts = []
    paragraphs = tables = 0
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = _paragraph(Paragraph(child, document))
            if text:
                parts.append(text)
                paragraphs += 1
        elif tag == "tbl":
            text = _table(Table(child, document))
            if text:
                parts.append(text)
                tables += 1
    if not parts:
        return Conversion(state="unsupported", reason="Word 里没有文字", stats={"paragraphs": 0, "tables": 0})
    markdown, cut = cap_output("\n\n".join(parts) + "\n", limits)
    return Conversion(state="converted", markdown=markdown, note="内容太长，只转了前一部分" if cut else None,
                      stats={"paragraphs": paragraphs, "tables": tables})
