"""FastAPI test helpers for migrated Consumer route contracts."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI
from starlette.requests import Request as StarletteRequest

from api.exceptions import APIError, api_error_handler
from api.middleware import install_http_middleware


class TestApplication(FastAPI):
    """Small explicit ASGI app used by focused router tests."""

    __test__ = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.state.services = {}
        install_http_middleware(self)
        self.add_exception_handler(APIError, api_error_handler)


@contextmanager
def request_scope(
    app: FastAPI,
    *,
    path: str = "/",
    method: str = "GET",
    headers: dict[str, str] | None = None,
    user=None,
):
    """Build an explicit in-process request scope for direct helper tests."""
    raw_headers = [
        (str(key).lower().encode("latin-1"), str(value).encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
        "state": {},
        "path_params": {},
    }
    request = StarletteRequest(scope)
    from api.auth.auth import current_user_scope

    with current_user_scope(user):
        yield request
