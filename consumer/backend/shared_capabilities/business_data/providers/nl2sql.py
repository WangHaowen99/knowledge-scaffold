"""NL2SQL HTTP protocol implementation."""

from __future__ import annotations

from typing import Any

import requests

from ..connection import EffectiveConnection
from ..contracts import ProviderCapabilities, ProviderResult
from ..transport.http import create_retry_session, extract_error_detail, sanitize_public_text


class NL2SQLProvider:
    capabilities = ProviderCapabilities(query=True, query_sql=True, native_streaming=False, probe=True)

    def __init__(self, connection: EffectiveConnection, session=None):
        self.connection = connection
        self._session = session or create_retry_session(int(connection.options.get("max_retries", 3)))
        self._owns_session = session is None

    def query(self, question: str) -> ProviderResult:
        question = str(question or "").strip()
        if not question:
            return ProviderResult(success=False, error="查询问题不能为空")
        body: dict[str, Any] = {"question": question}
        self._append_scene(body)
        return self._post("/api/v1/query", body)

    def query_sql(self, sql: str) -> ProviderResult:
        sql = str(sql or "").strip()
        if not sql:
            return ProviderResult(success=False, error="SQL 不能为空")
        body: dict[str, Any] = {"sql": sql}
        self._append_scene(body)
        return self._post("/api/v1/sql", body)

    def probe(self, timeout_seconds: float | None = None) -> ProviderResult:
        body: dict[str, Any] = {"sql": "SELECT 1"}
        self._append_scene(body)
        result = self._post(
            "/api/v1/sql", body, timeout_override=timeout_seconds or 10
        )
        if result.success:
            return ProviderResult(success=True, message="NL2SQL HTTP 服务连接正常")
        return result

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.connection.auth.type == "bearer" and self.connection.auth.token:
            headers["Authorization"] = "Bearer {}".format(self.connection.auth.token)
        return headers

    def _append_scene(self, body: dict[str, Any]) -> None:
        scene = str(self.connection.options.get("scene") or "").strip()
        if scene:
            body["scene"] = scene

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        timeout_override: float | None = None,
    ) -> ProviderResult:
        contract_error = self._required_contract_error()
        if contract_error:
            return ProviderResult(
                success=False,
                error=contract_error,
                metadata={"error_code": "invalid_configuration"},
            )
        try:
            response = self._session.post(
                self.connection.endpoint + path,
                headers=self._headers(),
                json=body,
                timeout=timeout_override
                or float(self.connection.options.get("timeout_seconds", 600)),
            )
        except requests.exceptions.RequestException:
            return ProviderResult(success=False, error="Connection failed", metadata={"error_code": "transport_error"})
        if response.status_code >= 400:
            return ProviderResult(
                success=False,
                error="HTTP {}: {}".format(response.status_code, self._safe_detail(response)),
                metadata={"error_code": "http_error"},
            )
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return ProviderResult(success=False, error="Invalid JSON response", metadata={"error_code": "invalid_json"})
        if not isinstance(payload, dict):
            return ProviderResult(success=False, error="Invalid response payload", metadata={"error_code": "invalid_payload"})
        success = bool(payload.get("success", False))
        row_count = int(payload.get("row_count") or 0)
        metadata = {
            key: value
            for key, value in payload.items()
            if key not in {"success", "data", "message", "error"}
        }
        if "row_count" in payload:
            metadata["row_count"] = row_count
        if success:
            message = str(payload.get("message") or ("查询成功，返回 {} 行数据".format(row_count) if row_count else "查询成功，无数据返回"))
            return ProviderResult(success=True, data=payload.get("data"), message=message, metadata=metadata)
        return ProviderResult(success=False, data=payload.get("data"), error=sanitize_public_text(
            payload.get("error") or payload.get("message") or "未知错误",
            self.connection.endpoint,
            self.connection.auth.token,
            self.connection.auth.password,
        ), metadata=metadata)

    def _required_contract_error(self) -> str:
        token = str(self.connection.auth.token or "").strip()
        if self.connection.auth.type != "bearer" or not token:
            return "访问 Token 不能为空"
        if not str(self.connection.options.get("scene") or "").strip():
            return "Scene 不能为空"
        return ""

    def _safe_detail(self, response: Any) -> str:
        return sanitize_public_text(
            extract_error_detail(response),
            self.connection.endpoint,
            self.connection.auth.token,
            self.connection.auth.password,
        )
