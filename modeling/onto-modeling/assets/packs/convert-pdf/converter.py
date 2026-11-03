"""PDF（文字版）转 Markdown（ADR-0073）：每页前一行页码标记，表格转成 Markdown 表格，正文按行距分段。

没有文字层的扫描件、图片型 PDF 判「暂不支持」；只有个别页是图片的，那几页标出来，其余照转。
跑在连接器网关的子进程里（有超时、内存上限），用 pdfplumber / pdfminer.six（MIT）。
"""

from __future__ import annotations

import io
import re

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect

from app.evidence.convert import Conversion, ConvertError, ConvertLimits, cap_output, md_table, page_marker, \
    parse_locations, unsupported

MIN_PAGE_CHARS = 20          # 少于这么多字的页按「没有文字」算
IMAGE_PAGE_AREA = 0.5        # 图片占页面这么大、又没有文字的页按「图片页」算
FIGURE_AREA = 0.1            # 有文字、但图片占这么大的页，记一笔「图里的文字没转」
PARAGRAPH_GAP = 0.8          # 行距超过行高的这么多倍就分段
_CJK = re.compile(r"[　-〿㐀-鿿＀-￯]")


def _join(lines: list[str]) -> str:
    """同一段里的几行接起来：中文接中文不加空格。"""
    out = ""
    for line in lines:
        if out and not (_CJK.match(out[-1]) and _CJK.match(line[0])):
            out += " "
        out += line
    return out


def _inside(line: dict, boxes: list[tuple]) -> bool:
    middle_x = (line["x0"] + line["x1"]) / 2
    middle_y = (line["top"] + line["bottom"]) / 2
    return any(x0 <= middle_x <= x1 and top <= middle_y <= bottom for x0, top, x1, bottom in boxes)


def _page_blocks(page) -> tuple[list[tuple[float, str]], int]:
    """一页里按从上到下排好的块（段落或表格）与字数。"""
    tables = page.find_tables()
    boxes = [table.bbox for table in tables]
    blocks: list[tuple[float, str]] = []
    for table in tables:
        text = md_table(table.extract() or [])
        if text:
            blocks.append((table.bbox[1], text))
    lines = [line for line in page.extract_text_lines() if line.get("text", "").strip() and not _inside(line, boxes)]
    paragraph: list[str] = []
    top = previous_bottom = None
    for line in lines:
        height = max(line["bottom"] - line["top"], 1.0)
        if paragraph and previous_bottom is not None and line["top"] - previous_bottom > height * PARAGRAPH_GAP:
            blocks.append((top, _join(paragraph)))
            paragraph, top = [], None
        if top is None:
            top = line["top"]
        paragraph.append(line["text"].strip())
        previous_bottom = line["bottom"]
    if paragraph:
        blocks.append((top, _join(paragraph)))
    blocks.sort(key=lambda item: item[0])
    chars = sum(len(re.sub(r"\s", "", text)) for _, text in blocks)
    return blocks, chars


def _image_area(page) -> float:
    area = float(page.width * page.height) or 1.0
    return sum(max(image["x1"] - image["x0"], 0) * max(image["bottom"] - image["top"], 0) for image in page.images) / area


def _ranges(numbers: list[int]) -> str:
    parts, start = [], None
    for index, number in enumerate(numbers):
        if start is None:
            start = number
        if index + 1 == len(numbers) or numbers[index + 1] != number + 1:
            parts.append(str(start) if start == number else f"{start}–{number}")
            start = None
    return "、".join(parts)


def convert(data: bytes, filename: str, limits: ConvertLimits) -> Conversion:
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
        total = len(pdf.pages)
    except PDFPasswordIncorrect:
        return unsupported("PDF 设了密码，暂不支持")
    except Exception as exc:  # noqa: BLE001 - pdfminer 对坏文件抛各种异常
        raise ConvertError("corrupt") from exc
    with pdf:
        count = min(total, limits.max_pages)
        parts: list[str] = []
        image_pages: list[int] = []
        figure_pages: list[int] = []
        text_pages = 0
        for index in range(count):
            page = pdf.pages[index]
            blocks, chars = _page_blocks(page)
            area = _image_area(page)
            parts.append(page_marker(index + 1))
            if chars >= MIN_PAGE_CHARS:
                text_pages += 1
                if area >= FIGURE_AREA:
                    figure_pages.append(index + 1)
            elif area >= IMAGE_PAGE_AREA:
                image_pages.append(index + 1)
                parts.append("（这一页是图片，没有转）")
            parts.extend(text for _, text in blocks)
            parts.append("")
            page.close()
    if text_pages == 0:
        reason = "扫描件或图片型 PDF，暂不支持" if image_pages or figure_pages else "PDF 里没有文字"
        return unsupported(reason, pages=total)
    markdown, cut = cap_output("\n\n".join(part for part in parts if part != "").replace(" -->\n\n", " -->\n") + "\n",
                               limits)
    notes = []
    if total > count or cut:
        notes.append(f"只转了前 {count if not cut else len(parse_locations(markdown))} 页（共 {total} 页）")
    if image_pages:
        notes.append(f"第 {_ranges(image_pages)} 页是图片，没有转")
    if figure_pages:
        notes.append(f"第 {_ranges(figure_pages)} 页的图片里的文字没有转")
    return Conversion(state="converted", markdown=markdown, locations=parse_locations(markdown),
                      note="；".join(notes) or None, stats={"pages": total, "converted_pages": count})
