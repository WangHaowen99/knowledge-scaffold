"""Telling the encoding of a plain-text file.

Standard library only: kb-service parses text files with it, and the adapters
(through provider-kit's ``textfiles``) use it to hand text over as UTF-8, so
every backend reads the same bytes the same way.

Order: a Unicode byte order mark; UTF-8 (a few stray NUL bytes are dropped);
UTF-16 without a byte order mark, told by NUL high bytes on one side of the
2-byte units; UTF-8 with a few broken sequences; GB18030 (covers GBK and
GB2312, four-byte characters included); Big5; Windows-1252 for mostly-ASCII
Western text. Anything else is refused rather than stored as garbage.
"""

from __future__ import annotations

import codecs
import re

SUPPORTED = "UTF-8、UTF-16、GB18030/GBK、Big5、Windows-1252"

# UTF-32 first: its little-endian mark starts with the UTF-16 one.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)
# What a correctly decoded text hardly ever holds: control characters other than tab,
# line breaks, form feed and ESC (colour codes in logs), private-use characters, U+FFFD.
_IMPLAUSIBLE = re.compile("[\x00-\x08\x0b\x0e-\x1a\x1c-\x1f\x7f\ue000-\uf8ff\ufffd]")
# A UTF-8 file may carry a few stray NULs (a log, a bad export); UTF-16 text with ASCII
# in it has a NUL in up to every other byte.
_STRAY_NUL_SHARE = 0.1
# UTF-16 without a mark: NUL high bytes in at least this share of the 2-byte units (and
# at least two of them), and this many times more on that side than on the other.
_UTF16_NUL_SHARE = 0.02
_UTF16_SKEW = 8
# Correctly decoded Chinese uses common characters: at least this share of the CJK
# ideographs is in GB2312 or Big5. ASCII bytes paired the wrong way land on rare ones.
_COMMON_CJK_SHARE = 0.8
_CJK_SAMPLE = 400
# Big5 is taken over GB18030 only with clearly more common characters (by this share).
_BIG5_MARGIN = 0.1
# A damaged UTF-8 file still has this many good non-ASCII characters per broken sequence.
_UTF8_GOOD_PER_BAD = 20
# Windows-1252 is taken for Western text only: at most this share of non-ASCII characters.
_WESTERN_MAX_NON_ASCII = 0.3


def _strict(data: bytes, codec: str) -> str | None:
    try:
        return data.decode(codec)
    except UnicodeDecodeError:
        return None


def plausible(text: str) -> bool:
    """At most one control, private-use or replacement character per 1000 characters (and one more)."""

    limit = 1 + len(text) // 1000
    for count, _ in enumerate(_IMPLAUSIBLE.finditer(text), 1):
        if count > limit:
            return False
    return True


def _non_ascii(text: str) -> int:
    return len(text) - len(text.encode("ascii", "ignore"))


def _common_cjk(text: str) -> bool:
    ideographs = [ch for ch in text if "\u4e00" <= ch <= "\u9fff"][:_CJK_SAMPLE]
    if len(ideographs) < 5:
        return True
    common = 0
    for ch in ideographs:
        for codec in ("gb2312", "cp950"):
            try:
                ch.encode(codec)
            except UnicodeEncodeError:
                continue
            common += 1
            break
    return common >= _COMMON_CJK_SHARE * len(ideographs)


def _utf16_without_bom(data: bytes) -> str | None:
    if len(data) % 2:
        return None
    units = len(data) // 2
    even, odd = data[0::2].count(0), data[1::2].count(0)
    high, low, codec = (odd, even, "utf-16-le") if odd >= even else (even, odd, "utf-16-be")
    if high < max(2, _UTF16_NUL_SHARE * units) or low * _UTF16_SKEW > high:
        return None
    text = _strict(data, codec)
    if text is None or "\x00" in text or not plausible(text) or not _common_cjk(text):
        return None
    return text


def _damaged_utf8(data: bytes) -> str | None:
    # e.g. a UTF-8 file cut inside a character: GB18030 would read all of it as other characters
    text = data.decode("utf-8", errors="replace")
    bad = text.count("\ufffd")
    return text if bad and _non_ascii(text) - bad >= _UTF8_GOOD_PER_BAD * bad else None


def _level_one(ch: str) -> bool:
    """One of the most used Chinese characters: GB2312 level 1 or Big5 level 1."""

    try:
        if 0xB0 <= ch.encode("gb2312")[0] <= 0xD7:
            return True
    except UnicodeEncodeError:
        pass
    try:
        return 0xA440 <= int.from_bytes(ch.encode("cp950"), "big") <= 0xC67E
    except UnicodeEncodeError:
        return False


def _common_share(text: str) -> float:
    # kana and Hangul count against: GB18030 reads the most frequent Big5 lead bytes as kana
    letters = [ch for ch in text if "\u3040" <= ch <= "\u9fff" or "\uac00" <= ch <= "\ud7af"][:_CJK_SAMPLE]
    if not letters:
        return 1.0
    return sum(_level_one(ch) for ch in letters) / len(letters)


def _chinese(data: bytes) -> str | None:
    """GB18030 (GBK, GB2312) or Big5; when both read the bytes, the one with more common characters."""

    texts = []
    for codec in ("gb18030", "cp950"):
        text = _strict(data, codec)
        # GB18030 also reads Big5 byte pairs, but lands many of them in its private-use area
        if text is not None and plausible(text):
            texts.append(text)
    if len(texts) == 2 and _common_share(texts[1]) > _common_share(texts[0]) + _BIG5_MARGIN:
        return texts[1]
    return texts[0] if texts else None


def _western(data: bytes) -> str | None:
    text = _strict(data, "cp1252")
    if text is None or _non_ascii(text) > _WESTERN_MAX_NON_ASCII * len(text):
        return None
    return text if plausible(text) else None


def decode(data: bytes) -> str | None:
    """The text in ``data`` without NUL characters, or None when its encoding cannot be told."""

    for bom, codec in _BOMS:
        if data.startswith(bom):
            text = _strict(data[len(bom) :], codec)
            return text if text is not None and plausible(text) else None
    data = data.removeprefix(codecs.BOM_UTF8)
    nuls = data.count(0)
    if nuls:
        stray = nuls <= _STRAY_NUL_SHARE * len(data)
        text = _strict(data, "utf-8")
        if text is not None and stray:
            text = text.replace("\x00", "")
            return text if plausible(text) else None
        text = _utf16_without_bom(data)
        if text is not None or not stray:
            return text
        data = data.replace(b"\x00", b"")
    text = _strict(data, "utf-8")
    if text is not None:
        return text
    for guess in (_damaged_utf8, _chinese, _western):
        text = guess(data)
        if text is not None:
            return text
    return None
