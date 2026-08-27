"""Deterministic Unicode and Chinese-bigram lexical analysis."""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable


ANALYZER_VERSION = "zh-bigram/1"
_ASCII_RE = re.compile(r"[a-z0-9_./-]+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


def normalize_query(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().lower()


def lexical_tokens(*values: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for value in values:
        normalized = normalize_query(value)
        for token in _ASCII_RE.findall(normalized):
            _append_unique(tokens, token)
        for sequence in _CJK_RE.findall(normalized):
            if len(sequence) == 1:
                _append_unique(tokens, sequence)
                continue
            for index in range(len(sequence) - 1):
                _append_unique(tokens, sequence[index : index + 2])
    return tuple(tokens)


def fts_query(tokens: Iterable[str], *, limit: int = 32) -> str:
    safe: list[str] = []
    for token in tokens:
        value = str(token or "").replace('"', '""').strip()
        if value:
            safe.append('"{}"'.format(value))
        if len(safe) >= limit:
            break
    return " OR ".join(safe)


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)
