"""JiuwenSwarm application plugin: knowledge-base management page.

The page is an iframe served from ``frontend/dist``. It calls this backend
through the host's web channel: every method in ``METHODS`` is registered as
``plugin.kb_manager.<method>`` with ``channel.register_method`` (the plugin
RPC the framework prescribes), so the host's connection checks and the
plugin's enabled state apply to it. The backend forwards each call to the
knowledge service with the plugin service key, which never reaches the browser.

What a knowledge backend declares (actions, views, settings, search options,
export formats; knowledge-provider contract section 9) reaches the page through
``backends.list``; the methods below only forward calls about those
declarations, the gateway refuses anything a backend did not declare.

Environment:
    KB_SERVICE_URL   e.g. http://kbs-api:8080
    KBS_PLUGIN_KEY   kb-service key with manage/ingest/retrieve
"""

from __future__ import annotations

import base64
import logging
import os
import re
from typing import Any
from urllib.parse import unquote

import httpx

from jiuwenswarm.extensions.sdk import ApplicationPluginExtension, FrontendContribution

logger = logging.getLogger(__name__)

PLUGIN_ID = "kb-manager"
RPC_PREFIX = "plugin.kb_manager."
# the page's whole API; anything else is not reachable from the browser
METHODS = (
    "status", "backends.list", "config.meta",
    "kbs.list", "kbs.get", "kbs.create", "kbs.update", "kbs.delete", "kbs.reprocess",
    "kbs.profile_generate", "kbs.profile_apply",
    "docs.list", "docs.get", "docs.chunks", "docs.content", "docs.file", "docs.reprocess", "docs.cancel",
    "docs.delete", "docs.text", "docs.url", "docs.questions_regenerate", "chunks.questions",
    "upload.begin", "upload.part", "upload.complete", "search",
    "wiki.status", "wiki.pages", "wiki.page", "wiki.edit", "wiki.delete", "wiki.graph", "wiki.rebuild",
    "wiki.finalize",
    # contract section 9: declared actions, jobs, views, backend settings, evidence, ranges, export, folders
    "actions.run", "jobs.get", "jobs.cancel", "jobs.list", "views.get", "settings.get", "settings.put",
    "evidence.get", "docs.file_range", "docs.export", "folders.list", "folders.delete",
)
DECLARED_METHODS = frozenset(METHODS[METHODS.index("actions.run"):])
MAX_FILE_DOWNLOAD = 30 * 1024 * 1024
MAX_RANGE = 8 * 1024 * 1024
# ids travel in URL paths: anything that could leave the segment is refused (":" stays, anchors use it)
_SEGMENT = re.compile(r"^[^/?#%\\\s]+$")
_DECLARED_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_EXPORT_FORMAT = re.compile(r"^[a-z0-9]+$")
_BACKEND_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_JOB_STATUS = frozenset({"queued", "running", "completed", "failed", "cancelled"})


class RpcError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _p(params: dict, name: str, default: Any = None, *, required: bool = False) -> Any:
    value = params.get(name, default)
    if required and (value is None or value == ""):
        raise RpcError("bad_request", f"缺少参数 {name}")
    return value


def _segment(params: dict, name: str) -> str:
    value = _p(params, name, required=True)
    if not isinstance(value, str) or not _SEGMENT.match(value) or ".." in value:
        raise RpcError("bad_request", f"参数 {name} 不合法")
    return value


def _matching(params: dict, name: str, pattern: re.Pattern[str]) -> str:
    value = _p(params, name, required=True)
    if not isinstance(value, str) or not pattern.match(value):
        raise RpcError("bad_request", f"参数 {name} 不合法")
    return value


def _count(params: dict, name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(params.get(name, default))
    except (TypeError, ValueError) as exc:
        raise RpcError("bad_request", f"参数 {name} 应为整数") from exc
    if not low <= value <= high:
        raise RpcError("bad_request", f"参数 {name} 应在 {low} 到 {high} 之间")
    return value


def _folder(params: dict, name: str) -> str:
    """A folder path without leading or trailing ``/``; empty when not given."""

    value = params.get(name) or ""
    if not isinstance(value, str) or len(value) > 1000:
        raise RpcError("bad_request", f"参数 {name} 不合法")
    path = value.strip("/")
    if path and any(part in ("", ".", "..") for part in path.split("/")):
        raise RpcError("bad_request", f"参数 {name} 不合法")
    return path


def _target(params: dict) -> str:
    """Path of the document (``doc_id``) or knowledge base (``kb_id``) an action or view applies to."""

    if params.get("doc_id"):
        return f"/v1/documents/{_segment(params, 'doc_id')}"
    return f"/v1/kbs/{_segment(params, 'kb_id')}"


def _file_name(disposition: str) -> str | None:
    match = re.search(r"filename\*\s*=\s*[\w-]+'[^']*'([^;]+)", disposition, re.I)
    if match:
        return unquote(match.group(1).strip())
    match = re.search(r'filename\s*=\s*"?([^";]+)"?', disposition, re.I)
    return match.group(1).strip() if match else None


def _content_range(value: str) -> tuple[int | None, int | None]:
    """``bytes a-b/total`` -> (a, total); ``total`` may be ``*`` (unknown)."""

    match = re.match(r"^\s*bytes\s+(\d+)-\d+/(\d+|\*)\s*$", value or "")
    if not match:
        return None, None
    return int(match.group(1)), None if match.group(2) == "*" else int(match.group(2))


async def _read_capped(resp: httpx.Response, cap: int, message: str) -> bytes:
    declared = resp.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap and "content-encoding" not in resp.headers:
        raise RpcError("too_large", message)
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf += chunk
        if len(buf) > cap:
            raise RpcError("too_large", message)
    return bytes(buf)


class KbManagerPlugin(ApplicationPluginExtension):
    plugin_id = PLUGIN_ID

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def initialize(self, config: Any) -> None:
        del config

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def frontend_contributions(self) -> tuple[FrontendContribution, ...]:
        return (
            FrontendContribution(
                id="kb-manager-page",
                nav_key=f"app:{PLUGIN_ID}",
                title="知识库",
                render_mode="iframe",
                entrypoint="index.html",
                position=20,
            ),
        )

    def bind_web_channel(self, channel: Any, services: Any) -> None:
        del services
        for name in METHODS:
            channel.register_method(f"{RPC_PREFIX}{name}", self._rpc_handler(channel, name), local_only=True)

    def _rpc_handler(self, channel: Any, name: str) -> Any:
        async def handler(ws: Any, req_id: str, params: Any, session_id: Any) -> None:
            del session_id
            try:
                result = await self._dispatch(name, params if isinstance(params, dict) else {})
            except RpcError as exc:
                await channel.send_response(ws, req_id, ok=False, payload={"status": exc.status},
                                            error=exc.message, code=exc.code)
                return
            except Exception as exc:  # noqa: BLE001 - report to the page, keep the channel alive
                logger.exception("[kb-manager] rpc %s failed", name)
                await channel.send_response(ws, req_id, ok=False, payload={"status": 500}, error=str(exc),
                                            code="internal")
                return
            # payload must be an object; lists and markdown text travel under "result"
            await channel.send_response(ws, req_id, ok=True, payload={"result": result})

        return handler

    # ---- kb-service HTTP --------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            base = os.getenv("KB_SERVICE_URL", "").rstrip("/")
            if not base:
                raise RpcError("kb_service_unconfigured", "知识服务地址 KB_SERVICE_URL 未配置", 503)
            key = os.getenv("KBS_PLUGIN_KEY", "")
            self._client = httpx.AsyncClient(
                base_url=base,
                headers={"Authorization": f"Bearer {key}"},
                timeout=httpx.Timeout(120.0, connect=10.0),
                trust_env=False,
            )
        return self._client

    @staticmethod
    def _error(resp: httpx.Response) -> RpcError:
        """The service's ``{"error": {code, message}}`` as an RpcError; the body must have been read."""

        message = resp.text[:500]
        code = "kb_service_error"
        if "application/json" in resp.headers.get("content-type", ""):
            try:
                err = resp.json().get("error") or {}
                message = err.get("message", message)
                code = err.get("code", code)
            except ValueError:
                pass
        return RpcError(code, message, resp.status_code)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = await self._http().request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RpcError("kb_service_unavailable", f"知识服务不可用：{exc}", 502) from exc
        if resp.status_code >= 400:
            raise self._error(resp)
        return resp

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = await self._request(method, path, **kwargs)
        if resp.status_code == 204:
            return None
        if "application/json" in resp.headers.get("content-type", ""):
            return resp.json()
        return resp.text

    async def _download(self, path: str, cap: int, message: str, *, headers: dict | None = None,
                        error: RpcError | None = None) -> tuple[httpx.Response, bytes]:
        """GET a file body without holding more than ``cap`` bytes; ``error`` replaces the service's own."""

        try:
            async with self._http().stream("GET", path, headers=headers) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    if error is not None:
                        error.status = resp.status_code
                        raise error
                    raise self._error(resp)
                return resp, await _read_capped(resp, cap, message)
        except httpx.HTTPError as exc:
            raise RpcError("kb_service_unavailable", f"知识服务不可用：{exc}", 502) from exc

    async def _dispatch(self, method: str, params: dict) -> Any:
        def kb() -> str:
            return _segment(params, "kb_id")

        def doc() -> str:
            return _segment(params, "doc_id")

        if method == "status":
            health = await self._call("GET", "/healthz")
            me = await self._call("GET", "/v1/me")
            return {"health": health, "me": me}
        if method == "kbs.list":
            return await self._call("GET", "/v1/kbs")
        if method == "backends.list":
            return await self._call("GET", "/v1/backends")
        if method == "kbs.get":
            return await self._call("GET", f"/v1/kbs/{kb()}")
        if method == "kbs.create":
            body = {
                k: params.get(k)
                for k in ("name", "description", "chunk_size", "chunk_overlap", "config", "backend")
                if params.get(k) not in (None, "")
            }
            return await self._call("POST", "/v1/kbs", json=body)
        if method == "kbs.update":
            body = {k: params.get(k) for k in ("name", "description", "chunk_size", "chunk_overlap", "config") if k in params}
            return await self._call("PATCH", f"/v1/kbs/{kb()}", json=body)
        if method == "kbs.reprocess":
            body = {"target": _p(params, "target", required=True), "reset_wiki": bool(params.get("reset_wiki"))}
            return await self._call("POST", f"/v1/kbs/{kb()}/reprocess", json=body)
        if method == "kbs.profile_generate":
            return await self._call("POST", f"/v1/kbs/{kb()}/profile/generate")
        if method == "kbs.profile_apply":
            return await self._call("POST", f"/v1/kbs/{kb()}/profile/apply")
        if method == "config.meta":
            query = {"backend": params["backend"]} if params.get("backend") else None
            return await self._call("GET", "/v1/config/meta", params=query)
        if method == "kbs.delete":
            return await self._call("DELETE", f"/v1/kbs/{kb()}")
        if method == "docs.list":
            query = {k: params[k] for k in ("status", "q", "limit", "offset") if params.get(k) not in (None, "")}
            if _folder(params, "folder"):
                query["folder"] = _folder(params, "folder")
            return await self._call("GET", f"/v1/kbs/{kb()}/documents", params=query)
        if method == "docs.get":
            return await self._call("GET", f"/v1/documents/{doc()}")
        if method == "docs.chunks":
            query = {"offset": int(params.get("offset") or 0), "limit": int(params.get("limit") or 20)}
            return await self._call("GET", f"/v1/documents/{doc()}/chunks", params=query)
        if method == "docs.content":
            return await self._call("GET", f"/v1/documents/{doc()}/content", params={"level": params.get("level") or "L1"})
        if method == "docs.file":
            resp, data = await self._download(
                f"/v1/documents/{doc()}/file", MAX_FILE_DOWNLOAD, "文件过大，无法在页面内下载",
                error=RpcError("file_unavailable", "原始文件不可用"),
            )
            return {
                "content_type": resp.headers.get("content-type", "application/octet-stream"),
                "data": base64.b64encode(data).decode("ascii"),
            }
        if method == "docs.questions_regenerate":
            return await self._call("POST", f"/v1/documents/{doc()}/questions/regenerate")
        if method == "chunks.questions":
            chunk = _segment(params, "chunk_id")
            body = {"questions": [str(q) for q in params.get("questions") or []]}
            return await self._call("PUT", f"/v1/chunks/{chunk}/questions", json=body)
        if method.startswith("wiki."):
            return await self._wiki(method[5:], kb(), params)
        if method in DECLARED_METHODS:
            return await self._declared(method, params)
        if method == "docs.reprocess":
            return await self._call("POST", f"/v1/documents/{doc()}/reprocess")
        if method == "docs.cancel":
            return await self._call("POST", f"/v1/documents/{doc()}/cancel")
        if method == "docs.delete":
            return await self._call("DELETE", f"/v1/documents/{doc()}")
        if method == "docs.text":
            body = {
                "title": params.get("title") or "",
                "text": _p(params, "text", required=True),
                "folder_path": params.get("folder_path") or "",
                "process_overrides": params.get("process_overrides") or None,
            }
            return await self._call("POST", f"/v1/kbs/{kb()}/documents/text", json=body)
        if method == "docs.url":
            body = {
                "url": _p(params, "url", required=True),
                "folder_path": params.get("folder_path") or "",
                "process_overrides": params.get("process_overrides") or None,
            }
            return await self._call("POST", f"/v1/kbs/{kb()}/documents/url", json=body)
        if method == "upload.begin":
            body = {
                "kb_id": kb(),
                "file_name": _p(params, "file_name", required=True),
                "file_size": int(_p(params, "file_size", required=True)),
                "folder_path": params.get("folder_path") or "",
                "process_overrides": params.get("process_overrides") or None,
            }
            if params.get("sha256"):
                body["sha256"] = params["sha256"]
            return await self._call("POST", "/v1/uploads", json=body)
        if method == "upload.part":
            upload_id = _segment(params, "upload_id")
            index = int(_p(params, "index", required=True))
            data = base64.b64decode(_p(params, "data", required=True))
            return await self._call(
                "PUT", f"/v1/uploads/{upload_id}/parts/{index}", content=data,
                headers={"Content-Type": "application/octet-stream"},
            )
        if method == "upload.complete":
            return await self._call("POST", f"/v1/uploads/{_segment(params, 'upload_id')}/complete")
        if method == "search":
            body = {
                "query": _p(params, "query", required=True),
                "kb_ids": params.get("kb_ids") or None,
                "top_k": int(params.get("top_k") or 8),
                "rerank": params.get("rerank"),
                "min_score": params.get("min_score"),
                "include_wiki": params.get("include_wiki", True),
                "options": params.get("options") or None,
                "trace": bool(params.get("trace")),
            }
            return await self._call("POST", "/v1/search", json=body)
        raise RpcError("unknown_method", f"未知方法 {method}", 404)

    async def _declared(self, method: str, params: dict) -> Any:
        """Contract section 9: what a backend declared in its capabilities (the gateway checks the declaration)."""

        if method == "actions.run":
            action = _matching(params, "action", _DECLARED_ID)
            values = params.get("params") or {}
            if not isinstance(values, dict):
                raise RpcError("bad_request", "params 必须是对象")
            return await self._call("POST", f"{_target(params)}/actions/{action}", json={"params": values},
                                    timeout=httpx.Timeout(600.0, connect=10.0))
        if method == "jobs.get":
            report = "true" if params.get("report") else "false"
            return await self._call("GET", f"/v1/jobs/{_segment(params, 'job_id')}", params={"report": report})
        if method == "jobs.cancel":
            return await self._call("POST", f"/v1/jobs/{_segment(params, 'job_id')}/cancel")
        if method == "jobs.list":
            status = params.get("status") or None
            if status is not None and status not in _JOB_STATUS:
                raise RpcError("bad_request", "参数 status 不合法")
            return await self._call("GET", f"/v1/kbs/{_segment(params, 'kb_id')}/jobs",
                                    params={"status": status} if status else None)
        if method == "views.get":
            view = _matching(params, "view", _DECLARED_ID)
            try:
                resp = await self._request("GET", f"{_target(params)}/views/{view}")
            except RpcError as exc:
                if exc.status == 404:  # not produced yet: an empty view, not an error
                    return {"media_type": None, "content": None}
                raise
            media_type = resp.headers.get("content-type", "").split(";")[0].strip()
            if media_type == "application/json":
                return {"media_type": media_type, "content": resp.json()}
            return {"media_type": media_type or "text/markdown", "content": resp.text}
        if method in ("settings.get", "settings.put"):
            query = {"backend": _matching(params, "backend", _BACKEND_ID)}
            if method == "settings.get":
                return await self._call("GET", "/v1/settings", params=query)
            values = params.get("values")
            if not isinstance(values, dict):
                raise RpcError("bad_request", "values 必须是对象")
            return await self._call("PUT", "/v1/settings", params=query, json=values)
        if method == "evidence.get":
            return await self._call("GET", f"/v1/evidence/{_segment(params, 'anchor_id')}")
        if method == "docs.file_range":
            return await self._file_range(params)
        if method == "docs.export":
            fmt = _matching(params, "format", _EXPORT_FORMAT)
            resp, data = await self._download(
                f"/v1/documents/{_segment(params, 'doc_id')}/export/{fmt}", MAX_FILE_DOWNLOAD,
                "导出的文件过大，无法在页面内下载",
            )
            return {
                "content_type": resp.headers.get("content-type", "application/octet-stream"),
                "file_name": _file_name(resp.headers.get("content-disposition", "")),
                "data": base64.b64encode(data).decode("ascii"),
            }
        if method == "folders.list":
            return await self._call("GET", f"/v1/kbs/{_segment(params, 'kb_id')}/folders")
        if method == "folders.delete":
            path = _folder(params, "path")
            if not path:
                raise RpcError("bad_request", "缺少参数 path")
            return await self._call("DELETE", f"/v1/kbs/{_segment(params, 'kb_id')}/folders", params={"path": path})
        raise RpcError("unknown_method", f"未知方法 {method}", 404)

    async def _file_range(self, params: dict) -> dict:
        """``length`` bytes of the original file from ``offset``; ``total`` is the file size when known.

        A backend without range support answers 200 with the whole file: only the asked slice is kept and the
        body is not read past it, so the page can still assemble a file part by part.
        """

        doc_id = _segment(params, "doc_id")
        offset = _count(params, "offset", 0, 0, 2**53)
        length = _count(params, "length", MAX_RANGE, 1, MAX_RANGE)
        headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
        try:
            async with self._http().stream("GET", f"/v1/documents/{doc_id}/file", headers=headers) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise self._error(resp)
                partial = resp.status_code == 206
                start, total = _content_range(resp.headers.get("content-range", "")) if partial else (None, None)
                if not partial and "content-encoding" not in resp.headers and resp.headers.get("content-length", "").isdigit():
                    total = int(resp.headers["content-length"])
                skip = 0 if partial else offset
                buf, seen, ended = bytearray(), 0, True
                async for chunk in resp.aiter_bytes():
                    if seen + len(chunk) > skip:
                        buf += chunk[max(0, skip - seen):]
                    seen += len(chunk)
                    if len(buf) >= length:
                        ended = False
                        break
                if len(buf) > length and partial:
                    raise RpcError("too_large", "分段过大")
                if not partial and total is None and ended:
                    total = seen
        except httpx.HTTPError as exc:
            raise RpcError("kb_service_unavailable", f"知识服务不可用：{exc}", 502) from exc
        return {
            "content_type": resp.headers.get("content-type", "application/octet-stream"),
            "offset": offset if start is None else start,
            "total": total,
            "data": base64.b64encode(bytes(buf[:length])).decode("ascii"),
        }

    async def _wiki(self, action: str, kb_id: str, params: dict) -> Any:
        base = f"/v1/kbs/{kb_id}/wiki"

        def slug() -> dict:
            return {"slug": _p(params, "slug", required=True)}

        if action == "status":
            return await self._call("GET", f"{base}/status")
        if action == "pages":
            query = {k: params[k] for k in ("type", "q", "limit", "offset") if params.get(k) not in (None, "")}
            return await self._call("GET", f"{base}/pages", params=query)
        if action == "page":
            return await self._call("GET", f"{base}/page", params=slug())
        if action == "edit":
            body = {k: params[k] for k in ("title", "summary", "content", "aliases") if k in params}
            return await self._call("PUT", f"{base}/page", params=slug(), json=body)
        if action == "delete":
            return await self._call("DELETE", f"{base}/page", params=slug())
        if action == "graph":
            query = {k: params[k] for k in ("center", "limit") if params.get(k) not in (None, "")}
            return await self._call("GET", f"{base}/graph", params=query)
        if action == "rebuild":
            return await self._call("POST", f"{base}/rebuild", json={"reset": bool(params.get("reset"))})
        if action == "finalize":
            return await self._call("POST", f"{base}/finalize")
        raise RpcError("unknown_method", f"未知方法 wiki.{action}", 404)


async def register_extensions(registry: Any) -> list[KbManagerPlugin]:
    plugin = KbManagerPlugin()
    registry.register_application_plugin(plugin)
    return [plugin]
