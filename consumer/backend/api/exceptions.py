"""Application HTTP errors rendered by FastAPI exception handlers."""

from __future__ import annotations

from typing import Any, Mapping

from fastapi.responses import JSONResponse


class APIError(Exception):
    """Stop request processing with an explicit JSON API response."""

    def __init__(
        self,
        status_code: int,
        content: Any,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(str(content))
        self.status_code = int(status_code)
        self.content = content
        self.headers = dict(headers or {})


async def api_error_handler(_request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        content=exc.content,
        status_code=exc.status_code,
        headers=exc.headers,
    )
