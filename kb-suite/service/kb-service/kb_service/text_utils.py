"""Small text helpers shared by retrieval post-processing and LLM jobs."""

from __future__ import annotations

import json
import re

_CJK = r"㐀-䶿一-鿿豈-﫿"
_TOKEN_RE = re.compile(rf"[{_CJK}]+|[A-Za-z0-9_]+")
_CJK_RUN = re.compile(rf"^[{_CJK}]+$")
_SPACE_RE = re.compile(r"\s+")
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def tokens(text: str) -> set[str]:
    """Bag of tokens for Jaccard: CJK runs as character bigrams, Latin/digit runs as words.

    WeKnora uses jieba here; bigrams need no dictionary and give a comparable
    overlap signal for redundancy checks. Single characters are dropped.
    """

    out: set[str] = set()
    for run in _TOKEN_RE.findall(text.lower()):
        if _CJK_RUN.match(run):
            if len(run) == 1:
                continue
            out.update(run[i : i + 2] for i in range(len(run) - 1))
        elif len(run) > 1:
            out.add(run)
    return out


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def overlap_min(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def normalize(text: str) -> str:
    return _SPACE_RE.sub(" ", text).strip().lower()


_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>", re.IGNORECASE)
_LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")
_TAG_RE = re.compile(r"<[^>]+>")
_LETTER_RE = re.compile(r"[^\W_]")


def text_content(markdown: str) -> tuple[int, int]:
    """(letters and digits outside markup, images) of an L1 markdown.

    Image references, link targets and HTML tags do not count as text, so a
    scanned PDF whose L1 holds only page images has (almost) no text.
    """

    images = len(_IMAGE_RE.findall(markdown))
    bare = _TAG_RE.sub(" ", _LINK_TARGET_RE.sub("]", _IMAGE_RE.sub(" ", markdown)))
    return len(_LETTER_RE.findall(bare)), images


def parse_json_object(text: str) -> dict:
    """Best-effort JSON object from an LLM reply (fences, preamble, raw newlines)."""

    cleaned = _FENCE_RE.sub("", text.strip())
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model output")
    value = json.loads(cleaned[start : end + 1], strict=False)
    if not isinstance(value, dict):
        raise ValueError("model output is not a JSON object")
    return value


def split_summary_line(text: str) -> tuple[str, str]:
    """``SUMMARY: ...`` first line + body, as produced by the wiki prompts."""

    body = _FENCE_RE.sub("", text.strip())
    lines = body.split("\n", 1)
    first = lines[0].strip()
    if first.upper().startswith("SUMMARY:") or first.startswith("SUMMARY："):
        summary = first.split(":", 1)[-1] if ":" in first else first.split("：", 1)[-1]
        rest = lines[1] if len(lines) > 1 else ""
        return summary.strip(), rest.strip()
    return "", body.strip()
