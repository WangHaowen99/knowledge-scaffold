"""Wiki pages: no internal slugs in the text, grounded prompts, index introduction follows deletions (synthetic data)."""

from __future__ import annotations

import asyncio

from kb_service import kb_config, models_client
from kb_service.wiki import jobs as wiki_jobs
from kb_service.wiki import links, prompts, store

NAMES = links.link_names(
    [{"slug": "entity/y-chu-neng", "title": "乙储能", "aliases": ["乙储能公司"]}, {"slug": "entity/甲电池", "name": "甲电池"}]
)


def test_slugs_written_as_text_become_links_or_names():
    assert links.normalize_links("与entity/y-chu-neng（乙储能）合作", NAMES) == "与[[entity/y-chu-neng|乙储能]]合作"
    assert links.normalize_links("[[entity/y-chu-neng]]（乙储能公司）的产品", NAMES) == "[[entity/y-chu-neng|乙储能公司]]的产品"
    assert links.normalize_links("[[entity/y-chu-neng]]发布", NAMES) == "[[entity/y-chu-neng|乙储能]]发布"
    # a bracketed remark that is not a name stays after the link
    assert links.normalize_links("[[entity/y-chu-neng]]（2024 年）", NAMES) == "[[entity/y-chu-neng|乙储能]]（2024 年）"
    assert links.normalize_links("entity/甲电池的工厂", NAMES) == "[[entity/甲电池|甲电池]]的工厂"
    # unknown page: only its name is kept
    assert links.normalize_links("与entity/bing-gong-si（丙公司）签约", NAMES) == "与丙公司签约"
    # links with display text, paths and URLs are left alone
    for text in ("[[entity/y-chu-neng|乙储能]]（乙储能）", "https://example.com/entity/abc-def（x）", "docs/entity/abc",
                 "代码 `entity/y-chu-neng（乙储能）` 原样", "https://example.com/?p=entity/y-chu-neng"):
        assert links.normalize_links(text, NAMES) == text


def test_plain_text_never_shows_a_slug():
    assert links.plain("与entity/y-chu-neng（乙储能）合作") == "与乙储能合作"
    assert links.plain("[[entity/y-chu-neng]]（乙储能）的储能项目") == "乙储能的储能项目"
    assert links.plain("由[[entity/y-chu-neng]]提供", NAMES) == "由乙储能提供"
    assert links.strip_dead_links("见[[concept/gone]]", set()) == "见gone"


def test_rewrite_normalizes_links_in_summary_and_body(monkeypatch):
    async def chat_long(messages, **kwargs):
        return "SUMMARY: 与[[entity/y-chu-neng]]（乙储能）合作\n# 甲电池\n见 entity/y-chu-neng（乙储能）。", True

    monkeypatch.setattr(models_client, "chat_long", chat_long)
    summary, body = asyncio.run(wiki_jobs._rewrite([], kb_config.defaults(), NAMES))
    assert links.plain(summary) == "与乙储能合作"
    assert body == "# 甲电池\n见 [[entity/y-chu-neng|乙储能]]。"


def test_prompts_ask_for_grounded_text_and_named_links():
    summary = prompts.render(prompts.SUMMARY, Content="x", Slugs="(none)", Language="中文", ExtraInstructions="")
    assert "300-1200 words" not in summary
    assert "Source grounding" in summary and "一家专注于" in summary
    assert "never write [[slug]] without the display name" in summary
    assert "Source Grounding" in prompts.CANDIDATES and "15-40 words" not in prompts.CANDIDATES
    assert "based only on the summaries" in prompts.INDEX_INTRO
    assert "Do not characterize the subject" in prompts.MODIFY_SYSTEM


def _merge(monkeypatch, chunk_text: dict[str, str]) -> tuple[list[dict], dict]:
    seen: list[dict] = []
    created: dict = {}

    async def chat_long(messages, **kwargs):
        seen.extend(messages)
        return "SUMMARY: 乙储能在文档中列出\n# 乙储能\n文档列出乙储能。", True

    async def get(kb_id, slug):
        return None

    async def create(**fields):
        created.update(fields)
        return fields

    monkeypatch.setattr(models_client, "chat_long", chat_long)
    monkeypatch.setattr(store, "get", get)
    monkeypatch.setattr(store, "create", create)
    item = {"type": "entity", "name": "乙储能", "slug": "entity/y-chu-neng", "aliases": [],
            "description": "储能企业", "details": ""}
    doc = {"id": "doc_b", "kb_id": "kb_demo", "tenant_id": "tenant_demo"}
    asyncio.run(wiki_jobs._merge_item(doc, item, set(), chunk_text, kb_config.defaults(), "", {"entity/y-chu-neng"}, "", NAMES))
    return seen, created


def test_page_without_citations_is_built_from_chunks_that_name_it(monkeypatch):
    seen, created = _merge(monkeypatch, {"c1": "乙储能 2025 年装机 1.2 GWh。", "c2": "无关内容。"})
    user = seen[-1]["content"]
    assert "乙储能 2025 年装机 1.2 GWh。" in user and "VERBATIM" in user and "无关内容" not in user
    assert created["chunk_refs"] == {"doc_b": ["c1"]}


def test_page_from_an_extracted_description_is_marked_as_not_verbatim(monkeypatch):
    seen, created = _merge(monkeypatch, {"c2": "无关内容。"})
    user = seen[-1]["content"]
    assert "储能企业" in user and "not verbatim source text" in user and "VERBATIM source chunks" not in user


def _page(slug: str, page_type: str, title: str, summary: str, content: str, sources: list[str]) -> dict:
    return {"id": f"id-{slug}", "kb_id": "kb_demo", "tenant_id": "tenant_demo", "slug": slug, "title": title,
            "page_type": page_type, "summary": summary, "content": content, "aliases": [], "source_doc_ids": sources,
            "chunk_refs": {}, "version": 1, "last_edit_source": "pipeline"}


OLD_INTRO = "# 储能资料\n涵盖甲电池与乙储能两家企业的资料。"


def _index(listed: list[str]) -> dict:
    directory = "\n".join(f"- [[summary/{d}|{d}]]：摘要" for d in listed)
    return _page("index", "index", "储能资料", OLD_INTRO, f"{OLD_INTRO}\n\n## 文档（{len(listed)}）\n\n{directory}\n", [])


def _run_finalize(monkeypatch, fake_pool, pages: list[dict]) -> dict:
    calls: dict = {"chat": [], "doc_ids": None, "updates": []}

    def handler(kind, sql, args):
        if "FROM wiki_pages" in sql:
            return pages
        if "FROM documents" in sql:
            calls["doc_ids"] = list(args[1])
            return [{"title": "doc_b 乙储能年报", "summary": "乙储能 2025 年报"}]
        raise AssertionError(sql)

    fake_pool(handler)

    async def chat(prompt, **kwargs):
        calls["chat"].append(prompt)
        return "# 储能资料\n本 Wiki 收录乙储能年报。"

    async def update(page, **fields):
        calls["updates"].append((page["slug"], fields))
        return {**page, **fields, "version": page["version"] + 1}

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(models_client, "chat", chat)
    monkeypatch.setattr(store, "update", update)
    monkeypatch.setattr(store, "refresh_links", nothing)
    monkeypatch.setattr(store, "embed_pages", nothing)
    asyncio.run(wiki_jobs.finalize("kb_demo", kb_config.defaults(), "tenant_demo"))
    return calls


def test_index_introduction_is_rewritten_after_a_document_is_deleted(monkeypatch, fake_pool):
    summary_b = _page("summary/doc_b", "summary", "doc_b 乙储能年报", "介绍entity/y-chu-neng（乙储能）的装机",
                      "# 年报", ["doc_b"])
    calls = _run_finalize(monkeypatch, fake_pool, [_index(["doc_a", "doc_b"]), summary_b])
    assert len(calls["chat"]) == 1 and calls["doc_ids"] == ["doc_b"]
    index_update = dict(calls["updates"])["index"]
    assert "甲电池" not in index_update["summary"] and "summary/doc_a" not in index_update["content"]
    # the stored slug in the summary sentence is cleaned on the same pass
    assert dict(calls["updates"])["summary/doc_b"]["summary"] == "介绍乙储能的装机"


def test_index_introduction_is_kept_while_the_documents_are_the_same(monkeypatch, fake_pool):
    summary_b = _page("summary/doc_b", "summary", "doc_b", "摘要", "# 年报", ["doc_b"])
    calls = _run_finalize(monkeypatch, fake_pool, [_index(["doc_b"]), summary_b])
    assert calls["chat"] == []
