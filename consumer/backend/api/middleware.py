# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Application-wide ASGI middleware."""

from __future__ import annotations

from typing import Optional

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from integration.observability.performance import (
    get_server_timing_header,
    request_performance_context,
)
from server_config import MAX_CONTENT_LENGTH


MAX_REQUEST_BODY_BYTES = MAX_CONTENT_LENGTH


class _RequestBodyTooLarge(Exception):
    pass


def _parse_content_length(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _too_large_response() -> JSONResponse:
    return JSONResponse(
        {"detail": "Request body exceeds the configured size limit"},
        status_code=413,
    )


class RequestBodyLimitMiddleware:
    """Reject oversized bodies before a route consumes them."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = int(max_body_bytes)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _parse_content_length(Headers(scope=scope).get("content-length"))
        if declared is not None and declared > self.max_body_bytes:
            await _too_large_response()(scope, receive, send)
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            await _too_large_response()(scope, receive, send)


class ServerTimingMiddleware:
    """Expose phases completed before response start as Server-Timing.

    Streaming-body work happens after HTTP headers are committed. Those late
    phases remain observable through metrics and are intentionally not buffered
    just to delay the response start.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        with request_performance_context():
            async def timing_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    timing_value = get_server_timing_header()
                    if timing_value:
                        headers = MutableHeaders(scope=message)
                        existing = str(headers.get("server-timing") or "").strip()
                        headers["server-timing"] = (
                            "{}, {}".format(existing, timing_value)
                            if existing
                            else timing_value
                        )
                await send(message)

            await self.app(scope, receive, timing_send)


def install_http_middleware(app) -> None:
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=MAX_REQUEST_BODY_BYTES,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=r".*",
        # Authentication cookies are same-origin only. Cross-origin callers
        # may use public APIs, but cannot ask the browser to attach the refresh
        # cookie.
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            "X-Workspace-Id",
            "Idempotency-Key",
            "Last-Event-ID",
            "traceparent",
        ],
    )
    app.add_middleware(ServerTimingMiddleware)
