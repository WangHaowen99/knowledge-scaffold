"""Single deterministic parser for ontology Context, Evidence, and Graph."""

from __future__ import annotations

import re
import threading
import unicodedata
from collections import OrderedDict
from pathlib import PurePosixPath
from typing import Any, Iterable, Optional

import yaml
from ontology.hash_contract import field_s, hash_frames, normalize_relative_path
from ontology.resource_types import (
    ParsedResource,
    ParsedTable,
    ParserWarning,
    ResourceLink,
    ResourceSection,
    ResourceSource,
)

PARSER_VERSION = "resource-parser/1"
RESOURCE_TYPES = {"root", "operation", "object", "rule", "metric", "script", "asset"}
LINK_RELATIONS = {"uses", "applies_to", "references", "bound_operation"}
PUBLIC_METADATA = {
    "key",
    "canonical_key",
    "title",
    "summary",
    "description",
    "aliases",
    "tags",
    "intent_examples",
}
_KEY_FIELDS = ("key", "canonical_key", "operation_key", "object_key", "rule_key", "metric_key", "script_key")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_WIKI_LINK_RE = re.compile(
    r"\[\[(?P<resource>[a-z]+)[:/](?P<key>[^\]|#]+)(?:#(?P<anchor>[^\]|]+))?(?:\|(?P<relation>[a-z_]+))?\]\]"
)
_ONTOLOGY_LINK_RE = re.compile(r"\[[^\]]*\]\(ontology://(?P<resource>[a-z]+)/(?P<key>[^)#]+)(?:#(?P<anchor>[^)]+))?\)")


class ResourceParser:
    """Parse one already-authorized UTF-8 ontology resource."""

    def parse(self, source: ResourceSource) -> ParsedResource:
        resource = str(source.resource or "").strip()
        if resource not in RESOURCE_TYPES:
            raise ValueError("unsupported ontology resource type")
        source_path = normalize_relative_path(source.source_path)
        if not re.fullmatch(r"[0-9a-f]{64}", source.source_content_sha256):
            raise ValueError("source_content_sha256 must be lowercase SHA-256 hex")

        display_text = _normalize_source_text(source.source_text)
        frontmatter, body_text, body_start_line, warnings = _parse_frontmatter(display_text)
        headings = _collect_headings(body_text, body_start_line)
        key = _resource_key(resource, frontmatter, source_path)
        canonical_key = str(frontmatter.get("canonical_key") or key).strip()
        title = _resource_title(resource, key, frontmatter, headings)
        metadata = _build_public_metadata(key, canonical_key, title, frontmatter)

        sections, section_warnings = _build_sections(body_text, body_start_line, headings)
        warnings.extend(section_warnings)
        document_links = _deduplicate_links(link for section in sections for link in section.links)
        if any(str(key).strip() not in PUBLIC_METADATA and key not in _KEY_FIELDS for key in frontmatter):
            warnings.append(ParserWarning(code="UNKNOWN_METADATA_IGNORED"))

        return ParsedResource(
            parser_version=PARSER_VERSION,
            ontology_id=str(source.ontology_id),
            ontology_version=str(source.ontology_version),
            resource=resource,
            key=key,
            canonical_key=canonical_key,
            title=title,
            source_path=source_path,
            source_content_sha256=source.source_content_sha256,
            media_type=str(source.media_type),
            display_text=display_text,
            frontmatter=frontmatter,
            evidence_metadata=metadata,
            body_text=body_text,
            sections=tuple(sections),
            document_links=tuple(document_links),
            warnings=tuple(_deduplicate_warnings(warnings)),
        )


class ResourceParserCache:
    """Bounded single-flight cache shared by Context and index construction."""

    def __init__(self, *, max_entries: int = 512) -> None:
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Condition()
        self._cache: OrderedDict[tuple[Any, ...], ParsedResource] = OrderedDict()
        self._inflight: set[tuple[Any, ...]] = set()

    def parse(
        self,
        source: ResourceSource,
        *,
        workspace_id: str,
        mutation_revision: int,
    ) -> ParsedResource:
        key = (
            str(workspace_id),
            str(source.ontology_id),
            int(mutation_revision),
            str(source.source_path),
            str(source.source_content_sha256),
            PARSER_VERSION,
        )
        while True:
            with self._lock:
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
                    return cached
                if key not in self._inflight:
                    self._inflight.add(key)
                    break
                self._lock.wait()
        try:
            parsed = ResourceParser().parse(source)
        except Exception:
            with self._lock:
                self._inflight.discard(key)
                self._lock.notify_all()
            raise
        with self._lock:
            self._cache[key] = parsed
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)
            self._inflight.discard(key)
            self._lock.notify_all()
        return parsed


_SHARED_PARSER_CACHE = ResourceParserCache()


def parse_resource_cached(
    source: ResourceSource,
    *,
    workspace_id: str,
    mutation_revision: int,
) -> ParsedResource:
    return _SHARED_PARSER_CACHE.parse(
        source,
        workspace_id=workspace_id,
        mutation_revision=mutation_revision,
    )


def _normalize_source_text(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", normalized)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str, int, list[ParserWarning]]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text, 1, []
    closing = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if closing is None:
        return {}, text, 1, [ParserWarning(code="FRONTMATTER_INVALID", start_line=1, end_line=1)]
    raw = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :])
    try:
        parsed = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError:
        return {}, body, closing + 2, [ParserWarning(code="FRONTMATTER_INVALID", start_line=1, end_line=closing + 1)]
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        return {}, body, closing + 2, [ParserWarning(code="FRONTMATTER_NOT_OBJECT", start_line=1, end_line=closing + 1)]
    return dict(parsed), body, closing + 2, []


def _collect_headings(body: str, body_start_line: int) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    path_by_level: dict[int, str] = {}
    for offset, line in enumerate(body.splitlines()):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        for stale_level in [key for key in path_by_level if key >= level]:
            path_by_level.pop(stale_level, None)
        path_by_level[level] = title
        path = tuple(path_by_level[key] for key in sorted(path_by_level) if key <= level)
        headings.append(
            {"index": offset, "line": body_start_line + offset, "level": level, "title": title, "path": path}
        )
    return headings


def _resource_key(resource: str, frontmatter: dict[str, Any], source_path: str) -> str:
    if resource == "root":
        return "root"
    if resource == "asset":
        return source_path.removeprefix("assets/")
    for field_name in _KEY_FIELDS:
        value = str(frontmatter.get(field_name) or "").strip()
        if value:
            return value
    return PurePosixPath(source_path).stem


def _resource_title(
    resource: str,
    key: str,
    frontmatter: dict[str, Any],
    headings: list[dict[str, Any]],
) -> str:
    explicit = str(frontmatter.get("title") or frontmatter.get("name") or "").strip()
    if explicit:
        return explicit
    first_h1 = next((heading["title"] for heading in headings if heading["level"] == 1), "")
    return first_h1 or ("root" if resource == "root" else key)


def _build_public_metadata(
    key: str,
    canonical_key: str,
    title: str,
    frontmatter: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {"key": key, "canonical_key": canonical_key, "title": title}
    for name in ("summary", "description"):
        value = str(frontmatter.get(name) or "").strip()
        if value:
            result[name] = value
    for name in ("aliases", "tags", "intent_examples"):
        result[name] = _string_list(frontmatter.get(name))
    return result


def _string_list(value: Any) -> list[str]:
    items = value if isinstance(value, list) else re.split(r"[,，、;；\n]+", str(value or ""))
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _build_sections(
    body: str,
    body_start_line: int,
    headings: list[dict[str, Any]],
) -> tuple[list[ResourceSection], list[ParserWarning]]:
    lines = body.splitlines()
    warnings: list[ParserWarning] = []
    h1s = [heading for heading in headings if heading["level"] == 1]
    if len(h1s) > 1:
        warnings.extend(
            ParserWarning(code="MULTIPLE_H1", start_line=item["line"], end_line=item["line"]) for item in h1s[1:]
        )
    section_headings = [heading for heading in headings if 2 <= heading["level"] <= 6]
    ranges: list[tuple[Optional[dict[str, Any]], int, int]] = []
    preamble_start = h1s[0]["index"] + 1 if h1s else 0
    first_section_index = section_headings[0]["index"] if section_headings else len(lines)
    if any(line.strip() for line in lines[preamble_start:first_section_index]):
        ranges.append((None, preamble_start, first_section_index))
    all_heading_indexes = [heading["index"] for heading in headings]
    for heading in section_headings:
        next_index = next((index for index in all_heading_indexes if index > heading["index"]), len(lines))
        ranges.append((heading, heading["index"], next_index))

    sections: list[ResourceSection] = []
    anchor_counts: dict[str, int] = {}
    for ordinal, (heading, start, end) in enumerate(ranges):
        raw_text = "\n".join(lines[start:end]).strip("\n")
        if not raw_text.strip():
            continue
        path = tuple(heading["path"]) if heading else tuple(h1["title"] for h1 in h1s[:1])
        base_anchor = "_preamble" if heading is None else "/".join(_encode_anchor_part(item) for item in path)
        anchor_counts[base_anchor] = anchor_counts.get(base_anchor, 0) + 1
        anchor = base_anchor if anchor_counts[base_anchor] == 1 else f"{base_anchor}~{anchor_counts[base_anchor]}"
        if anchor != base_anchor:
            warnings.append(
                ParserWarning(
                    code="DUPLICATE_ANCHOR_RENAMED",
                    start_line=body_start_line + start,
                    end_line=body_start_line + start,
                )
            )
        start_line = body_start_line + start
        tables, table_warnings = _parse_tables(raw_text, start_line)
        warnings.extend(table_warnings)
        links, link_warnings = _parse_links(raw_text, start_line)
        warnings.extend(link_warnings)
        normalized_text = _normalize_section_text(raw_text)
        sections.append(
            ResourceSection(
                section_anchor=anchor,
                heading_path=path,
                heading_level=heading["level"] if heading else None,
                title=heading["title"] if heading else "",
                ordinal=len(sections),
                text=raw_text,
                plain_text=_plain_text(raw_text),
                start_line=start_line,
                end_line=body_start_line + end - 1,
                tables=tuple(tables),
                links=tuple(links),
                content_hash=hash_frames("resource_section_content/v1", [field_s("text", normalized_text)]),
            )
        )
    return sections, warnings


def _encode_anchor_part(value: str) -> str:
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFC", value).strip()).lower()
    encoded: list[str] = []
    for char in text:
        if char == "%":
            encoded.append("%25")
        elif char == "/":
            encoded.append("%2F")
        elif ord(char) < 32 or ord(char) == 127:
            encoded.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
        else:
            encoded.append(char)
    return "".join(encoded)


def _normalize_section_text(text: str) -> str:
    lines = [
        unicodedata.normalize("NFC", line).rstrip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _plain_text(text: str) -> str:
    value = re.sub(r"<[^>]+>", " ", text)
    value = re.sub(r"!?(?:\[([^\]]*)\])\([^)]*\)", r"\1", value)
    value = re.sub(r"\[\[([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"[`*_>#|~-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _parse_tables(text: str, start_line: int) -> tuple[list[ParsedTable], list[ParserWarning]]:
    lines = text.splitlines()
    tables: list[ParsedTable] = []
    warnings: list[ParserWarning] = []
    index = 0
    while index < len(lines):
        if not lines[index].strip().startswith("|"):
            index += 1
            continue
        end = index
        while end < len(lines) and lines[end].strip().startswith("|"):
            end += 1
        block = lines[index:end]
        parsed = _parse_table_block(block, start_line + index)
        if parsed is None:
            warnings.append(
                ParserWarning(code="TABLE_MALFORMED", start_line=start_line + index, end_line=start_line + end - 1)
            )
        else:
            tables.append(parsed)
        index = end
    return tables, warnings


def _parse_table_block(lines: list[str], start_line: int) -> Optional[ParsedTable]:
    if len(lines) < 2:
        return None
    headers = _table_cells(lines[0])
    separators = _table_cells(lines[1])
    if not headers or len(headers) != len(set(headers)) or any(not header for header in headers):
        return None
    if len(separators) != len(headers) or not all(
        re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in separators
    ):
        return None
    rows: list[dict[str, str]] = []
    for line in lines[2:]:
        cells = _table_cells(line)
        if len(cells) != len(headers):
            return None
        rows.append(dict(zip(headers, cells)))
    return ParsedTable(tuple(headers), tuple(rows), start_line, start_line + len(lines) - 1)


def _table_cells(line: str) -> list[str]:
    sentinel = "\x00PIPE\x00"
    value = line.strip().strip("|").replace(r"\|", sentinel)
    return [cell.strip().replace(sentinel, "|") for cell in value.split("|")]


def _parse_links(text: str, start_line: int) -> tuple[list[ResourceLink], list[ParserWarning]]:
    links: list[ResourceLink] = []
    warnings: list[ParserWarning] = []
    for offset, line in enumerate(text.splitlines()):
        for pattern in (_WIKI_LINK_RE, _ONTOLOGY_LINK_RE):
            for match in pattern.finditer(line):
                resource = match.group("resource")
                key = match.group("key").strip()
                relation = (match.groupdict().get("relation") or "references").strip()
                if resource not in RESOURCE_TYPES - {"root", "asset"} or not key or relation not in LINK_RELATIONS:
                    warnings.append(
                        ParserWarning(
                            code="LINK_UNRESOLVED", start_line=start_line + offset, end_line=start_line + offset
                        )
                    )
                    continue
                links.append(
                    ResourceLink(resource, key, relation, match.groupdict().get("anchor"), start_line + offset)
                )
    return list(_deduplicate_links(links)), warnings


def _deduplicate_links(links: Iterable[ResourceLink]) -> list[ResourceLink]:
    result: list[ResourceLink] = []
    seen: set[tuple[Any, ...]] = set()
    for link in links:
        identity = (link.resource, link.key, link.relation, link.section_anchor, link.source_line)
        if identity not in seen:
            seen.add(identity)
            result.append(link)
    return result


def _deduplicate_warnings(warnings: Iterable[ParserWarning]) -> list[ParserWarning]:
    result: list[ParserWarning] = []
    seen: set[tuple[Any, ...]] = set()
    for warning in warnings:
        identity = (warning.code, warning.start_line, warning.end_line)
        if identity not in seen:
            seen.add(identity)
            result.append(warning)
    return result
