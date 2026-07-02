"""DataAgent A2A REST client helpers."""

from __future__ import annotations

import ast
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_DEFAULT_TIMEOUT = 300
_DEFAULT_MAX_RETRIES = 3
_PROTOCOL_VERSION = "1.0"
_REST_PATH = "/a2a/rest/message:send"


@dataclass(frozen=True)
class DataAgentConfig:
    url: str
    token: str = ""
    timeout_seconds: int = _DEFAULT_TIMEOUT
    max_retries: int = _DEFAULT_MAX_RETRIES


def call_query(
    server: str,
    token: str,
    question: str,
    timeout: int = _DEFAULT_TIMEOUT,
    *,
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> Dict[str, Any]:
    """Call DataAgent through its A2A REST message endpoint."""
    client = DataAgentClient(
        DataAgentConfig(
            url=str(server or "").strip(),
            token=str(token or "").strip(),
            timeout_seconds=int(timeout or _DEFAULT_TIMEOUT),
            max_retries=int(max_retries or _DEFAULT_MAX_RETRIES),
        )
    )
    try:
        return client.query(question)
    finally:
        client.close()


class DataAgentClient:
    """Requests-based client for DataAgent A2A REST."""

    def __init__(self, config: DataAgentConfig):
        self.config = config
        self.base_url = str(config.url or "").rstrip("/")
        self.token = str(config.token or "").strip()
        self.timeout = int(config.timeout_seconds or _DEFAULT_TIMEOUT)
        self._session = _create_session(config.max_retries)

    def query(self, question: str) -> Dict[str, Any]:
        normalized_question = str(question or "").strip()
        if not normalized_question:
            return _failure("查询问题不能为空")
        if not self.base_url:
            return _failure("DataAgent 服务地址未配置")

        response = self._post_json(
            _REST_PATH,
            {
                "message": {
                    "role": "ROLE_USER",
                    "parts": [{"text": normalized_question}],
                    "messageId": "m-{}".format(uuid.uuid4().hex[:12]),
                    "contextId": "c-{}".format(uuid.uuid4().hex[:8]),
                },
                "configuration": {"returnImmediately": False},
            },
        )
        if not response.get("ok"):
            return _failure(str(response.get("error") or "DataAgent 请求失败"), raw=response)
        return _parse_a2a_response(response.get("body") or {})

    def close(self) -> None:
        self._session.close()

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "A2A-Version": _PROTOCOL_VERSION,
        }
        if self.token:
            headers["Authorization"] = "Bearer {}".format(self.token)
        return headers

    def _post_json(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = self._session.post(
                "{}{}".format(self.base_url, path),
                headers=self._headers(),
                json=body,
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as exc:
            return {"ok": False, "error": "Connection failed: {}".format(exc)}

        if response.status_code >= 400:
            return {
                "ok": False,
                "error": "HTTP {}: {}".format(response.status_code, _extract_error_detail(response)),
            }

        try:
            payload = response.json()
        except ValueError:
            return {"ok": False, "error": "Invalid JSON response"}

        if not isinstance(payload, dict):
            return {"ok": False, "error": "Invalid response payload"}
        return {"ok": True, "body": payload}


def _create_session(retries: int = _DEFAULT_MAX_RETRIES) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=max(0, int(retries or 0)),
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _failure(error: str, *, raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "success": False,
        "data": None,
        "sql": None,
        "row_count": 0,
        "confidence": None,
        "error": str(error or "未知错误"),
        "raw": raw or {},
    }


def _parse_a2a_response(body: Dict[str, Any]) -> Dict[str, Any]:
    task = body.get("task") if isinstance(body, dict) else {}
    task = task if isinstance(task, dict) else {}
    status = task.get("status") if isinstance(task, dict) else {}
    status = status if isinstance(status, dict) else {}
    state = str(status.get("state") or "").strip()
    body_error = body.get("error")
    if isinstance(body_error, dict):
        body_error = body_error.get("message") or body_error.get("detail") or body_error
    error = status.get("error") or body_error

    if state not in {"TASK_STATE_COMPLETED", "completed"}:
        reason = error or _extract_text(status.get("message") or {}) or "task state: {}".format(state or "unknown")
        return _failure(str(reason), raw=body)

    text = _extract_text(status.get("message") or {})
    if not text:
        text = _extract_artifact_text(task)

    fields = _extract_fields(text)
    if not fields:
        return {
            "success": True,
            "data": None,
            "sql": None,
            "row_count": 0,
            "confidence": None,
            "answer": text[:500],
            "raw": body,
        }

    sql = fields.get("sql")
    columns = fields.get("columns") or []
    rows = _normalize_rows(fields.get("rows_preview") or fields.get("rows") or [])
    confidence = fields.get("confidence")
    inner_error = fields.get("error")

    return {
        "success": sql is not None and not inner_error,
        "data": {"columns": columns, "rows": rows} if columns or rows else None,
        "sql": sql,
        "row_count": len(rows),
        "confidence": confidence,
        "error": str(inner_error) if inner_error else None,
        "raw": body,
    }


_FIELD_PATTERNS = {
    "sql": re.compile(r"'sql':\s*('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"),
    "columns": re.compile(r"'columns':\s*(\[[^\]]*\])"),
    "rows_preview": re.compile(r"'rows_preview':\s*(\[(?:[^\[\]]|\([^\(\)]*\))*\])"),
    "rows": re.compile(r"'rows':\s*(\[(?:[^\[\]]|\([^\(\)]*\))*\])"),
    "confidence": re.compile(r"'confidence':\s*([0-9]+\.?[0-9]*|None|True|False)"),
    "error": re.compile(r"'error':\s*('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|None)"),
}


def _extract_fields(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, pattern in _FIELD_PATTERNS.items():
        match = pattern.search(text or "")
        if not match:
            continue
        raw = match.group(1)
        value = _safe_literal_eval(raw)
        if value is None and key != "error":
            value = raw
        out[key] = value
    return out


def _extract_text(message: Dict[str, Any]) -> str:
    parts = message.get("parts") if isinstance(message, dict) else []
    for part in parts or []:
        if isinstance(part, dict) and part.get("text"):
            return str(part["text"])
        if isinstance(part, str):
            return part
    return ""


def _extract_artifact_text(task: Dict[str, Any]) -> str:
    artifacts = task.get("artifacts") if isinstance(task, dict) else []
    for artifact in artifacts or []:
        if not isinstance(artifact, dict):
            continue
        text = _extract_text({"parts": artifact.get("parts") or []})
        if text:
            return text
    return ""


def _safe_literal_eval(text: str) -> Any:
    if not text:
        return None
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None


def _normalize_rows(rows: Any) -> list[list[Any]]:
    out: list[list[Any]] = []
    for row in rows or []:
        if isinstance(row, (list, tuple)):
            out.append([_coerce_json_value(value) for value in row])
        else:
            out.append([_coerce_json_value(row)])
    return out


def _coerce_json_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _extract_error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return str(getattr(response, "text", "") or "").strip()
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or error).strip()
        return str(payload.get("message") or payload.get("detail") or error or payload).strip()
    return str(payload).strip()


def format_output(result: Dict[str, Any], show_sql: bool = False) -> Dict[str, Any]:
    output: Dict[str, Any] = {
        "success": bool(result.get("success", False)),
        "data": result.get("data"),
    }
    if "row_count" in result:
        output["row_count"] = result["row_count"]
    if result.get("confidence") is not None:
        output["confidence"] = result["confidence"]
    if show_sql and result.get("sql"):
        output["sql"] = result["sql"]
    if not show_sql and result.get("sql"):
        output["has_sql"] = True

    if output["success"]:
        row_count = int(output.get("row_count") or 0)
        if row_count > 0:
            output["message"] = "查询成功，返回 {} 行数据".format(row_count)
        elif result.get("answer"):
            output["message"] = "查询成功：{}".format(result["answer"])
        else:
            output["message"] = "查询成功，无数据返回"
    else:
        output["message"] = "查询失败: {}".format(result.get("error") or "未知错误")
    return output
