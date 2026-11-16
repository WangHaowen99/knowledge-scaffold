"""provider-kit's ``kb_provider_kit.textfiles``, shared by both adapters (synthetic text only).

provider-kit has no test run of its own; its pieces are tested from the adapters.
"""

from __future__ import annotations

import pytest

from kb_provider_kit import ProviderError
from kb_provider_kit.textfiles import decode_text, file_type, fit_file_name, fit_text, to_utf8

CHINESE = "编码测试，标记 SAMPLE-MARK-0042。\n生僻字：𠀀𪚥龘\n"


@pytest.mark.parametrize("data, text", [
    (CHINESE.encode("gb18030"), CHINESE),
    ("采购会议纪要：基准价上调。".encode("gbk"), "采购会议纪要：基准价上调。"),
    (CHINESE.encode("utf-16-le"), CHINESE),
    (CHINESE.encode("utf-16-be"), CHINESE),
    (CHINESE.encode("utf-16"), CHINESE),
    (CHINESE.encode("utf-32"), CHINESE),
    ("繁體中文內容測試，資料庫".encode("cp950"), "繁體中文內容測試，資料庫"),
    (b"\x93Quoted\x94 text \x96 costs 5 \x80.", "“Quoted” text – costs 5 €."),
    (b"caf\xe9 menu", "café menu"),
])
def test_encodings_are_told_apart(data, text):
    assert decode_text(data) == text
    assert to_utf8("a.txt", data) == text.encode("utf-8")


def test_utf8_comes_back_byte_for_byte():
    for data in (CHINESE.encode(), b"\xef\xbb\xbf" + CHINESE.encode(), b"plain ascii"):
        assert to_utf8("a.md", data) is data


def test_a_utf8_file_cut_inside_a_character_stays_utf8():
    # GB18030 would read every byte pair of it as some other character
    data = ("中文内容" * 10).encode()[:-1]
    assert decode_text(data) == "中文内容" * 9 + "中文内\ufffd"


@pytest.mark.parametrize("data", [
    bytes(range(256)) * 4,                 # binary
    b"\x00\x01\x02\x03" * 50,              # NULs but not UTF-16 text
    "数据".encode("utf-16-le") + b"\x00",  # odd length
])
def test_unknown_encodings_are_refused(data):
    assert decode_text(data) is None
    with pytest.raises(ProviderError) as refused:
        to_utf8("C99_未知编码.csv", data)
    assert refused.value.status == 400 and refused.value.code == "bad_encoding"
    assert "C99_未知编码.csv" in refused.value.message and "UTF-8" in refused.value.message


def test_only_text_types_are_converted():
    gbk = CHINESE.encode("gb18030")
    for name in ("a.pdf", "a.html", "a.xml", "a.xlsx", "noext"):
        assert to_utf8(name, gbk) is gbk
    for name in ("a.TXT", "a.markdown", "a.csv", "a.tsv", "a.json", "a.log", "a.yaml", "dir/a.yml"):
        assert to_utf8(name, gbk) == CHINESE.encode(), name
    assert file_type("目录/报告.JSON") == "json" and file_type("目录.d/README") == ""


def test_names_are_cut_keeping_their_extension():
    name = "C14_超长文件名_" + "储能电池运维知识" * 24 + ".md"
    cut = fit_file_name(name, max_bytes=200)
    assert len(cut.encode()) <= 200 and cut.endswith("….md") and name.startswith(cut[:-4])
    assert fit_file_name("名" * 300 + ".json", max_chars=251) == "名" * 245 + "….json"
    assert fit_file_name("短名.md", max_chars=10, max_bytes=100) == "短名.md"
    # no real extension: the whole name is cut
    assert fit_file_name("x" * 30, max_chars=10) == "x" * 9 + "…"
    assert fit_file_name("a." + "b" * 40, max_chars=10) == "a." + "b" * 7 + "…"


def test_text_is_cut_on_character_boundaries():
    assert fit_text("标" * 301, max_chars=252) == "标" * 251 + "…"
    assert fit_text("标" * 100, max_bytes=31) == "标" * 9 + "…"
    assert fit_text("尾部空格   " + "x" * 20, max_chars=8) == "尾部空格…"
    assert fit_text("不需要截断", max_chars=5) == "不需要截断"
