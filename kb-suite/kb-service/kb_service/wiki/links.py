"""Wiki link helpers: ``[[slug|text]]`` parsing, dead-link cleanup, auto-linking.

Behaviour follows WeKnora (wiki_linkify.go and the finalize step): out-links
are parsed from content on every write, dead links collapse to their text,
and the first safe occurrence of another page's title is linked (longest
titles first, never a single Han character, never inside code, links,
images or headings).
"""

from __future__ import annotations

import re
import unicodedata

LINK_RE = re.compile(r"\[\[([^\[\]|\n]+?)(?:\|([^\[\]\n]+?))?\]\]")
_PROTECTED_RE = re.compile(
    r"```.*?```|~~~.*?~~~|`[^`\n]*`|\[\[[^\]\n]*\]\]|!?\[[^\]\n]*\]\([^)\n]*\)|<[^>\n]+>|https?://\S+|^#{1,6} [^\n]*$",
    re.DOTALL | re.MULTILINE,
)
_SLUG_SAFE = re.compile(r"[^0-9a-z㐀-鿿\-]+")
_HAN_ONLY = re.compile(r"^[㐀-鿿]$")
PAGE_TYPES = ("entity", "concept")


def parse_links(content: str) -> list[str]:
    out: list[str] = []
    for match in LINK_RE.finditer(content):
        slug = match.group(1).strip()
        if slug and slug not in out:
            out.append(slug)
    return out


def plain(text: str) -> str:
    """``[[slug|text]]`` -> ``text`` for one-line summaries and search snippets."""

    return LINK_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), text or "")


def strip_dead_links(content: str, valid: set[str], self_slug: str = "") -> str:
    def repl(match: re.Match) -> str:
        slug = match.group(1).strip()
        text = (match.group(2) or slug).strip()
        if slug in valid and slug != self_slug:
            return match.group(0)
        return text

    return LINK_RE.sub(repl, content)


def _protected_spans(content: str) -> list[tuple[int, int]]:
    return [m.span() for m in _PROTECTED_RE.finditer(content)]


def linkify(content: str, targets: list[tuple[str, str]], self_slug: str = "") -> str:
    """Link the first safe occurrence of each ``(title, slug)`` not yet linked."""

    linked = set(parse_links(content))
    for title, slug in sorted(targets, key=lambda t: len(t[0]), reverse=True):
        title = title.strip()
        if slug == self_slug or slug in linked or len(title) < 2 or _HAN_ONLY.match(title):
            continue
        spans = _protected_spans(content)
        start = 0
        while True:
            idx = content.find(title, start)
            if idx == -1:
                break
            end = idx + len(title)
            inside = any(s <= idx < e or s < end <= e for s, e in spans)
            # Do not split a Latin word: "API" inside "APIs" is not a match.
            left_ok = idx == 0 or not (content[idx - 1].isascii() and content[idx - 1].isalnum())
            right_ok = end >= len(content) or not (content[end].isascii() and content[end].isalnum())
            if not inside and left_ok and right_ok:
                content = f"{content[:idx]}[[{slug}|{title}]]{content[end:]}"
                linked.add(slug)
                break
            start = idx + 1
    return content


def normalize_title(name: str) -> str:
    text = unicodedata.normalize("NFKC", name or "").lower()
    return re.sub(r"[\s\-_·•.,，。:：;；'\"“”‘’()（）\[\]【】/\\]+", "", text)


def clean_slug(page_type: str, slug: str, name: str) -> str:
    """``<type>/<lowercase-hyphenated>``; falls back to the name when the model's slug is unusable."""

    raw = (slug or "").strip().lower()
    if "/" in raw:
        raw = raw.split("/", 1)[1]
    raw = raw.replace(" ", "-").replace("_", "-")
    raw = _SLUG_SAFE.sub("", raw).strip("-")
    if not raw:
        raw = _SLUG_SAFE.sub("", unicodedata.normalize("NFKC", name).lower().replace(" ", "-")).strip("-")
    raw = re.sub(r"-{2,}", "-", raw)[:80] or "page"
    return f"{page_type}/{raw}"
