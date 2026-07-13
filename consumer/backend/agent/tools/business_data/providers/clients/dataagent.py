"""DataAgent A2A REST client helpers."""

from __future__ import annotations

import ast
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional
from urllib.parse import quote

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
    """Call DataAgent through its A2A 1.0 REST message endpoint."""
    try:
        client = DataAgentClient(
            DataAgentConfig(
                url=str(server or "").strip(),
                token=str(token or "").strip(),
                timeout_seconds=int(_DEFAULT_TIMEOUT if timeout is None else timeout),
                max_retries=int(max_retries or 0),
            )
        )
    except (TypeError, ValueError) as exc:
        return _failure(str(exc))
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
        self.timeout = int(
            _DEFAULT_TIMEOUT if config.timeout_seconds is None else config.timeout_seconds
        )
        if self.timeout <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if int(config.max_retries) < 0:
            raise ValueError("max_retries 不能小于 0")
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
            return _failure(
                _redact_secrets(
                    str(response.get("error") or "DataAgent 请求失败"),
                    (self.token,),
                ),
                raw=_redact_secret_values(response, (self.token,)),
            )

        result = _parse_a2a_response(response.get("body") or {})
        return _redact_secret_values(result, (self.token,))

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
            detail = _redact_secrets(str(exc), (self.token,))
            return {"ok": False, "error": "Connection failed: {}".format(detail)}

        if response.status_code >= 400:
            detail = _extract_error_detail(response)
            return {
                "ok": False,
                "error": _redact_secrets(
                    "HTTP {}: {}".format(response.status_code, detail),
                    (self.token,),
                ),
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
        allowed_methods=frozenset({"POST"}),
        raise_on_status=False,
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
    """Normalize completed A2A tasks into one business-data result shape."""
    if not isinstance(body, dict):
        return _failure("Invalid response payload")

    task = body.get("task")
    task = task if isinstance(task, dict) else {}
    status = task.get("status")
    status = status if isinstance(status, dict) else {}
    state = str(status.get("state") or "").strip().upper()
    body_error = body.get("error")
    if isinstance(body_error, dict):
        body_error = body_error.get("message") or body_error.get("detail") or body_error
    error = status.get("error") or body_error

    if state not in {"TASK_STATE_COMPLETED", "COMPLETED"}:
        reason = error or _extract_text(status.get("message") or {}) or "task state: {}".format(
            state or "unknown"
        )
        return _failure(str(reason), raw=body)

    payload, text = _extract_completed_payload(task, status)
    if payload is not None:
        normalized = _normalize_structured_payload(payload, raw=body)
        if normalized is not None:
            return normalized

    fields = _extract_fields(text)
    if fields:
        normalized = _normalize_structured_payload(fields, raw=body)
        if normalized is not None:
            return normalized

    return {
        "success": True,
        "data": None,
        "sql": None,
        "row_count": 0,
        "confidence": None,
        "answer": text[:500],
        "raw": body,
    }


def _extract_completed_payload(
    task: Dict[str, Any],
    status: Dict[str, Any],
) -> tuple[Optional[Dict[str, Any]], str]:
    text = _extract_text(status.get("message") or {})
    payload = _extract_data_payload(status.get("message") or {})

    for artifact in task.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        if payload is None:
            payload = _extract_data_payload({"parts": artifact.get("parts") or []})
        if not text:
            text = _extract_text({"parts": artifact.get("parts") or []})
        if payload is not None and text:
            break

    if payload is None and text:
        payload = _decode_mapping(text)
    return payload, text


def _extract_data_payload(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    parts = message.get("parts") if isinstance(message, dict) else []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        for key in ("data", "json"):
            value = part.get(key)
            if isinstance(value, dict):
                return value
        text_value = part.get("text")
        if isinstance(text_value, dict):
            return text_value
    return None


def _decode_mapping(text: str) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = _safe_literal_eval(text)
    return payload if isinstance(payload, dict) else None


def _normalize_structured_payload(
    payload: Mapping[str, Any],
    *,
    raw: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    nested = payload.get("result") or payload.get("output")
    if isinstance(nested, dict):
        payload = nested

    sql = payload.get("sql")
    confidence = payload.get("confidence")
    explicit_success = payload.get("success")
    answer = payload.get("answer") or payload.get("message")
    if answer is None and isinstance(payload.get("output"), str):
        answer = payload["output"]
    inner_error = payload.get("error")
    if inner_error is None and explicit_success is False:
        inner_error = answer
    columns = payload.get("columns") or []
    rows = payload.get("rows_preview")
    if rows is None:
        rows = payload.get("rows")

    data = payload.get("data")
    if isinstance(data, dict):
        columns = columns or data.get("columns") or []
        if rows is None:
            rows = data.get("rows")
    elif isinstance(data, list) and rows is None:
        rows = data

    normalized_columns, normalized_rows = _normalize_tabular_data(columns, rows)
    normalized_data = None
    if normalized_columns or normalized_rows:
        normalized_data = {
            "columns": normalized_columns,
            "rows": normalized_rows,
        }

    has_structured_result = any(
        key in payload
        for key in (
            "sql",
            "columns",
            "rows_preview",
            "rows",
            "data",
            "confidence",
            "error",
            "success",
        )
    )
    if not has_structured_result:
        if answer is None:
            return None
        return {
            "success": True,
            "data": None,
            "sql": None,
            "row_count": 0,
            "confidence": confidence,
            "answer": str(answer)[:500],
            "raw": raw,
        }

    if explicit_success is not None:
        success = _coerce_success(explicit_success)
    else:
        has_result_value = any(
            key in payload for key in ("sql", "columns", "rows_preview", "rows", "data")
        ) or answer is not None
        success = not bool(inner_error) and has_result_value

    result = {
        "success": success,
        "data": normalized_data,
        "sql": str(sql) if sql is not None else None,
        "row_count": len(normalized_rows),
        "confidence": confidence,
        "error": str(inner_error) if inner_error else None,
        "raw": raw,
    }
    if answer and not normalized_data:
        result["answer"] = str(answer)[:500]
    return result


_FIELD_PATTERNS = {
    "sql": re.compile(r"['\"]sql['\"]:\s*('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"),
    "columns": re.compile(r"['\"]columns['\"]:\s*(\[[^\]]*\])"),
    "rows_preview": re.compile(
        r"['\"]rows_preview['\"]:\s*(\[(?:[^\[\]]|\([^\(\)]*\))*\])"
    ),
    "rows": re.compile(r"['\"]rows['\"]:\s*(\[(?:[^\[\]]|\([^\(\)]*\))*\])"),
    "confidence": re.compile(
        r"['\"]confidence['\"]:\s*([0-9]+\.?[0-9]*|None|null|True|False|true|false)"
    ),
    "error": re.compile(
        r"['\"]error['\"]:\s*('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|None|null)"
    ),
}


def _extract_fields(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, pattern in _FIELD_PATTERNS.items():
        match = pattern.search(text or "")
        if not match:
            continue
        raw = match.group(1)
        value = _safe_literal_eval(raw)
        if value is None and raw == "null":
            value = None
        elif value is None and raw == "true":
            value = True
        elif value is None and raw == "false":
            value = False
        elif value is None and key != "error":
            value = raw
        out[key] = value
    return out


def _extract_text(message: Dict[str, Any]) -> str:
    parts = message.get("parts") if isinstance(message, dict) else []
    for part in parts or []:
        if isinstance(part, dict) and part.get("text") is not None:
            text = part["text"]
            if isinstance(text, str):
                return text
            if isinstance(text, (dict, list)):
                return json.dumps(text, ensure_ascii=False, default=str)
            return str(text)
        if isinstance(part, str):
            return part
    return ""


def _safe_literal_eval(text: str) -> Any:
    if not text:
        return None
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None


def _normalize_tabular_data(columns: Any, rows: Any) -> tuple[list[str], list[list[Any]]]:
    normalized_columns = [str(column) for column in columns] if isinstance(columns, (list, tuple)) else []
    row_items = list(rows) if isinstance(rows, (list, tuple)) else ([] if rows is None else [rows])

    if not normalized_columns:
        for row in row_items:
            if isinstance(row, Mapping):
                for key in row:
                    column = str(key)
                    if column not in normalized_columns:
                        normalized_columns.append(column)

    normalized_rows: list[list[Any]] = []
    for row in row_items:
        if isinstance(row, Mapping):
            string_keyed_row = {str(key): value for key, value in row.items()}
            if not normalized_columns:
                normalized_columns = list(string_keyed_row)
            normalized_rows.append(
                [_coerce_json_value(string_keyed_row.get(column)) for column in normalized_columns]
            )
        elif isinstance(row, (list, tuple)):
            normalized_rows.append([_coerce_json_value(value) for value in row])
        else:
            normalized_rows.append([_coerce_json_value(row)])
    return normalized_columns, normalized_rows


def _normalize_rows(rows: Any) -> list[list[Any]]:
    return _normalize_tabular_data([], rows)[1]


def _coerce_json_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _coerce_success(value: Any) -> bool:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"false", "0", "no", "off", ""}:
            return False
        if normalized in {"true", "1", "yes", "on"}:
            return True
    return bool(value)


def _extract_error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return str(getattr(response, "text", "") or "").strip() or "Unknown error"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or error).strip()
        return str(payload.get("message") or payload.get("detail") or error or payload).strip()
    return str(payload).strip()


def _redact_secrets(value: str, secrets: Iterable[str]) -> str:
    redacted = str(value or "")
    for secret in secrets:
        normalized = str(secret or "")
        if not normalized:
            continue
        candidates = {normalized, quote(normalized, safe="")}
        for candidate in candidates:
            redacted = redacted.replace(candidate, "[REDACTED]")
    return redacted


def _redact_secret_values(value: Any, secrets: Iterable[str]) -> Any:
    secret_values = tuple(secrets)
    if isinstance(value, dict):
        return {
            _redact_secrets(key, secret_values) if isinstance(key, str) else key: _redact_secret_values(
                item,
                secret_values,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secret_values(item, secret_values) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_secret_values(item, secret_values) for item in value)
    if isinstance(value, str):
        return _redact_secrets(value, secret_values)
    return value


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
