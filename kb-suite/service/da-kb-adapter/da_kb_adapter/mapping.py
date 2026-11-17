"""DeepAnalyze objects -> knowledge-provider/v1 shapes (pure functions)."""

from __future__ import annotations

import re
from urllib.parse import quote

from kb_service.chunking import parse_blocks

from kb_provider_kit import Ids

# DA document status -> contract
STATUS = {
    "uploaded": "pending", "parsing": "processing", "compiling": "processing", "indexing": "processing",
    "linking": "processing", "quality_audit": "processing", "ready": "completed", "error": "failed",
}
CANCELLED = "Cancelled by user"
# DA page types that hold a whole document at L1 (structure first, then the LLM overview)
L1_STRUCTURE = {"structure_md", "structure", "structure_dt"}
CHUNK_CHARS = 1200
_MARK = re.compile(r"</?mark>")
# what is left of a tag DA cut through: right after the leading "..." / at the very end
_CUT_TAG_HEAD = re.compile(r"^(\.\.\.|…)(?:/?[a-z_]*\d*>)+")
_CUT_TAG_TAIL = re.compile(r"</?[a-z][a-z_]*\d*$")


def kb_view(kb: dict, docs: list[dict], ids: Ids) -> dict:
    statuses = [doc_status(d) for d in docs]
    return {
        "id": ids.out(kb["id"]),
        "name": kb.get("name") or "",
        "description": kb.get("description") or "",
        "created_at": kb.get("createdAt"),
        "updated_at": kb.get("updatedAt"),
        "config": {},
        "stats": {
            "documents": len(docs),
            "completed": statuses.count("completed"),
            "processing": statuses.count("processing") + statuses.count("pending"),
            "failed": statuses.count("failed"),
            "cancelled": statuses.count("cancelled"),
        },
    }


def doc_status(doc: dict) -> str:
    status = STATUS.get(doc.get("status") or "", "processing")
    if status == "failed" and (doc.get("processing_error") or "").startswith(CANCELLED):
        return "cancelled"
    return status


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def doc_view(doc: dict, kb_id: str, ids: Ids, summary: str | None = None) -> dict:
    name = doc.get("filename") or ""
    return {
        "id": ids.out(doc["id"]),
        "kb_id": ids.out(kb_id),
        "title": name.rsplit(".", 1)[0] if "." in name else name,
        "file_name": name or None,
        # node-pg returns BIGINT as a string
        "file_size": _int(doc.get("file_size")),
        "file_type": (doc.get("file_type") or "").lower(),
        "folder_path": doc.get("folder_path") or "",
        "source_type": "file",
        "source_url": None,
        "parse_status": doc_status(doc),
        "stage": doc.get("processing_step") or doc.get("status"),
        "error_message": doc.get("processing_error") or None,
        "summary": summary,
        "chunk_count": None,
        "l1_chars": None,
        "created_at": doc.get("created_at"),
        "processed_at": doc.get("updated_at") if doc.get("status") == "ready" else None,
        "updated_at": doc.get("updated_at"),
    }


def split_chunks(text: str, size: int = CHUNK_CHARS) -> list[tuple[int, int, str]]:
    """Paragraph-aligned pieces of the L1 text: [(line_start, char_offset, content)]."""

    pieces: list[tuple[int, int, str]] = []
    start = 0
    # line numbers are counted forward from the previous piece, not from the top each time
    line, counted = 1, 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind("\n\n", start + size // 3, end)
            if cut != -1:
                end = cut + 2
            else:
                cut = text.rfind("\n", start + size // 3, end)
                if cut != -1:
                    end = cut + 1
        piece = text[start:end]
        if piece.strip():
            first = start + len(piece) - len(piece.lstrip("\n"))
            line += text.count("\n", counted, first)
            counted = first
            pieces.append((line, first, piece.strip("\n")))
        start = end
    return pieces


def chunk_view(doc_id: str, seq: int, line_start: int, content: str, ids: Ids, anchor_id: str | None = None) -> dict:
    return {
        "id": ids.out(f"{doc_id}.{seq}"),
        "seq": seq,
        "chunk_type": "text",
        "content": content,
        "context_header": "",
        "line_start": line_start,
        "anchor_id": anchor_id,
        "source_locators": [],
        "questions": [],
    }


def _page_title(title: str) -> str:
    # legacy pages are titled "Abstract: {docId}" and so on
    for prefix in ("Abstract: ", "Overview: ", "Document: "):
        title = title.removeprefix(prefix)
    return title


def distinct_hits(hits: list[dict]) -> list[dict]:
    """Drop repeats of a document: DA returns its abstract, structure and full-text pages as separate hits.

    A hit without an anchor stands for a whole page of the document, so only the best-ranked one per
    document is kept; anchored hits (a passage) are kept unless the same anchor came already. A hit whose
    text is the same as an earlier hit of the same document (the structure page and the full-text page of
    a short document) is dropped too.
    """

    out: list[dict] = []
    seen: set[tuple[str, str | None]] = set()
    texts: set[tuple[str, str]] = set()
    for hit in hits:
        # a Wiki page (an agent report, an entity) belongs to no document and stands on its own
        owner = hit["doc_id"] or f"wiki:{hit.get('chunk_id')}"
        key = (owner, hit.get("anchor_id"))
        text = (owner, " ".join((hit.get("content") or "").split()))
        if key in seen or (text[1] and text in texts):
            continue
        seen.add(key)
        texts.add(text)
        out.append(hit)
    # each document's best hit first, so the top of the list shows different documents; the rest keep their order
    firsts: set[str] = set()
    head, tail = [], []
    for hit in out:
        owner = hit["doc_id"] or f"wiki:{hit.get('chunk_id')}"
        (tail if owner in firsts else head).append(hit)
        firsts.add(owner)
    return head + tail


def hit_text(snippet: object) -> str:
    """A hit's text: DA marks query words with <mark>, and a hit on a DocTags page carries its markup.

    DA cuts a snippet out of the page without regard to tags, so a cut tag
    (``...119>`` of ``<loc_119>``) may be left at either end.
    """

    text = strip_doctags(_MARK.sub("", str(snippet or "")))
    return _CUT_TAG_TAIL.sub("", _CUT_TAG_HEAD.sub(r"\1", text))


def hit_view(r: dict, ids: Ids, filenames: dict[str, str]) -> dict:
    doc_id = r.get("docId") or ""
    if not doc_id and r.get("pageType") in WIKI_TYPE:
        return wiki_hit(r, ids)
    anchor = r.get("anchorId")
    return {
        "chunk_id": ids.out(r.get("pageId")),
        "doc_id": ids.out(doc_id),
        "kb_id": ids.out(r.get("kbId")),
        "seq": 0,
        "chunk_type": "summary" if r.get("pageType") == "abstract" else "text",
        "title": filenames.get(doc_id) or _page_title(r.get("title") or ""),
        "file_name": filenames.get(doc_id),
        "context_header": "",
        "content": hit_text(r.get("snippet")),
        "score": float(r.get("score") or 0.0),
        "relevance": None,
        "line_start": None,
        # anchors are "{docId}:{type}:{index}", so the doc id prefix goes in front
        "anchor_id": ids.out(anchor) if anchor else None,
        "locators": [],
        "page_type": r.get("pageType"),
        "level": r.get("level"),
    }


# ---- search options (contract search.options) ---------------------------------------

LEVELS = ("L0", "L1", "L2")
MODES = ("hybrid", "vector", "keyword")
SEARCH_OPTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "title": "检索方式", "enum": list(MODES), "default": "hybrid",
                 "x-enum-labels": {"hybrid": "混合", "vector": "语义", "keyword": "关键词"}},
        "levels": {"type": "object", "title": "检索层级", "properties": {
            "L0": {"type": "boolean", "title": "摘要", "default": True},
            "L1": {"type": "boolean", "title": "概览与结构", "default": True},
            "L2": {"type": "boolean", "title": "全文", "default": True},
        }},
    },
}


def search_options(options: object) -> tuple[str | None, list[str] | None]:
    """(DA search mode, DA levels) from a request's ``options``; ``None`` where DA's default applies.

    ``levels`` is the schema's object of switches; a list or a comma-separated
    string is accepted too. Selecting no level means all of them.
    """

    if not isinstance(options, dict):
        return None, None
    mode = options.get("mode")
    # hybrid is DA's own default: leave it out, so the default options keep the plain fusion search
    mode = "vector" if mode == "semantic" else mode if mode in MODES and mode != "hybrid" else None
    raw = options.get("levels")
    if isinstance(raw, dict):
        chosen = [level for level in LEVELS if raw.get(level, True) is not False]
    elif isinstance(raw, (list, tuple)):
        chosen = [level for level in LEVELS if level in raw]
    elif isinstance(raw, str):
        chosen = [level for level in LEVELS if level in {p.strip() for p in raw.split(",")}]
    else:
        chosen = []
    levels = chosen if chosen and len(chosen) < len(LEVELS) else None
    return mode, levels


def leveled_results(results: object) -> list[dict]:
    """DA's per-level search results (``{L0: [...], L1: [...], L2: [...]}``) as one list by score."""

    if isinstance(results, list):
        return results
    merged: dict[str, dict] = {}
    for level in LEVELS:
        for r in (results or {}).get(level) or [] if isinstance(results, dict) else []:
            page = r.get("pageId")
            # L1 may be an overview or a structure page; L0 and L2 have one page type each
            row = {**r, "level": level, "pageType": r.get("pageType") or {"L0": "abstract", "L2": "fulltext"}.get(level)}
            if page not in merged or float(row.get("score") or 0) > float(merged[page].get("score") or 0):
                merged[page] = row
    return sorted(merged.values(), key=lambda r: -float(r.get("score") or 0))


# ---- folders -------------------------------------------------------------------------


def norm_folder(path: object) -> str:
    return "/".join(p for p in str(path or "").replace("\\", "/").split("/") if p)


def in_folder(doc: dict, folder: str) -> bool:
    path = norm_folder(doc.get("folder_path"))
    return path == folder or path.startswith(folder + "/")


def folders(docs: list[dict]) -> list[dict]:
    """Every folder holding documents, with the number of documents in it and below it."""

    counts: dict[str, int] = {}
    for doc in docs:
        parts = norm_folder(doc.get("folder_path")).split("/")
        if parts == [""]:
            continue
        for depth in range(1, len(parts) + 1):
            path = "/".join(parts[:depth])
            counts[path] = counts.get(path, 0) + 1
    return [{"path": path, "documents": n} for path, n in sorted(counts.items())]


# ---- wiki (contract wiki.read) ------------------------------------------------------------
# DA compiles each document into an abstract (L0) and, for plain documents, an LLM
# overview; together they are the document's summary page. Agent reports kept in
# the knowledge base are concept pages. DA's entity extraction and cross-document
# links are switched off in DA itself, so entity pages and links are normally absent.

WIKI_TYPE = {"report": "concept", "entity": "entity", "concept": "concept"}
SLUG_KINDS = ("summary", *WIKI_TYPE)


def summary_slug(doc_id: str) -> str:
    return f"summary/{doc_id}"


def page_slug(page_type: str, page_id: str) -> str:
    return f"{page_type}/{page_id}"


def parse_slug(slug: str) -> tuple[str, str] | None:
    kind, _, item = (slug or "").partition("/")
    if kind not in SLUG_KINDS or not item or "/" in item or item in (".", ".."):
        return None
    return kind, item


def doc_title(doc: dict) -> str:
    name = doc.get("filename") or ""
    return name.rsplit(".", 1)[0] if "." in name else name


def _item(item_id: str, slug: str, title: str, page_type: str, summary: str, sources: int, updated_at: object) -> dict:
    return {"id": item_id, "slug": slug, "title": title, "page_type": page_type, "summary": summary,
            "aliases": [], "sources": sources, "in_count": 0, "out_count": 0, "version": 0,
            "last_edit_source": "auto", "updated_at": updated_at}


def summary_item(doc: dict, ids: Ids, summary: str | None) -> dict:
    return _item(ids.out(f"{doc['id']}.summary"), summary_slug(doc["id"]), doc_title(doc), "summary",
                 summary or "", 1, doc.get("updated_at"))


def page_item(page_type: str, page: dict, ids: Ids, summary: str = "", sources: int = 0) -> dict:
    return _item(ids.out(page["id"]), page_slug(page_type, page["id"]), _page_title(page.get("title") or ""),
                 WIKI_TYPE[page_type], summary, sources, page.get("updatedAt") or page.get("updated_at"))


def wiki_hit(r: dict, ids: Ids) -> dict:
    page_type = r.get("pageType") or "report"
    return {
        "chunk_id": ids.out(r.get("pageId")), "doc_id": None, "kb_id": ids.out(r.get("kbId")), "seq": 0,
        "chunk_type": "wiki_page", "title": _page_title(r.get("title") or ""), "file_name": None,
        "context_header": "", "content": hit_text(r.get("snippet")), "score": float(r.get("score") or 0.0),
        "relevance": None, "line_start": None, "anchor_id": None, "locators": [],
        "wiki_slug": page_slug(page_type, r.get("pageId")), "wiki_type": WIKI_TYPE[page_type], "wiki_sources": [],
        "page_type": page_type,
    }


def wiki_page(item: dict, kb_id: str, content: str, ids: Ids, *, links_to: list[dict], linked_from: list[dict],
              sources: list[dict]) -> dict:
    return {**item, "kb_id": ids.out(kb_id), "content": content, "links_to": links_to, "linked_from": linked_from,
            "sources": sources, "source_doc_ids": [s["id"] for s in sources], "revisions": []}


def plain_snippet(markdown: str, limit: int = 160) -> str:
    """The first line of prose of a markdown text, for a page summary."""

    for line in (markdown or "").splitlines():
        text = line.strip()
        if not text or text.startswith(("#", ">", "|", "```", "---")):
            continue
        text = re.sub(r"[*_`]+", "", text)
        return text[:limit]
    return ""


def graph_view(nodes: dict[str, dict], edges: list[tuple[str, str]], center: str | None, limit: int) -> dict:
    """Contract wiki graph: ``nodes`` by slug ({title, page_type}), links between them as edges."""

    pairs = sorted({(s, t) for s, t in edges if s != t and s in nodes and t in nodes})
    degree = {slug: 0 for slug in nodes}
    for s, t in pairs:
        degree[s] += 1
        degree[t] += 1
    keep = set(nodes)
    if center and center in nodes:
        keep = {center} | {t for s, t in pairs if s == center} | {s for s, t in pairs if t == center}
    ranked = sorted(keep, key=lambda slug: (-degree[slug], nodes[slug]["title"]))[:limit]
    chosen = set(ranked)
    return {
        "nodes": [{"slug": slug, "title": nodes[slug]["title"], "page_type": nodes[slug]["page_type"],
                   "degree": degree[slug]} for slug in ranked],
        "edges": [{"source": s, "target": t} for s, t in pairs if s in chosen and t in chosen],
    }


# ---- views (contract views) -----------------------------------------------------------------

ANCHOR_TYPE = {"heading": "标题", "paragraph": "段落", "table": "表格", "image": "图片", "code": "代码",
               "list": "列表", "formula": "公式", "turn": "对话", "scene": "场景"}


def quality_rows(report: dict) -> list[dict]:
    """Documents DA has audited, worst first."""

    rows = []
    for doc in report.get("documents") or []:
        audit = doc.get("qualityAudit")
        if not isinstance(audit, dict):
            continue
        issues = audit.get("issues") or []
        rows.append({
            "文档": doc.get("filename"), "评分": audit.get("score"),
            "问题": "；".join(str(i) for i in issues) if isinstance(issues, list) else str(issues),
            "已重新提取": bool(audit.get("reExtracted")), "模型": audit.get("modelUsed"),
            "审计时间": audit.get("auditedAt"),
        })
    rows.sort(key=lambda r: (not isinstance(r["评分"], (int, float)), r["评分"] if isinstance(r["评分"], (int, float)) else 0))
    return rows


def structure_rows(structure_map: list, ids: Ids) -> list[dict]:
    """DA's structure map (structure pages and their anchors) as one row per anchor."""

    rows = []
    for page in structure_map or []:
        anchors = page.get("anchors") or []
        if not anchors:
            rows.append({"章节": page.get("title"), "类型": None, "内容": None, "锚点": None})
        for a in anchors:
            rows.append({"章节": a.get("sectionTitle") or page.get("title"),
                         "类型": ANCHOR_TYPE.get(a.get("type") or "", a.get("type")),
                         "内容": a.get("preview"), "锚点": ids.out(a.get("id"))})
    return rows


def _turns(turns: list) -> list[dict]:
    return [{"开始": t.get("start"), "结束": t.get("end"), "说话人": t.get("speaker"), "内容": t.get("text")}
            for t in turns or [] if isinstance(t, dict)]


def media_view(meta: dict) -> dict | None:
    kind = meta.get("type")
    if kind == "image":
        image = meta.get("image") or {}
        return {"类型": "图片", "宽": image.get("width"), "高": image.get("height"), "描述": image.get("description") or None}
    if kind == "audio":
        audio = meta.get("audio") or {}
        return {"类型": "音频", "时长（秒）": audio.get("duration"), "说话人": audio.get("speakers") or [],
                "对话": _turns(audio.get("turns"))}
    if kind == "video":
        video = meta.get("video") or {}
        transcript = video.get("transcript") or {}
        scenes = [{"开始": s.get("start"), "结束": s.get("end"), "描述": s.get("description")}
                  for s in video.get("scenes") or [] if isinstance(s, dict)]
        return {"类型": "视频", "时长（秒）": video.get("duration"), "关键帧": video.get("frameCount"), "场景": scenes,
                "说话人": transcript.get("speakers") or [], "对话": _turns(transcript.get("turns"))}
    return None


def timeline_events(data: dict, titles: dict[str, str]) -> list[dict]:
    """DA's dated events; a source page named after a document id shows the document's title."""

    events = []
    for e in data.get("events") or []:
        source = _page_title(e.get("sourceTitle") or "")
        events.append({"date": e.get("date"), "title": strip_doctags(e.get("title") or ""),
                       "description": strip_doctags(e.get("description") or ""), "source": titles.get(source, source)})
    return events


def attachment(name: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "export"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


# ---- evidence (contract evidence) ----------------------------------------------------------

CONTEXT_CHARS = 300


def _line_offset(text: str, line: int) -> int | None:
    pos = 0
    for _ in range(line):
        pos = text.find("\n", pos)
        if pos == -1:
            return None
        pos += 1
    return pos


def _locate(text: str, snippet: str, line0: int | None) -> int | None:
    """Offset of an anchor in the L1 text: its line when that line holds the snippet, else the snippet's first match."""

    if line0 is not None and line0 >= 0:
        start = _line_offset(text, line0)
        if start is not None:
            end = text.find("\n", start)
            if not snippet or snippet in text[start:end if end != -1 else len(text)]:
                return start
    if snippet:
        found = text.find(snippet)
        if found != -1:
            return found
    return None


def _bbox(raw: object) -> tuple[int | None, list[float] | None]:
    """(1-based page, [x0, y0, x1, y1] top-left origin) from the raw parser node of an anchor, when it has them."""

    if not isinstance(raw, dict):
        return None, None
    box = raw.get("bbox")
    if isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box):
        return None, [float(v) for v in box]
    prov = raw.get("prov")
    first = prov[0] if isinstance(prov, list) and prov and isinstance(prov[0], dict) else None
    if first:
        page = first.get("page_no") if isinstance(first.get("page_no"), int) else None
        b = first.get("bbox") if isinstance(first.get("bbox"), dict) else {}
        if str(b.get("coord_origin") or "").upper() == "TOPLEFT" and all(k in b for k in "ltrb"):
            return page, [float(b["l"]), float(b["t"]), float(b["r"]), float(b["b"])]
        return page, None
    return None, None


def _anchor_locator(anchor: dict, raw_context: object) -> dict:
    meta = anchor.get("metadata") if isinstance(anchor.get("metadata"), dict) else {}
    kind = anchor.get("element_type")
    out: dict = {}
    if kind == "turn":
        out["time_start"], out["time_end"] = meta.get("startTime"), meta.get("endTime")
    elif kind == "scene":
        out["time_start"] = meta.get("time")
    elif meta.get("source") == "mineru" and isinstance(anchor.get("page_number"), int):
        # MinerU pages are counted from 0
        out["page"] = anchor["page_number"] + 1
    page, box = _bbox(raw_context)
    if page is not None and "page" not in out and kind not in ("turn", "scene"):
        out["page"] = page
    if box:
        out["bbox"] = box
    if meta.get("sheetName"):
        out["sheet"] = meta["sheetName"]
    return {k: v for k, v in out.items() if v is not None}


def evidence_view(anchor_id: str, doc: dict, kb_id: str, l1: str, ids: Ids, *,
                  anchor: dict | None = None, raw_context: object = None, items: list[dict] | None = None,
                  at: int | None = None, own_anchors: bool = False) -> dict | None:
    """Contract evidence for ``anchor_id`` (prefixed) in a document's L1 text.

    ``at`` is where DA's anchor sits in the L1 text (``locate_anchors``); else
    the block is found by the anchor's preview (DA's line numbers of paragraphs
    are not reliable). Without a DA anchor the id is matched against the block
    anchors ``content.structure`` hands out, unless DA keeps anchors of its own
    for the document (``own_anchors``): their ids would name other blocks.
    """

    blocks = parse_blocks(l1, ids.out(doc["id"]) or "")
    block = None
    if at is not None:
        block = next((b for b in blocks if b.start <= at < b.end), None) or next(
            (b for b in blocks if b.start >= at), None)
    elif own_anchors and anchor is None:
        return None
    elif anchor is not None:
        preview = (anchor.get("content_preview") or "").strip()
        snippet = preview.split("\n", 1)[0].strip()[:50]
        line0 = anchor.get("line_start") if anchor.get("element_type") != "paragraph" else None
        offset = _locate(l1, snippet, line0 if isinstance(line0, int) else None)
        if offset is not None:
            block = next((b for b in blocks if b.start <= offset < b.end), None) or next(
                (b for b in blocks if b.start >= offset), None)
    else:
        block = next((b for b in blocks if b.anchor_id == anchor_id), None)
        if block is None:
            return None
    locator: dict = _anchor_locator(anchor, raw_context) if anchor is not None else {}
    section = (anchor or {}).get("section_title")
    if block is not None:
        text = l1[block.start:block.end].strip()
        line_start = l1.count("\n", 0, block.start) + 1
        locator["line_start"] = line_start
        locator["line_end"] = line_start + l1[block.start:block.end].rstrip("\n").count("\n")
        before = l1[max(0, block.start - CONTEXT_CHARS):block.start].strip()
        after = l1[block.end:block.end + CONTEXT_CHARS].strip()
        section = section or (block.header_path[-1] if block.header_path else None)
    else:
        text, before, after = (anchor or {}).get("content_preview") or "", "", ""
    if items and "page" not in locator and "time_start" not in locator:
        # DA keeps no page for anchors made from docling's markdown; the parser's JSON has it
        found = match_item(items, text) or match_item(items, (anchor or {}).get("content_preview") or "")
        if found:
            locator = {"page": found["page"], "bbox": found["bbox"], **locator}
    return {
        "anchor_id": anchor_id, "doc_id": ids.out(doc["id"]), "kb_id": ids.out(kb_id), "title": doc_title(doc),
        "file_name": doc.get("filename"), "section_title": section, "text": text, "context_before": before,
        "context_after": after, "locators": [locator] if locator else [],
        "anchor_type": (anchor or {}).get("element_type") or anchor_id.split(":")[-2],
    }


# ---- text DA hands out in other shapes -------------------------------------------------------

# DocTags (docling's structure_dt pages): every element carries <loc_N> positions
_DOCTAGS = re.compile(r"<loc_\d+>")
_DOCTAGS_CELL = re.compile(r"</?(?:fcel|ecel|lcel|ucel|xcel|ched|rhed|srow)>")
_DOCTAGS_TAG = re.compile(r"</?[a-z][a-z_]*(?:_\d+)?>")


def strip_doctags(text: str) -> str:
    """Plain text of DocTags markup; text without DocTags locations is returned as it is (it may hold real HTML)."""

    if not text or not _DOCTAGS.search(text):
        return text
    text = _DOCTAGS.sub("", text).replace("<nl>", "\n")
    text = _DOCTAGS_CELL.sub(" | ", text)
    text = _DOCTAGS_TAG.sub("\n", text)
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line.strip(" |") for line in lines if line.strip(" |"))


def _match_at(text: str, query: str) -> tuple[int, int] | None:
    """(start, end) of the query in the text: the whole query, else its words, else its longest piece."""

    lowered, query = text.lower(), " ".join(query.lower().split())[:64]
    if not query:
        return None
    for term in [query, *sorted((t for t in query.split() if len(t) > 1), key=len, reverse=True)]:
        found = lowered.find(term)
        if found != -1:
            return found, found + len(term)
    compact = query.replace(" ", "")
    for size in range(len(compact) - 1, 1, -1):
        for start in range(len(compact) - size + 1):
            found = lowered.find(compact[start:start + size])
            if found != -1:
                return found, found + size
    return None


def snippet_around(text: str, query: str, before: int = 100, after: int = 300) -> tuple[str, int | None]:
    """(snippet of the text around the query, offset of the match); the text's head when the query is not in it."""

    match = _match_at(text, query)
    if match is None:
        return text[:before + after].strip(), None
    start, end = max(0, match[0] - before), min(len(text), match[1] + after)
    snippet = text[start:end].strip()
    return ("..." if start else "") + snippet + ("..." if end < len(text) else ""), match[0]


def passage_at(text: str, snippet: str, query: str) -> int | None:
    """Offset in ``text`` of the passage a hit shows: the query inside the snippet's span, else where the snippet starts.

    ``None`` when the snippet is not found in the text and neither is the query.
    """

    body = snippet.strip().removeprefix("...").removeprefix("…").removesuffix("...").removesuffix("…")
    start = None
    for line in [ln.strip() for ln in body.splitlines() if ln.strip()][:3]:
        if len(line) >= 4 and (found := text.find(line[:40])) != -1:
            start = found
            break
    if start is None:
        match = _match_at(text, query)
        return match[0] if match else None
    match = _match_at(text[start:start + len(body) + 200], query)
    return start + match[0] if match else start


# ---- anchors placed in the L1 text -----------------------------------------------------------


def _anchor_index(anchor_id: str) -> tuple[int, str]:
    tail = anchor_id.rsplit(":", 1)[-1]
    return (int(tail), "") if tail.isdigit() else (0, tail)


def locate_anchors(text: str, anchors: list[dict]) -> list[tuple[int, str]]:
    """(offset in ``text``, anchor id) of DA's anchors, in reading order.

    DA's own line numbers of paragraphs are wrong, so each anchor is found by
    its preview; anchors of one type are numbered in reading order, so each is
    looked for after the previous one of its type (repeated text stays in order).
    """

    found: list[tuple[int, str]] = []
    by_type: dict[str, list[dict]] = {}
    for a in anchors:
        if a.get("id") and a.get("preview"):
            by_type.setdefault(a.get("type") or "", []).append(a)
    for group in by_type.values():
        pos = 0
        for a in sorted(group, key=lambda x: _anchor_index(x["id"])):
            needle = str(a["preview"]).strip().split("\n", 1)[0].strip()[:50]
            at = text.find(needle, pos) if needle else -1
            if at == -1:
                continue
            found.append((at, a["id"]))
            pos = at + 1
    return sorted(found)


def anchor_for(located: list[tuple[int, str]], start: int, end: int) -> str | None:
    """The anchor whose text starts in [start, end), else the one the span continues."""

    inside = next((a for at, a in located if start <= at < end), None)
    if inside:
        return inside
    before = [a for at, a in located if at < start]
    return before[-1] if before else None


# ---- page and box from the parser's JSON (docling or MinerU) ----------------------------------------


def _norm(text: object) -> str:
    return re.sub(r"[\W_]+", "", str(text or "")).lower()


def raw_items(raw: object) -> list[dict]:
    """Text items of DA's raw parser JSON with their page (from 1) and box ([x0, y0, x1, y1], points, top-left)."""

    if not isinstance(raw, dict):
        return []
    items: list[dict] = []
    if isinstance(raw.get("pdf_info"), list):  # MinerU: boxes are top-left already
        for page_idx, page in enumerate(raw["pdf_info"]):
            for block in (page or {}).get("para_blocks") or []:
                spans = [s.get("content") or "" for line in block.get("lines") or [] for s in line.get("spans") or []]
                box = block.get("bbox")
                if spans and isinstance(box, list) and len(box) == 4:
                    items.append({"text": _norm("".join(spans)), "page": page_idx + 1, "bbox": [float(v) for v in box]})
        return items
    heights = {str(k): (v.get("size") or {}).get("height") for k, v in (raw.get("pages") or {}).items() if isinstance(v, dict)}
    for kind in ("texts", "tables"):
        for item in raw.get(kind) or []:
            text = item.get("text")
            if kind == "tables":
                cells = ((item.get("data") or {}).get("table_cells") or [])
                text = "".join(str(c.get("text") or "") for c in cells)
            prov = (item.get("prov") or [None])[0]
            if not text or not isinstance(prov, dict) or not isinstance(prov.get("bbox"), dict):
                continue
            b, page = prov["bbox"], prov.get("page_no")
            if not isinstance(page, int):
                continue
            try:
                left, top, right, bottom = (float(b[k]) for k in ("l", "t", "r", "b"))
            except (KeyError, TypeError, ValueError):
                continue
            if str(b.get("coord_origin") or "").upper() == "BOTTOMLEFT":
                height = heights.get(str(page))
                if not isinstance(height, (int, float)):
                    continue
                top, bottom = height - top, height - bottom
            items.append({"text": _norm(text), "page": page, "bbox": [round(left, 2), round(top, 2), round(right, 2), round(bottom, 2)]})
    return items


def match_item(items: list[dict], text: str) -> dict | None:
    """The parser item a passage came from: the same text, else the item holding it, else the items it holds."""

    needle = _norm(text)
    if len(needle) < 2:
        return None
    for item in items:
        if item["text"] == needle:
            return {"page": item["page"], "bbox": item["bbox"]}
    head = needle[:40]
    for item in items:
        if len(item["text"]) >= len(head) and head in item["text"]:
            return {"page": item["page"], "bbox": item["bbox"]}
    inner = [i for i in items if len(i["text"]) >= 4 and i["text"] in needle]
    if not inner:
        return None
    page = inner[0]["page"]
    boxes = [i["bbox"] for i in inner if i["page"] == page]
    return {"page": page, "bbox": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                   max(b[2] for b in boxes), max(b[3] for b in boxes)]}


def block_anchors(text: str, doc_id: str) -> list[tuple[int, str]]:
    """(offset, anchor id) of the blocks ``content.structure`` names when DA keeps no anchors (``doc_id`` prefixed)."""

    return [(b.start, b.anchor_id) for b in parse_blocks(text, doc_id) if b.anchor_id]


def window_anchors(text: str, doc_id: str, located: list[tuple[int, str, dict]], start: int, end: int,
                   ids: Ids) -> list[dict]:
    """``content.structure`` anchors of a window, named after DA's own anchors placed in the text."""

    blocks = parse_blocks(text, doc_id)
    out = []
    for at, raw_id, anchor in located:
        if not start <= at < end:
            continue
        block = next((b for b in blocks if b.start <= at < b.end), None)
        out.append({
            "id": ids.out(raw_id), "type": anchor.get("type") or raw_id.split(":")[-2],
            "line_start": text.count("\n", 0, at) + 1,
            "preview": (text[at:block.end] if block else str(anchor.get("preview") or ""))[:80],
            "section_title": (block.header_path[-1] if block and block.header_path else None),
        })
    return out
