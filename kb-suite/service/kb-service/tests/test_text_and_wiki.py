from kb_service import text_utils
from kb_service.chunking import chunk_markdown
from kb_service.questions import parse_questions
from kb_service.wiki import links


def test_tokens_use_cjk_bigrams_and_words():
    assert text_utils.tokens("压缩机 API v2") == {"压缩", "缩机", "api", "v2"}
    assert text_utils.tokens("机 a") == set()


def test_parse_json_object_tolerates_fences_preamble_and_raw_newlines():
    text = '好的：\n```json\n{"a": "line1\nline2", "b": [1]}\n```'
    assert text_utils.parse_json_object(text) == {"a": "line1\nline2", "b": [1]}


def test_split_summary_line():
    assert text_utils.split_summary_line("SUMMARY: 一句话\n# 标题\n正文") == ("一句话", "# 标题\n正文")
    assert text_utils.split_summary_line("# 无摘要") == ("", "# 无摘要")


def test_parse_questions_strips_numbering_and_dedupes():
    text = "1. 压缩机润滑油压力低如何处理？\n- 压缩机润滑油压力低如何处理？\n2) 油泵启动条件是什么？\n#\n好"
    assert parse_questions(text, 5) == ["压缩机润滑油压力低如何处理？", "油泵启动条件是什么？"]
    assert len(parse_questions(text, 1)) == 1


def test_wiki_link_parsing_and_dead_links():
    content = "见 [[entity/ya-suo-ji|压缩机]] 和 [[concept/gone|旧概念]] 以及 [[entity/self|自己]]"
    assert links.parse_links(content) == ["entity/ya-suo-ji", "concept/gone", "entity/self"]
    cleaned = links.strip_dead_links(content, {"entity/ya-suo-ji", "entity/self"}, "entity/self")
    assert cleaned == "见 [[entity/ya-suo-ji|压缩机]] 和 旧概念 以及 自己"


def test_linkify_skips_protected_spans_and_links_first_safe_occurrence():
    content = "# 压缩机\n`压缩机` 不链接。压缩机需要润滑。压缩机再出现。API 和 APIs。机"
    out = links.linkify(
        content,
        [("压缩机", "entity/ya-suo-ji"), ("API", "concept/api"), ("机", "entity/ji")],
        self_slug="summary/x",
    )
    assert out.count("[[entity/ya-suo-ji|压缩机]]") == 1
    assert out.startswith("# 压缩机\n`压缩机` 不链接。[[entity/ya-suo-ji|压缩机]]需要润滑")
    assert "[[concept/api|API]] 和 APIs" in out
    assert "entity/ji" not in out


def test_linkify_does_not_link_self_or_already_linked():
    content = "[[entity/a|甲设备]] 甲设备"
    assert links.linkify(content, [("甲设备", "entity/a")]) == content
    assert links.linkify("甲设备", [("甲设备", "entity/a")], self_slug="entity/a") == "甲设备"


def test_clean_slug_and_normalize_title():
    assert links.clean_slug("entity", "entity/Ya Suo_Ji!", "压缩机") == "entity/ya-suo-ji"
    assert links.clean_slug("concept", "", "润滑 油压") == "concept/润滑-油压"
    assert links.normalize_title("Acme  Corp.") == links.normalize_title("acme-corp")


def test_separator_strategy_ignores_headings():
    md = "# A\n\n" + "甲。" * 40 + "\n\n# B\n\n" + "乙。" * 40
    chunks, _ = chunk_markdown(md, chunk_size=100, overlap=0, strategy="separator", separators=["。"])
    assert all(len(c.content) <= 100 for c in chunks)
    assert any("A" in c.context_header for c in chunks) and any("B" in c.context_header for c in chunks)
    assert all(c.content == md[c.start : c.end] for c in chunks)


def test_plain_turns_links_into_text():
    assert links.plain("由[[entity/a|主油泵]]和[[concept/b]]组成") == "由主油泵和concept/b组成"
