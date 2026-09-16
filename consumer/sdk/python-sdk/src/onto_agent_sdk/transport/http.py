# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from typing import Any, Mapping, Optional
from urllib.parse import quote, urljoin

import requests

from ..errors import ApiError
from ..types import JsonDict


class HttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: Optional[str] = None,
        workspace_id: Optional[str] = None,
        timeout: Optional[float] = 60.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        base_url = base_url.strip()
        if not base_url:
            raise ValueError("base_url is required")

        self.base_url = base_url if base_url.endswith("/") else "{}/".format(base_url)
        self._token = token
        self._workspace_id = workspace_id
        self.timeout = timeout
        self.session = session or requests.Session()

    def set_token(self, token: Optional[str]) -> None:
        self._token = token

    def set_workspace(self, workspace_id: Optional[str]) -> None:
        self._workspace_id = workspace_id

    def close(self) -> None:
        self.session.close()

    def _build_url(self, path: str) -> str:
        normalized_path = path[1:] if path.startswith("/") else path
        return urljoin(self.base_url, normalized_path)

    def _build_headers(
        self,
        headers: Optional[Mapping[str, str]] = None,
        *,
        has_json_body: bool = False,
    ) -> dict[str, str]:
        request_headers = dict(headers or {})
        if self._token:
            request_headers.setdefault("Authorization", "Bearer {}".format(self._token))
        if self._workspace_id:
            request_headers.setdefault("X-Workspace-Id", quote(self._workspace_id, safe=""))
        if has_json_body:
            request_headers.setdefault("Content-Type", "application/json")
        return request_headers

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        files: Optional[Any] = None,
        stream: bool = False,
        timeout: Optional[float] = None,
    ) -> Any:
        response = self.session.request(
            method=method,
            url=self._build_url(path),
            params={key: value for key, value in (params or {}).items() if value is not None},
            headers=self._build_headers(headers, has_json_body=json is not None and files is None),
            json=json,
            data=data,
            files=files,
            timeout=self.timeout if timeout is None else timeout,
            stream=stream,
        )

        if not response.ok:
            error = ApiError.from_response(response)
            response.close()
            raise error

        if stream:
            return response

        try:
            if response.status_code == 204:
                return None
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                return response.json()
            return response.text
        finally:
            response.close()

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self.request("PATCH", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)
