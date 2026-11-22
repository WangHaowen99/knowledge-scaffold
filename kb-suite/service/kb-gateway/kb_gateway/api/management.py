"""Management API for the knowledge-base page: kb-service's ``/v1`` paths, routed to the owning backend."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from .. import notes, ops
from ..auth import CAP_INGEST, CAP_MANAGE, CAP_RETRIEVE, AuthError, Principal, check_scope
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


def _guarded(response: Response) -> Response:
    """A document view or list whose summary repeats instructions embedded in the document gets a warning
    (``notes.guard_summary``); backends write their own summaries and some repeat such instructions."""

    if response.status_code != 200 or not response.body:
        return response
    media = response.media_type or ""
    if "json" in media:
        try:
            data = json.loads(response.body)
        except ValueError:
            return response
        return Response(json.dumps(data, ensure_ascii=False), media_type=media) if notes.guard_summaries(data) else response
    guarded = notes.guard_summary(response.body.decode("utf-8", "replace"))
    return response if guarded is None else Response(guarded, media_type=media)


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
    folder: str | None = None,
    _: Principal = Depends(need(CAP_RETRIEVE)),
) -> Response:
    params = {k: v for k, v in {"status": status, "q": q, "limit": limit, "offset": offset, "folder": folder}.items()
              if v is not None}
    return _guarded(await _forward(_kb(kb_id), "GET", f"/v1/kbs/{kb_id}/documents", params=params))


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
    return _guarded(await _forward(_kb(doc_id), "GET", f"/v1/documents/{doc_id}"))


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
    response = await _forward(_kb(doc_id), "GET", f"/v1/documents/{doc_id}/content", feature, params={"level": level})
    return _guarded(response) if level == "L0" else response


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


# headers relayed from the backend's file response; content-length and -encoding stay true because
# the body is relayed as received (no decoding)
_FILE_HEADERS = {"content-disposition", "content-length", "content-encoding", "content-range", "accept-ranges"}


async def _stream(backend: Backend, path: str, feature: str = "documents.file", headers: dict | None = None) -> StreamingResponse:
    require(backend, feature)
    upstream = await backend.send("GET", path, stream=True, timeout=600, headers=headers)
    passed = {k: v for k, v in upstream.headers.items() if k.lower() in _FILE_HEADERS}

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, media_type=upstream.headers.get("content-type"),
                             headers=passed)


@router.get("/documents/{doc_id}/file")
async def get_file(
    doc_id: str, range_header: str | None = Header(None, alias="Range"), _: Principal = Depends(need(CAP_RETRIEVE)),
) -> StreamingResponse:
    backend = _kb(doc_id)
    # a backend without documents.file.range gets no Range and answers with the whole file (200)
    headers = {"Range": range_header} if range_header and backend.has("documents.file.range") else None
    return await _stream(backend, f"/v1/documents/{doc_id}/file", headers=headers)


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
    hits, failed, traces, warnings = await ops.search(registry(), kb_ids, extra, top_k)
    out: dict = {"query": query, "items": hits}
    if failed:
        out["failed_backends"] = failed
    if warnings:
        out["warnings"] = warnings
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


# ---- v1.1: actions, jobs, views, settings, evidence, export, folders (contract section 9) -------------

_DECLARED_ID = re.compile(r"^[a-z][a-z0-9_]*$")


def _declared(backend: Backend, key: str, entry_id: str, scope: str, feature: str) -> dict:
    """Only what the backend declared is forwarded, so no other backend path is reachable through here."""

    require(backend, feature)
    entry = backend.entry(key, entry_id, scope) if _DECLARED_ID.match(entry_id) else None
    if entry is None:
        kind = "操作" if key == "actions" else "视图"
        raise UpstreamError(501, "unsupported", f"{backend.config.label} 没有这个{kind}：{entry_id}", backend.id)
    return entry


async def _action(backend: Backend, scope: str, item_id: str, action: str, request: Request) -> Response:
    _declared(backend, "actions", action, scope, "actions")
    body = await request.body()
    params = (await request.json() if body else {}).get("params") or {}
    if not isinstance(params, dict):
        raise UpstreamError(400, "bad_request", "params 必须是对象")
    path = f"/v1/{'kbs' if scope == 'kb' else 'documents'}/{item_id}/actions/{action}"
    return _relay(await backend.send("POST", path, json={"params": params}, timeout=600))


@router.post("/kbs/{kb_id}/actions/{action}")
async def kb_action(kb_id: str, action: str, request: Request, _: Principal = Depends(need(CAP_MANAGE))) -> Response:
    return await _action(_kb(kb_id), "kb", kb_id, action, request)


@router.post("/documents/{doc_id}/actions/{action}")
async def document_action(doc_id: str, action: str, request: Request, _: Principal = Depends(need(CAP_MANAGE))) -> Response:
    return await _action(_kb(doc_id), "document", doc_id, action, request)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, report: bool = False, _: Principal = Depends(need(CAP_RETRIEVE))) -> Response:
    return await _forward(_kb(job_id), "GET", f"/v1/jobs/{job_id}", "jobs", params={"report": str(report).lower()})


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, _: Principal = Depends(need(CAP_MANAGE))) -> Response:
    return await _forward(_kb(job_id), "POST", f"/v1/jobs/{job_id}/cancel", "jobs")


@router.get("/kbs/{kb_id}/jobs")
async def list_jobs(kb_id: str, status: str | None = None, _: Principal = Depends(need(CAP_RETRIEVE))) -> Response:
    return await _forward(_kb(kb_id), "GET", f"/v1/kbs/{kb_id}/jobs", "jobs", params={"status": status} if status else None)


async def _view(backend: Backend, scope: str, item_id: str, view: str) -> Response:
    _declared(backend, "views", view, scope, "views")
    # views are written by the backend from the documents, like summaries
    return _guarded(_relay(await backend.send("GET", f"/v1/{'kbs' if scope == 'kb' else 'documents'}/{item_id}/views/{view}")))


@router.get("/kbs/{kb_id}/views/{view}")
async def kb_view(kb_id: str, view: str, _: Principal = Depends(need(CAP_RETRIEVE))) -> Response:
    return await _view(_kb(kb_id), "kb", kb_id, view)


@router.get("/documents/{doc_id}/views/{view}")
async def document_view(doc_id: str, view: str, _: Principal = Depends(need(CAP_RETRIEVE))) -> Response:
    return await _view(_kb(doc_id), "document", doc_id, view)


# backend-wide settings: same capability as creating knowledge bases, the values may name endpoints and models
@router.get("/settings")
async def get_backend_settings(backend: str | None = None, _: Principal = Depends(need(CAP_MANAGE))) -> Response:
    return await _forward(registry().get(backend), "GET", "/v1/settings", "settings")


@router.put("/settings")
async def put_backend_settings(request: Request, backend: str | None = None,
                               _: Principal = Depends(need(CAP_MANAGE))) -> Response:
    values = await request.json()
    if not isinstance(values, dict):
        raise UpstreamError(400, "bad_request", "设置必须是对象")
    return await _forward(registry().get(backend), "PUT", "/v1/settings", "settings", json=values)


@router.get("/evidence/{anchor_id}")
async def get_evidence(anchor_id: str, principal: Principal = Depends(need(CAP_RETRIEVE))) -> dict:
    # anchors are "{docId}:{type}:{index}": the document id decides the backend
    backend = _kb(anchor_id.split(":", 1)[0])
    require(backend, "evidence")
    evidence = await backend.json("GET", f"/v1/evidence/{anchor_id}")
    if not isinstance(evidence, dict):
        raise UpstreamError(502, "backend_error", f"{backend.config.label} 返回的证据格式不对", backend.id)
    # a scoped connection only reads evidence from its own knowledge bases
    if principal.kb_allowlist and evidence.get("kb_id") not in principal.kb_allowlist:
        raise AuthError(403, "knowledge base outside this connection's scope")
    return evidence


@router.get("/documents/{doc_id}/export/{fmt}")
async def export_doc(doc_id: str, fmt: str, _: Principal = Depends(need(CAP_RETRIEVE))) -> StreamingResponse:
    backend = _kb(doc_id)
    require(backend, "documents.export")
    if fmt not in backend.declared("export_formats"):
        raise UpstreamError(501, "unsupported", f"{backend.config.label} 不能导出为 {fmt}", backend.id)
    return await _stream(backend, f"/v1/documents/{doc_id}/export/{fmt}", "documents.export")


@router.get("/kbs/{kb_id}/folders")
async def list_folders(kb_id: str, _: Principal = Depends(need(CAP_RETRIEVE))) -> Response:
    return await _forward(_kb(kb_id), "GET", f"/v1/kbs/{kb_id}/folders", "folders")


@router.delete("/kbs/{kb_id}/folders", status_code=204)
async def delete_folder(kb_id: str, path: str = Query(..., min_length=1),
                        _: Principal = Depends(need(CAP_INGEST))) -> Response:
    return await _forward(_kb(kb_id), "DELETE", f"/v1/kbs/{kb_id}/folders", "folders.delete", params={"path": path})
