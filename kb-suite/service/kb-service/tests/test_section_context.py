"""Context headers follow the sections a chunk or a widened search hit really covers (synthetic regulation text)."""

from __future__ import annotations

import asyncio

from kb_service import kb_config, models_client, postprocess, questions, retrieval
from kb_service.chunking import chunk_markdown, parse_blocks, section_bounds, span_header
from kb_service.retrieval import Hit

RULES = (
    "**示例制度（摘录）**\n\n"
    "（合成测试数据）\n\n"
    "# 第三章 审批\n\n"
    "第十二条　单笔金额 5 万元以下的采购由专员审批。\n\n"
    "第十三条　服务类采购须有比价记录。\n\n"
    "# 第四章 合同\n\n"
    "第二十一条　设备类合同应约定履约保证金 5,000 元。\n\n"
    "| 金额区间 | 审批人 |\n| --- | --- |\n| 5 万元以下 | 专员 |\n"
)

NESTED = "# 一 总则\n\n总则说明。\n\n## 1.1 范围\n\n范围说明。\n\n## 1.2 定义\n\n定义说明。\n\n# 二 附则\n\n附则说明。\n"


def _chunks():
    chunks, blocks = chunk_markdown(RULES, doc_id="doc_rules")
    by_header = {c.context_header: c for c in chunks}
    return chunks, blocks, by_header


def _hit(chunk, score: float = 0.5) -> Hit:
    return Hit(
        chunk_id=f"c{chunk.seq}", document_id="doc_rules", kb_id="kb_demo", seq=chunk.seq, chunk_type=chunk.chunk_type,
        content=chunk.content, context_header=chunk.context_header, start_offset=chunk.start, end_offset=chunk.end,
        line_start=chunk.line_start, anchor_id=chunk.anchor_id, source_locators=[], score=score,
    )


def test_heading_chunks_stay_inside_their_chapter():
    chunks, _, by_header = _chunks()
    assert set(by_header) == {"", "第三章 审批", "第四章 合同"}
    assert "第二十一条" not in by_header["第三章 审批"].content
    assert by_header["第四章 合同"].content.startswith("# 第四章 合同")


def test_section_bounds_end_at_the_next_heading_of_the_same_or_higher_level():
    blocks = parse_blocks(NESTED)
    total = len(NESTED)
    overview = NESTED.index("总则说明")
    scope = NESTED.index("范围说明")
    assert section_bounds(blocks, overview, total) == (0, NESTED.index("# 二 附则"))
    assert section_bounds(blocks, scope, total) == (NESTED.index("## 1.1"), NESTED.index("## 1.2"))
    assert section_bounds(blocks, NESTED.index("附则说明"), total) == (NESTED.index("# 二 附则"), total)
    preamble = "前言。\n\n" + NESTED
    assert section_bounds(parse_blocks(preamble), 0, len(preamble)) == (0, preamble.index("# 一 总则"))


def test_span_header_names_every_section_a_range_covers():
    blocks = parse_blocks(NESTED)
    assert span_header(blocks, NESTED.index("范围说明"), NESTED.index("范围说明") + 4) == "一 总则 > 1.1 范围"
    # the chapter's intro and its subsections: the chapter, then the subsections reached
    assert span_header(blocks, 0, NESTED.index("## 1.1")) == "一 总则"
    assert span_header(blocks, 0, NESTED.index("# 二 附则")) == "一 总则 > 1.1 范围 / 1.2 定义"
    assert span_header(blocks, NESTED.index("范围说明"), NESTED.index("定义说明") + 4) == "一 总则 > 1.1 范围 / 1.2 定义"
    assert span_header(blocks, NESTED.index("定义说明"), len(NESTED)) == "一 总则 / 二 附则"
    assert span_header(parse_blocks(RULES), 0, len(RULES)) == "第三章 审批 / 第四章 合同"


def test_separator_chunks_crossing_a_heading_name_both_sections():
    chunks, _ = chunk_markdown(RULES, chunk_size=120, overlap=0, strategy="separator", separators=["\n\n"])
    crossing = [c for c in chunks if "第十三条" in c.content and "第二十一条" in c.content]
    assert crossing and all(c.context_header == "第三章 审批 / 第四章 合同" for c in crossing)
    alone = [c for c in chunks if "第二十一条" in c.content and "第十三条" not in c.content]
    assert all("第四章 合同" in c.context_header and "第三章" not in c.context_header for c in alone)


def test_expanding_a_short_hit_stays_in_its_chapter():
    _, blocks, by_header = _chunks()
    deposit = _hit(by_header["第四章 合同"])
    postprocess.expand_short(deposit, RULES, 350, 850, blocks)
    assert "第十二条" not in deposit.content and "第二十一条" in deposit.content
    assert deposit.context_header == "第四章 合同"

    # without the heading structure the window grows over the whole short document (previous behaviour)
    blind = _hit(by_header["第四章 合同"])
    postprocess.expand_short(blind, RULES, 350, 850)
    assert "第十二条" in blind.content


def test_hits_of_neighbouring_chapters_are_not_merged_under_one_heading():
    _, blocks, by_header = _chunks()
    approval, deposit = _hit(by_header["第三章 审批"], 0.4), _hit(by_header["第四章 合同"], 0.9)
    merged = postprocess.merge_adjacent([deposit, approval], lambda d: RULES, lambda d: blocks)
    assert len(merged) == 2
    assert {h.context_header for h in merged} == {"第三章 审批", "第四章 合同"}


def test_overlapping_hits_across_chapters_get_a_header_for_both():
    _, blocks, by_header = _chunks()
    approval, deposit = _hit(by_header["第三章 审批"], 0.4), _hit(by_header["第四章 合同"], 0.9)
    approval.end_offset = deposit.start_offset + 5  # e.g. separator-strategy chunks that overlap
    merged = postprocess.merge_adjacent([deposit, approval], lambda d: RULES, lambda d: blocks)
    assert len(merged) == 1 and merged[0].context_header == "第三章 审批 / 第四章 合同"
    assert merged[0].chunk_id == "c" + str(by_header["第四章 合同"].seq)


def test_search_result_for_a_later_article_keeps_its_own_chapter(monkeypatch):
    chunks, blocks, by_header = _chunks()

    async def kb_configs(kb_ids):
        return {k: kb_config.defaults() for k in kb_ids}

    async def embed_query(text):
        return [0.1, 0.2]

    async def vector(tenant_id, kb_ids, vec, limit):
        found = []
        for header, score in (("第四章 合同", 0.8), ("第三章 审批", 0.5)):
            hit = _hit(by_header[header])
            hit.vector_score = score
            found.append(hit)
        return found

    async def attach(hits):
        for h in hits:
            h.title, h.l1_key = "示例制度", "rules.md"
        return hits

    async def empty(*args, **kwargs):
        return []

    async def no_questions(ids):
        return {}

    monkeypatch.setattr(retrieval, "kb_configs", kb_configs)
    monkeypatch.setattr(models_client, "embed_query", embed_query)
    monkeypatch.setattr(retrieval, "_vector", vector)
    monkeypatch.setattr(retrieval, "_attach_documents", attach)
    monkeypatch.setattr(retrieval, "_l1_text", lambda key: RULES)
    monkeypatch.setattr(retrieval, "_l1_blocks", lambda key: blocks)
    monkeypatch.setattr(questions, "questions_for_chunks", no_questions)
    for name in ("_keywords", "_question_vector", "_question_keywords", "_wiki_vector", "_wiki_keywords", "_exact",
                 "_kbs_with_questions"):
        monkeypatch.setattr(retrieval, name, empty)

    hits = asyncio.run(retrieval.search("tenant_demo", ["kb_demo"], "履约保证金", top_k=5, options={"rerank_enabled": False}))
    deposit = next(h for h in hits if "第二十一条" in h.content)
    assert deposit.context_header == "第四章 合同"
    assert "第十二条" not in deposit.content
