from __future__ import annotations

from typing import Any, Optional

import requests


class ApiError(Exception):
    def __init__(
        self, message: str, *, status: int, data: Any = None, response: Optional[requests.Response] = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.data = data
        self.response = response

    @classmethod
    def from_response(cls, response: requests.Response) -> "ApiError":
        data: Any = None
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                data = response.json()
            except ValueError:
                data = None
        else:
            try:
                data = response.text or None
            except Exception:
                data = None

        detail = None
        if isinstance(data, dict):
            for key in ("detail", "error", "message"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    detail = value.strip()
                    break
        elif isinstance(data, str) and data.strip():
            detail = data.strip()

        return cls(
            detail or "Request failed with status {}".format(response.status_code),
            status=response.status_code,
            data=data,
            response=response,
        )
