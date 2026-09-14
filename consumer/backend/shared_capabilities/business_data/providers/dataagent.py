# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""DataAgent A2A REST protocol implementation."""

from __future__ import annotations

import ast
import json
import re
import uuid

import requests

from ..connection import EffectiveConnection
from ..contracts import ProviderCapabilities, ProviderResult
from ..transport.http import create_retry_session, extract_error_detail, sanitize_public_text

# Upper bound for the free-text answer carried in ProviderResult.message.
# Without this cap, arbitrary-length remote text flows directly into the tool
# output.  8000 chars is large enough for value-domain mapping tables while
# still bounding the response.
_MAX_TEXT_ANSWER_CHARS = 8000


class DataAgentProvider:
    capabilities = ProviderCapabilities(query=True, query_sql=False, native_streaming=False, probe=True)

    def __init__(self, connection: EffectiveConnection, session=None):
        self.connection = connection
        self._session = session or create_retry_session(int(connection.options.get("max_retries", 3)))
        self._owns_session = session is None

    def query(
        self,
        question: str,
    ) -> ProviderResult:
        body = {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": str(question or "")}],
                "messageId": "m-{}".format(uuid.uuid4().hex[:12]),
                "contextId": "c-{}".format(uuid.uuid4().hex[:8]),
            },
            "configuration": {"returnImmediately": False},
        }
        headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
        if self.connection.auth.type == "bearer" and self.connection.auth.token:
            headers["Authorization"] = "Bearer {}".format(self.connection.auth.token)
        try:
            response = self._session.post(
                self.connection.endpoint + "/a2a/rest/message:send",
                headers=headers,
                json=body,
                timeout=float(self.connection.options.get("timeout_seconds", 600)),
            )
        except requests.exceptions.RequestException:
            return ProviderResult(success=False, error="Connection failed", metadata={"error_code": "transport_error"})
        except Exception as exc:
            detail = sanitize_public_text(
                "{}: {}".format(type(exc).__name__, exc),
                self.connection.endpoint,
                self.connection.auth.token,
                self.connection.auth.password,
            )
            return ProviderResult(
                success=False,
                error=detail,
                metadata={"error_code": "internal_error"},
            )
        if response.status_code >= 400:
            return ProviderResult(
                success=False,
                error="HTTP {}: {}".format(
                    response.status_code,
                    sanitize_public_text(
                        extract_error_detail(response),
                        self.connection.endpoint,
                        self.connection.auth.token,
                        self.connection.auth.password,
                    ),
                ),
                metadata={"error_code": "http_error"},
            )
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return ProviderResult(success=False, error="Invalid JSON response", metadata={"error_code": "invalid_json"})
        return _parse_response(payload)

    def probe(self, timeout_seconds: float | None = None) -> ProviderResult:
        headers = {}
        if self.connection.auth.type == "bearer" and self.connection.auth.token:
            headers["Authorization"] = "Bearer {}".format(self.connection.auth.token)
        timeout = timeout_seconds or 10
        try:
            response = self._session.get(
                self.connection.endpoint + "/.well-known/agent-card.json",
                headers=headers,
                timeout=timeout,
            )
            if response.status_code == 404:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                response = self._session.get(
                    self.connection.endpoint + "/.well-known/agent.json",
                    headers=headers,
                    timeout=timeout,
                )
        except requests.exceptions.RequestException:
            return ProviderResult(success=False, error="Connection failed")
        if response.status_code >= 400:
            return ProviderResult(
                success=False,
                error="HTTP {}: {}".format(
                    response.status_code,
                    sanitize_public_text(
                        extract_error_detail(response),
                        self.connection.endpoint,
                        self.connection.auth.token,
                        self.connection.auth.password,
                    ),
                ),
            )
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return ProviderResult(success=False, error="AgentCard 响应格式无效")
        if not isinstance(payload, dict) or not payload.get("name"):
            return ProviderResult(success=False, error="AgentCard 响应格式无效")
        return ProviderResult(success=True, message="DataAgent REST 服务连接正常")

    def close(self) -> None:
        if self._owns_session:
            self._session.close()


def _parse_response(body: dict) -> ProviderResult:
    task = body.get("task") or {}
    status = task.get("status") or {}
    state = status.get("state", "")
    text = _extract_text(status.get("message") or {})
    if state != "TASK_STATE_COMPLETED":
        reason = (
            status.get("error") or (body.get("error") or {}).get("message") or text or "task state: {}".format(state)
        )
        return ProviderResult(success=False, error=str(reason))
    fields = _extract_fields(text)
    if not fields:
        bounded_text = _bounded_text(text)
        return ProviderResult(
            success=True,
            message=bounded_text or "查询成功",
            metadata={"row_count": 0},
        )
    sql = fields.get("sql")
    columns = fields.get("columns") or []
    rows = _normalize_rows(fields.get("rows_preview") or fields.get("rows") or [])
    error = fields.get("error")
    metadata = {"row_count": len(rows)}
    if sql:
        metadata["sql"] = sql
    if fields.get("confidence") is not None:
        metadata["confidence"] = fields["confidence"]
    if error:
        return ProviderResult(success=False, error=str(error), metadata=metadata)
    if not sql:
        metadata["error_code"] = "missing_sql"
        return ProviderResult(
            success=False,
            error="DataAgent 未返回有效的结构化查询结果",
            metadata=metadata,
        )
    return ProviderResult(
        success=True,
        data={"columns": columns, "rows": rows} if columns or rows else None,
        message="查询成功，返回 {} 行数据".format(len(rows)) if rows else "查询成功，无数据返回",
        metadata=metadata,
    )


_FIELD_PATTERNS = {
    "sql": re.compile(r"'sql':\s*('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"),
    "columns": re.compile(r"'columns':\s*(\[[^\]]*\])"),
    "rows_preview": re.compile(r"'rows_preview':\s*(\[(?:[^\[\]]|\([^\(\)]*\))*\])"),
    "rows": re.compile(r"'rows':\s*(\[(?:[^\[\]]|\([^\(\)]*\))*\])"),
    "confidence": re.compile(r"'confidence':\s*([0-9]+\.?[0-9]*|None|True|False)"),
    "error": re.compile(r"'error':\s*('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|None)"),
}


def _extract_text(message: dict) -> str:
    for part in message.get("parts") or []:
        if isinstance(part, dict) and part.get("text"):
            return str(part["text"])
        if isinstance(part, str):
            return part
    return ""


def _bounded_text(text: str) -> str:
    """Truncate free-text answers to the configured character budget."""
    if not text:
        return ""
    if len(text) <= _MAX_TEXT_ANSWER_CHARS:
        return text
    return "{}...".format(text[:_MAX_TEXT_ANSWER_CHARS])


def _extract_fields(text: str) -> dict:
    """从 state 字符串里按字段名抽取值，逐字段 ast.literal_eval。"""
    out: dict = {}
    for key, pat in _FIELD_PATTERNS.items():
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1)
        val = _safe_literal_eval(raw)
        if val is None and key != "error":
            # 抽到了但解析失败：保留原始字符串便于排查
            val = raw
        out[key] = val
    return out


def _safe_literal_eval(text: str):
    """安全解析 Python 字面量；失败返回 None。"""
    if not text:
        return None
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None


def _normalize_rows(rows) -> list[list]:
    """把 tuple/行记录统一成 list[list]，便于 JSON 序列化。"""
    out: list[list] = []
    for r in rows or []:
        if isinstance(r, (list, tuple)):
            out.append([_coerce(v) for v in r])
        else:
            out.append([_coerce(r)])
    return out


def _coerce(v):
    """Decimal/datetime 等不可 JSON 序列化的值转 str。"""
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)
