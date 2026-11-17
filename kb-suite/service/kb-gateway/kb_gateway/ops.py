"""Knowledge operations over the registered backends, shared by the management API, DA tools and MCP."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, NamedTuple

from .config import get_settings
from .registry import Backend, Registry, UpstreamError, require

logger = logging.getLogger(__name__)

RRF_K = 60
# contract caps (POST /v1/search, POST /v1/grep, GET .../structure); callers clamp before forwarding
SEARCH_MAX_TOP_K = 50
GREP_MAX_RESULTS = 200
GREP_MAX_LINES_PER_DOC = 100
STRUCTURE_MAX_CHARS = 200000


def annotate_kb(kb: dict, backend: Backend) -> dict:
    return {**kb, "backend": backend.ref(), "features": sorted(backend.features)}


def failure(backend: Backend, exc: BaseException) -> dict:
    """A backend's failed part of a call: its ref, a readable message and the error code."""

    if isinstance(exc, UpstreamError):
        return {**backend.ref(), "error": exc.message, "code": exc.code}
    return {**backend.ref(), "error": str(exc), "code": "backend_error"}


def _unlisted(backend: Backend, error: str | None) -> dict:
    # id_prefix and the knowledge bases last listed there let a page keep a selection made on this backend
    return {**backend.ref(), "error": error, "id_prefix": backend.config.id_prefix,
            "knowledge_bases": [{"id": kb_id, "name": name} for kb_id, name in backend.kb_names.items()]}


async def list_kbs(registry: Registry) -> tuple[list[dict], list[dict]]:
    """All knowledge bases of all healthy backends, plus the backends that could not be listed."""

    async def one(backend: Backend) -> list[dict]:
        # every page load lists all backends: one that accepts but never answers must not stall it
        data = await backend.json("GET", "/v1/kbs", timeout=get_settings().search_timeout_s)
        kbs = (data or {}).get("items", [])
        backend.kb_names = {kb["id"]: str(kb.get("name") or kb["id"]) for kb in kbs}
        return [annotate_kb(kb, backend) for kb in kbs]

    items: list[dict] = []
    unavailable: list[dict] = []
    backends = registry.backends
    results = await asyncio.gather(*(one(b) for b in backends if b.healthy), return_exceptions=True)
    for backend, result in zip([b for b in backends if b.healthy], results):
        if isinstance(result, Exception):
            unavailable.append(_unlisted(backend, getattr(result, "message", str(result))))
        else:
            items.extend(result)
    unavailable.extend(_unlisted(b, b.last_error) for b in backends if not b.healthy and b.visible)
    return items, unavailable


def _scope_groups(registry: Registry, kb_ids: list[str]) -> list[tuple[Backend, list[str]]]:
    """``[]`` means every knowledge base: each backend searches all of its own.

    Unavailable backends stay in, so the caller reports them instead of silently searching less.
    """

    if kb_ids:
        return [(registry.by_id[bid], ids) for bid, ids in registry.group(kb_ids).items()]
    return [(b, []) for b in registry.backends if b.visible]


def rrf_merge(lists: list[list[dict]], top_k: int) -> list[dict]:
    """Reciprocal rank fusion: backend scores are not comparable, ranks are."""

    if len(lists) == 1:
        return lists[0][:top_k]
    fused: list[tuple[float, int, dict]] = []
    for items in lists:
        for rank, item in enumerate(items):
            fused.append((1.0 / (RRF_K + rank + 1), rank, item))
    fused.sort(key=lambda x: (-x[0], x[1]))
    out = []
    for score, _, item in fused[:top_k]:
        out.append({**item, "fused_score": round(score, 6)})
    return out


class SearchOutcome(NamedTuple):
    hits: list[dict]
    failed: list[dict]  # per backend: ref, error, code
    traces: list[dict]  # per backend, when asked for
    warnings: list[str]  # what a backend says about its results, e.g. a degraded vector leg


async def search(
    registry: Registry, kb_ids: list[str], body: dict, top_k: int, *, fan_out: bool = False,
) -> SearchOutcome:
    """Search across backends.

    ``fan_out``: this call is one of several the caller runs in parallel, so it gets the short timeout too.
    """

    groups = _scope_groups(registry, kb_ids)
    if not groups:
        raise UpstreamError(503, "backend_unavailable", "没有可用的知识后端")
    if len(groups) == 1:
        require(groups[0][0])
    trace: list[dict] = []
    warnings: list[str] = []

    async def one(backend: Backend, ids: list[str]) -> list[dict]:
        # an unhealthy backend fails its own group only; the others still answer
        require(backend)
        data = await backend.json(
            "POST", "/v1/search", json={**body, "kb_ids": ids, "top_k": top_k},
            timeout=get_settings().search_timeout_s if fan_out or len(groups) > 1 else None,
        )
        if isinstance((data or {}).get("trace"), dict):
            trace.append({"backend": backend.id, **data["trace"]})
        for warning in (data or {}).get("warnings") or []:
            warnings.append(f"{backend.config.label}：{warning}" if len(groups) > 1 else str(warning))
        return [{**hit, "backend": backend.id} for hit in (data or {}).get("items", [])]

    if len(groups) == 1:
        return SearchOutcome(await one(*groups[0]), [], trace, warnings)
    results = await asyncio.gather(*(one(b, ids) for b, ids in groups), return_exceptions=True)
    lists, failed = [], []
    for (backend, _), result in zip(groups, results):
        if isinstance(result, Exception):
            failed.append(failure(backend, result))
            logger.warning("search on %s failed: %s", backend.id, result)
        else:
            lists.append(result)
    if not lists:
        codes = {f["code"] for f in failed}
        raise UpstreamError(502, codes.pop() if len(codes) == 1 else "backend_error",
                            "所有知识后端检索都失败了：" + "；".join(f"{f['label']}：{f['error']}" for f in failed))
    return SearchOutcome(rrf_merge(lists, top_k), failed, trace, warnings)


async def get_document(registry: Registry, doc_id: str) -> dict | None:
    """``None`` when the backend has no such document; an unavailable backend raises (it is not "not found")."""

    backend = registry.route(doc_id)
    require(backend)
    try:
        return await backend.json("GET", f"/v1/documents/{doc_id}")
    except UpstreamError as exc:
        if exc.status == 404:
            return None
        raise


async def read_content(registry: Registry, doc_id: str, level: str) -> str:
    backend = registry.route(doc_id)
    require(backend, "content.L2" if level == "L2" else None)
    return await backend.text("GET", f"/v1/documents/{doc_id}/content", params={"level": level})


async def read_chunks(registry: Registry, doc_id: str, offset: int, limit: int) -> dict:
    backend = registry.route(doc_id)
    require(backend)
    return await backend.json("GET", f"/v1/documents/{doc_id}/chunks", params={"offset": offset, "limit": limit})


async def list_documents(registry: Registry, kb_id: str, *, limit: int = 2000, status: str | None = None) -> list[dict]:
    backend = registry.route(kb_id)
    require(backend)
    out: list[dict] = []
    while len(out) < limit:
        params: dict[str, Any] = {"limit": min(500, limit - len(out)), "offset": len(out)}
        if status:
            params["status"] = status
        page = await backend.json("GET", f"/v1/kbs/{kb_id}/documents", params=params)
        items = (page or {}).get("items", [])
        out.extend(items)
        if not items or len(out) >= int((page or {}).get("total") or 0):
            break
    return out


async def grep(
    registry: Registry, kb_ids: list[str], pattern: str, *, max_results: int = 50, max_lines_per_doc: int = 10,
) -> dict:
    """Merged ``POST /v1/grep`` of the backends in scope.

    Backends without ``grep`` are listed in ``skipped_backends``; unavailable ones, and those whose grep
    failed, in ``failed_backends``.
    """

    groups = _scope_groups(registry, kb_ids)
    eligible = [(b, ids) for b, ids in groups if b.healthy and b.has("grep")]
    skipped = [b.ref() for b, _ in groups if b.healthy and not b.has("grep")]
    down = [{**b.ref(), "error": f"{b.config.label} 当前不可用：{b.last_error}", "code": "backend_unavailable"}
            for b, _ in groups if not b.healthy]
    body = {"pattern": pattern, "max_results": max_results, "max_lines_per_doc": max_lines_per_doc}
    timeout = get_settings().search_timeout_s if len(eligible) > 1 else None
    outcomes = await asyncio.gather(
        *(b.json("POST", "/v1/grep", json={**body, "kb_ids": ids}, timeout=timeout) for b, ids in eligible),
        return_exceptions=True,
    )
    found, failed = [], list(down)
    for (backend, _), outcome in zip(eligible, outcomes):
        if isinstance(outcome, Exception):
            failed.append(failure(backend, outcome))
        else:
            found.append(outcome)
    if eligible and not found:
        first = outcomes[0]
        raise first if isinstance(first, UpstreamError) else UpstreamError(502, "backend_unavailable", str(first))
    matches = [m for r in found for m in r.get("matches") or []]
    out = {
        "pattern": pattern,
        "search_mode": found[0].get("search_mode") if found else "regex",
        "total_matches": sum(int(r.get("total_matches") or 0) for r in found),
        "total_documents": sum(int(r.get("total_documents") or 0) for r in found),
        "has_more": any(r.get("has_more") for r in found) or len(matches) > max_results,
        "matches": matches[:max_results],
    }
    if skipped:
        out["skipped_backends"] = skipped
    if failed:
        out["failed_backends"] = failed
    return out


def grep_view(result: dict) -> dict:
    """The contract grep result in the camelCase shape DA's doc_grep and the MCP tool have always returned."""

    out = {
        "pattern": result["pattern"], "searchMode": result["search_mode"], "totalMatches": result["total_matches"],
        "totalDocuments": result["total_documents"], "hasMore": result["has_more"],
        "matches": [
            {"docId": m["doc_id"], "kbId": m["kb_id"], "title": m.get("title"), "fileName": m.get("file_name"),
             "pageId": f"{m['doc_id']}:L1", "pageType": "structure_md",
             "lines": [{"lineNumber": x["line_number"], "text": x["text"]} for x in m.get("lines") or []]}
            for m in result["matches"]
        ],
    }
    for key in ("skipped_backends", "failed_backends"):
        if key in result:
            out[key] = result[key]
    return out


async def structure(registry: Registry, doc_id: str, *, offset: int = 0, limit: int = 30000,
                    heading: str | None = None) -> dict | None:
    """``GET /v1/documents/{id}/structure``; ``None`` when ``heading`` is not in the document."""

    backend = registry.route(doc_id)
    require(backend, "content.structure")
    params: dict[str, Any] = {"offset": offset, "limit": limit}
    if heading:
        params["heading"] = heading
    try:
        return await backend.json("GET", f"/v1/documents/{doc_id}/structure", params=params)
    except UpstreamError as exc:
        if exc.status == 404 and heading:
            return None
        raise
