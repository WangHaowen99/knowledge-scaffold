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

# A slug the model wrote as text instead of a link ("entity/y-chu-neng（乙储能）"),
# or a link without display text followed by the name in brackets
# ("[[entity/y-chu-neng]]（乙储能）"). Slugs inside links, paths and URLs are left
# alone. Unknown slugs are matched in their romanized form only: a slug with Han
# characters cannot be told apart from the text that follows it.
_PAREN = r"(?:[ \t]*[（(]([^（）()\[\]\n]{1,80})[）)])?"
_SLUG_BEFORE = r"(?<![A-Za-z0-9_/\[|.\-])"
_SLUG_AFTER = r"(?![A-Za-z0-9_/\-])"
_ROMAN_SLUG = r"(?:entity|concept)/[0-9a-z]+(?:-[0-9a-z]+)*"
_BARE_SLUG_RE = re.compile(f"{_SLUG_BEFORE}({_ROMAN_SLUG}){_SLUG_AFTER}{_PAREN}")
_NAMELESS_LINK_RE = re.compile(r"\[\[([^\[\]|\n]+?)\]\]" + _PAREN)
_CODE_OR_URL_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`|https?://\S+", re.DOTALL)


def parse_links(content: str) -> list[str]:
    out: list[str] = []
    for match in LINK_RE.finditer(content):
        slug = match.group(1).strip()
        if slug and slug not in out:
            out.append(slug)
    return out


def link_names(pages: list[dict]) -> dict[str, list[str]]:
    """``{slug: [title, *aliases]}`` of pages (``title`` / ``aliases`` or a candidate's ``name``)."""

    out: dict[str, list[str]] = {}
    for page in pages:
        title = str(page.get("title") or page.get("name") or "").strip()
        names = [n for n in (title, *(page.get("aliases") or [])) if str(n).strip()]
        if page.get("slug") and names:
            out.setdefault(page["slug"], [str(n).strip() for n in names])
    return out


def _slug_text(slug: str) -> str:
    return slug.split("/", 1)[-1]


def _resolve(slug: str, shown: str | None, names: dict[str, list[str]]) -> tuple[str | None, str]:
    """(display name, trailing text) for a slug written without display text.

    The bracketed text is the display name when it is one of the page's names
    or the page is unknown; otherwise the page title is shown and the
    bracketed remark stays after it.
    """

    known = names.get(slug)
    shown = (shown or "").strip()
    if not known:
        return (shown or None), ""
    if not shown or normalize_title(shown) in {normalize_title(n) for n in known}:
        return (shown or known[0]), ""
    return known[0], f"（{shown}）"


def _bare_slug_re(names: dict[str, list[str]]) -> re.Pattern:
    known = sorted((s for s in names if "/" in s), key=len, reverse=True)
    if not known:
        return _BARE_SLUG_RE
    alternatives = "|".join([*(re.escape(s) for s in known), _ROMAN_SLUG])
    return re.compile(f"{_SLUG_BEFORE}({alternatives}){_SLUG_AFTER}{_PAREN}")


def normalize_links(content: str, names: dict[str, list[str]]) -> str:
    """Rewrite slugs written as text, and links without display text, as ``[[slug|name]]``.

    ``names`` maps slugs to their page names (see :func:`link_names`). A text
    slug of an unknown page becomes its bracketed name; unknown links are left
    for :func:`strip_dead_links`.
    """

    def nameless(match: re.Match) -> str:
        slug = match.group(1).strip()
        shown, rest = _resolve(slug, match.group(2), names)
        if shown is None:
            return match.group(0)
        return f"[[{slug}|{shown}]]{rest}"

    def bare(match: re.Match) -> str:
        if any(s <= match.start() < e for s, e in protected):
            return match.group(0)
        slug = match.group(1)
        shown, rest = _resolve(slug, match.group(2), names)
        if shown is None:
            return match.group(0)
        return f"[[{slug}|{shown}]]{rest}" if slug in names else shown

    text = _NAMELESS_LINK_RE.sub(nameless, content or "")
    protected = [m.span() for m in _CODE_OR_URL_RE.finditer(text)]
    return _bare_slug_re(names).sub(bare, text)


def plain(text: str, names: dict[str, list[str]] | None = None) -> str:
    """``[[slug|text]]`` -> ``text`` for one-line summaries and search snippets.

    Slugs never show as text: a link without display text shows the page name
    (from ``names``, or the name the model put in brackets after it).
    """

    text = normalize_links(text or "", names or {})
    return LINK_RE.sub(lambda m: (m.group(2) or _slug_text(m.group(1))).strip(), text)


def strip_dead_links(content: str, valid: set[str], self_slug: str = "") -> str:
    def repl(match: re.Match) -> str:
        slug = match.group(1).strip()
        text = (match.group(2) or _slug_text(slug)).strip()
        if slug in valid and slug != self_slug:
            return match.group(0)
        return text

    return LINK_RE.sub(repl, content)


def _protected_spans(content: str) -> list[tuple[int, int]]:
    return [m.span() for m in _PROTECTED_RE.finditer(content)]


def _linkable_title(title: str) -> bool:
    return len(title) >= 2 and not _HAN_ONLY.match(title)


def linkify(content: str, targets: list[tuple[str, str]], self_slug: str = "") -> str:
    """Link the first safe occurrence of each ``(title, slug)`` not yet linked."""

    linked = set(parse_links(content))
    for title, slug in sorted(targets, key=lambda t: len(t[0]), reverse=True):
        title = title.strip()
        if slug == self_slug or slug in linked or not _linkable_title(title):
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
