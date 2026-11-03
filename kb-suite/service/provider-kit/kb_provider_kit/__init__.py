"""Shared pieces of knowledge-provider/v1 adapters (contracts/knowledge-provider/v1).

An adapter puts its configured id prefix on every id it returns and strips it
from every id it receives, so the gateway can route by prefix. Only the gateway
calls an adapter, with one bearer key.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

CONTRACT = "knowledge-provider/v1"
# contract ids carry no ':', ',', '/' or whitespace; '?', '#', '%' and '\' would change an upstream URL
_ID_FORBIDDEN = re.compile(r"[:,/?#%\\\s]")


class ProviderError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def not_found(what: str) -> ProviderError:
    return ProviderError(404, "not_found", f"{what}不存在")


class Ids:
    """Adds and strips the adapter's id prefix."""

    def __init__(self, prefix: str) -> None:
        if not prefix or any(c in prefix for c in ":,/ "):
            raise ValueError(f"id prefix {prefix!r} must be non-empty and free of ':', ',', '/' and spaces")
        self.prefix = prefix

    def out(self, raw: object) -> str | None:
        if raw in (None, ""):
            return None
        return f"{self.prefix}{raw}"

    def inp(self, item_id: str, what: str = "对象") -> str:
        """The backend's own id; anything this adapter could not have issued is not found."""

        raw = item_id[len(self.prefix):] if item_id.startswith(self.prefix) else ""
        if raw in ("", ".", "..") or _ID_FORBIDDEN.search(raw):
            raise not_found(what)
        return raw


def segment(value: object, what: str = "对象") -> str:
    """One upstream URL path segment: percent-encoded, never empty or a dot segment.

    Upstream paths are built by interpolation and httpx resolves ``..``, so every
    value that is not a literal of the adapter goes through here.
    """

    text = str(value or "")
    if text in ("", ".", ".."):
        raise not_found(what)
    return quote(text, safe="")


def segments(path: str, what: str = "对象") -> str:
    """A '/'-separated upstream sub-path (e.g. a WeKnora wiki slug), each segment as in ``segment``."""

    return "/".join(segment(part, what) for part in path.split("/"))


def check_bearer(request: Request, expected: str) -> None:
    if not expected:
        raise ProviderError(500, "misconfigured", "适配器未配置访问密钥")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(hashlib.sha256(token.encode()).digest(), hashlib.sha256(expected.encode()).digest()):
        raise ProviderError(401, "auth", "invalid key")


def install_error_handler(app: FastAPI) -> None:
    @app.exception_handler(ProviderError)
    async def _provider_error(_: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)


def page_span(offset: int, limit: int, page_size: int) -> list[tuple[int, int, int]]:
    """Map an offset/limit window onto fixed-size pages: [(page, start_in_page, end_in_page)], pages from 1."""

    out = []
    pos, end = offset, offset + limit
    while pos < end:
        page = pos // page_size
        start = pos - page * page_size
        stop = min(page_size, end - page * page_size)
        out.append((page + 1, start, stop))
        pos = (page + 1) * page_size
    return out
