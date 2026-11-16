"""JiuwenSwarm application plugin: knowledge-base management page.

The page is an iframe served from ``frontend/dist``. It talks to this backend
over the plugin's own WebSocket route; the backend forwards whitelisted calls
to kb-service with the plugin service key, which never reaches the browser.

Environment:
    KB_SERVICE_URL   e.g. http://kbs-api:8080
    KBS_PLUGIN_KEY   kb-service key with manage/ingest/retrieve
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Any

import httpx

from jiuwenswarm.extensions.sdk import (
    ApplicationPluginExtension,
    FrontendContribution,
    WebSocketRouteContribution,
)

logger = logging.getLogger(__name__)

PLUGIN_ID = "kb-manager"
WS_PATH = "/ws/plugins/kb-manager"
MAX_FILE_DOWNLOAD = 30 * 1024 * 1024


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


async def handshake_denied(websocket: Any) -> bool:
    """Apply Swarm's own login check to this route; True when the connection was refused.

    Swarm checks a handshake (``WebChannel.authenticate_handshake``, set up when Swarm login is on) only for
    its main ``/ws``, not for plugin routes, so the plugin asks the same hook. Without Swarm login there is
    no hook and nothing changes; foreign Origins are refused at the edge (deploy/compose/edge/nginx.conf).
    """

    scope = getattr(websocket, "scope", None) or {}
    channel = getattr(getattr(scope.get("app"), "state", None), "web_channel", None)
    authenticate = getattr(channel, "authenticate_handshake", None)
    if authenticate is None:
        return False
    client = scope.get("client") or ("", "")
    query = scope.get("query_string", b"").decode("latin-1")
    path = scope.get("path", "") + (f"?{query}" if query else "")
    try:
        result = await authenticate(path=path, headers=websocket.headers, remote=f"{client[0]}:{client[1]}" if client[0] else "",
                                    channel="web")
        allowed = result is None or bool(getattr(result, "success", True))
    except Exception:  # noqa: BLE001 - a login check that fails refuses the connection
        logger.warning("[%s] handshake check failed", PLUGIN_ID, exc_info=True)
        allowed = False
    if not allowed:
        await websocket.close(code=1008)
    return not allowed


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

    def websocket_routes(self) -> tuple[WebSocketRouteContribution, ...]:
        return (WebSocketRouteContribution(path=WS_PATH, endpoint=self._serve),)

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

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._http().request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RpcError("kb_service_unavailable", f"知识服务不可用：{exc}", 502) from exc
        if resp.status_code == 204:
            return None
        content_type = resp.headers.get("content-type", "")
        if resp.status_code >= 400:
            message = resp.text[:500]
            code = "kb_service_error"
            if "application/json" in content_type:
                try:
                    err = resp.json().get("error") or {}
                    message = err.get("message", message)
                    code = err.get("code", code)
                except ValueError:
                    pass
            raise RpcError(code, message, resp.status_code)
        if "application/json" in content_type:
            return resp.json()
        return resp.text

    async def _dispatch(self, method: str, params: dict) -> Any:
        def kb() -> str:
            return _p(params, "kb_id", required=True)

        def doc() -> str:
            return _p(params, "doc_id", required=True)

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
            return await self._call("GET", f"/v1/kbs/{kb()}/documents", params=query)
        if method == "docs.get":
            return await self._call("GET", f"/v1/documents/{doc()}")
        if method == "docs.chunks":
            query = {"offset": int(params.get("offset") or 0), "limit": int(params.get("limit") or 20)}
            return await self._call("GET", f"/v1/documents/{doc()}/chunks", params=query)
        if method == "docs.content":
            return await self._call("GET", f"/v1/documents/{doc()}/content", params={"level": params.get("level") or "L1"})
        if method == "docs.file":
            resp = await self._http().get(f"/v1/documents/{doc()}/file")
            if resp.status_code >= 400:
                raise RpcError("file_unavailable", "原始文件不可用", resp.status_code)
            if len(resp.content) > MAX_FILE_DOWNLOAD:
                raise RpcError("too_large", "文件过大，无法在页面内下载")
            return {
                "content_type": resp.headers.get("content-type", "application/octet-stream"),
                "data": base64.b64encode(resp.content).decode("ascii"),
            }
        if method == "docs.questions_regenerate":
            return await self._call("POST", f"/v1/documents/{doc()}/questions/regenerate")
        if method == "chunks.questions":
            chunk = _p(params, "chunk_id", required=True)
            body = {"questions": [str(q) for q in params.get("questions") or []]}
            return await self._call("PUT", f"/v1/chunks/{chunk}/questions", json=body)
        if method.startswith("wiki."):
            return await self._wiki(method[5:], kb(), params)
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
            upload_id = _p(params, "upload_id", required=True)
            index = int(_p(params, "index", required=True))
            data = base64.b64decode(_p(params, "data", required=True))
            return await self._call(
                "PUT", f"/v1/uploads/{upload_id}/parts/{index}", content=data,
                headers={"Content-Type": "application/octet-stream"},
            )
        if method == "upload.complete":
            return await self._call("POST", f"/v1/uploads/{_p(params, 'upload_id', required=True)}/complete")
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

    # ---- WebSocket --------------------------------------------------------

    async def _serve(self, websocket: Any) -> None:
        if await handshake_denied(websocket):
            return
        await websocket.accept()
        send_lock = asyncio.Lock()
        tasks: set[asyncio.Task] = set()

        async def reply(payload: dict) -> None:
            async with send_lock:
                await websocket.send_text(json.dumps(payload, ensure_ascii=False, default=str))

        async def handle(req: dict) -> None:
            req_id = req.get("id")
            try:
                result = await self._dispatch(str(req.get("method", "")), req.get("params") or {})
                await reply({"id": req_id, "ok": True, "result": result})
            except RpcError as exc:
                await reply({"id": req_id, "ok": False, "error": {"code": exc.code, "message": exc.message, "status": exc.status}})
            except Exception as exc:  # noqa: BLE001 - report, keep the socket alive
                logger.exception("[kb-manager] rpc %s failed", req.get("method"))
                await reply({"id": req_id, "ok": False, "error": {"code": "internal", "message": str(exc), "status": 500}})

        try:
            while True:
                text = await websocket.receive_text()
                try:
                    req = json.loads(text)
                except ValueError:
                    continue
                task = asyncio.create_task(handle(req))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except Exception:  # noqa: BLE001 - disconnect ends the session
            pass
        finally:
            for task in tasks:
                task.cancel()


async def register_extensions(registry: Any) -> list[KbManagerPlugin]:
    plugin = KbManagerPlugin()
    registry.register_application_plugin(plugin)
    return [plugin]
