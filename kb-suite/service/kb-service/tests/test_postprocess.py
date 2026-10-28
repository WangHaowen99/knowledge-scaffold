from kb_service import postprocess
from kb_service.chunking import parse_blocks
from kb_service.retrieval import Hit


def hit(cid, *, doc="d1", kb="kb", seq=0, start=0, end=10, content=None, score=0.5, base=0.5, chunk_type="text"):
    h = Hit(
        chunk_id=cid,
        document_id=doc,
        kb_id=kb,
        seq=seq,
        chunk_type=chunk_type,
        content=content if content is not None else f"content {cid}",
        context_header="",
        start_offset=start,
        end_offset=end,
        line_start=1,
        anchor_id=f"{doc}:p:{seq}",
        source_locators=[{"type": "pdf", "page": seq + 1}],
        score=score,
    )
    h.base_score = base
    return h


def test_rerank_keeps_hits_above_threshold_with_composite_score():
    pool = [hit("a", base=0.8), hit("b", base=0.2)]
    out = postprocess.apply_rerank(pool, [(0, 0.9), (1, 0.1)], lambda h: 0.3)
    assert [h.chunk_id for h in out] == ["a"]
    assert abs(out[0].score - (0.6 * 0.9 + 0.3 * 0.8 + 0.1)) < 1e-9
    assert out[0].relevance == 0.9 and "rerank" in out[0].sources


def test_rerank_degrades_high_threshold_once():
    pool = [hit("a"), hit("b")]
    out = postprocess.apply_rerank(pool, [(0, 0.45), (1, 0.2)], lambda h: 0.6)
    # 0.6 * 0.7 = 0.42 -> "a" passes the degraded threshold
    assert [h.chunk_id for h in out] == ["a"]


def test_rerank_falls_back_to_best_hit_or_nothing():
    out = postprocess.apply_rerank([hit("a"), hit("b")], [(0, 0.18), (1, 0.05)], lambda h: 0.3)
    assert [h.chunk_id for h in out] == ["a"]
    assert postprocess.apply_rerank([hit("a")], [(0, 0.1)], lambda h: 0.3) == []


def test_rerank_without_composite_uses_model_score():
    out = postprocess.apply_rerank([hit("a", base=0.0)], [(0, 0.7)], lambda h: 0.0, composite=False)
    assert out[0].score == 0.7


def test_mmr_prefers_diverse_hit_over_near_duplicate():
    a = hit("a", content="压缩机润滑油压力低的处理步骤", score=1.0)
    b = hit("b", content="压缩机润滑油压力低的处理步骤", score=0.95)
    c = hit("c", content="冷却水温度异常时检查换热器", score=0.9)
    out = postprocess.select_mmr([a, b, c], 2, 0.7)
    assert [h.chunk_id for h in out] == ["a", "c"]


def test_selection_caps_wiki_pages():
    w1 = hit("w1", chunk_type="wiki_page", content="wiki one", score=1.0)
    w2 = hit("w2", chunk_type="wiki_page", content="wiki two other", score=0.9)
    c = hit("c", content="chunk text here", score=0.5)
    assert [h.chunk_id for h in postprocess.select_mmr([w1, w2, c], 3, 0.7, max_wiki=1)] == ["w1", "c"]
    assert [h.chunk_id for h in postprocess.select_top([w1, w2, c], 3, max_wiki=1)] == ["w1", "c"]


def test_merge_adjacent_uses_l1_span_and_keeps_best_score():
    l1 = "0123456789abcdefghijKLMNOPQRST"
    a = hit("a", seq=0, start=0, end=12, score=0.4)
    b = hit("b", seq=1, start=10, end=20, score=0.9)
    far = hit("c", seq=5, start=25, end=30, score=0.1)
    other_doc = hit("x", doc="d2", start=0, end=5, score=0.3)
    out = postprocess.merge_adjacent([b, a, far, other_doc], lambda d: l1 if d == "d1" else None)
    assert [h.chunk_id for h in out] == ["b", "c", "x"]
    merged = out[0]
    assert merged.content == l1[0:20]
    assert merged.merged_chunk_ids == ["a", "b"]
    assert merged.core == (10, 20)
    assert merged.score == 0.9 and merged.line_start == 1
    assert {"page": 1, "type": "pdf"} in merged.source_locators and "merged" in merged.sources


def test_expand_short_grows_both_sides_within_budget():
    l1 = "前文第一句。前文第二句。" * 20 + "命中的短句。" + "后文第一句。后文第二句。" * 20
    start = l1.index("命中的短句")
    h = hit("a", start=start, end=start + 6, content=l1[start : start + 6])
    postprocess.expand_short(h, l1, 350, 120)
    assert h.expanded and "命中的短句" in h.content
    assert h.start_offset < start and h.end_offset > start + 6
    assert len(h.content) <= 120
    assert h.content[h.core[0] : h.core[1]] == "命中的短句。"


def test_expand_short_passes_unused_share_at_document_start():
    l1 = "命中。" + "后面的内容。" * 50
    h = hit("a", start=0, end=3, content=l1[:3])
    postprocess.expand_short(h, l1, 350, 100)
    assert h.start_offset == 0 and h.end_offset > 60


def test_expand_short_skips_long_hits_and_tables():
    l1 = "x" * 1000
    long_hit = hit("a", start=0, end=500, content=l1[:500])
    table = hit("t", start=0, end=10, content=l1[:10], chunk_type="table")
    postprocess.expand_short(long_hit, l1, 350, 850)
    postprocess.expand_short(table, l1, 350, 850)
    assert not long_hit.expanded and not table.expanded


def test_parent_window_returns_enclosing_section():
    md = "# A\n\n第一段。\n\n第二段命中。\n\n# B\n\n别的章节。\n"
    start = md.index("第二段命中")
    h = hit("a", start=start, end=start + 6, content=md[start : start + 6])
    postprocess.parent_window(h, md, parse_blocks(md), 2048)
    assert h.content.startswith("# A") and "第一段" in h.content and "别的章节" not in h.content
    assert h.content[h.core[0] : h.core[1]] == "第二段命中。"


def test_remove_overlaps_drops_contained_lower_scored_hit():
    a = hit("a", content="润滑油压力低时先检查油位，再检查过滤器压差。", score=0.9)
    b = hit("b", doc="d2", content="再检查过滤器压差", score=0.5)
    c = hit("c", doc="d3", content="完全不同的内容，关于冷却水", score=0.4)
    assert [h.chunk_id for h in postprocess.remove_overlaps([a, b, c])] == ["a", "c"]


def test_expand_query_variants():
    variants = postprocess.expand_query("请问压缩机润滑油压力低怎么处理？")
    assert variants and all("请问" not in v for v in variants)
    assert postprocess.expand_query("压缩机") == []


def test_expanded_windows_of_one_document_merge_again():
    l1 = "".join(f"第{i}句内容。" for i in range(200))
    s1 = l1.index("第50句")
    s2 = l1.index("第60句")
    a = hit("a", seq=10, start=s1, end=s1 + 6, content=l1[s1 : s1 + 6], score=0.9)
    b = hit("b", seq=12, start=s2, end=s2 + 6, content=l1[s2 : s2 + 6], score=0.5)
    for h in (a, b):
        postprocess.expand_short(h, l1, 350, 300)
    out = postprocess.merge_adjacent([a, b], lambda d: l1)
    assert len(out) == 1 and out[0] is a
    assert out[0].merged_chunk_ids == ["a", "b"]
    assert out[0].content[out[0].core[0] : out[0].core[1]] == l1[s1 : s1 + 6]
