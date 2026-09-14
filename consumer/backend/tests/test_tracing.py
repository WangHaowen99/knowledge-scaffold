"""Tests for the low-level Langfuse client adapter."""

import builtins
import importlib
from unittest.mock import MagicMock

import integration.observability.exporters.langfuse.client as langfuse_client
from backend_synthetic_credentials import synthetic_secret


def test_langfuse_client_module_does_not_import_langfuse_during_module_load(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "langfuse" or name.startswith("langfuse."):
            raise AssertionError("langfuse should be imported lazily")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    importlib.reload(langfuse_client)


def test_create_langfuse_client_returns_none_when_disabled():
    assert langfuse_client.create_langfuse_client({"enabled": False}) is None


def test_create_langfuse_client_uses_explicit_config(monkeypatch):
    created = {}

    class FakeLangfuse:
        def __init__(self, **kwargs):
            created.update(kwargs)

    monkeypatch.setattr(langfuse_client, "_load_langfuse_class", lambda: FakeLangfuse)

    client = langfuse_client.create_langfuse_client(
        {
            "enabled": True,
            "public_key": "pk-test",
            "secret_key": synthetic_secret("tracing-credential-02"),
            "base_url": "https://langfuse.example.net",
            "debug": True,
        }
    )

    assert client is not None
    assert created == {
        "public_key": "pk-test",
        "secret_key": synthetic_secret("tracing-credential-02"),
        "host": "https://langfuse.example.net",
        "debug": True,
    }


def test_create_langfuse_client_does_not_read_environment(monkeypatch):
    monkeypatch.setattr(langfuse_client, "_load_langfuse_class", lambda: object)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-env")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-env")

    assert langfuse_client.create_langfuse_client({"enabled": True}) is None


def test_create_langfuse_client_does_not_load_sdk_when_config_is_incomplete(monkeypatch):
    def fail_load():
        raise AssertionError("Langfuse SDK should not load for incomplete config")

    monkeypatch.setattr(langfuse_client, "_load_langfuse_class", fail_load)

    assert langfuse_client.create_langfuse_client({"enabled": True}) is None


def test_trace_adapter_creates_trace_and_masks_sensitive_metadata():
    mock_langfuse = MagicMock()
    mock_trace = MagicMock()
    mock_trace.id = "trace-1"
    mock_langfuse.trace.return_value = mock_trace
    adapter = langfuse_client.LangfuseTraceAdapter(mock_langfuse)

    result = adapter.create_trace(
        "test_name",
        session_id="session-1",
        metadata={"api_key": synthetic_secret("tracing-credential-01"), "email": "user@example.com"},
        input={"query": "call me at 13812345678"},
    )

    assert result == "trace-1"
    mock_langfuse.trace.assert_called_once_with(
        id=None,
        name="test_name",
        user_id=None,
        session_id="session-1",
        metadata={"api_key": "[REDACTED]", "email": "[REDACTED]"},
        tags=[],
        input={"query": "call me at 1*** **** ****"},
    )


def test_trace_adapter_creates_and_ends_spans():
    mock_trace = MagicMock()
    mock_span = MagicMock()
    mock_span.id = "span-1"
    mock_trace.span.return_value = mock_span
    adapter = langfuse_client.LangfuseTraceAdapter(None)
    adapter.seed_trace_for_test("trace-1", mock_trace)

    span_id = adapter.create_span("trace-1", "tool:test", tags=["tool"])

    assert span_id == "span-1"
    mock_trace.span.assert_called_once_with(
        name="tool:test",
        metadata={},
        tags=["tool"],
        parent_observation_id=None,
        input={},
        output={},
    )

    adapter.end_observation("trace-1", "span-1", status="success", output={"ok": True})

    mock_span.end.assert_called_once_with(output={"ok": True})
    assert adapter.trace_span_ids_for_test("trace-1") == set()


def test_trace_adapter_releases_terminal_trace_after_spans_finish():
    mock_trace = MagicMock()
    mock_span = MagicMock()
    mock_span.id = "span-1"
    mock_trace.generation.return_value = mock_span
    adapter = langfuse_client.LangfuseTraceAdapter(None)
    adapter.seed_trace_for_test("trace-1", mock_trace)

    span_id = adapter.create_llm_span("trace-1", "llm:test")
    adapter.update_trace_output("trace-1", {"status": "success", "final_answer": "done"})
    assert adapter.has_trace_for_test("trace-1")

    adapter.update_llm_span_tokens("trace-1", span_id, {"prompt_tokens": 1, "completion_tokens": 2})

    mock_span.end.assert_called_once_with(usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
    assert not adapter.has_trace_for_test("trace-1")


def test_trace_adapter_ends_llm_span_with_output_when_provided():
    mock_trace = MagicMock()
    mock_span = MagicMock()
    mock_span.id = "span-1"
    mock_trace.generation.return_value = mock_span
    adapter = langfuse_client.LangfuseTraceAdapter(None)
    adapter.seed_trace_for_test("trace-1", mock_trace)

    span_id = adapter.create_llm_span("trace-1", "llm:test")
    adapter.update_llm_span_tokens(
        "trace-1",
        span_id,
        {"prompt_tokens": 1, "completion_tokens": 2},
        output={"content": "email user@example.com"},
    )

    mock_span.end.assert_called_once_with(
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        output={"content": "email ***@***.***"},
    )


def test_trace_adapter_flushes_its_own_client():
    mock_langfuse = MagicMock()
    adapter = langfuse_client.LangfuseTraceAdapter(mock_langfuse)

    adapter.flush()

    mock_langfuse.flush.assert_called_once_with()


def test_trace_adapter_shutdowns_client_when_supported():
    mock_langfuse = MagicMock()
    adapter = langfuse_client.LangfuseTraceAdapter(mock_langfuse)

    adapter.close()

    mock_langfuse.shutdown.assert_called_once_with()


def test_trace_adapter_close_falls_back_to_flush_when_shutdown_is_missing():
    class FlushOnlyClient:
        def __init__(self):
            self.flushed = False

        def flush(self):
            self.flushed = True

    client = FlushOnlyClient()
    adapter = langfuse_client.LangfuseTraceAdapter(client)

    adapter.close()

    assert client.flushed is True
