from __future__ import annotations

import json  # noqa: F401

import requests  # noqa: F401

from shared_capabilities.business_data import (
    AuthSpec,
    EffectiveConnection,
    ProviderResult,
    create_provider_client,
    iter_query_events,
)
from shared_capabilities.business_data.providers.dataagent import (
    DataAgentProvider,
    _extract_fields as extract_dataagent_fields,
)
from shared_capabilities.business_data.providers.mindsdb import (
    MindsDBProvider,
    _chunk_events,
)
from shared_capabilities.business_data.providers.nl2sql import NL2SQLProvider


class _Response:
    def __init__(self, payload, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class _Session:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return self.response

    def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return self.response

    def close(self):
        self.closed = True


class _SequenceSession(_Session):
    def __init__(self, responses):
        super().__init__(None)
        self.responses = list(responses)

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return self.responses.pop(0)


def _stream_response(*frames):
    response = _Response({})
    lines = []
    for frame in frames:
        lines.extend(["data: {}".format(json.dumps(frame)), ""])

    def iter_lines(decode_unicode=True):
        del decode_unicode
        return iter(lines)

    response.iter_lines = iter_lines
    return response


def _connection(provider, *, auth=None, options=None):
    return EffectiveConnection(
        version=1,
        provider=provider,
        endpoint="https://provider.example.com",
        auth=auth or AuthSpec(type="none"),
        options=options or {},
    )


def _nl2sql_connection(*, auth=None, options=None):
    return _connection(
        "nl2sql_http",
        auth=auth or AuthSpec(type="bearer", token="token-x"),
        options={"scene": "scene-x", **dict(options or {})},
    )


def test_nl2sql_query_uses_explicit_connection_and_normalizes_result():
    session = _Session(
        _Response(
            {
                "success": True,
                "data": [{"id": 1}],
                "row_count": 1,
                "sql": "SELECT 1",
            }
        )
    )
    provider = NL2SQLProvider(
        _connection(
            "nl2sql_http",
            auth=AuthSpec(type="bearer", token="token-x"),
            options={"scene": "scene-x", "timeout_seconds": 88},
        ),
        session=session,
    )

    result = provider.query("find one")

    assert session.requests == [
        (
            "POST",
            "https://provider.example.com/api/v1/query",
            {
                "headers": {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer token-x",
                },
                "json": {"question": "find one", "scene": "scene-x"},
                "timeout": 88,
            },
        )
    ]
    assert result == ProviderResult(
        success=True,
        data=[{"id": 1}],
        message="查询成功，返回 1 行数据",
        metadata={"sql": "SELECT 1", "row_count": 1},
    )


def test_nl2sql_invalid_json_is_a_failed_result_and_external_session_is_not_closed():
    session = _Session(_Response(ValueError("secret-token must not leak")))
    provider = NL2SQLProvider(_nl2sql_connection(), session=session)
    result = provider.query("find one")
    provider.close()
    assert result.success is False
    assert result.metadata["error_code"] == "invalid_json"
    assert "secret-token" not in result.error
    assert session.closed is False


def test_nl2sql_preserves_all_non_contract_response_fields_in_metadata():
    provider = NL2SQLProvider(
        _nl2sql_connection(),
        session=_Session(
            _Response(
                {
                    "success": True,
                    "data": [[1, "A"]],
                    "columns": ["id", "name"],
                    "row_count": "1",
                    "sql": "SELECT id, name FROM parts",
                    "trace_id": "trace-123",
                }
            )
        ),
    )

    result = provider.query("find parts")

    assert result.success is True
    assert result.data == [[1, "A"]]
    assert result.metadata == {
        "columns": ["id", "name"],
        "row_count": 1,
        "sql": "SELECT id, name FROM parts",
        "trace_id": "trace-123",
    }


def test_nl2sql_http_error_redacts_endpoint_and_token():
    provider = NL2SQLProvider(
        _nl2sql_connection(),
        session=_Session(
            _Response(
                {"detail": "token-x rejected by https://provider.example.com"},
                status_code=403,
            )
        ),
    )

    result = provider.query("find one")

    assert result.success is False
    assert "token-x" not in result.error
    assert "https://provider.example.com" not in result.error
    assert "[REDACTED]" in result.error


def test_nl2sql_rejects_missing_required_contract_before_network():
    cases = [
        (
            _connection(
                "nl2sql_http",
                auth=AuthSpec(type="none"),
                options={"scene": "scene-x"},
            ),
            "访问 Token 不能为空",
        ),
        (
            _connection(
                "nl2sql_http",
                auth=AuthSpec(type="bearer", token="token-x"),
            ),
            "Scene 不能为空",
        ),
    ]

    for connection, expected_error in cases:
        session = _Session(_Response({"success": True}))
        result = NL2SQLProvider(connection, session=session).query("find one")

        assert result == ProviderResult(
            success=False,
            error=expected_error,
            metadata={"error_code": "invalid_configuration"},
        )
        assert session.requests == []


def test_nl2sql_probe_executes_select_one_through_sql_endpoint():
    session = _Session(_Response({"success": True}))
    provider = NL2SQLProvider(
        _nl2sql_connection(options={"timeout_seconds": 88}),
        session=session,
    )

    result = provider.probe(timeout_seconds=10)

    assert result == ProviderResult(
        success=True,
        message="NL2SQL HTTP 服务连接正常",
    )
    assert session.requests == [
        (
            "POST",
            "https://provider.example.com/api/v1/sql",
            {
                "headers": {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer token-x",
                },
                "json": {"sql": "SELECT 1", "scene": "scene-x"},
                "timeout": 10,
            },
        )
    ]


def test_nl2sql_probe_requires_explicit_success_true():
    session = _Session(_Response({"data": [{"value": 1}]}))
    result = NL2SQLProvider(
        _nl2sql_connection(),
        session=session,
    ).probe()

    assert result.success is False
    assert result.error == "未知错误"


def test_mindsdb_sql_authenticates_and_normalizes_result():
    class _MindsSession(_Session):
        def post(self, url, **kwargs):
            self.requests.append(("POST", url, kwargs))
            if url.endswith("/api/login"):
                return _Response({"token": "jwt-x"})
            return _Response({"type": "table", "data": [{"id": 1}]})

    session = _MindsSession(None)
    provider = MindsDBProvider(
        _connection(
            "mindsdb",
            auth=AuthSpec(
                type="mindsdb_password", username="mindsdb", password="password-x"
            ),
            options={"default_database": "analytics", "timeout_seconds": 120},
        ),
        session=session,
    )

    result = provider.query_sql("SELECT 1")

    assert result.success is True
    assert result.data == {"type": "table", "data": [{"id": 1}]}
    assert session.requests[1][2]["headers"]["Authorization"] == "Bearer jwt-x"
    assert session.requests[1][2]["json"]["context"]["db"] == "analytics"


def test_mindsdb_sql_normalizes_http_200_error_payload():
    provider = MindsDBProvider(
        _connection("mindsdb"),
        session=_Session(
            _Response({"type": " ERROR ", "error_message": "权限拒绝"})
        ),
    )

    result = provider.query_sql("SELECT 1")

    assert result == ProviderResult(
        success=False,
        error="权限拒绝",
        metadata={"error_code": "query_error"},
    )


def test_mindsdb_sql_error_payload_uses_fallback_fields_and_default():
    for payload, expected in (
        ({"type": "error", "error": "invalid SQL"}, "invalid SQL"),
        ({"type": "error", "message": "table not found"}, "table not found"),
        ({"type": "error"}, "MindsDB SQL query failed"),
    ):
        provider = MindsDBProvider(
            _connection("mindsdb"),
            session=_Session(_Response(payload)),
        )

        result = provider.query_sql("SELECT 1")

        assert result.success is False
        assert result.error == expected
        assert result.message != "查询成功"


def test_mindsdb_sql_error_payload_redacts_connection_secrets():
    provider = MindsDBProvider(
        _connection(
            "mindsdb",
            auth=AuthSpec(
                type="bearer",
                token="token-x",
                password="password-x",
            ),
        ),
        session=_Session(
            _Response(
                {
                    "type": "error",
                    "error_message": (
                        "token-x and password-x rejected by "
                        "https://provider.example.com"
                    ),
                }
            )
        ),
    )

    result = provider.query_sql("SELECT 1")

    assert result.success is False
    assert "token-x" not in result.error
    assert "password-x" not in result.error
    assert "https://provider.example.com" not in result.error
    assert result.error.count("[REDACTED]") == 3


def test_mindsdb_anonymous_401_is_not_replayed_with_empty_credentials():
    session = _Session(_Response({"detail": "authentication required"}, status_code=401))
    provider = MindsDBProvider(_connection("mindsdb"), session=session)

    result = provider.query_sql("SELECT 1")

    assert result.success is False
    assert len(session.requests) == 1
    assert "Authorization" not in session.requests[0][2]["headers"]


def test_mindsdb_native_event_sequence_uses_shared_contract():
    response = _Response({})

    def iter_lines(decode_unicode=True):
        del decode_unicode
        return iter([
            'data: {"type":"start","prompt":"starting"}',
            "",
            'data: {"output":"final answer"}',
            "",
            'data: {"type":"end"}',
            "",
            'data: {"type":"end"}',
            "",
        ])
    response.iter_lines = iter_lines
    session = _Session(response)
    provider = MindsDBProvider(
        _connection(
            "mindsdb",
            options={"default_agent_name": "agent-x", "project_name": "project-x"},
        ),
        session=session,
    )
    events = list(iter_query_events(provider, "question"))
    assert [event.type for event in events] == ["start", "final", "done"]
    assert events[1].content == "final answer"
    assert len(session.requests) == 1


def test_mindsdb_chunk_events_preserve_pre_refactor_branch_priority_and_content():
    thought = "```\nThought:"
    cases = [
        (
            {"type": "start"},
            [("start", "开始执行本体探索")],
        ),
        (
            {"type": "start", "messages": [{"content": "start from messages"}]},
            [("start", "start from messages")],
        ),
        (
            {"type": "start", "content": "ignored start content"},
            [("start", "开始执行本体探索")],
        ),
        (
            {"type": "sql", "content": "SELECT 1"},
            [],
        ),
        (
            {"type": "final", "content": "not a baseline final"},
            [("intermediate", "not a baseline final")],
        ),
        (
            {"type": "other", "content": "## 最终回答:\nkeep marker"},
            [("intermediate", "## 最终回答:\nkeep marker")],
        ),
        (
            {"type": "data", "content": "Executing final SQL query: SELECT 1"},
            [("final", "Executing final SQL query: SELECT 1")],
        ),
        (
            {"type": "data", "content": ""},
            [("final", "查询成功，未匹配到数据。")],
        ),
        (
            {"type": "data", "content": "", "is_task_complete": True},
            [("final", "查询成功，未匹配到数据。")],
        ),
        (
            {"output": "final answer", "is_task_complete": True},
            [("final", "final answer")],
        ),
        (
            {"output": "", "is_task_complete": True},
            [("done", "查询流程结束")],
        ),
        (
            {"output": "", "content": "content is not output"},
            [],
        ),
        (
            {
                "actions": [
                    {"log": "prepare SQL" + thought},
                    {"log": "prepare SQL" + thought},
                ],
                "steps": [
                    {
                        "action": {"tool": "sql_query"},
                        "observation": "mindsdb.result",
                    }
                ],
                "content": "ignored because actions win",
            },
            [("intermediate", "prepare SQL")],
        ),
        (
            {
                "actions": [],
                "steps": [
                    {
                        "action": {"tool": "sql_query"},
                        "observation": "mindsdb.result",
                    }
                ],
            },
            [],
        ),
        (
            {"type": "done"},
            [("done", "查询流程结束")],
        ),
        (
            {"is_task_complete": True},
            [("done", "查询流程结束")],
        ),
        (
            {"messages": ["plain string", {"content": "dict content"}]},
            [("intermediate", "plain string\ndict content")],
        ),
        (
            '{"type":"data","content":"json string final"}',
            [("final", "json string final")],
        ),
    ]

    for chunk, expected in cases:
        actual = [(event.type, event.content) for event in _chunk_events(chunk)]
        assert actual == expected, chunk


def test_mindsdb_query_without_final_is_not_reported_as_success():
    response = _Response({})

    def iter_lines(decode_unicode=True):
        del decode_unicode
        return iter([
            'data: {"type":"start","prompt":"starting"}',
            "",
            'data: {"type":"end"}',
            "",
        ])
    response.iter_lines = iter_lines
    session = _Session(response)
    sleep_calls = []
    provider = MindsDBProvider(
        _connection("mindsdb"),
        session=session,
        sleep_fn=sleep_calls.append,
    )

    result = provider.query("question")

    assert result == ProviderResult(
        success=False,
        error="MindsDB agent stream ended before a final answer",
        metadata={"error_code": "missing_final", "stream_attempts": 2},
    )
    assert len(session.requests) == 2
    assert sleep_calls == [5]


def test_mindsdb_native_error_chunk_is_failure_and_redacts_secrets():
    response = _Response({})

    def iter_lines(decode_unicode=True):
        del decode_unicode
        return iter([
            (
                'data: {"error":"token-x and password-x rejected by '
                'https://provider.example.com"}'
            ),
            "",
            'data: {"type":"end"}',
            "",
        ])
    response.iter_lines = iter_lines
    session = _Session(response)
    sleep_calls = []
    provider = MindsDBProvider(
        _connection(
            "mindsdb",
            auth=AuthSpec(
                type="bearer",
                token="token-x",
                password="password-x",
            ),
        ),
        session=session,
        sleep_fn=sleep_calls.append,
    )

    result = provider.query("question")

    assert result.success is False
    assert result.metadata["error_code"] == "query_error"
    assert "token-x" not in result.error
    assert "password-x" not in result.error
    assert "https://provider.example.com" not in result.error
    assert result.error.count("[REDACTED]") == 3
    assert result.metadata["stream_attempts"] == 2
    assert len(session.requests) == 2
    assert sleep_calls == [5]


def test_mindsdb_stream_error_recovers_once_and_returns_final():
    session = _SequenceSession(
        [
            _stream_response(
                {
                    "type": "error",
                    "error": (
                        "token-x rejected by https://provider.example.com"
                    ),
                },
                {"type": "end"},
            ),
            _stream_response(
                {"type": "start", "prompt": "retrying"},
                {"type": "data", "text": "recovered answer"},
                {"type": "end"},
            ),
        ]
    )
    sleep_calls = []
    provider = MindsDBProvider(
        _connection(
            "mindsdb",
            auth=AuthSpec(type="bearer", token="token-x"),
        ),
        session=session,
        sleep_fn=sleep_calls.append,
    )

    events = list(iter_query_events(provider, "question"))

    assert [event.type for event in events] == [
        "start",
        "intermediate",
        "final",
        "done",
    ]
    assert events[2].content == "recovered answer"
    assert len(session.requests) == 2
    assert sleep_calls == [5]
    assert "token-x" not in events[1].content
    assert "https://provider.example.com" not in events[1].content


def test_mindsdb_stream_read_failure_recovers_once():
    broken = _Response({})

    def broken_lines(decode_unicode=True):
        del decode_unicode
        yield 'data: {"type":"start","prompt":"starting"}'
        yield ""
        raise requests.exceptions.ChunkedEncodingError(
            "token-x disconnected from https://provider.example.com"
        )

    broken.iter_lines = broken_lines
    session = _SequenceSession(
        [
            broken,
            _stream_response(
                {"type": "data", "content": "answer after reconnect"},
                {"type": "end"},
            ),
        ]
    )
    sleep_calls = []
    provider = MindsDBProvider(
        _connection(
            "mindsdb",
            auth=AuthSpec(type="bearer", token="token-x"),
        ),
        session=session,
        sleep_fn=sleep_calls.append,
    )

    events = list(iter_query_events(provider, "question"))

    assert [event.type for event in events] == [
        "start",
        "intermediate",
        "final",
        "done",
    ]
    assert events[2].content == "answer after reconnect"
    assert len(session.requests) == 2
    assert sleep_calls == [5]
    assert "token-x" not in events[1].content
    assert "https://provider.example.com" not in events[1].content


def test_mindsdb_empty_data_is_a_successful_terminal_result_without_retry():
    session = _SequenceSession(
        [
            _stream_response(
                {"type": "start"},
                {"type": "data", "content": ""},
                {"type": "end"},
            )
        ]
    )
    provider = MindsDBProvider(
        _connection("mindsdb"),
        session=session,
        sleep_fn=lambda _seconds: None,
    )

    result = provider.query("question")

    assert result == ProviderResult(
        success=True,
        data="查询成功，未匹配到数据。",
        message="查询成功，未匹配到数据。",
    )
    assert len(session.requests) == 1


def test_dataagent_builds_a2a_request_and_synthesizes_events():
    session = _Session(
        _Response(
            {
                "task": {
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {
                            "parts": [
                                {
                                    "text": "{'sql': 'SELECT 1', 'columns': ['n'], 'rows_preview': [(1,)]}"
                                }
                            ]
                        },
                    }
                }
            }
        )
    )
    provider = DataAgentProvider(
        _connection(
            "dataagent",
            auth=AuthSpec(type="bearer", token="data-token"),
            options={"timeout_seconds": 44},
        ),
        session=session,
    )
    events = list(iter_query_events(provider, "question"))
    assert [event.type for event in events] == ["start", "final", "done"]
    assert events[1].result is not None and events[1].result.success is True
    assert session.requests[0][1].endswith("/a2a/rest/message:send")
    assert session.requests[0][2]["timeout"] == 44


def test_dataagent_reports_incomplete_structured_result_without_exposing_sql_internals():
    session = _Session(
        _Response(
            {
                "task": {
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {
                            "parts": [
                                {
                                    "text": "{'columns': ['n'], 'rows_preview': [(1,)]}"
                                }
                            ]
                        },
                    }
                }
            }
        )
    )
    provider = DataAgentProvider(_connection("dataagent"), session=session)

    result = provider.query("question")

    assert result.success is False
    assert result.error == "DataAgent 未返回有效的结构化查询结果"
    assert result.metadata["error_code"] == "missing_sql"
    assert result.metadata["row_count"] == 1


def test_dataagent_field_parser_preserves_matched_raw_text_on_literal_failure():
    fields = extract_dataagent_fields(
        r"{'sql': '\x', 'columns': [invalid], 'confidence': None, 'error': None}"
    )

    assert fields["sql"] == r"'\x'"
    assert fields["columns"] == "[invalid]"
    assert fields["confidence"] == "None"
    assert fields["error"] is None


def test_dataagent_converts_unexpected_transport_exception_to_safe_failed_result():
    class _UnexpectedSession(_Session):
        def post(self, url, **kwargs):
            raise RuntimeError(
                "token-x failed at https://provider.example.com/internal"
            )

    provider = DataAgentProvider(
        _connection(
            "dataagent",
            auth=AuthSpec(type="bearer", token="token-x"),
        ),
        session=_UnexpectedSession(None),
    )

    result = provider.query("question")

    assert result.success is False
    assert result.metadata["error_code"] == "internal_error"
    assert result.error.startswith("RuntimeError: ")
    assert "token-x" not in result.error
    assert "https://provider.example.com" not in result.error
    assert "[REDACTED]" in result.error


def test_builtin_registry_constructs_all_agent_providers():
    for name in ("mindsdb", "nl2sql_http", "dataagent"):
        provider = create_provider_client(_connection(name))
        assert provider is not None
        provider.close()
