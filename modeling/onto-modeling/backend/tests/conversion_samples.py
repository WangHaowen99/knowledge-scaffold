"""转换器测试用的合成文件（ADR-0073）：在测试里现生成，不放真实资料。

- `pdf(pages)`：最小的 PDF 写法，每页可以有几行文字、一张带格线的表、一张铺满页面的图（模拟扫描件）；
- `docx(...)`、`xlsx(...)`：用 python-docx、openpyxl 生成；
- `zip_bomb(...)`：解压后很大的 OOXML。
"""

from __future__ import annotations

import io
import zipfile


def _stream(header: str, data: bytes) -> bytes:
    return f"<< {header} /Length {len(data)} >>\nstream\n".encode("latin-1") + data + b"\nendstream"


def _text(x: float, y: float, text: str, size: int = 11) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 {size} Tf {x} {y} Td ({escaped}) Tj ET\n".encode("latin-1")


def _table(x: float, top: float, rows: list[list[str]], width: float = 120, height: float = 20) -> bytes:
    out = b"0.5 w\n"
    columns = max(len(row) for row in rows)
    for r in range(len(rows) + 1):
        y = top - r * height
        out += f"{x} {y} m {x + columns * width} {y} l S\n".encode()
    for c in range(columns + 1):
        out += f"{x + c * width} {top} m {x + c * width} {top - len(rows) * height} l S\n".encode()
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            out += _text(x + c * width + 4, top - (r + 1) * height + 6, cell, 10)
    return out


def pdf(pages: list[dict]) -> bytes:
    """pages 每项：{"lines": [str], "table": [[str]] | None, "image": bool}。只用 ASCII（Helvetica）。"""
    objects: dict[int, bytes] = {}
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    objects[4] = _stream("/Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray /BitsPerComponent 8",
                         b"\x00\xff\xff\x00")
    kids = []
    number = 5
    for page in pages:
        content = b""
        if page.get("image"):
            content += b"q 595 0 0 842 0 0 cm /Im1 Do Q\n"
        y = 800
        for line in page.get("lines") or []:
            content += _text(50, y, line)
            y -= 16
        if page.get("table"):
            content += _table(50, y - 20, page["table"])
        objects[number] = _stream("", content)
        objects[number + 1] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {number} 0 R "
                               f"/Resources << /Font << /F1 3 0 R >> /XObject << /Im1 4 0 R >> >> >>").encode()
        kids.append(number + 1)
        number += 2
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for key in sorted(objects):
        offsets[key] = len(out)
        out += f"{key} 0 obj\n".encode() + objects[key] + b"\nendobj\n"
    xref = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for key in range(1, size):
        out += f"{offsets[key]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def docx(paragraphs: list[tuple[str, str]], table: list[list[str]] | None = None) -> bytes:
    """paragraphs 每项 (样式, 文字)，样式如 "Heading 1"、"List Bullet"、"Normal"。"""
    import docx as python_docx

    document = python_docx.Document()
    for style, text in paragraphs:
        document.add_paragraph(text, style=style)
    if table:
        grid = document.add_table(rows=len(table), cols=max(len(row) for row in table))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                grid.cell(r, c).text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def xlsx(sheets: dict[str, list[list]]) -> bytes:
    import openpyxl

    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def zip_bomb(member: str = "word/document.xml", expanded_mb: int = 200) -> bytes:
    """解压后 expanded_mb MiB 的 zip（压缩后很小）：zip 炸弹式的 docx / xlsx。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        with archive.open(member, "w", force_zip64=True) as stream:
            block = b"\0" * (1024 * 1024)
            for _ in range(expanded_mb):
                stream.write(block)
    return buffer.getvalue()


def write_text_version(root, rel: str) -> str:
    """测试里代替连接器网关：用预置转换器把资料区里的 Word、Excel、PDF 转成文本版，和内核落盘的写法一样
    （文件头 + 空行 + 正文，带原件锚点，ADR-0093）。返回文本版的相对路径；转不了（坏文件）时不写，返回空串。"""
    import hashlib
    import importlib.util
    from pathlib import Path

    from app.evidence.convert import ConvertError, ConvertLimits, header

    root = Path(root)
    path = root / rel
    name = {".docx": "convert-docx", ".xlsx": "convert-xlsx", ".xlsm": "convert-xlsx", ".pdf": "convert-pdf"}[path.suffix.lower()]
    entry = Path(__file__).resolve().parents[2] / "assets" / "packs" / name / "converter.py"
    spec = importlib.util.spec_from_file_location(f"samples_{name.replace('-', '_')}", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = path.read_bytes()
    try:
        result = module.convert(data, path.name, ConvertLimits())
    except ConvertError:
        return ""
    if result.state != "converted":
        return ""
    text_rel = "resource/text/" + rel.removeprefix("resource/") + ".md"
    target = root / text_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    body = result.markdown.strip("\n")
    target.write_text(header(path.name, hashlib.sha256(data).hexdigest()[:16], f"{name}@test") + "\n\n" + body + "\n",
                      encoding="utf-8")
    return text_rel
