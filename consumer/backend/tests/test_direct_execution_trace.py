"""Unit tests for DirectExecutionRuntimeTrace."""

from __future__ import annotations

from typing import List

import pytest

from integration.observability.events import RuntimeEvent, RuntimeEventType, RuntimeStatus
from integration.observability.traces.direct_execution import DirectExecutionRuntimeTrace


class MockObserver:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.events: List[RuntimeEvent] = []

    def emit(self, event: RuntimeEvent) -> None:
        self.events.append(event)


@pytest.fixture
def observer() -> MockObserver:
    return MockObserver(enabled=True)


@pytest.fixture
def trace(observer: MockObserver) -> DirectExecutionRuntimeTrace:
    return DirectExecutionRuntimeTrace(
        observer=observer,
        run_id="test-run-123",
        session_id="test-session-456",
        turn_id=42,
    )


def test_direct_execution_scope_closes_success(trace: DirectExecutionRuntimeTrace, observer: MockObserver):
    with trace.direct_execution_scope(ontology_id="onto-1", operation_key="op-test"):
        pass

    assert [event.type for event in observer.events] == [
        RuntimeEventType.AGENT_LOOP_STARTED,
        RuntimeEventType.AGENT_LOOP_FINISHED,
    ]
    assert observer.events[0].span_key == "agent_loop:direct"
    assert observer.events[0].payload["caller"] == "direct"
    assert observer.events[0].payload["ontology_id"] == "onto-1"
    assert observer.events[0].payload["operation_key"] == "op-test"
    assert observer.events[1].payload["status"] == RuntimeStatus.SUCCESS


def test_direct_execution_scope_closes_error_when_marked(trace: DirectExecutionRuntimeTrace, observer: MockObserver):
    with trace.direct_execution_scope(ontology_id="onto-1", operation_key="op-fail"):
        trace.mark_failed()

    assert observer.events[-1].type == RuntimeEventType.AGENT_LOOP_FINISHED
    assert observer.events[-1].payload["status"] == RuntimeStatus.ERROR


def test_direct_execution_scope_closes_error_on_exception(trace: DirectExecutionRuntimeTrace, observer: MockObserver):
    with pytest.raises(RuntimeError):
        with trace.direct_execution_scope(ontology_id="onto-1", operation_key="op-fail"):
            raise RuntimeError("boom")

    assert observer.events[-1].type == RuntimeEventType.AGENT_LOOP_FINISHED
    assert observer.events[-1].payload["status"] == RuntimeStatus.ERROR


def test_script_scope_emits_tool_chain_success(trace: DirectExecutionRuntimeTrace, observer: MockObserver):
    with trace.direct_execution_scope(ontology_id="onto-1", operation_key="op-test"):
        with trace.script_scope(
            script_name="test_operation.py",
            script_path="scripts/test_operation.py",
            ontology_id="onto-1",
            operation_key="op-test",
            arguments={"param": "value"},
        ) as script_trace:
            script_trace.record_result(
                output="Operation completed successfully",
                is_error=False,
                elapsed_ms=1500,
            )

    assert [event.type for event in observer.events] == [
        RuntimeEventType.AGENT_LOOP_STARTED,
        RuntimeEventType.TOOL_BATCH_STARTED,
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_FINISHED,
        RuntimeEventType.TOOL_BATCH_FINISHED,
        RuntimeEventType.AGENT_LOOP_FINISHED,
    ]
    tool_started = observer.events[2]
    assert tool_started.span_key == "tool:direct:0:execute_script:test_operation.py"
    assert tool_started.parent_key == "tools:direct:0"
    assert tool_started.payload["tool_name"] == "execute_script"
    assert tool_started.payload["display_name"] == "execute_script:test_operation.py"
    assert tool_started.payload["script_name"] == "test_operation.py"
    assert tool_started.payload["script_path"] == "scripts/test_operation.py"
    assert observer.events[3].payload["status"] == RuntimeStatus.SUCCESS
    assert observer.events[4].payload["status"] == RuntimeStatus.SUCCESS
    assert observer.events[5].payload["status"] == RuntimeStatus.SUCCESS


def test_script_scope_propagates_error_to_parent_spans(trace: DirectExecutionRuntimeTrace, observer: MockObserver):
    with trace.direct_execution_scope(ontology_id="onto-1", operation_key="op-fail"):
        with trace.script_scope(
            script_name="failing_script.py",
            script_path="scripts/failing_script.py",
            ontology_id="onto-1",
            operation_key="op-fail",
            arguments={},
        ) as script_trace:
            script_trace.record_result(
                output="Error: Script crashed",
                is_error=True,
                elapsed_ms=5000,
            )

    terminal_events = [
        event
        for event in observer.events
        if event.type
        in {
            RuntimeEventType.TOOL_FINISHED,
            RuntimeEventType.TOOL_BATCH_FINISHED,
            RuntimeEventType.AGENT_LOOP_FINISHED,
        }
    ]
    assert [event.payload["status"] for event in terminal_events] == [
        RuntimeStatus.ERROR,
        RuntimeStatus.ERROR,
        RuntimeStatus.ERROR,
    ]


def test_script_scope_closes_error_when_finish_not_called(trace: DirectExecutionRuntimeTrace, observer: MockObserver):
    with trace.direct_execution_scope(ontology_id="onto-1", operation_key="op-fail"):
        with trace.script_scope(
            script_name="failing_script.py",
            script_path="scripts/failing_script.py",
            ontology_id="onto-1",
            operation_key="op-fail",
            arguments={},
        ):
            pass

    assert observer.events[-3].type == RuntimeEventType.TOOL_FINISHED
    assert observer.events[-3].payload["status"] == RuntimeStatus.ERROR
    assert observer.events[-2].payload["status"] == RuntimeStatus.ERROR
    assert observer.events[-1].payload["status"] == RuntimeStatus.ERROR


def test_disabled_observer_emits_no_events():
    observer = MockObserver(enabled=False)
    trace = DirectExecutionRuntimeTrace(
        observer=observer,
        run_id="run-1",
        session_id="session-1",
        turn_id=1,
    )

    with trace.direct_execution_scope(ontology_id="onto-1", operation_key="op-test"):
        with trace.script_scope(
            script_name="test.py",
            script_path="test.py",
            ontology_id="onto-1",
            operation_key="op-test",
            arguments={},
        ) as script_trace:
            script_trace.record_result(output="ok", is_error=False, elapsed_ms=10)

    assert observer.events == []


def test_empty_run_id_prevents_events(observer: MockObserver):
    trace = DirectExecutionRuntimeTrace(
        observer=observer,
        run_id="",
        session_id="session-1",
        turn_id=1,
    )

    with trace.direct_execution_scope(ontology_id="onto-1", operation_key="op-test"):
        pass

    assert observer.events == []
