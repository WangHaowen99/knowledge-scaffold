"""Uploads every backend can read: text files as UTF-8, names within the backend's limits.

Backends guess a text file's encoding themselves and some guesses end in garbled
text that is still marked as parsed: DA's knowledge pipeline reads GB18030 with
four-byte characters as Latin-1, WeKnora reads UTF-16 without a byte order mark
byte by byte. Adapters therefore hand text files over as UTF-8 and refuse a
file whose encoding they cannot tell instead of passing it on. The encoding is
told by kb-service's ``kb_service.text_decode`` (standard library only), the
same code kb-service parses its own text files with.
"""

from __future__ import annotations

from kb_service import text_decode

from . import ProviderError

# read as plain text; html and xml are left alone because they declare their own
# charset, which re-encoded bytes would contradict
TEXT_TYPES = frozenset({"txt", "text", "md", "markdown", "csv", "tsv", "json", "log", "yaml", "yml"})
ELLIPSIS = "…"
# longest extension kept whole when a file name is cut
_MAX_EXT_BYTES = 16


def file_type(name: str) -> str:
    """The lower-case extension of ``name`` (empty without one)."""

    base = name.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[1].lower() if "." in base else ""


def decode_text(data: bytes) -> str | None:
    """The text in ``data`` without NUL characters, or None when its encoding cannot be told."""

    return text_decode.decode(data)


def to_utf8(name: str, data: bytes) -> bytes:
    """``data`` as UTF-8 when ``name`` is a text type; UTF-8 files come back byte for byte.

    A text file whose encoding cannot be told is refused with a 400 ``bad_encoding``.
    """

    if not data or file_type(name) not in TEXT_TYPES:
        return data
    if b"\x00" not in data:
        try:
            data.decode("utf-8")
            return data
        except UnicodeDecodeError:
            pass
    text = decode_text(data)
    if text is None:
        raise ProviderError(400, "bad_encoding",
                            f"无法识别文件「{name}」的文字编码（支持 {text_decode.SUPPORTED}），请另存为 UTF-8 后再上传")
    return text.encode("utf-8")


def _fits(text: str, max_chars: int | None, max_bytes: int | None) -> bool:
    return (max_chars is None or len(text) <= max_chars) and (max_bytes is None or len(text.encode()) <= max_bytes)


def fit_text(text: str, *, max_chars: int | None = None, max_bytes: int | None = None) -> str:
    """``text`` unchanged when it is within the limits, else cut and ended with an ellipsis."""

    if _fits(text, max_chars, max_bytes):
        return text
    if max_chars is not None:
        text = text[:max_chars - len(ELLIPSIS)]
    if max_bytes is not None:
        text = text.encode()[:max_bytes - len(ELLIPSIS.encode())].decode("utf-8", errors="ignore")
    return text.rstrip() + ELLIPSIS


def fit_file_name(name: str, *, max_chars: int | None = None, max_bytes: int | None = None) -> str:
    """A file name within the limits; a cut name keeps its extension (``很长的名字….md``)."""

    if _fits(name, max_chars, max_bytes):
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext.encode()) > _MAX_EXT_BYTES:
        return fit_text(name, max_chars=max_chars, max_bytes=max_bytes)
    suffix = dot + ext
    return fit_text(
        stem,
        max_chars=None if max_chars is None else max_chars - len(suffix),
        max_bytes=None if max_bytes is None else max_bytes - len(suffix.encode()),
    ) + suffix
