"""F4a 预置转换器（ADR-0073）：PDF（文字版）、Word、Excel 转 Markdown，带页码、工作表标记。

合成文件在测试里现生成（conversion_samples）。转换库只装在连接器网关镜像里（backend/connector-requirements.txt），
测试镜像没装时整个文件跳过。
"""

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("pdfplumber")
pytest.importorskip("docx")

import conversion_samples as samples  # noqa: E402

from app.evidence.convert import ConvertError, ConvertLimits, parse_locations  # noqa: E402

PACKS = Path(__file__).resolve().parents[2] / "assets" / "packs"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"f4a_{name.replace('-', '_')}", PACKS / name / "converter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PDF, DOCX, XLSX = _load("convert-pdf"), _load("convert-docx"), _load("convert-xlsx")
LIMITS = ConvertLimits()


def test_pdf_pages_tables_and_markers():
    data = samples.pdf([
        {"lines": ["Refund policy", "Orders older than 30 days are not refunded."],
         "table": [["Grade", "Limit"], ["A", "100"], ["B", "200"]]},
        {"lines": ["Second page carries enough text."]},
    ])
    result = PDF.convert(data, "policy.pdf", LIMITS)
    assert result.state == "converted"
    assert [item.label for item in result.locations] == ["第 1 页", "第 2 页"]
    assert result.markdown.startswith("<!-- 第 1 页 -->\n")
    assert "| Grade | Limit |\n|---|---|\n| A | 100 |" in result.markdown
    # 表格里的字不再在正文里重复一遍
    assert result.markdown.count("100") == 1
    assert parse_locations(result.markdown) == result.locations
    assert result.note is None


def test_pdf_scanned_is_unsupported():
    result = PDF.convert(samples.pdf([{"image": True}, {"image": True}]), "scan.pdf", LIMITS)
    assert result.state == "unsupported"
    assert result.reason == "扫描件或图片型 PDF，暂不支持"
    assert result.markdown == ""


def test_pdf_mixed_marks_image_pages():
    data = samples.pdf([{"lines": ["A page with plenty of text on it."]}, {"image": True}])
    result = PDF.convert(data, "mixed.pdf", LIMITS)
    assert result.state == "converted"
    assert "（这一页是图片，没有转）" in result.markdown
    assert result.note == "第 2 页是图片，没有转"


@pytest.mark.parametrize("data", [b"%PDF-1.4\ngarbage", b"not a pdf at all", b""])
def test_pdf_corrupt(data):
    with pytest.raises(ConvertError) as caught:
        PDF.convert(data, "bad.pdf", LIMITS)
    assert caught.value.code == "corrupt"


def test_pdf_too_many_pages_truncates_with_note():
    data = samples.pdf([{"lines": [f"Page {n} has a sentence of body text."]} for n in range(1, 31)])
    result = PDF.convert(data, "long.pdf", ConvertLimits(max_pages=10))
    assert result.state == "converted"
    assert len(result.locations) == 10
    assert result.note == "只转了前 10 页（共 30 页）"


def test_docx_headings_lists_tables():
    data = samples.docx([("Heading 1", "第一章 退款"), ("Normal", "金额 | 含竖线"), ("List Bullet", "门店先核对"),
                         ("Heading 2", "1.1 条件")], [["编号", "说明"], ["R-01", "超过 30 天不退"]])
    result = DOCX.convert(data, "规范.docx", LIMITS)
    assert result.state == "converted"
    lines = result.markdown.splitlines()
    assert "# 第一章 退款" in lines and "## 1.1 条件" in lines and "- 门店先核对" in lines
    assert "金额 | 含竖线" in lines  # 正文里的竖线不用转义，表格里的才转
    assert "| R-01 | 超过 30 天不退 |" in lines
    assert result.locations == []


def test_docx_zip_bomb_and_corrupt():
    with pytest.raises(ConvertError) as caught:
        DOCX.convert(samples.zip_bomb("word/document.xml", 120), "bomb.docx", LIMITS)
    assert caught.value.code == "too_large"
    with pytest.raises(ConvertError) as caught:
        DOCX.convert(b"PK\x03\x04 broken", "bad.docx", LIMITS)
    assert caught.value.code == "corrupt"


def test_xlsx_sheet_per_table_with_truncation():
    data = samples.xlsx({"客户": [["编号", "名称"], [1, "甲"], [2.0, None]],
                         "空": [],
                         "流水": [[n, n * 2] for n in range(50)]})
    result = XLSX.convert(data, "台账.xlsx", ConvertLimits(max_rows=10))
    assert result.state == "converted"
    assert [item.label for item in result.locations] == ["工作表 客户", "工作表 空", "工作表 流水"]
    assert "## 客户\n\n| 编号 | 名称 |\n|---|---|\n| 1 | 甲 |\n| 2 |  |" in result.markdown
    assert "（空表）" in result.markdown
    assert "（只转了前 10 行，共 50 行）" in result.markdown
    assert result.note == "工作表 流水 只转了前 10 行，共 50 行"


def test_xlsx_keeps_every_sheet_and_column_header_and_bounds_data_rows():
    """宽表、多工作表：表头一列不少、一个工作表不少（对象抽取按表头取列名）；数据行按格数少转几行。"""
    header = [f"COL_{n:03d}" for n in range(120)]
    sheets = {f"表{n:02d}": [header, *[[f"v{n}-{r}"] * 120 for r in range(5)]] for n in range(60)}
    result = XLSX.convert(samples.xlsx(sheets), "宽表.xlsx", ConvertLimits(max_sheet_cells=360))
    assert [item.label for item in result.locations] == [f"工作表 表{n:02d}" for n in range(60)]
    assert result.markdown.count("| " + " | ".join(header) + " |") == 60
    # 120 列、360 格：连表头每表只放 3 行，说明里写明
    assert "| v0-1 |" in result.markdown and "| v0-2 |" not in result.markdown
    assert "（只转了前 3 行，共 6 行）" in result.markdown


def test_xlsx_output_cap_drops_data_rows_before_headers():
    """全文到上限时后面的工作表只留表头，不截掉表头。"""
    header = [f"FIELD_{n:02d}" for n in range(30)]
    sheets = {f"S{n}": [header, *[[f"x{r}"] * 30 for r in range(40)]] for n in range(4)}
    one_head = len("| " + " | ".join(header) + " |")
    result = XLSX.convert(samples.xlsx(sheets), "多表.xlsx", ConvertLimits(max_output_chars=one_head * 12))
    assert result.markdown.count("| " + " | ".join(header) + " |") == 4
    assert "（只转了表头，共 41 行）" in result.markdown
    assert len(result.markdown) <= one_head * 12 and "内容太长" not in (result.note or "")


def test_xlsx_zip_bomb_and_corrupt():
    with pytest.raises(ConvertError) as caught:
        XLSX.convert(samples.zip_bomb("xl/worksheets/sheet1.xml", 120), "bomb.xlsx", LIMITS)
    assert caught.value.code == "too_large"
    with pytest.raises(ConvertError) as caught:
        XLSX.convert(b"PK\x03\x04 broken", "bad.xlsx", LIMITS)
    assert caught.value.code == "corrupt"
