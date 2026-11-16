"""The shared text-encoding guess (kb_service.text_decode), used by kb-service and both adapters (synthetic text only)."""

from __future__ import annotations

import pytest

from kb_service import text_decode
from kb_service.parsing import ParseError, native

CHINESE = "编码测试，标记 SAMPLE-MARK-0042。\n生僻字：𠀀𪚥龘\n"
LOG_LINE = b"2026-11-11 10:00:01 INFO job\x00 started ok"  # 41 bytes, one stray NUL


@pytest.mark.parametrize(
    "data, text",
    [
        (CHINESE.encode("gb18030"), CHINESE),  # four-byte GB18030 characters
        ("采购会议纪要：基准价上调。".encode("gbk"), "采购会议纪要：基准价上调。"),
        (CHINESE.encode("utf-16-le"), CHINESE),
        (CHINESE.encode("utf-16-be"), CHINESE),
        (CHINESE.encode("utf-16"), CHINESE),
        (CHINESE.encode("utf-32"), CHINESE),
        (b"\xef\xbb\xbf" + CHINESE.encode(), CHINESE),
        ("繁體中文內容測試，資料庫".encode("cp950"), "繁體中文內容測試，資料庫"),
        (b"\x93Quoted\x94 text \x96 costs 5 \x80.", "“Quoted” text – costs 5 €."),
        (b"caf\xe9 menu, cr\xe8me br\xfbl\xe9e", "café menu, crème brûlée"),
    ],
)
def test_encodings_are_told_apart(data, text):
    assert text_decode.decode(data) == text


@pytest.mark.parametrize("data", [LOG_LINE, LOG_LINE + b"!"])  # odd and even length
def test_utf8_with_one_nul_stays_utf8(data):
    assert text_decode.decode(data) == data.replace(b"\x00", b"").decode()


@pytest.mark.parametrize("extra", [b"", b"."])
def test_ascii_with_a_few_nuls_on_one_side_is_not_utf16(extra):
    data = b"hello world\x00 this is a plain ascii log line\x00 with two stray nul bytes on one side..." + extra
    assert data[1::2].count(0) == 2 or data[0::2].count(0) == 2  # both NULs at the same parity
    assert text_decode.decode(data) == data.replace(b"\x00", b"").decode()


def test_utf16_without_bom_with_little_ascii_is_found():
    text = "这是一段几乎全是中文的合成文本，只有换行是 ASCII 字符。\n" * 3
    assert text_decode.decode(text.encode("utf-16-le")) == text
    assert text_decode.decode(text.encode("utf-16-be")) == text


def test_gb18030_with_a_stray_nul_is_read():
    data = "合成日志：设备启动".encode("gb18030") + b"\x00" + "完成，共 12 项".encode("gb18030")
    assert text_decode.decode(data) == "合成日志：设备启动完成，共 12 项"


def test_a_utf8_file_cut_inside_a_character_stays_utf8():
    data = ("中文内容" * 10).encode()[:-1]
    assert text_decode.decode(data) == "中文内容" * 9 + "中文内\ufffd"


@pytest.mark.parametrize(
    "data",
    [
        bytes(range(256)) * 4,  # binary
        b"\x00\x01\x02\x03" * 50,  # NULs, but not UTF-16 text
        "数据".encode("utf-16-le") + b"\x00",  # odd length
        b"abc\x00de",  # too few NULs to tell UTF-16, too many to be stray
    ],
)
def test_unknown_encodings_are_refused(data):
    assert text_decode.decode(data) is None


def test_kb_service_reports_an_unknown_encoding_readably():
    with pytest.raises(ParseError) as info:
        native.decode_text(bytes(range(256)) * 4)
    assert info.value.code == "bad_encoding" and "UTF-8" in info.value.message and "Big5" in info.value.message
    assert native.decode_text("繁體中文".encode("cp950")) == "繁體中文"
