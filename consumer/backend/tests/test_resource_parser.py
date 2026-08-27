"""Contract tests for the shared ontology resource parser."""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ontology.resource_parser import PARSER_VERSION, ResourceParser, ResourceParserCache
from ontology.resource_types import ResourceSource


def _source(text: str, *, path: str = "objects/eco_affected_object.md") -> ResourceSource:
    return ResourceSource(
        ontology_id="aviation-plm-change-impact",
        ontology_version="0.1.0",
        resource="object",
        source_path=path,
        media_type="text/markdown",
        source_text=text,
        source_content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def test_parser_emits_stable_section_table_and_safe_metadata():
    text = """---
key: eco_affected_object
title: ECO受影响对象
aliases: [设计变更影响对象]
secret: must-not-project
---
# ECO受影响对象

## 字段
| 字段 | 含义 |
| --- | --- |
| eco_no | ECO编号 |
"""

    parsed = ResourceParser().parse(_source(text))

    assert parsed.contract == "ParsedResource/v1"
    assert parsed.parser_version == PARSER_VERSION
    assert parsed.key == "eco_affected_object"
    assert parsed.evidence_metadata == {
        "key": "eco_affected_object",
        "canonical_key": "eco_affected_object",
        "title": "ECO受影响对象",
        "aliases": ["设计变更影响对象"],
        "tags": [],
        "intent_examples": [],
    }
    assert len(parsed.sections) == 1
    section = parsed.sections[0]
    assert section.section_anchor == "eco受影响对象/字段"
    assert section.heading_path == ("ECO受影响对象", "字段")
    assert section.start_line == 9
    assert section.end_line == 12
    assert section.tables[0].rows == ({"字段": "eco_no", "含义": "ECO编号"},)
    assert len(section.content_hash) == 64
    assert "must-not-project" not in str(parsed.evidence_metadata)
    assert "UNKNOWN_METADATA_IGNORED" in {warning.code for warning in parsed.warnings}


def test_parser_separates_parent_and_child_sections_without_overlap():
    text = """# 标题
前言

## 流程
父级正文
### 子步骤
子级正文
## 流程
重复标题正文
"""
    parsed = ResourceParser().parse(_source(text))

    assert [section.section_anchor for section in parsed.sections] == [
        "_preamble",
        "标题/流程",
        "标题/流程/子步骤",
        "标题/流程~2",
    ]
    assert parsed.sections[1].text == "## 流程\n父级正文"
    assert "子级正文" not in parsed.sections[1].text
    assert "DUPLICATE_ANCHOR_RENAMED" in {warning.code for warning in parsed.warnings}


def test_parser_extracts_only_explicit_ontology_links():
    text = """# 示例
## 关系
使用 [[object:epart_version|uses]]，参考 [外部](https://example.com)。
继续 [对象](ontology://object/mpart#字段)。
"""
    parsed = ResourceParser().parse(_source(text))

    assert [(link.resource, link.key, link.relation) for link in parsed.document_links] == [
        ("object", "epart_version", "uses"),
        ("object", "mpart", "references"),
    ]


def test_parser_preserves_body_when_frontmatter_is_invalid():
    text = """---
key: [broken
---
# 标题
## 字段
正文
"""
    parsed = ResourceParser().parse(_source(text))

    assert parsed.key == "eco_affected_object"
    assert parsed.sections[0].title == "字段"
    assert "FRONTMATTER_INVALID" in {warning.code for warning in parsed.warnings}


def test_parser_normalizes_line_endings_but_keeps_raw_digest_contract():
    text = "# 标题\r\n## 字段\r\n正文\r\n"
    parsed = ResourceParser().parse(_source(text))

    assert "\r" not in parsed.display_text
    assert parsed.source_content_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_shared_parser_cache_is_single_flight_and_revision_scoped(monkeypatch):
    source = _source("# 标题\n## 字段\n正文\n", path="objects/cache.md")
    cache = ResourceParserCache(max_entries=4)
    original = ResourceParser.parse
    calls = 0
    calls_lock = threading.Lock()

    def counted(self, value):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        return original(self, value)

    monkeypatch.setattr(ResourceParser, "parse", counted)
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(
            executor.map(
                lambda _index: cache.parse(
                    source,
                    workspace_id="workspace-cache",
                    mutation_revision=7,
                ),
                range(6),
            )
        )
    assert calls == 1
    assert all(item is results[0] for item in results)

    cache.parse(source, workspace_id="workspace-cache", mutation_revision=8)
    assert calls == 2
