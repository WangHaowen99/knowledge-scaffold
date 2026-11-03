"""Management API for the knowledge-base page: kb-service's ``/v1`` paths, routed to the owning backend."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from .. import ops
from ..auth import CAP_INGEST, CAP_MANAGE, CAP_RETRIEVE, Principal, check_scope
from ..deps import need, registry
from ..registry import Backend, UpstreamError, require

router = APIRouter(prefix="/v1")


def _relay(response) -> Response:
    if response.status_code == 204:
        return Response(status_code=204)
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
    )


async def _forward(backend: Backend, method: str, path: str, feature: str | None = None, **kwargs: Any) -> Response:
    require(backend, feature)
    return _relay(await backend.send(method, path, **kwargs))


def _kb(kb_id: str) -> Backend:
    return registry().route(kb_id)


@router.get("/me")
async def me(principal: Principal = Depends(need(CAP_RETRIEVE))) -> dict:
    from ..config import get_settings

    return {"key": principal.name, "tenant_id": get_settings().tenant, "capabilities": sorted(principal.capabilities)}


@router.get("/backends")
async def backends(_: Principal = Depends(need(CAP_RETRIEVE))) -> dict:
    return {"items": [b.summary() for b in registry().backends if b.visible]}


@router.get("/config/meta")
async def config_meta(backend: str | None = None, _: Principal = Depends(need(CAP_RETRIEVE))) -> Response:
    return await _forward(registry().get(backend), "GET", "/v1/config/meta", "config.meta")


# ---- knowledge bases --------------------------------------------------------


@router.get("/kbs")
async def list_kbs(_: Principal = Depends(need(CAP_RETRIEVE))) -> dict:
    items, unavailable = await ops.list_kbs(registry())
    return {"items": items, "unavailable_backends": unavailable}


@router.post("/kbs", status_code=201)
async def create_kb(request: Request, _: Principal = Depends(need(CAP_MANAGE))) -> dict:
    body = await request.json()
    backend = registry().get(body.pop("backend", None))
    require(backend, "kbs.write")
    kb = await backend.json("POST", "/v1/kbs", json=body)
    return ops.annotate_kb(kb, backend)


@router.get("/kbs/{kb_id}")
async def get_kb(kb_id: str, _: Principal = Depends(need(CAP_RETRIEVE))) -> dict:
    backend = _kb(kb_id)
    require(backend)
    return ops.annotate_kb(await backend.json("GET", f"/v1/kbs/{kb_id}"), backend)


@router.patch("/kbs/{kb_id}")
async def patch_kb(kb_id: str, request: Request, _: Principal = Depends(need(CAP_MANAGE))) -> dict:
    backend = _kb(kb_id)
    require(backend, "kbs.write")
    kb = await backend.json("PATCH", f"/v1/kbs/{kb_id}", json=await request.json())
    return ops.annotate_kb(kb, backend)


@router.delete("/kbs/{kb_id}", status_code=204)
async def delete_kb(kb_id: str, _: Principal = Depends(need(CAP_MANAGE))) -> Response:
    return await _forward(_kb(kb_id), "DELETE", f"/v1/kbs/{kb_id}", "kbs.write")


@router.post("/kbs/{kb_id}/reprocess")
async def reprocess_kb(kb_id: str, request: Request, _: Principal = Depends(need(CAP_INGEST))) -> Response:
    return await _forward(_kb(kb_id), "POST", f"/v1/kbs/{kb_id}/reprocess", "kbs.reprocess", json=await request.json())


@router.post("/kbs/{kb_id}/profile/{action}")
async def profile(kb_id: str, action: str, _: Principal = Depends(need(CAP_MANAGE))) -> Response:
    if action not in {"generate", "apply"}:
        raise UpstreamError(404, "not_found", f"未知操作 {action}")
    return await _forward(_kb(kb_id), "POST", f"/v1/kbs/{kb_id}/profile/{action}", "profile")


# ---- documents ----------------------------------------------------------------


@router.get("/kbs/{kb_id}/documents")
async def list_docs(
    kb_id: str,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: Principal = Depends(need(CAP_RETRIEVE)),
) -> Response:
    params = {k: v for k, v in {"status": status, "q": q, "limit": limit, "offset": offset}.items() if v is not None}
    return await _forward(_kb(kb_id), "GET", f"/v1/kbs/{kb_id}/documents", params=params)


@router.post("/kbs/{kb_id}/documents", status_code=201)
async def upload_doc(
    kb_id: str,
    file: UploadFile = File(...),
    folder_path: str = Form(""),
    process_overrides: str = Form(""),
    _: Principal = Depends(need(CAP_INGEST)),
) -> Response:
    data = await file.read()
    return await _forward(
        _kb(kb_id), "POST", f"/v1/kbs/{kb_id}/documents", "ingest.file",
        files={"file": (file.filename or "upload", data, file.content_type or "application/octet-stream")},
        data={"folder_path": folder_path, "process_overrides": process_overrides},
        timeout=600,
    )


@router.post("/kbs/{kb_id}/documents/text", status_code=201)
async def text_doc(kb_id: str, request: Request, _: Principal = Depends(need(CAP_INGEST))) -> Response:
    return await _forward(_kb(kb_id), "POST", f"/v1/kbs/{kb_id}/documents/text", "ingest.text", json=await request.json())


@router.post("/kbs/{kb_id}/documents/url", status_code=201)
async def url_doc(kb_id: str, request: Request, _: Principal = Depends(need(CAP_INGEST))) -> Response:
    return await _forward(_kb(kb_id), "POST", f"/v1/kbs/{kb_id}/documents/url", "ingest.url", json=await request.json())


@router.get("/documents/{doc_id}")
async def get_doc(doc_id: str, _: Principal = Depends(need(CAP_RETRIEVE))) -> Response:
    return await _forward(_kb(doc_id), "GET", f"/v1/documents/{doc_id}")


@router.get("/documents/{doc_id}/chunks")
async def get_chunks(
    doc_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    _: Principal = Depends(need(CAP_RETRIEVE)),
) -> Response:
    return await _forward(_kb(doc_id), "GET", f"/v1/documents/{doc_id}/chunks", params={"offset": offset, "limit": limit})


@router.get("/documents/{doc_id}/content")
async def get_content(
    doc_id: str, level: str = Query("L1", pattern="^L[012]$"), _: Principal = Depends(need(CAP_RETRIEVE))
) -> Response:
    feature = "content.L2" if level == "L2" else None
    return await _forward(_kb(doc_id), "GET", f"/v1/documents/{doc_id}/content", feature, params={"level": level})


@router.get("/documents/{doc_id}/structure")
async def get_structure(
    doc_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(30000, ge=0, le=200000),
    heading: str | None = None,
    _: Principal = Depends(need(CAP_RETRIEVE)),
) -> Response:
    params = {k: v for k, v in {"offset": offset, "limit": limit, "heading": heading}.items() if v is not None}
    return await _forward(_kb(doc_id), "GET", f"/v1/documents/{doc_id}/structure", "content.structure", params=params)


async def _stream(backend: Backend, path: str) -> StreamingResponse:
    require(backend, "documents.file")
    upstream = await backend.send("GET", path, stream=True, timeout=600)
    # relayed as received (no decoding), so content-length and content-encoding stay true
    passed = {"content-disposition", "content-length", "content-encoding"}
    headers = {k: v for k, v in upstream.headers.items() if k.lower() in passed}

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), media_type=upstream.headers.get("content-type"), headers=headers)


@router.get("/documents/{doc_id}/file")
async def get_file(doc_id: str, _: Principal = Depends(need(CAP_RETRIEVE))) -> StreamingResponse:
    return await _stream(_kb(doc_id), f"/v1/documents/{doc_id}/file")


@router.get("/documents/{doc_id}/assets/{name:path}")
async def get_asset(doc_id: str, name: str, _: Principal = Depends(need(CAP_RETRIEVE))) -> StreamingResponse:
    return await _stream(_kb(doc_id), f"/v1/documents/{doc_id}/assets/{name}")


@router.post("/documents/{doc_id}/reprocess")
async def reprocess_doc(doc_id: str, _: Principal = Depends(need(CAP_INGEST))) -> Response:
    return await _forward(_kb(doc_id), "POST", f"/v1/documents/{doc_id}/reprocess", "documents.reprocess")


@router.post("/documents/{doc_id}/cancel")
async def cancel_doc(doc_id: str, _: Principal = Depends(need(CAP_INGEST))) -> Response:
    return await _forward(_kb(doc_id), "POST", f"/v1/documents/{doc_id}/cancel", "documents.cancel")


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_doc(doc_id: str, _: Principal = Depends(need(CAP_INGEST))) -> Response:
    return await _forward(_kb(doc_id), "DELETE", f"/v1/documents/{doc_id}", "documents.delete")


@router.post("/documents/{doc_id}/questions/regenerate")
async def regenerate_questions(doc_id: str, _: Principal = Depends(need(CAP_INGEST))) -> Response:
    return await _forward(_kb(doc_id), "POST", f"/v1/documents/{doc_id}/questions/regenerate", "questions")


@router.put("/chunks/{chunk_id}/questions")
async def put_chunk_questions(chunk_id: str, request: Request, _: Principal = Depends(need(CAP_MANAGE))) -> Response:
    return await _forward(_kb(chunk_id), "PUT", f"/v1/chunks/{chunk_id}/questions", "questions", json=await request.json())


# ---- uploads -------------------------------------------------------------------


# A session lives on the backend that owns the knowledge base; its upload_id carries that backend's prefix.
@router.post("/uploads", status_code=201)
async def init_upload(request: Request, _: Principal = Depends(need(CAP_INGEST))) -> Response:
    body = await request.json()
    return await _forward(_kb(str(body.get("kb_id") or "")), "POST", "/v1/uploads", "uploads", json=body)


@router.put("/uploads/{upload_id}/parts/{index}")
async def put_part(upload_id: str, index: int, request: Request, _: Principal = Depends(need(CAP_INGEST))) -> Response:
    return await _forward(_kb(upload_id), "PUT", f"/v1/uploads/{upload_id}/parts/{index}", "uploads",
                          content=await request.body(), headers={"Content-Type": "application/octet-stream"}, timeout=300)


@router.post("/uploads/{upload_id}/complete", status_code=201)
async def complete_upload(upload_id: str, _: Principal = Depends(need(CAP_INGEST))) -> Response:
    return await _forward(_kb(upload_id), "POST", f"/v1/uploads/{upload_id}/complete", "uploads", timeout=600)


# ---- search -------------------------------------------------------------------


@router.post("/search")
async def search(request: Request, principal: Principal = Depends(need(CAP_RETRIEVE))) -> dict:
    body = await request.json()
    query = str(body.get("query") or "").strip()
    if not query:
        raise UpstreamError(400, "bad_request", "query 不能为空")
    kb_ids = check_scope(principal, body.get("kb_ids") or principal.kb_allowlist)
    top_k = max(1, min(int(body.get("top_k") or 8), 50))
    extra = {k: v for k, v in body.items() if k not in {"kb_ids", "top_k"}}
    hits, failed, traces = await ops.search(registry(), kb_ids, extra, top_k)
    out: dict = {"query": query, "items": hits}
    if failed:
        out["failed_backends"] = failed
    # a trace describes one backend's pipeline; several cannot be merged into one
    if len(traces) == 1:
        out["trace"] = traces[0]
    return out


@router.post("/grep")
async def grep(request: Request, principal: Principal = Depends(need(CAP_RETRIEVE))) -> dict:
    body = await request.json()
    kb_ids = check_scope(principal, body.get("kb_ids") or principal.kb_allowlist)
    return await ops.grep(registry(), kb_ids, str(body.get("pattern") or ""),
                          max_results=max(1, min(int(body.get("max_results") or 50), ops.GREP_MAX_RESULTS)),
                          max_lines_per_doc=max(1, min(int(body.get("max_lines_per_doc") or 10),
                                                   ops.GREP_MAX_LINES_PER_DOC)))


# ---- wiki ---------------------------------------------------------------------


# the contract's wiki endpoints; anything else would reach other backend paths with the gateway's credential
WIKI_ENDPOINTS = {"GET": {"status", "pages", "page", "graph"}, "PUT": {"page"}, "DELETE": {"page"},
                  "POST": {"rebuild", "finalize"}}


@router.api_route("/kbs/{kb_id}/wiki/{rest}", methods=["GET", "POST", "PUT", "DELETE"])
async def wiki(kb_id: str, rest: str, request: Request) -> Response:
    from ..deps import caller

    if rest not in WIKI_ENDPOINTS[request.method]:
        raise UpstreamError(404, "not_found", f"没有这个 Wiki 接口：{request.method} {rest}")
    write = request.method != "GET"
    caller(request, CAP_MANAGE if write else CAP_RETRIEVE)
    body = await request.body()
    return await _forward(
        _kb(kb_id), request.method, f"/v1/kbs/{kb_id}/wiki/{rest}", "wiki.write" if write else "wiki.read",
        params=dict(request.query_params), content=body or None,
        headers={"Content-Type": request.headers.get("content-type", "application/json")} if body else None,
    )
