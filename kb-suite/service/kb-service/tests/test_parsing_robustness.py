"""Encodings, empty tables and damaged files end in a clear result instead of garbage or a stuck document."""

from __future__ import annotations

import asyncio
import codecs
import io
import zipfile

import pytest

from kb_service.parsing import (
    ParseError,
    SourceBlock,
    check_container,
    docreader_client,
    native,
    normalize_newlines,
    parse_file,
)

C17_TEXT = "UTF-16 测试文本\n标记 UTF16-MARK-3391\n第二行：合成数据\n"


def test_utf16le_without_bom_is_decoded():
    text = native.decode_text(C17_TEXT.encode("utf-16-le"))
    assert text == C17_TEXT and "\x00" not in text


def test_utf16be_without_bom_and_boms_are_decoded():
    assert native.decode_text(C17_TEXT.encode("utf-16-be")) == C17_TEXT
    assert native.decode_text(codecs.BOM_UTF16_LE + C17_TEXT.encode("utf-16-le")) == C17_TEXT
    assert native.decode_text(codecs.BOM_UTF32_LE + C17_TEXT.encode("utf-32-le")) == C17_TEXT
    assert native.decode_text(codecs.BOM_UTF8 + C17_TEXT.encode("utf-8")) == C17_TEXT


def test_utf8_and_gb18030_still_work_and_stray_nul_is_dropped():
    assert native.decode_text("合成文本 ABC".encode("utf-8")) == "合成文本 ABC"
    assert native.decode_text("合成文本 ABC".encode("gb18030")) == "合成文本 ABC"
    assert native.decode_text(b"line one\x00\nline two" + b" padding" * 50) == "line one\nline two" + " padding" * 50


def test_binary_data_named_txt_fails_with_a_reason():
    blob = bytes(range(256)) * 40
    with pytest.raises(ParseError) as info:
        native.decode_text(blob)
    assert info.value.code == "bad_encoding" and "二进制" in info.value.message


def test_parse_file_reads_utf16_text_to_completion():
    result = asyncio.run(parse_file("C17_UTF16LE.txt", C17_TEXT.encode("utf-16-le")))
    assert "UTF16-MARK-3391" in result.markdown and "\x00" not in result.markdown


def test_header_only_csv_and_tsv_fail_as_empty():
    for name, data in (("C19.csv", "编号,名称,数量\n,,\n"), ("C19.tsv", "编号\t名称\n")):
        with pytest.raises(ParseError) as info:
            asyncio.run(parse_file(name, data.encode("utf-8")))
        assert info.value.code == "empty_content" and "表格没有数据" in info.value.message


def test_csv_with_rows_still_parses():
    result = native.read_delimited("编号,名称\n1,泵\n2,阀\n".encode("utf-8"), delimiter=",")
    assert "| 1 | 泵 |" in result.markdown and result.metadata["rows"] == "2"


@pytest.mark.parametrize("line", ["编号,名称,数量", "地区,2025,2026", "Name,Type,Remark"])
def test_a_lone_header_row_is_still_empty(line):
    with pytest.raises(ParseError, match="只有表头"):
        native.read_delimited(line.encode("utf-8"), delimiter=",")


@pytest.mark.parametrize("line", ["1,离心泵,3.2", "P-01,2026-11-11,运行", "阀门,85%"])
def test_a_lone_row_with_numbers_or_dates_is_data(line):
    result = native.read_delimited(line.encode("utf-8"), delimiter=",")
    cells = line.split(",")
    assert "| " + " | ".join(cells) + " |" in result.markdown.splitlines()[2:]  # a body row, not the header
    assert result.metadata["rows"] == "1" and result.source_blocks[0].locator["row_start"] == 1


def test_header_only_xlsx_fails_as_empty():
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    workbook.active.append(["编号", "名称"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    with pytest.raises(ParseError) as info:
        native.read_xlsx(buffer.getvalue())
    assert info.value.code == "empty_content" and "Excel 没有数据" in info.value.message

    workbook = openpyxl.Workbook()
    workbook.active.append([1, "离心泵", 3.2])
    buffer = io.BytesIO()
    workbook.save(buffer)
    assert "| 1 | 离心泵 | 3.2 |" in native.read_xlsx(buffer.getvalue()).markdown


def _zip(names: list[str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, "<xml/>")
    return buffer.getvalue()


def test_damaged_docx_fails_before_docreader(monkeypatch):
    async def docreader_must_not_run(*args, **kwargs):
        raise AssertionError("a damaged docx must not be sent to docreader")

    monkeypatch.setattr(docreader_client, "read_file", docreader_must_not_run)
    for data in (b"PK\x03\x04 not really a docx", _zip(["[Content_Types].xml", "other/part.xml"])):
        with pytest.raises(ParseError) as info:
            asyncio.run(parse_file("C29_damaged.docx", data))
        assert info.value.code == "parse_failed" and "Word 文档" in info.value.message


def test_text_disguised_as_pdf_fails_with_a_reason():
    with pytest.raises(ParseError) as info:
        check_container("pdf", "这只是一段文字，不是 PDF".encode("utf-8"))
    assert info.value.code == "parse_failed" and "PDF" in info.value.message


def test_valid_containers_pass_the_check():
    check_container("docx", _zip(["[Content_Types].xml", "word/document.xml"]))
    check_container("pptx", _zip(["[Content_Types].xml", "ppt/presentation.xml"]))
    check_container("docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)  # old .doc renamed: docreader converts
    check_container("pdf", b"%PDF-1.7\n...")
    check_container("md", b"anything")


def test_normalize_drops_nul_and_shifts_blocks():
    markdown = "ab\x00c\r\nd"
    out, blocks = normalize_newlines(markdown, [SourceBlock(4, 7, {"page": 1})])
    assert out == "abc\nd" and out[blocks[0].start : blocks[0].end] == "\nd"
