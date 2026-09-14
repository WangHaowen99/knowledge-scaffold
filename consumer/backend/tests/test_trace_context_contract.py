"""Cross-entrypoint trace context and shutdown contract tests."""

from __future__ import annotations

import threading

import pytest
from agent.core.jiuwen.tools import CurrentRuntimeToolAdapter, _SharedInvocationIndex
from integration.mcp.scope import resolve_mcp_scope_from_token
from integration.mcp.token_service import MCPTokenAuthError
from integration.observability import ObservabilityBus, RuntimeEvent, RuntimeEventType
from integration.observability.exporters.langfuse import LangfuseExporter
from integration.observability.trace_context import (
    TraceParentValidationError,
    normalize_traceparent,
    parse_traceparent,
)

VALID_TRACEPARENT = "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01"


def test_jiuwen_tool_batches_share_one_stable_invocation_index():
    counter = _SharedInvocationIndex()
    first = CurrentRuntimeToolAdapter(
        tool_name="tool_a",
        state={},
        context_factory=lambda: None,
        invocation_index_provider=counter.next,
    )
    second = CurrentRuntimeToolAdapter(
        tool_name="tool_b",
        state={},
        context_factory=lambda: None,
        invocation_index_provider=counter.next,
    )

    assert first.next_invocation_index_for_test() == 1
    assert second.next_invocation_index_for_test() == 2
    assert first.next_invocation_index_for_test() == 3


def test_traceparent_parser_normalizes_valid_w3c_context():
    parsed = parse_traceparent(VALID_TRACEPARENT.upper())

    assert parsed is not None
    assert parsed.value == VALID_TRACEPARENT
    assert parsed.trace_id == "a" * 32
    assert parsed.parent_id == "b" * 16
    assert normalize_traceparent("") == ""


@pytest.mark.parametrize(
    "value,error",
    [
        ("00-short", "55 characters"),
        ("01-{}-{}-01".format("a" * 32, "b" * 16), "version"),
        ("00-{}-{}-01".format("0" * 32, "b" * 16), "trace-id"),
        ("00-{}-{}-01".format("a" * 32, "0" * 16), "parent-id"),
        ("00-{}-{}-zz".format("a" * 32, "b" * 16), "hexadecimal"),
    ],
)
def test_traceparent_parser_rejects_malformed_or_zero_ids(value, error):
    with pytest.raises(TraceParentValidationError, match=error):
        parse_traceparent(value)


def test_mcp_scope_rejects_invalid_traceparent_before_token_resolution():
    with pytest.raises(MCPTokenAuthError) as exc_info:
        resolve_mcp_scope_from_token(
            "mcp_token",
            required_scope="mcp:chat",
            traceparent="00-invalid",
        )

    assert exc_info.value.code == "INVALID_TRACEPARENT"


def test_external_trace_id_is_used_for_llm_and_tool_spans():
    calls = []

    class _Adapter:
        available = True

        @staticmethod
        def create_trace(*_args, **kwargs):
            calls.append(("trace", kwargs["trace_id"]))
            return kwargs["trace_id"]

        @staticmethod
        def create_llm_span(*_args, **kwargs):
            calls.append(("llm", kwargs["trace_id"]))
            return "llm-span"

        @staticmethod
        def create_span(*_args, **kwargs):
            calls.append(("tool", kwargs["trace_id"]))
            return "tool-span"

    exporter = LangfuseExporter(_Adapter())
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.RUN_STARTED,
            run_id="run-external",
            session_id="session-external",
            payload={"external_trace_id": "a" * 32, "user_query": "q"},
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.LLM_STARTED,
            run_id="run-external",
            session_id="session-external",
            span_key="llm:main:1:1",
            payload={"caller": "main", "iteration": 1, "model": "model-a"},
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.TOOL_STARTED,
            run_id="run-external",
            session_id="session-external",
            span_key="tool:main:1:search:call-1",
            payload={"tool_name": "search"},
        )
    )

    assert calls == [
        ("trace", "a" * 32),
        ("llm", "a" * 32),
        ("tool", "a" * 32),
    ]


def test_observability_close_flushes_and_closes_after_timeout():
    export_started = threading.Event()
    release_export = threading.Event()

    class _BlockingExporter:
        def __init__(self):
            self.flush_count = 0
            self.close_count = 0

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export(self, _event):
            export_started.set()
            release_export.wait(timeout=1)

        def flush(self):
            self.flush_count += 1

        def close(self):
            self.flush()
            self.close_count += 1

    exporter = _BlockingExporter()
    bus = ObservabilityBus(exporter, flush_interval_seconds=10)
    try:
        assert bus.emit(RuntimeEvent(type=RuntimeEventType.RUN_STARTED, run_id="run-1"))
        assert export_started.wait(timeout=1)
        bus.close(timeout=0.01)

        assert exporter.close_count == 0
        release_export.set()
        bus.wait_for_worker(timeout=1)

        assert not bus.worker_is_alive()
        assert exporter.close_count == 1
        assert exporter.flush_count >= 1
    finally:
        release_export.set()
        bus.wait_for_worker(timeout=1)
        bus.close(timeout=1)
