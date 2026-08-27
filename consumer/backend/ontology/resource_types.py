"""Typed contracts emitted by the shared ontology resource parser."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ResourceSource:
    ontology_id: str
    ontology_version: str
    resource: str
    source_path: str
    media_type: str
    source_text: str
    source_content_sha256: str


@dataclass(frozen=True)
class ParserWarning:
    code: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    severity: str = "warning"
    contract: str = "ParserWarning/v1"


@dataclass(frozen=True)
class ParsedTable:
    headers: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    start_line: int
    end_line: int
    contract: str = "ParsedTable/v1"


@dataclass(frozen=True)
class ResourceLink:
    resource: str
    key: str
    relation: str
    section_anchor: Optional[str]
    source_line: int
    contract: str = "ResourceLink/v1"


@dataclass(frozen=True)
class ResourceSection:
    section_anchor: str
    heading_path: tuple[str, ...]
    heading_level: Optional[int]
    title: str
    ordinal: int
    text: str
    plain_text: str
    start_line: int
    end_line: int
    tables: tuple[ParsedTable, ...]
    links: tuple[ResourceLink, ...]
    content_hash: str
    contract: str = "ResourceSection/v1"


@dataclass(frozen=True)
class ParsedResource:
    parser_version: str
    ontology_id: str
    ontology_version: str
    resource: str
    key: str
    canonical_key: str
    title: str
    source_path: str
    source_content_sha256: str
    media_type: str
    display_text: str
    frontmatter: dict[str, Any]
    evidence_metadata: dict[str, Any]
    body_text: str
    sections: tuple[ResourceSection, ...]
    document_links: tuple[ResourceLink, ...] = field(default_factory=tuple)
    warnings: tuple[ParserWarning, ...] = field(default_factory=tuple)
    contract: str = "ParsedResource/v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
