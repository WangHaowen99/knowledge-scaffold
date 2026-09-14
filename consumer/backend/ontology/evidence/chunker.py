"""Stable bounded chunk projection from ParsedResource contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from ontology.evidence.identity import EvidenceIdentity, build_evidence_id, normalize_chunk_text
from ontology.resource_types import ParsedResource, ParsedTable

TARGET_CHARS = 1200
MAX_CHARS = 1800
SNIPPET_CHARS = 420


@dataclass(frozen=True)
class EvidenceChunk:
    evidence_id: str
    ontology_id: str
    ontology_version: str
    ontology_epoch: str
    index_generation: str
    resource: str
    key: str
    canonical_key: str
    title: str
    section: str
    section_anchor: str
    chunk_type: str
    ordinal: int
    text: str
    snippet: str
    source_path: str
    content_hash: str
    source_content_hash: str
    chunk_contract_hash: str
    chunk_hash: str
    tags: tuple[str, ...]
    aliases: tuple[str, ...]
    intent_examples: tuple[str, ...]
    links: tuple[dict[str, Any], ...]
    contract: str = "OntologyEvidenceChunk/v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvidenceChunker:
    def chunk(
        self,
        parsed: ParsedResource,
        *,
        ontology_epoch: str,
        index_generation: str,
    ) -> tuple[EvidenceChunk, ...]:
        chunks: list[EvidenceChunk] = []
        metadata_text = _metadata_text(parsed)
        if metadata_text:
            chunks.append(
                self._make_chunk(
                    parsed,
                    ontology_epoch=ontology_epoch,
                    index_generation=index_generation,
                    section=parsed.title,
                    section_anchor="_metadata",
                    chunk_type="frontmatter",
                    ordinal=0,
                    text=metadata_text,
                    links=(),
                )
            )
        for section in parsed.sections:
            for ordinal, text in enumerate(_split_text(section.plain_text)):
                chunks.append(
                    self._make_chunk(
                        parsed,
                        ontology_epoch=ontology_epoch,
                        index_generation=index_generation,
                        section=section.title,
                        section_anchor=section.section_anchor,
                        chunk_type="markdown_section",
                        ordinal=ordinal,
                        text=text,
                        links=tuple(_link_dict(link) for link in section.links),
                    )
                )
            for ordinal, table in enumerate(section.tables):
                table_text = _table_text(table)
                if table_text:
                    chunks.append(
                        self._make_chunk(
                            parsed,
                            ontology_epoch=ontology_epoch,
                            index_generation=index_generation,
                            section=section.title,
                            section_anchor=section.section_anchor,
                            chunk_type="table",
                            ordinal=ordinal,
                            text=table_text,
                            links=tuple(_link_dict(link) for link in section.links),
                        )
                    )
        return tuple(chunks)

    @staticmethod
    def _make_chunk(
        parsed: ParsedResource,
        *,
        ontology_epoch: str,
        index_generation: str,
        section: str,
        section_anchor: str,
        chunk_type: str,
        ordinal: int,
        text: str,
        links: tuple[dict[str, Any], ...],
    ) -> EvidenceChunk:
        normalized_text = normalize_chunk_text(text)
        identity = build_evidence_id(
            ontology_id=parsed.ontology_id,
            ontology_epoch=ontology_epoch,
            parser_version=parsed.parser_version,
            resource=parsed.resource,
            key=parsed.key,
            source_path=parsed.source_path,
            section_anchor=section_anchor,
            chunk_type=chunk_type,
            ordinal=ordinal,
            text=normalized_text,
        )
        metadata = parsed.evidence_metadata
        return EvidenceChunk(
            evidence_id=identity.evidence_id,
            ontology_id=parsed.ontology_id,
            ontology_version=parsed.ontology_version,
            ontology_epoch=ontology_epoch,
            index_generation=index_generation,
            resource=parsed.resource,
            key=parsed.key,
            canonical_key=parsed.canonical_key,
            title=parsed.title,
            section=section,
            section_anchor=section_anchor,
            chunk_type=chunk_type,
            ordinal=ordinal,
            text=normalized_text,
            snippet=normalized_text[:SNIPPET_CHARS],
            source_path=parsed.source_path,
            content_hash=identity.content_hash,
            source_content_hash=parsed.source_content_sha256,
            chunk_contract_hash=identity.chunk_contract_hash,
            chunk_hash=identity.chunk_hash,
            tags=tuple(metadata.get("tags") or ()),
            aliases=tuple(metadata.get("aliases") or ()),
            intent_examples=tuple(metadata.get("intent_examples") or ()),
            links=links,
        )


def _metadata_text(parsed: ParsedResource) -> str:
    metadata = parsed.evidence_metadata
    values = [parsed.title]
    values.extend(metadata.get("aliases") or [])
    values.extend(metadata.get("tags") or [])
    values.extend(metadata.get("intent_examples") or [])
    for field_name in ("summary", "description"):
        value = str(metadata.get(field_name) or "").strip()
        if value:
            values.append(value)
    return "\n".join(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _split_text(text: str) -> Iterable[str]:
    normalized = normalize_chunk_text(text)
    if not normalized:
        return ()
    if len(normalized) <= MAX_CHARS:
        return (normalized,)
    paragraphs = normalized.split("\n\n")
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        for piece in _hard_split(paragraph):
            candidate = piece if not current else current + "\n\n" + piece
            if current and len(candidate) > TARGET_CHARS:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return tuple(chunks)


def _hard_split(text: str) -> Iterable[str]:
    if len(text) <= MAX_CHARS:
        return (text,)
    return tuple(text[index : index + MAX_CHARS] for index in range(0, len(text), MAX_CHARS))


def _table_text(table: ParsedTable) -> str:
    header = " | ".join(table.headers)
    rows = [" | ".join(row.get(name, "") for name in table.headers) for row in table.rows]
    return "\n".join([header, *rows])


def _link_dict(link: Any) -> dict[str, Any]:
    return {
        "resource": link.resource,
        "key": link.key,
        "relation": link.relation,
        "section_anchor": link.section_anchor,
    }
