"""MindsDB SQL and native agent-stream protocol implementation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

import requests

from ..connection import EffectiveConnection
from ..contracts import ProviderCapabilities, ProviderEvent, ProviderResult
from ..transport.http import create_retry_session, extract_error_detail, sanitize_public_text
from ..transport.sse import iter_sse_data

_THOUGHT_MARKER = "```\nThought:"


class MindsDBProvider:
    capabilities = ProviderCapabilities(query=True, query_sql=True, native_streaming=True, probe=True)

    def __init__(self, connection: EffectiveConnection, session=None):
        self.connection = connection
        self._session = session or create_retry_session(int(connection.options.get("max_retries", 3)))
        self._owns_session = session is None
        self._auth_token: str = ""

    def query(self, question: str) -> ProviderResult:
        final: ProviderResult | None = None
        error: ProviderResult | None = None
        for event in self._iter_native_query_events(question):
            if event.type == "final":
                final = event.result or ProviderResult(success=True, data=event.content, message=event.content)
            elif event.type == "error":
                error = event.result or ProviderResult(success=False, error=event.content)
        return error or final or ProviderResult(
            success=True,
            data="",
            message="Query successful",
        )

    def query_sql(self, sql: str) -> ProviderResult:
        sql = str(sql or "").strip()
        if not sql:
            return ProviderResult(success=False, error="SQL 不能为空")
        payload = {
            "query": sql,
            "context": {
                "db": str(self.connection.options.get("default_database") or "mindsdb"),
                "show_secrets": False,
            },
        }
        response = self._post_with_auth_retry("/api/sql/query", payload)
        if isinstance(response, ProviderResult):
            return response
        try:
            data = response.json()
        except (ValueError, TypeError):
            return ProviderResult(success=False, error="Invalid MindsDB SQL response JSON", metadata={"error_code": "invalid_json"})
        if (
            isinstance(data, dict)
            and str(data.get("type") or "").strip().lower() == "error"
        ):
            detail = next(
                (
                    data.get(field)
                    for field in ("error_message", "error", "message")
                    if data.get(field) not in (None, "")
                ),
                "",
            )
            public_detail = sanitize_public_text(
                detail,
                self.connection.endpoint,
                self.connection.auth.token,
                self.connection.auth.password,
                self._auth_token,
            ).strip()[:200]
            return ProviderResult(
                success=False,
                error=public_detail or "MindsDB SQL query failed",
                metadata={"error_code": "query_error"},
            )
        return ProviderResult(success=True, data=data, message="查询成功")

    def _iter_native_query_events(self, question: str) -> Iterator[ProviderEvent]:
        question = str(question or "").strip()
        if not question:
            yield ProviderEvent(type="start")
            yield ProviderEvent(type="error", content="查询问题不能为空", result=ProviderResult(success=False, error="查询问题不能为空"))
            yield ProviderEvent(type="done")
            return
        project = str(self.connection.options.get("project_name") or "mindsdb")
        agent = str(self.connection.options.get("default_agent_name") or "my_agent")
        path = "/api/projects/{}/agents/{}/completions/stream".format(project, agent)
        response = self._post_with_auth_retry(path, {"messages": [{"question": question, "answer": ""}]}, stream=True)
        if isinstance(response, ProviderResult):
            yield ProviderEvent(type="start")
            yield ProviderEvent(type="error", content=response.error, result=response)
            yield ProviderEvent(type="done")
            return
        with response:
            for chunk in iter_sse_data(response.iter_lines(decode_unicode=True)):
                yield from _chunk_events(chunk)

    def probe(self, timeout_seconds: float | None = None) -> ProviderResult:
        endpoint = self.connection.endpoint.rstrip("/")
        if not endpoint:
            return _probe_failure("MindsDB URL 未配置", "missing_endpoint")

        timeout = min(
            int(
                timeout_seconds
                or self.connection.options.get("timeout_seconds")
                or 15
            ),
            15,
        )
        auth = self.connection.auth
        project_name = str(
            self.connection.options.get("project_name") or "mindsdb"
        ).strip() or "mindsdb"
        agent_name = str(
            self.connection.options.get("default_agent_name")
            or self.connection.options.get("agent_name")
            or "my_agent"
        ).strip() or "my_agent"
        headers = {"Content-Type": "application/json"}

        try:
            if auth.type == "mindsdb_password":
                if not auth.password:
                    return _probe_failure(
                        "已启用鉴权，但密码未配置",
                        "missing_password",
                    )
                login_response = self._session.post(
                    endpoint + "/api/login",
                    json={
                        "username": auth.username or "mindsdb",
                        "password": auth.password,
                    },
                    timeout=timeout,
                )
                try:
                    if login_response.status_code >= 400:
                        return self._probe_http_failure(login_response)
                    try:
                        login_payload = login_response.json()
                    except (TypeError, ValueError):
                        login_payload = None
                    if not isinstance(login_payload, dict):
                        return _probe_failure(
                            "MindsDB 登录响应格式无效",
                            "invalid_login_payload",
                        )
                    token = str(
                        login_payload.get("token")
                        or login_payload.get("access_token")
                        or ""
                    ).strip()
                    if not token:
                        return _probe_failure(
                            "MindsDB 登录返回成功，但未返回 token",
                            "missing_auth_token",
                        )
                    headers["Authorization"] = "Bearer {}".format(token)
                finally:
                    _close_response(login_response)
            elif auth.type == "bearer" and auth.token:
                headers["Authorization"] = "Bearer {}".format(auth.token)

            response = self._session.get(
                endpoint
                + "/api/projects/{}/agents".format(
                    quote(project_name, safe="")
                ),
                headers=headers,
                timeout=timeout,
            )
            try:
                if response.status_code == 401:
                    return _probe_failure(
                        "MindsDB 要求鉴权，请启用鉴权并配置用户名密码",
                        "authentication_required",
                    )
                if response.status_code == 404:
                    return _probe_failure(
                        "MindsDB Project '{}' 不存在或 Agent 列表接口不可用".format(
                            project_name
                        ),
                        "project_not_found",
                    )
                if response.status_code >= 400:
                    return self._probe_http_failure(response)
                try:
                    payload = response.json()
                except (TypeError, ValueError):
                    payload = None
                agent_names = _mindsdb_agent_names(payload)
                if agent_names is None:
                    return _probe_failure(
                        "MindsDB Agent 列表响应格式无效",
                        "invalid_agent_list",
                    )
                if agent_name not in agent_names:
                    return _probe_failure(
                        "MindsDB 连接成功，但 Project '{}' 中不存在 Agent '{}'".format(
                            project_name,
                            agent_name,
                        ),
                        "agent_not_found",
                    )
            finally:
                _close_response(response)
        except requests.exceptions.RequestException as exc:
            detail = sanitize_public_text(
                str(exc),
                endpoint,
                auth.token,
                auth.password,
            )
            return _probe_failure(
                "MindsDB 请求失败：{}".format(detail),
                "transport_error",
            )

        message = (
            "MindsDB 连接成功，鉴权与 Agent 配置可用"
            if auth.type in {"mindsdb_password", "bearer"}
            else "MindsDB 连接成功，匿名访问与 Agent 配置可用"
        )
        return ProviderResult(success=True, message=message)

    def _probe_http_failure(self, response: requests.Response) -> ProviderResult:
        detail = sanitize_public_text(
            extract_error_detail(response),
            self.connection.endpoint,
            self.connection.auth.token,
            self.connection.auth.password,
        )
        message = "MindsDB 连接失败（HTTP {}）".format(response.status_code)
        if detail:
            message = "{}：{}".format(message, detail[:200])
        return _probe_failure(message, "http_error")

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def _login(self) -> ProviderResult | None:
        auth = self.connection.auth
        if auth.type != "mindsdb_password":
            return None
        try:
            response = self._session.post(
                self.connection.endpoint + "/api/login",
                json={"username": auth.username or "mindsdb", "password": auth.password},
                timeout=10,
            )
        except requests.exceptions.RequestException:
            return ProviderResult(success=False, error="Failed to authenticate to MindsDB")
        if response.status_code != 200:
            return ProviderResult(success=False, error="Failed to authenticate to MindsDB")
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return ProviderResult(success=False, error="Invalid MindsDB authentication response")
        self._auth_token = str(payload.get("token") or payload.get("access_token") or "")
        if not self._auth_token:
            return ProviderResult(success=False, error="MindsDB authentication returned no token")
        return None

    def _headers(self) -> tuple[dict[str, str], ProviderResult | None]:
        headers = {"Content-Type": "application/json"}
        if self.connection.auth.type == "bearer" and self.connection.auth.token:
            headers["Authorization"] = "Bearer {}".format(self.connection.auth.token)
        elif self.connection.auth.type == "mindsdb_password":
            if not self._auth_token:
                failure = self._login()
                if failure:
                    return headers, failure
            headers["Authorization"] = "Bearer {}".format(self._auth_token)
        return headers, None

    def _post_with_auth_retry(
        self,
        path: str,
        payload: dict[str, Any],
        stream: bool = False,
        timeout_override: float | None = None,
    ):
        headers, failure = self._headers()
        if failure:
            return failure
        kwargs = {
            "headers": headers,
            "json": payload,
            "timeout": timeout_override
            or float(self.connection.options.get("timeout_seconds", 600)),
        }
        if stream:
            kwargs["stream"] = True
        try:
            response = self._session.post(self.connection.endpoint + path, **kwargs)
            if (
                response.status_code == 401
                and self.connection.auth.type == "mindsdb_password"
            ):
                response.close()
                self._auth_token = ""
                auth_failure = self._login()
                if auth_failure:
                    return auth_failure
                kwargs["headers"] = {"Content-Type": "application/json", "Authorization": "Bearer {}".format(self._auth_token)}
                response = self._session.post(self.connection.endpoint + path, **kwargs)
        except requests.exceptions.RequestException:
            return ProviderResult(success=False, error="MindsDB request failed", metadata={"error_code": "transport_error"})
        if response.status_code != 200:
            detail = sanitize_public_text(
                extract_error_detail(response),
                self.connection.endpoint,
                self.connection.auth.token,
                self.connection.auth.password,
            )
            response.close()
            return ProviderResult(success=False, error="HTTP {}: {}".format(response.status_code, detail), metadata={"error_code": "http_error"})
        return response


def _probe_failure(message: str, error_code: str) -> ProviderResult:
    return ProviderResult(
        success=False,
        message=message,
        error=message,
        metadata={"error_code": error_code},
    )


def _mindsdb_agent_names(payload: Any) -> list[str] | None:
    rows = payload
    if isinstance(payload, dict):
        if "agents" in payload:
            rows = payload.get("agents")
        elif "data" in payload:
            rows = payload.get("data")
        else:
            rows = None
    if not isinstance(rows, list):
        return None

    names: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized = {str(key).strip().lower(): value for key, value in row.items()}
        name = str(
            normalized.get("name") or normalized.get("agent_name") or ""
        ).strip()
        if name:
            names.append(name)
    return names


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _chunk_events(raw: Any) -> Iterator[ProviderEvent]:
    """Adapt the pre-refactor MindsDB event parser to ProviderEvent.

    The parsing rules intentionally remain identical to the former Agent/local
    client.  This shared layer only changes the event container.
    """
    for event in _parse_mindsdb_chunk_to_events(raw):
        event_type = event["type"]
        content = event["content"]
        result = None
        if event_type == "final":
            result = ProviderResult(success=True, data=content, message=content)
        yield ProviderEvent(type=event_type, content=content, result=result)


def _parse_mindsdb_chunk_to_events(raw_chunk: Any) -> list[dict[str, str]]:
    chunk = _coerce_chunk_dict(raw_chunk)
    if not chunk:
        return []

    chunk_type = str(chunk.get("type") or "").strip().lower()

    if chunk_type == "start":
        content = str(chunk.get("prompt") or "").strip() or _collect_messages_text(
            chunk.get("messages")
        )
        return [{"type": "start", "content": content or "开始执行本体探索"}]

    if chunk_type == "end":
        return [{"type": "done", "content": "查询流程结束"}]

    if chunk_type == "sql":
        return []

    if chunk_type == "context":
        context = _collect_chunk_text(chunk)
        if not context or _should_skip_context_chunk(context):
            return []
        return [{"type": "intermediate", "content": context}]

    if chunk_type == "data":
        data = _extract_final_content(_collect_chunk_text(chunk))
        if not data:
            return []
        return [{"type": "final", "content": data}]

    if "output" in chunk:
        output = str(chunk.get("output") or "").strip() or _collect_messages_text(
            chunk.get("messages")
        )
        if not output:
            return []
        return [{"type": "final", "content": _extract_final_content(output)}]

    actions = chunk.get("actions")
    if isinstance(actions, list):
        action_texts = _deduplicate_texts(_extract_action_messages(actions))
        return [
            {"type": "intermediate", "content": text} for text in action_texts
        ]

    steps = chunk.get("steps")
    if isinstance(steps, list):
        step_texts = _extract_step_messages(steps)
        return [
            {"type": "intermediate", "content": text} for text in step_texts
        ]

    message_text = _collect_chunk_text(chunk)
    if not message_text:
        return []
    return [{"type": "intermediate", "content": message_text}]


def _coerce_chunk_dict(raw_chunk: Any) -> dict[str, Any] | None:
    if isinstance(raw_chunk, dict):
        return raw_chunk

    if isinstance(raw_chunk, str):
        text = raw_chunk.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"messages": [{"content": text}]}
        if isinstance(parsed, dict):
            return parsed
        return {"messages": [{"content": text}]}

    for attr in ("model_dump", "dict", "to_dict"):
        converter = getattr(raw_chunk, attr, None)
        if callable(converter):
            converted = converter()
            if isinstance(converted, dict):
                return converted
    return None


def _extract_action_messages(actions: Any) -> list[str]:
    if not isinstance(actions, list):
        return []

    messages: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        log_text = str(action.get("log") or "").strip()
        if not log_text:
            continue
        marker_index = log_text.find(_THOUGHT_MARKER)
        if marker_index < 0:
            continue
        prefix = log_text[:marker_index].strip()
        if prefix:
            messages.append(prefix)
    return messages


def _extract_step_messages(steps: Any) -> list[str]:
    if not isinstance(steps, list):
        return []

    messages: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        action = step.get("action") if isinstance(step.get("action"), dict) else {}
        tool_name = str(action.get("tool") or "").strip()
        observation = str(step.get("observation") or "").strip()
        if not tool_name or not observation:
            continue
        cleaned_observation = observation.replace("mindsdb.", "")
        messages.append("工具调用: {}".format(tool_name))
        messages.append("结果: {}".format(cleaned_observation))
    return messages


def _collect_messages_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""

    texts: list[str] = []
    for item in messages:
        if isinstance(item, str):
            text = item.strip()
            if text:
                texts.append(text)
            continue
        if isinstance(item, dict):
            text = str(item.get("content") or "").strip()
            if text:
                texts.append(text)
    return "\n".join(texts).strip()


def _collect_chunk_text(chunk: dict[str, Any]) -> str:
    return str(chunk.get("content") or "").strip() or _collect_messages_text(
        chunk.get("messages")
    )


def _should_skip_context_chunk(content: str) -> bool:
    normalized = str(content or "").strip().lower()
    if not normalized:
        return True
    return normalized.startswith("executing final sql query:")


def _deduplicate_texts(texts: list[str]) -> list[str]:
    deduplicated: list[str] = []
    seen: set[str] = set()
    for item in texts:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduplicated.append(text)
    return deduplicated


def _extract_final_content(content: str) -> str:
    marker = "## 最终回答:"
    if marker in content:
        return content.split(marker, 1)[1].strip()
    return content.strip()
