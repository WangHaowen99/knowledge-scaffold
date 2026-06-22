"""Semantic observer facade for direct execution workflow.

Encapsulates all trace/span emission logic for the Direct Execution branch,
providing a clean API that mirrors AgentRuntimeTrace but optimized for
single-script execution without LLM iteration loops.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from ...utils.logging import get_logger
from ..events import RuntimeEvent, RuntimeEventType, RuntimeStatus
from ..observer import NOOP_OBSERVER

logger = get_logger("observability.traces.direct_execution")


class _DirectScriptTrace:
    def __init__(
        self,
        runtime_trace: "DirectExecutionRuntimeTrace",
        *,
        script_name: str,
        script_path: str,
    ) -> None:
        self._runtime_trace = runtime_trace
        self._script_name = script_name
        self._script_path = script_path
        self._finished = False

    def record_result(self, *, output: str, is_error: bool, elapsed_ms: int) -> None:
        self._finished = True
        self._runtime_trace.finish_tool(
            output=output,
            is_error=is_error,
            elapsed_ms=elapsed_ms,
            script_name=self._script_name,
            script_path=self._script_path,
        )
        self._runtime_trace.finish_tool_batch(
            is_error=is_error,
            tool_names=["execute_script"],
            duration_ms=elapsed_ms,
        )
        if is_error:
            self._runtime_trace.mark_failed()


class DirectExecutionRuntimeTrace:
    """Business-facing facade for direct execution workflow tracing.

    Runtime code calls this layer to describe direct execution lifecycle
    events. Provider-specific mapping and I/O stay outside this facade.
    """

    def __init__(
        self,
        *,
        observer: Any = None,
        run_id: str,
        session_id: Optional[str],
        turn_id: Optional[int],
    ) -> None:
        self.observer = observer or NOOP_OBSERVER
        self.run_id = str(run_id or "")
        self.session_id = session_id
        self.turn_id = turn_id

        # Span keys for hierarchical tracing
        self.loop_span_key = "agent_loop:direct"
        self.tool_batch_span_key = "tools:direct:0"
        self._tool_span_key: Optional[str] = None
        self._loop_finished: bool = False
        self._failed: bool = False

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.observer, "enabled", True))

    def _emit(
        self,
        event_type: str,
        *,
        span_key: Optional[str] = None,
        parent_key: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.run_id or not self.enabled:
            return
        try:
            self.observer.emit(
                RuntimeEvent(
                    type=event_type,
                    run_id=self.run_id,
                    session_id=self.session_id,
                    turn_id=self.turn_id,
                    span_key=span_key,
                    parent_key=parent_key,
                    payload=payload or {},
                )
            )
        except Exception:
            logger.warning("Failed to emit direct execution observability event", exc_info=True)

    @contextmanager
    def direct_execution_scope(
        self,
        *,
        ontology_id: str,
        operation_key: str,
    ) -> Iterator["DirectExecutionRuntimeTrace"]:
        """Emit and close the direct execution loop as one lifecycle."""
        self.start_loop(ontology_id=ontology_id, operation_key=operation_key)
        try:
            yield self
        except Exception:
            self.finish_loop(is_error=True)
            raise
        else:
            self.finish_loop(is_error=self._failed)

    @contextmanager
    def script_scope(
        self,
        *,
        script_name: str,
        script_path: str,
        ontology_id: str,
        operation_key: str,
        arguments: Dict[str, Any],
    ) -> Iterator[_DirectScriptTrace]:
        """Emit and close the single execute_script tool lifecycle."""
        self.start_tool_batch()
        self.start_tool(
            script_name=script_name,
            script_path=script_path,
            ontology_id=ontology_id,
            operation_key=operation_key,
            arguments=arguments,
        )
        script_trace = _DirectScriptTrace(
            self,
            script_name=script_name,
            script_path=script_path,
        )
        started_at = time.perf_counter()
        try:
            yield script_trace
        except Exception as exc:
            if not script_trace._finished:
                script_trace.record_result(
                    output="Direct script execution failed: {}".format(str(exc)),
                    is_error=True,
                    elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                )
            else:
                self.mark_failed()
            raise
        finally:
            if not script_trace._finished:
                script_trace.record_result(
                    output="Direct script execution did not report a result",
                    is_error=True,
                    elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                )

    def mark_failed(self) -> None:
        self._failed = True

    def start_loop(self, *, ontology_id: str, operation_key: str) -> None:
        """Emit AGENT_LOOP_STARTED event."""
        self._emit(
            RuntimeEventType.AGENT_LOOP_STARTED,
            span_key=self.loop_span_key,
            payload={
                "caller": "direct",
                "agent_mode": "direct",
                "execution_path": "direct_execution",
                "ontology_id": ontology_id,
                "operation_key": operation_key,
            },
        )

    def finish_loop(self, *, is_error: bool = False) -> None:
        """Emit AGENT_LOOP_FINISHED event."""
        if self._loop_finished:
            return
        self._loop_finished = True
        self._emit(
            RuntimeEventType.AGENT_LOOP_FINISHED,
            span_key=self.loop_span_key,
            payload={
                "status": RuntimeStatus.ERROR if is_error else RuntimeStatus.SUCCESS,
                "caller": "direct",
            },
        )

    def start_tool_batch(self) -> None:
        """Emit TOOL_BATCH_STARTED event."""
        self._emit(
            RuntimeEventType.TOOL_BATCH_STARTED,
            span_key=self.tool_batch_span_key,
            parent_key=self.loop_span_key,
            payload={
                "caller": "direct",
                "iteration": 0,
            },
        )

    def finish_tool_batch(
        self,
        *,
        is_error: bool = False,
        tool_names: Optional[list] = None,
        duration_ms: int = 0,
    ) -> None:
        """Emit TOOL_BATCH_FINISHED event."""
        self._emit(
            RuntimeEventType.TOOL_BATCH_FINISHED,
            span_key=self.tool_batch_span_key,
            payload={
                "status": RuntimeStatus.ERROR if is_error else RuntimeStatus.SUCCESS,
                "caller": "direct",
                "iteration": 0,
                "tool_calls_count": 1,
                "tool_names": tool_names or ["execute_script"],
                "duration_ms": duration_ms,
            },
        )

    def start_tool(
        self,
        *,
        script_name: str,
        script_path: str,
        ontology_id: str,
        operation_key: str,
        arguments: Dict[str, Any],
    ) -> None:
        """Emit TOOL_STARTED event with script name for trace display."""
        self._tool_span_key = "tool:direct:0:execute_script:{}".format(script_name)

        self._emit(
            RuntimeEventType.TOOL_STARTED,
            span_key=self._tool_span_key,
            parent_key=self.tool_batch_span_key,
            payload={
                "tool_name": "execute_script",
                "display_name": "execute_script:{}".format(script_name),
                "tool_call_id": "direct",
                "caller": "direct",
                "iteration": 0,
                "script_name": script_name,
                "script_path": script_path,
                "input": {
                    "ontology_id": ontology_id,
                    "operation_key": operation_key,
                    "arguments": arguments,
                },
            },
        )

    def finish_tool(
        self,
        *,
        output: str,
        is_error: bool,
        elapsed_ms: int,
        script_name: str,
        script_path: str,
    ) -> None:
        """Emit TOOL_FINISHED event with script name in output."""
        if not self._tool_span_key:
            return

        self._emit(
            RuntimeEventType.TOOL_FINISHED,
            span_key=self._tool_span_key,
            payload={
                "tool_name": "execute_script",
                "display_name": "execute_script:{}".format(script_name),
                "status": RuntimeStatus.ERROR if is_error else RuntimeStatus.SUCCESS,
                "caller": "direct",
                "iteration": 0,
                "elapsed_ms": elapsed_ms,
                "output": output[:2000],
                "error": output if is_error else None,
                "script_name": script_name,
                "script_path": script_path,
            },
        )
