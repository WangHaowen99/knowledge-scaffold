"""DeepAnalyze objects -> knowledge-provider/v1 shapes (pure functions)."""

from __future__ import annotations

import re

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


def chunk_view(doc_id: str, seq: int, line_start: int, content: str, ids: Ids) -> dict:
    return {
        "id": ids.out(f"{doc_id}.{seq}"),
        "seq": seq,
        "chunk_type": "text",
        "content": content,
        "context_header": "",
        "line_start": line_start,
        "anchor_id": None,
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
        key = (hit["doc_id"], hit.get("anchor_id"))
        text = (hit["doc_id"], " ".join((hit.get("content") or "").split()))
        if key in seen or (text[1] and text in texts):
            continue
        seen.add(key)
        texts.add(text)
        out.append(hit)
    # each document's best hit first, so the top of the list shows different documents; the rest keep their order
    firsts: set[str] = set()
    head, tail = [], []
    for hit in out:
        (tail if hit["doc_id"] in firsts else head).append(hit)
        firsts.add(hit["doc_id"])
    return head + tail


def hit_view(r: dict, ids: Ids, filenames: dict[str, str]) -> dict:
    doc_id = r.get("docId") or ""
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
        "content": _MARK.sub("", r.get("snippet") or ""),
        "score": float(r.get("score") or 0.0),
        "relevance": None,
        "line_start": None,
        # anchors are "{docId}:{type}:{index}", so the doc id prefix goes in front
        "anchor_id": ids.out(anchor) if anchor else None,
        "locators": [],
        "page_type": r.get("pageType"),
    }
