"""Registered backends: capabilities, health, id routing and upstream calls."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import BackendConfig, get_settings

logger = logging.getLogger(__name__)

CONTRACT = "knowledge-provider/v1"
WRITE_FEATURES = frozenset({
    "kbs.write", "kbs.reprocess", "ingest.file", "ingest.text", "ingest.url", "documents.delete",
    "documents.reprocess", "documents.cancel", "wiki.write", "questions", "profile", "uploads",
})


class UpstreamError(Exception):
    """A backend answered with an error or could not be reached."""

    def __init__(self, status: int, code: str, message: str, backend: str = "",
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.backend = backend
        # other scalar fields of the backend's error object (e.g. existingDocId of a duplicate upload)
        self.details = details or {}


@dataclass
class Backend:
    config: BackendConfig
    client: httpx.AsyncClient
    capabilities: dict[str, Any] = field(default_factory=dict)
    healthy: bool = False
    last_error: str | None = None
    answered: bool = False
    # names of its knowledge bases as last listed: lets a tool result name them while the backend is down
    kb_names: dict[str, str] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def type(self) -> str:
        return str((self.capabilities.get("provider") or {}).get("type") or "unknown")

    @property
    def features(self) -> frozenset[str]:
        declared = frozenset(self.capabilities.get("features") or ())
        return declared - WRITE_FEATURES if self.config.readonly else declared

    @property
    def visible(self) -> bool:
        """An optional backend that never answered is simply not deployed: not listed, not reported unavailable."""

        return self.answered or not self.config.optional

    def has(self, feature: str) -> bool:
        return feature in self.features

    def owns(self, item_id: str) -> bool:
        return bool(self.config.id_prefix) and item_id.startswith(self.config.id_prefix)

    def summary(self) -> dict:
        return {
            "id": self.id,
            "label": self.config.label,
            "type": self.type,
            "default": self.config.default,
            "readonly": self.config.readonly,
            "healthy": self.healthy,
            "error": self.last_error,
            "features": sorted(self.features),
            "file_types": self.capabilities.get("file_types") or [],
            "limits": self.capabilities.get("limits") or {},
            "config_schema": self.capabilities.get("config_schema"),
        }

    def ref(self) -> dict:
        return {"id": self.id, "label": self.config.label, "type": self.type}

    def _headers(self, tools: bool, extra: dict | None) -> dict:
        headers = {"X-Tenant-Id": get_settings().tenant}
        credential = self.config.credential(tools=tools)
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        if extra:
            headers.update(extra)
        return headers

    async def send(
        self,
        method: str,
        path: str,
        *,
        tools: bool = False,
        headers: dict | None = None,
        timeout: float | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        _check_path(path)
        request = self.client.build_request(
            method, path, headers=self._headers(tools, headers),
            timeout=timeout or get_settings().backend_timeout_s, **kwargs,
        )
        try:
            response = await self.client.send(request, stream=stream)
        except httpx.HTTPError as exc:
            raise UpstreamError(502, "backend_unavailable", f"{self.config.label} 不可用：{exc}", self.id) from exc
        if response.status_code >= 400:
            if stream:
                await response.aread()
                await response.aclose()
            raise _upstream_error(self, response)
        return response

    async def json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.send(method, path, **kwargs)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def text(self, method: str, path: str, **kwargs: Any) -> str:
        response = await self.send(method, path, **kwargs)
        return response.text

    async def refresh(self) -> None:
        try:
            caps = await self.json("GET", "/v1/capabilities", timeout=10)
        except UpstreamError as exc:
            self.healthy, self.last_error = False, exc.message
            return
        except ValueError as exc:
            # e.g. a base_url that points at an HTML page: not a backend, but not fatal either
            self.healthy, self.last_error = False, f"capabilities 不是 JSON：{exc}"
            return
        self.answered = True
        problem = _check_capabilities(caps)
        if problem:
            self.healthy, self.last_error = False, problem
            logger.error("backend %s rejected: %s", self.id, problem)
            return
        self.capabilities, self.healthy, self.last_error = caps, True, None


def _check_path(path: str) -> None:
    """Paths are built from caller-supplied ids; query strings go in ``params``, never in ``path``.

    Dot segments or URL syntax in an id would reach other backend endpoints with the gateway's credential.
    """

    if any(c in path for c in "?#%\\") or any(seg in {"", ".", ".."} for seg in path.split("/")[1:]):
        raise UpstreamError(400, "bad_request", "ID 含有非法字符")


def _check_capabilities(caps: Any) -> str | None:
    if not isinstance(caps, dict) or caps.get("contract") != CONTRACT:
        return f"不是 {CONTRACT} 后端"
    if caps.get("role", "base") != "base":
        return "增强服务（role=enhancement）在 v1 中只预留了设计，gateway 暂不接入"
    return None


ERROR_DETAIL_KEYS = 10
_RESERVED_ERROR_KEYS = ("code", "message", "backend")


def _passed_detail(key: Any, value: Any) -> bool:
    """A field of a backend error object that reaches the caller: a scalar under a key of its own."""

    scalar = value is None or isinstance(value, (str, int, float, bool))
    return isinstance(key, str) and key not in _RESERVED_ERROR_KEYS and scalar


def _upstream_error(backend: Backend, response: httpx.Response) -> UpstreamError:
    code, message, details = "backend_error", response.text[:500], {}
    try:
        err = response.json().get("error") or {}
        if isinstance(err, dict):
            code = err.get("code") or code
            message = err.get("message") or message
            details = {k: v for k, v in err.items() if _passed_detail(k, v)}
            details = dict(list(details.items())[:ERROR_DETAIL_KEYS])
        elif isinstance(err, str):
            message = err
    except (ValueError, AttributeError):
        pass
    return UpstreamError(response.status_code, code, message, backend.id, details)


class Registry:
    def __init__(self, backends: list[Backend]) -> None:
        self.backends = backends
        self.by_id = {b.id: b for b in backends}
        # where new knowledge bases go
        self.default = next(b for b in backends if b.config.default)
        # longest prefix first so "da2_" is not taken for "da_"
        self._prefixed = sorted((b for b in backends if b.config.id_prefix), key=lambda b: -len(b.config.id_prefix))
        # owner of ids without a known prefix; with none configured the default answers (its adapter says 404)
        self._unprefixed = next((b for b in backends if not b.config.id_prefix), self.default)
        self._task: asyncio.Task | None = None

    @classmethod
    def from_configs(cls, configs: list[BackendConfig], transport: httpx.AsyncBaseTransport | None = None) -> "Registry":
        backends = [
            Backend(c, httpx.AsyncClient(base_url=c.base_url, transport=transport, trust_env=False))
            for c in configs
        ]
        return cls(backends)

    def route(self, item_id: str) -> Backend:
        for backend in self._prefixed:
            if backend.owns(item_id):
                return backend
        return self._unprefixed

    def get(self, backend_id: str | None) -> Backend:
        if not backend_id:
            return self.default
        backend = self.by_id.get(backend_id)
        if backend is None:
            raise UpstreamError(404, "not_found", f"没有这个知识后端：{backend_id}")
        return backend

    def group(self, item_ids: list[str]) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for item_id in item_ids:
            groups.setdefault(self.route(item_id).id, []).append(item_id)
        return groups

    def healthy(self) -> list[Backend]:
        return [b for b in self.backends if b.healthy]

    async def refresh(self) -> None:
        await asyncio.gather(*(b.refresh() for b in self.backends))

    async def _loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.refresh()
            except Exception:  # the loop is the only thing that ever marks a backend healthy again
                logger.exception("backend refresh failed")

    def start(self, interval: float) -> None:
        self._task = asyncio.create_task(self._loop(interval), name="kb-gateway-refresh")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        await asyncio.gather(*(b.client.aclose() for b in self.backends), return_exceptions=True)


def require(backend: Backend, feature: str | None = None) -> None:
    """``feature=None`` checks only health: core operations every backend implements."""

    if not backend.healthy:
        raise UpstreamError(503, "backend_unavailable", f"{backend.config.label} 当前不可用：{backend.last_error}", backend.id)
    if feature and not backend.has(feature):
        raise UpstreamError(501, "unsupported", f"{backend.config.label} 不支持此操作（{feature}）", backend.id)
