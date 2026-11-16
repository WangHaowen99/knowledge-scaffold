"""WeKnora objects -> knowledge-provider/v1 shapes (pure functions)."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from kb_provider_kit import Ids, ProviderError
from kb_provider_kit.textfiles import fit_file_name

# WeKnora parse_status -> contract. "finalizing" is already indexed and searchable;
# only enrichment (summary, questions) is still running. "deleting" is on its way out.
PARSE_STATUS = {
    "pending": "pending", "draft": "pending", "processing": "processing", "deleting": "processing",
    "finalizing": "completed", "completed": "completed", "failed": "failed", "cancelled": "cancelled",
}
SOURCE_TYPE = {"file": "file", "url": "url", "manual": "text"}
# WeKnora page types -> contract page types (summary / entity / concept / index)
WIKI_TYPE = {"summary": "summary", "entity": "entity", "concept": "concept", "index": "index",
             "synthesis": "concept", "comparison": "concept"}
LOCATOR_KEYS = ("page", "bbox", "sheet", "row_start", "row_end", "slide")
RRF_K = 60

# knowledges.title and knowledges.file_name are varchar(255) in WeKnora's schema; a longer
# value ends in a database error (SQLSTATE 22001) that WeKnora passes on as a 500
MAX_NAME_CHARS = 255
# a text entry is stored with file_name "<title>.md"
MAX_TITLE_CHARS = MAX_NAME_CHARS - len(".md")
# WeKnora rejects malformed .json and rewrites valid JSON (arrays become objects keyed
# "0", "1", ...). JSON handed over as plain text gets this suffix after its own name;
# no user names a file like that, so only such names are shown as the .json again
JSON_TEXT_SUFFIX = ".as-text.txt"
_JSON_TEXT = re.compile(r"(\.json)\.as-text\.txt(?=[\";]|$)", re.IGNORECASE)
URL_REFUSED = "不允许导入内部或私有地址"
# WeKnora's SSRF refusals name the resolved address and its allow-list settings
_SSRF_MARKERS = ("ssrf", "安全校验", "restricted", "whitelist", "白名单")
# ASCII neighbours only: Chinese text runs straight up to an address
_IPV4 = re.compile(r"(?<![0-9A-Za-z_.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9A-Za-z_]|\.[0-9])")
_IPV6 = re.compile(r"(?<![0-9A-Za-z_:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Za-z_:])")
_SETTING = re.compile(r"(?<![0-9A-Za-z_])SSRF_[0-9A-Z_]+")

DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 200
DEFAULT_SEPARATORS = ["\n\n", "\n", "。"]

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "chunk_size": {"type": "integer", "title": "分块大小（字符）", "minimum": 100, "maximum": 10000,
                       "default": DEFAULT_CHUNK_SIZE},
        "chunk_overlap": {"type": "integer", "title": "分块重叠（字符）", "minimum": 0, "maximum": 2000,
                          "default": DEFAULT_CHUNK_OVERLAP},
        "wiki_enabled": {"type": "boolean", "title": "Wiki",
                         "description": "由 WeKnora 把文档整理成相互链接的 Wiki 页面，需要配置对话模型。", "default": False},
    },
}


def upstream_values(table: dict[str, str], value: str) -> list[str]:
    """The WeKnora values that ``table`` maps to the contract ``value`` (a filter in reverse)."""

    return [raw for raw, mapped in table.items() if mapped == value]


def _config_int(config: dict, key: str) -> int | None:
    """An integer field of CONFIG_SCHEMA; ``None`` when it is missing or null (cleared in the form)."""

    value = config.get(key)
    if value is None:
        return None
    spec = CONFIG_SCHEMA["properties"][key]
    try:
        number = int(value) if isinstance(value, str) else value
        valid = not isinstance(number, bool) and isinstance(number, (int, float)) and int(number) == number
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid or not spec["minimum"] <= number <= spec["maximum"]:
        raise ProviderError(400, "bad_request", f"{spec['title']}需要是 {spec['minimum']}–{spec['maximum']} 之间的整数")
    return int(number)


def _check_overlap(size: int, overlap: int) -> None:
    if overlap >= size:
        raise ProviderError(400, "bad_request", "分块重叠需要小于分块大小")


def kb_config(kb: dict) -> dict:
    chunking = kb.get("chunking_config") or {}
    strategy = kb.get("indexing_strategy") or {}
    return {
        "chunk_size": int(chunking.get("chunk_size") or DEFAULT_CHUNK_SIZE),
        "chunk_overlap": int(chunking.get("chunk_overlap") or 0),
        "wiki_enabled": bool(strategy.get("wiki_enabled")),
    }


def kb_view(kb: dict, ids: Ids, failed: int = 0, cancelled: int = 0, chunks: int | None = None,
            documents: int | None = None) -> dict:
    """``chunks`` is the chunk total; None (not computed yet) leaves it out of the stats.

    ``documents`` is the document total counted from the document list; None falls back to
    WeKnora's ``knowledge_count``.
    """

    documents = int(kb.get("knowledge_count") or 0) if documents is None else documents
    # WeKnora counts pending + processing here, the same as the contract's "processing"
    processing = int(kb.get("processing_count") or 0)
    stats = {
        "documents": documents,
        "completed": max(0, documents - processing - failed - cancelled),
        "processing": processing,
        "failed": failed,
        "cancelled": cancelled,
    }
    if chunks is not None:
        stats["chunks"] = chunks
    return {
        "id": ids.out(kb["id"]),
        "name": kb.get("name") or "",
        "description": kb.get("description") or "",
        "created_at": kb.get("created_at"),
        "updated_at": kb.get("updated_at"),
        "config": kb_config(kb),
        "stats": stats,
    }


def create_body(body: dict, *, embedding_model_id: str, chat_model_id: str) -> dict:
    config = body.get("config") or {}
    wiki = bool(config.get("wiki_enabled"))
    size = _config_int(config, "chunk_size")
    size = DEFAULT_CHUNK_SIZE if size is None else size
    overlap = _config_int(config, "chunk_overlap")
    overlap = min(DEFAULT_CHUNK_OVERLAP, size // 2) if overlap is None else overlap
    _check_overlap(size, overlap)
    out: dict[str, Any] = {
        "name": body.get("name"),
        "description": body.get("description") or "",
        "embedding_model_id": embedding_model_id,
        "chunking_config": {"chunk_size": size, "chunk_overlap": overlap, "separators": DEFAULT_SEPARATORS},
        "indexing_strategy": {"vector_enabled": True, "keyword_enabled": True, "wiki_enabled": wiki, "graph_enabled": False},
    }
    if chat_model_id:
        out["summary_model_id"] = chat_model_id
        out["wiki_config"] = {"synthesis_model_id": chat_model_id}
    return out


def update_body(current: dict, patch: dict, *, chat_model_id: str) -> dict:
    """WeKnora's PUT needs the name and takes config sections whole; merge the patch into them."""

    config = patch.get("config") or {}
    chunking = {**(current.get("chunking_config") or {})}
    changed = {key: _config_int(config, key) for key in ("chunk_size", "chunk_overlap")}
    changed = {key: value for key, value in changed.items() if value is not None}
    if changed:
        chunking.update(changed)
        _check_overlap(int(chunking.get("chunk_size") or DEFAULT_CHUNK_SIZE), int(chunking.get("chunk_overlap") or 0))
    strategy = {**(current.get("indexing_strategy") or {})}
    if config.get("wiki_enabled") is not None:
        strategy["wiki_enabled"] = bool(config["wiki_enabled"])
    out_config: dict[str, Any] = {"chunking_config": chunking, "indexing_strategy": strategy}
    if strategy.get("wiki_enabled") and chat_model_id and not (current.get("wiki_config") or {}).get("synthesis_model_id"):
        out_config["wiki_config"] = {**(current.get("wiki_config") or {}), "synthesis_model_id": chat_model_id}
    return {
        "name": patch.get("name") or current.get("name"),
        "description": patch["description"] if "description" in patch else current.get("description", ""),
        "config": out_config,
    }


def upload_name(name: str, *, as_text: bool) -> str:
    """The base file name WeKnora gets: within its column, with the suffix of JSON handed over as text.

    The ``fileName`` field is "folder/name"; WeKnora splits it at the last '/'
    (types.SplitKnowledgeRelativePath): the folder goes to folder_path, varchar(1024),
    which WeKnora fits itself (each level cut to 128 bytes, at most 16 levels), and
    only the base name goes to file_name and title, varchar(255).
    """

    suffix = JSON_TEXT_SUFFIX if as_text else ""
    return fit_file_name(name, max_chars=MAX_NAME_CHARS - len(suffix)) + suffix


def display_name(name: str) -> str:
    """A JSON file this adapter handed over as text shows under its own name again."""

    return _JSON_TEXT.sub(r"\1", name)


def scrub_network(message: str) -> str:
    """``message`` without IP addresses or SSRF setting names."""

    def hide(match: re.Match) -> str:
        try:
            ipaddress.ip_address(match.group())
        except ValueError:
            return match.group()
        return "（地址已隐藏）"

    return _SETTING.sub("相关配置", _IPV6.sub(hide, _IPV4.sub(hide, message)))


def url_import_error(exc: ProviderError) -> ProviderError:
    """What the caller is told when WeKnora refuses a URL import.

    WeKnora's SSRF answer carries the address the host name resolved to and the
    names of its allow-list settings; neither is for the user.
    """

    lower = exc.message.lower()
    if any(marker in lower for marker in _SSRF_MARKERS):
        if "dns resolution failed" in lower:
            return ProviderError(400, "bad_url", "无法解析网址中的域名，请检查网址")
        if "invalid scheme" in lower:
            return ProviderError(400, "bad_url", "只能导入 http 或 https 网址")
        return ProviderError(400, "url_not_allowed", URL_REFUSED)
    if lower.endswith("invalid url"):
        # WeKnora's service-level check answers a bare 500 "invalid URL", SSRF included
        return ProviderError(400, "bad_url", f"网址无效，或{URL_REFUSED}")
    return ProviderError(exc.status, exc.code, scrub_network(exc.message))


def doc_view(k: dict, ids: Ids, chunk_count: int | None = None) -> dict:
    """``chunk_count`` comes from the adapter's chunk statistics; None when not counted yet."""

    kind = k.get("type") or "file"
    file_type = (k.get("file_type") or ("md" if kind == "manual" else "")).lower().lstrip(".")
    title, file_name = k.get("title") or "", k.get("file_name") or ""
    if kind == "file" and display_name(file_name) != file_name:
        title, file_name, file_type = display_name(title), display_name(file_name), "json"
    error = k.get("error_message") or None
    return {
        "id": ids.out(k["id"]),
        "kb_id": ids.out(k.get("knowledge_base_id")),
        "title": title or file_name,
        "file_name": file_name or None,
        "file_type": file_type,
        "file_size": k.get("file_size") or None,
        "folder_path": k.get("folder_path") or "",
        "source_type": SOURCE_TYPE.get(kind, "file"),
        "source_url": k.get("source") if kind == "url" else None,
        "parse_status": PARSE_STATUS.get(k.get("parse_status") or "", "processing"),
        "stage": k.get("parse_status"),
        # a failed fetch names the address it tried
        "error_message": scrub_network(error) if error and kind == "url" else error,
        "summary": k.get("description") or None,
        "summary_status": k.get("summary_status"),
        "chunk_count": chunk_count,
        "l1_chars": None,
        "created_at": k.get("created_at"),
        "processed_at": k.get("processed_at"),
        "updated_at": k.get("updated_at"),
    }


def locators(raw: list | None) -> list[dict]:
    out = []
    for loc in raw or []:
        item = {key: loc[key] for key in LOCATOR_KEYS if loc.get(key) not in (None, "", 0)}
        if item:
            out.append(item)
    return out


def chunk_view(c: dict, ids: Ids) -> dict:
    return {
        "id": ids.out(c["id"]),
        "seq": int(c.get("chunk_index") or 0),
        "chunk_type": "text",
        "content": c.get("content") or "",
        "context_header": "",
        "line_start": None,
        "anchor_id": None,
        "source_locators": locators(c.get("source_locators")),
        "questions": [q.get("question") for q in ((c.get("metadata") or {}).get("generated_questions") or []) if q.get("question")],
    }


def _repeated_table_head(content: str, span: int) -> int:
    """Length of the table header WeKnora puts in front of a continued table's chunk, else 0.

    Such a chunk is longer than its ``start_at``..``end_at`` span by exactly the header lines
    (every line a Markdown table row), which are not part of the parsed text at that offset.
    """

    extra = len(content) - span
    if extra <= 0 or content[extra - 1] != "\n":
        return 0
    lines = content[:extra].split("\n")[:-1]
    return extra if lines and all(line.lstrip().startswith("|") for line in lines) else 0


def stitch(chunks: list[dict]) -> str:
    """Rebuild the parsed text from text chunks, dropping the overlap between neighbours.

    ``start_at``/``end_at`` are offsets in WeKnora's parsed text. A chunk that continues a
    table starts with the table's header again; that header is dropped. When the offsets
    do not match the chunk otherwise (or are missing), chunks are joined with a blank line.
    """

    parts: list[str] = []
    cursor: int | None = None
    for chunk in sorted(chunks, key=lambda c: int(c.get("chunk_index") or 0)):
        content = chunk.get("content") or ""
        start, end = chunk.get("start_at"), chunk.get("end_at")
        offsets = isinstance(start, int) and isinstance(end, int)
        body = content[_repeated_table_head(content, end - start):] if offsets and cursor is not None else content
        exact = offsets and end - start == len(body)
        if exact and cursor is not None and start <= cursor:
            # contiguous (start == cursor) or overlapping: keep only the new text
            parts.append(body[cursor - start:])
        else:
            parts.append(("\n\n" if parts else "") + content)
        cursor = max(cursor or 0, end) if exact else None
    return "".join(parts)


def hit_view(r: dict, ids: Ids) -> dict:
    return {
        "chunk_id": ids.out(r.get("id")),
        "doc_id": ids.out(r.get("knowledge_id")),
        "kb_id": ids.out(r.get("knowledge_base_id")),
        "seq": int(r.get("chunk_index") or 0),
        "chunk_type": "text",
        "title": display_name(r.get("knowledge_title") or r.get("knowledge_filename") or ""),
        "file_name": display_name(r.get("knowledge_filename") or "") or None,
        "context_header": "",
        "content": r.get("content") or "",
        "score": float(r.get("score") or 0.0),
        "relevance": None,
        "line_start": None,
        "anchor_id": None,
        "locators": locators(r.get("source_locators")),
        # WeKnora's matched_content is the matched chunk text for documents, not a question
    }


EXACT_BEFORE = 300
EXACT_AFTER = 500


def exact_hit(doc: dict, text: str, pos: int) -> dict:
    """A search hit for an identifier found at ``pos`` of a document's parsed text: the lines around it."""

    start = text.rfind("\n", 0, max(0, pos - EXACT_BEFORE)) + 1
    end = text.find("\n", min(len(text), pos + EXACT_AFTER))
    end = len(text) if end < 0 else end
    line = text.count("\n", 0, start) + 1
    return {
        "chunk_id": f"{doc['id']}#line-{line}", "doc_id": doc["id"], "kb_id": doc["kb_id"],
        "seq": 0, "chunk_type": "text", "title": doc.get("title"), "file_name": doc.get("file_name"),
        "context_header": "", "content": text[start:end], "score": 0.0, "relevance": None,
        "line_start": line, "anchor_id": None, "locators": [], "matched_by": ["exact"],
    }


def wiki_hit_view(p: dict, ids: Ids) -> dict:
    return {
        "chunk_id": ids.out(p.get("id")),
        "doc_id": "",
        "kb_id": ids.out(p.get("knowledge_base_id")),
        "seq": 0,
        "chunk_type": "wiki_page",
        "title": p.get("title") or p.get("slug"),
        "context_header": WIKI_TYPE.get(p.get("page_type") or "", "concept"),
        "content": "\n\n".join(x for x in (p.get("summary"), p.get("match_snippet")) if x),
        # placed among the chunk hits by ``fuse``
        "score": 0.0,
        "relevance": None,
        "wiki_slug": p.get("slug"),
        "wiki_type": WIKI_TYPE.get(p.get("page_type") or "", "concept"),
        "wiki_sources": [],
        "locators": [],
    }


def fuse(chunks: list[dict], wiki: list[dict], top_k: int) -> list[dict]:
    """Interleave chunk hits and wiki hits by reciprocal rank; ``score`` becomes the fused score.

    WeKnora ranks the two separately and their scores are not comparable; the
    chunk hit's own score stays in ``weknora_score``.
    """

    fused = []
    for hits in (chunks, wiki):
        for rank, hit in enumerate(hits):
            fused.append((1.0 / (RRF_K + rank + 1), rank, hit))
    fused.sort(key=lambda x: (-x[0], x[1]))
    out = []
    for score, _, hit in fused[:top_k]:
        extra = {} if hit["chunk_type"] == "wiki_page" else {"weknora_score": hit["score"]}
        out.append({**hit, **extra, "score": round(score, 6)})
    return out


def source_refs(refs: list | None, ids: Ids) -> list[dict]:
    out = []
    for ref in refs or []:
        doc_id, _, title = str(ref).partition("|")
        if doc_id:
            out.append({"id": ids.out(doc_id), "title": title or doc_id})
    return out


def wiki_item(p: dict, ids: Ids) -> dict:
    return {
        "id": ids.out(p.get("id")), "slug": p.get("slug"), "title": p.get("title"),
        "page_type": WIKI_TYPE.get(p.get("page_type") or "", "concept"), "summary": p.get("summary") or "",
        "aliases": p.get("aliases") or [], "updated_at": p.get("updated_at"),
    }


def wiki_page(p: dict, ids: Ids) -> dict:
    titles = p.get("in_link_titles") or {}
    sources = source_refs(p.get("source_refs"), ids)
    return {
        **wiki_item(p, ids),
        "content": p.get("content") or "",
        "links_to": [{"slug": s, "title": s, "page_type": None} for s in p.get("out_links") or []],
        "linked_from": [{"slug": s, "title": titles.get(s, s), "page_type": None} for s in p.get("in_links") or []],
        "sources": sources,
        "source_doc_ids": [s["id"] for s in sources],
        "out_links": p.get("out_links") or [],
        "in_links": p.get("in_links") or [],
        "last_edit_source": p.get("last_edit_source"),
        "revisions": [],
    }
