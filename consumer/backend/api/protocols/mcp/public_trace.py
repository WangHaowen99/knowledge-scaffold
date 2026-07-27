"""Bounded public execution summaries for MCP chat responses."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping


_SENSITIVE_KEYS = {
    "authorization",
    "password",
    "passwd",
    "secret",
    "apikey",
    "token",
    "cookie",
    "credential",
}
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization|api[_-]?key|password|passwd|secret|token|cookie|credential)"
    r"\b\s*[:=]\s*([^\s,;&]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+)?\b")
_URI_CREDENTIAL_RE = re.compile(r"(://)[^/@\s:]+:[^/@\s]+@")
_TIMING_FIELDS = (
    "total_elapsed_ms",
    "model_elapsed_ms",
    "tools_elapsed_ms",
    "setup_elapsed_ms",
)
_PARSE_BYTES_HARD_LIMIT = 128 * 1024
_EXECUTED_STEP_LIMIT = 8
_EXECUTED_STEP_SUMMARY_LIMIT = 300
_WARNING_LIMIT = 8


@dataclass(frozen=True)
class _TraceEntry:
    order: int
    kind: str
    priority: int
    item: dict[str, Any]


class MCPPublicTraceCollector:
    """Incrementally retain only fields safe for an external MCP client."""

    def __init__(
        self,
        *,
        max_events: int = 30,
        max_bytes: int = 32768,
        max_text_chars: int = 500,
    ) -> None:
        self.max_events = min(500, max(1, int(max_events)))
        self.max_bytes = min(1024 * 1024, max(1024, int(max_bytes)))
        self.max_text_chars = min(4000, max(32, int(max_text_chars)))
        self._parse_chars_limit = min(
            _PARSE_BYTES_HARD_LIMIT,
            max(4096, self.max_bytes * 2),
        )
        self._started_at = time.perf_counter()
        self._lock = threading.RLock()
        self._order = 0
        self._entries: list[_TraceEntry] = []
        self._agent_content: dict[int, dict[str, Any]] = {}
        self._seen_iterations: set[int] = set()
        self._pending_tools: dict[str, dict[str, Any]] = {}
        self._pending_order: list[str] = []
        self._attempt_counts: dict[str, int] = {}
        self._omitted: dict[str, int] = {}
        self._text_truncations = 0
        self._totals = {
            "tool_calls": 0,
            "tool_calls_succeeded": 0,
            "tool_calls_failed": 0,
            "warnings": 0,
            "retries": 0,
            "tool_execution_elapsed_ms": 0,
        }

    def consume(self, envelope: Mapping[str, Any]) -> None:
        """Consume one envelope without retaining raw reasoning or tool payloads."""
        event_type = str(envelope.get("type") or "").strip()
        if event_type not in {
            "token.delta",
            "iteration.completed",
            "tool.started",
            "tool.progress",
            "tool.finished",
            "turn.finished",
        }:
            return
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}

        with self._lock:
            self._order += 1
            order = self._order
            if event_type == "token.delta":
                self._consume_content_delta(payload, order)
            elif event_type == "iteration.completed":
                self._consume_iteration(payload, order)
            elif event_type == "tool.started":
                self._consume_tool_started(payload, order)
            elif event_type == "tool.progress":
                self._consume_tool_progress(payload, order)
            elif event_type == "tool.finished":
                self._consume_tool_finished(payload, order)
            elif event_type == "turn.finished":
                self._consume_turn_finished(payload, order)

    def snapshot(self) -> dict[str, Any]:
        """Return a thread-safe JSON-ready trace within the configured byte cap."""
        with self._lock:
            entries = [copy.deepcopy(entry) for entry in self._entries]
            entries.extend(self._pending_entries())
            entries.extend(self._agent_content_entries())
            omitted = dict(self._omitted)
            if len(entries) > self.max_events:
                selected = self._select_entries(entries, self.max_events)
                selected_keys = {
                    (entry.order, entry.kind, entry.priority)
                    for entry in selected
                }
                for entry in entries:
                    if (entry.order, entry.kind, entry.priority) not in selected_keys:
                        omitted[entry.kind] = omitted.get(entry.kind, 0) + 1
                entries = selected
            else:
                entries.sort(key=lambda entry: entry.order)
            if self._text_truncations:
                omitted["text_fields_truncated"] = self._text_truncations
            totals = dict(self._totals)
            totals["iterations"] = len(self._seen_iterations)
            totals["overall_elapsed_ms"] = max(
                0,
                int((time.perf_counter() - self._started_at) * 1000),
            )

        truncated = bool(omitted)
        while True:
            trace = self._build_snapshot(entries, totals, omitted, truncated)
            if self._json_size(trace) <= self.max_bytes or not entries:
                return trace
            remove_index = min(
                range(len(entries)),
                key=lambda index: (entries[index].priority, entries[index].order),
            )
            removed = entries.pop(remove_index)
            omitted[removed.kind] = omitted.get(removed.kind, 0) + 1
            omitted["byte_limit_events"] = omitted.get("byte_limit_events", 0) + 1
            truncated = True

    def _consume_content_delta(self, payload: Mapping[str, Any], order: int) -> None:
        if str(payload.get("channel") or "").strip().lower() != "content":
            return
        value = payload.get("text")
        if value is None:
            return
        text = str(value)
        if not text:
            return
        iteration = _trace_iteration(payload)
        self._seen_iterations.add(iteration)
        state = self._agent_content.get(iteration)
        if state is None:
            if len(self._agent_content) >= self.max_events:
                stale_iteration = min(
                    self._agent_content,
                    key=lambda key: int(self._agent_content[key].get("order") or 0),
                )
                self._agent_content.pop(stale_iteration, None)
                self._omitted["agent_outputs"] = self._omitted.get("agent_outputs", 0) + 1
            state = {"order": order, "text": "", "truncated": False}
            self._agent_content[iteration] = state
        state["order"] = order
        if state.get("truncated"):
            return
        current = str(state.get("text") or "")
        remaining = self.max_text_chars - len(current)
        if len(text) > remaining:
            state["text"] = current + text[:max(0, remaining)]
            state["truncated"] = True
            self._text_truncations += 1
            return
        state["text"] = current + text

    def _consume_iteration(self, payload: Mapping[str, Any], order: int) -> None:
        item: dict[str, Any] = {}
        iteration = _non_negative_int(payload.get("iteration"))
        if iteration is not None:
            item["iteration"] = iteration
            self._seen_iterations.add(iteration)
        status = self._text(payload.get("status"), limit=80)
        if status:
            item["status"] = status
        timing = _compact_iteration_timing(payload)
        if timing:
            item["timing"] = timing
        tool_calls_count = _non_negative_int(payload.get("tool_calls_count"))
        if tool_calls_count is not None:
            item["tool_calls_count"] = tool_calls_count
        tool_names = payload.get("tool_names")
        if isinstance(tool_names, list):
            names = [self._text(value, limit=120) for value in tool_names[:16]]
            names = [value for value in names if value]
            if names:
                item["tool_names"] = names
        agent_output = self._public_agent_output(payload.get("agent_output"))
        if agent_output:
            item["agent_output"] = agent_output
        self._upsert_iteration_entry(_TraceEntry(order, "iterations", 2, item))

    def _consume_tool_started(self, payload: Mapping[str, Any], order: int) -> None:
        key = self._tool_key(payload, order, prefer_pending=False)
        state = self._new_tool_state(payload, order)
        self._remember_tool_iteration(state)
        self._pending_tools[key] = state
        self._pending_order.append(key)
        while len(self._pending_order) > max(16, self.max_events * 2):
            stale = self._pending_order.pop(0)
            self._pending_tools.pop(stale, None)

    def _consume_tool_progress(self, payload: Mapping[str, Any], order: int) -> None:
        key = self._tool_key(payload, order, prefer_pending=True)
        state = self._pending_tools.get(key)
        if state is None:
            state = self._new_tool_state(payload, order)
            self._pending_tools[key] = state
            self._pending_order.append(key)
        self._merge_tool_fields(state, payload)
        self._remember_tool_iteration(state)

        progress = payload.get("progress")
        if not isinstance(progress, Mapping):
            return
        business_event = progress.get("business_data_search_event")
        if not isinstance(business_event, Mapping):
            return
        event_type = str(business_event.get("type") or "").strip().lower()
        if event_type not in {"final", "error"}:
            return
        parsed = self._parse_mapping(business_event.get("content"))
        if parsed is not None:
            self._merge_result_metadata(state, parsed)

    def _consume_tool_finished(self, payload: Mapping[str, Any], order: int) -> None:
        key = self._tool_key(payload, order, prefer_pending=True)
        state = self._pending_tools.pop(key, None)
        if key in self._pending_order:
            self._pending_order.remove(key)
        if state is None:
            state = self._new_tool_state(payload, order)
        self._merge_tool_fields(state, payload)
        self._remember_tool_iteration(state)

        projection = payload.get("output_projection")
        if isinstance(projection, Mapping):
            self._merge_result_metadata(state, projection)
        parsed = self._parse_mapping(payload.get("output_preview"))
        if parsed is not None:
            self._merge_result_metadata(state, parsed)

        status = str(payload.get("status") or "completed").strip().lower() or "completed"
        state["status"] = status
        elapsed_ms = _non_negative_int(payload.get("elapsed_ms"))
        if elapsed_ms is not None:
            state["elapsed_ms"] = elapsed_ms
            self._totals["tool_execution_elapsed_ms"] += elapsed_ms
        error = self._error_text(payload.get("error"))
        if error:
            state["error_summary"] = error

        inferred_retry_count = self._register_attempt(state)
        explicit_retry_count = _non_negative_int(state.get("retry_count")) or 0
        retry_count = max(inferred_retry_count, explicit_retry_count)
        if retry_count:
            state["retry_count"] = retry_count
            self._totals["retries"] += max(
                1 if inferred_retry_count else 0,
                explicit_retry_count,
            )

        warnings = _dedupe_text_list(state.pop("_warnings", []), _WARNING_LIMIT)
        if warnings:
            state["_warnings"] = warnings
            state["warning_count"] = len(warnings)
            self._totals["warnings"] += len(warnings)

        self._totals["tool_calls"] += 1
        failed = status in {"error", "cancelled", "canceled", "failed"} or bool(error)
        if failed:
            self._totals["tool_calls_failed"] += 1
        else:
            self._totals["tool_calls_succeeded"] += 1
        priority = 5 if failed else 4 if state.get("_has_evidence") else 3
        self._append_entry(
            _TraceEntry(order, "tools", priority, self._public_tool_item(state))
        )

    def _consume_turn_finished(self, payload: Mapping[str, Any], order: int) -> None:
        iteration = _non_negative_int(payload.get("iteration"))
        if iteration is None:
            iteration = max(self._seen_iterations, default=1)
        self._seen_iterations.add(iteration)
        status = str(payload.get("status") or "").strip().lower()
        summary_value, source = _terminal_summary_source(payload, status)
        summary, summary_truncated = self._text_with_truncation(summary_value)
        item: dict[str, Any] = {
            "iteration": iteration,
            "status": "final_output" if status == "success" else status or "final_output",
        }
        if summary:
            item["agent_output"] = {
                "summary": summary,
                "source": source,
                "truncated": summary_truncated,
            }
            streamed_content = self._agent_content.pop(iteration, None)
            if streamed_content and streamed_content.get("truncated"):
                self._text_truncations = max(0, self._text_truncations - 1)
        self._upsert_iteration_entry(_TraceEntry(order, "iterations", 5, item))

    def _new_tool_state(self, payload: Mapping[str, Any], order: int) -> dict[str, Any]:
        state: dict[str, Any] = {"_order": order, "status": "running"}
        self._merge_tool_fields(state, payload)
        return state

    def _merge_tool_fields(self, state: dict[str, Any], payload: Mapping[str, Any]) -> None:
        tool_name = self._text(payload.get("tool_name"), limit=160)
        if tool_name:
            state["tool_name"] = tool_name
        tool_call_id = self._text(payload.get("tool_call_id"), limit=200)
        if tool_call_id:
            state["tool_call_id"] = tool_call_id
        iteration = _non_negative_int(payload.get("iteration"))
        if iteration is not None:
            state["iteration"] = iteration

        tool_input = payload.get("input")
        if not isinstance(tool_input, Mapping):
            tool_input = {}
        ontology_id = payload.get("ontology_id") or tool_input.get("ontology_id")
        entity = payload.get("entity") or tool_input.get("entity")
        query = payload.get("query") or tool_input.get("query")
        provider = payload.get("database_provider") or payload.get("provider")
        self._set_text(state, "ontology_id", ontology_id, limit=240)
        self._set_text(state, "entity", entity, limit=240)
        self._set_text(state, "database_provider", provider, limit=120)
        if str(state.get("tool_name") or "").strip() == "ontology_context":
            self._set_text(
                state,
                "resource",
                payload.get("resource") or tool_input.get("resource"),
                limit=80,
            )
            self._set_text(
                state,
                "key",
                payload.get("key") or tool_input.get("key"),
                limit=240,
            )
        query_summary, query_summary_truncated = self._text_with_truncation(query)
        if query_summary:
            state["query_summary"] = query_summary
            state["_query_summary_truncated"] = query_summary_truncated
            state.setdefault("query_digest", _query_digest(query_summary))

    def _merge_result_metadata(self, state: dict[str, Any], result: Mapping[str, Any]) -> None:
        candidates = [result]
        for key in ("result", "metadata"):
            nested = result.get(key)
            if isinstance(nested, Mapping):
                candidates.append(nested)

        for candidate in candidates:
            if not state.get("database_provider"):
                provider = candidate.get("provider") or candidate.get("database_provider")
                self._set_text(state, "database_provider", provider, limit=120)
            if not state.get("entity"):
                self._set_text(state, "entity", candidate.get("entity"), limit=240)
            if not state.get("query_summary"):
                query, query_truncated = self._text_with_truncation(candidate.get("query"))
                if query:
                    state["query_summary"] = query
                    state["_query_summary_truncated"] = query_truncated
                    state.setdefault("query_digest", _query_digest(query))
            if "record_count" not in state:
                for key in ("result_count", "row_count", "record_count", "total_count", "count"):
                    count = _non_negative_int(candidate.get(key))
                    if count is not None:
                        state["record_count"] = count
                        break
                else:
                    data = candidate.get("data")
                    if isinstance(data, list):
                        state["record_count"] = len(data)
            error_code = candidate.get("error_code")
            if error_code and not state.get("error_code"):
                self._set_text(state, "error_code", error_code, limit=160)
            retry_count = _non_negative_int(
                candidate.get("retry_count")
                if candidate.get("retry_count") is not None
                else candidate.get("retries")
            )
            if retry_count is not None:
                state["retry_count"] = max(retry_count, int(state.get("retry_count") or 0))
            projected_query_digest = self._text(candidate.get("query_digest"), limit=200)
            if projected_query_digest:
                state["query_digest"] = projected_query_digest
            if candidate.get("has_evidence") is True:
                state["_has_evidence"] = True
            if str(state.get("tool_name") or "").strip() == "business_data_search":
                executed_steps = self._public_executed_steps(candidate.get("executed_steps"))
                if executed_steps:
                    state["executed_steps"] = executed_steps

        evidence = result.get("evidence")
        query_digest = ""
        if isinstance(evidence, list):
            state["_has_evidence"] = bool(evidence)
            for item in evidence:
                if not isinstance(item, Mapping):
                    continue
                if not query_digest:
                    query_digest = self._text(item.get("query_digest"), limit=200)
        if query_digest:
            state["query_digest"] = query_digest

        warnings = result.get("warnings")
        if isinstance(warnings, list):
            current = list(state.get("_warnings") or [])
            for warning in warnings[:_WARNING_LIMIT]:
                text = self._text(warning)
                if text and text not in current:
                    current.append(text)
            state["_warnings"] = current[:_WARNING_LIMIT]

    def _public_executed_steps(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, str]] = []
        for raw_step in value[:_EXECUTED_STEP_LIMIT]:
            if not isinstance(raw_step, Mapping):
                continue
            step: dict[str, str] = {}
            tool = self._text(raw_step.get("tool"), limit=120)
            summary = self._text(
                raw_step.get("summary"),
                limit=_EXECUTED_STEP_SUMMARY_LIMIT,
            )
            if tool:
                step["tool"] = tool
            if summary:
                step["summary"] = summary
            if step:
                result.append(step)
        return result

    def _register_attempt(self, state: Mapping[str, Any]) -> int:
        if not state.get("query_digest"):
            return 0
        fingerprint_parts = (
            state.get("tool_name"),
            state.get("ontology_id"),
            state.get("entity"),
            state.get("query_digest"),
        )
        fingerprint = "\x1f".join(str(value or "") for value in fingerprint_parts)
        prior_attempts = self._attempt_counts.get(fingerprint, 0)
        self._attempt_counts[fingerprint] = prior_attempts + 1
        return prior_attempts

    def _error_text(self, value: Any) -> str:
        text = self._text(value)
        if not text:
            return ""
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if first_line.lower().startswith("traceback"):
            return "Tool execution failed"
        return first_line

    def _remember_tool_iteration(self, state: Mapping[str, Any]) -> None:
        iteration = _non_negative_int(state.get("iteration"))
        self._seen_iterations.add(iteration if iteration is not None else 1)

    def _public_agent_output(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        summary, summary_truncated = self._text_with_truncation(value.get("summary"))
        if not summary:
            return {}
        source = self._text(value.get("source"), limit=80)
        if not source:
            source = _legacy_agent_output_source(value.get("type"))
        result: dict[str, Any] = {
            "summary": summary,
            "source": source or "iteration_event",
            "truncated": bool(value.get("truncated")) or summary_truncated,
        }
        return result

    def _upsert_iteration_entry(self, entry: _TraceEntry) -> None:
        iteration = _non_negative_int(entry.item.get("iteration"))
        normalized_iteration = iteration if iteration is not None else 1
        item = {**entry.item, "iteration": normalized_iteration}
        self._seen_iterations.add(normalized_iteration)
        for index, existing in enumerate(self._entries):
            if existing.kind != "iterations":
                continue
            existing_iteration = _non_negative_int(existing.item.get("iteration"))
            if (existing_iteration if existing_iteration is not None else 1) != normalized_iteration:
                continue
            merged = copy.deepcopy(existing.item)
            merged.update(copy.deepcopy(item))
            self._entries[index] = _TraceEntry(
                order=max(existing.order, entry.order),
                kind="iterations",
                priority=max(existing.priority, entry.priority),
                item=merged,
            )
            return
        self._append_entry(
            _TraceEntry(entry.order, entry.kind, entry.priority, copy.deepcopy(item))
        )

    def _append_entry(self, entry: _TraceEntry) -> None:
        self._entries.append(entry)
        if len(self._entries) <= self.max_events:
            return
        remove_index = min(
            range(len(self._entries)),
            key=lambda index: (self._entries[index].priority, self._entries[index].order),
        )
        removed = self._entries.pop(remove_index)
        self._omitted[removed.kind] = self._omitted.get(removed.kind, 0) + 1

    def _pending_entries(self) -> list[_TraceEntry]:
        entries: list[_TraceEntry] = []
        for state in self._pending_tools.values():
            entries.append(
                _TraceEntry(
                    int(state.get("_order") or 0),
                    "tools",
                    3,
                    self._public_tool_item(state),
                )
            )
        return entries

    def _agent_content_entries(self) -> list[_TraceEntry]:
        entries: list[_TraceEntry] = []
        for iteration, state in self._agent_content.items():
            summary = _safe_text(state.get("text"))
            if not summary:
                continue
            entries.append(
                _TraceEntry(
                    int(state.get("order") or 0),
                    "agent_outputs",
                    3,
                    {
                        "iteration": iteration,
                        "summary": summary,
                        "source": "agent_content",
                        "truncated": bool(state.get("truncated")),
                    },
                )
            )
        return entries

    def _public_tool_item(self, state: Mapping[str, Any]) -> dict[str, Any]:
        allowed = (
            "tool_name",
            "tool_call_id",
            "status",
            "iteration",
            "elapsed_ms",
            "ontology_id",
            "resource",
            "key",
            "entity",
            "query_summary",
            "database_provider",
            "record_count",
            "executed_steps",
            "query_digest",
            "error_code",
            "error_summary",
            "retry_count",
            "warning_count",
            "_query_summary_truncated",
        )
        item = {key: copy.deepcopy(state[key]) for key in allowed if state.get(key) is not None}
        warnings = state.get("_warnings")
        if isinstance(warnings, list):
            item["_warnings"] = list(warnings)
        return item

    def _tool_key(
        self,
        payload: Mapping[str, Any],
        order: int,
        *,
        prefer_pending: bool,
    ) -> str:
        tool_call_id = str(payload.get("tool_call_id") or "").strip()
        if tool_call_id:
            return "id:{}".format(tool_call_id)
        tool_name = str(payload.get("tool_name") or "").strip()
        if prefer_pending:
            for key in reversed(self._pending_order):
                state = self._pending_tools.get(key)
                if state and state.get("tool_name") == tool_name:
                    return key
        return "anonymous:{}:{}".format(tool_name or "tool", order)

    def _text(self, value: Any, *, limit: int | None = None) -> str:
        return self._text_with_truncation(value, limit=limit)[0]

    def _text_with_truncation(
        self,
        value: Any,
        *,
        limit: int | None = None,
    ) -> tuple[str, bool]:
        text = _safe_text(value)
        effective_limit = self.max_text_chars if limit is None else min(self.max_text_chars, limit)
        if len(text) > effective_limit:
            self._text_truncations += 1
            return text[:effective_limit], True
        return text, False

    def _set_text(
        self,
        target: dict[str, Any],
        key: str,
        value: Any,
        *,
        limit: int | None = None,
    ) -> None:
        text = self._text(value, limit=limit)
        if text:
            target[key] = text

    def _parse_mapping(self, value: Any) -> Mapping[str, Any] | None:
        if isinstance(value, Mapping):
            return value
        if not isinstance(value, str) or not value or len(value) > self._parse_chars_limit:
            return None
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, Mapping) else None

    @staticmethod
    def _select_entries(entries: list[_TraceEntry], limit: int) -> list[_TraceEntry]:
        if len(entries) <= limit:
            return sorted(entries, key=lambda entry: entry.order)
        selected = sorted(entries, key=lambda entry: (-entry.priority, -entry.order))[:limit]
        return sorted(selected, key=lambda entry: entry.order)

    def _build_snapshot(
        self,
        entries: list[_TraceEntry],
        totals: dict[str, int],
        omitted: dict[str, int],
        truncated: bool,
    ) -> dict[str, Any]:
        iterations_by_id: dict[int, dict[str, Any]] = {}
        iteration_order: dict[int, int] = {}
        agent_output_by_iteration: dict[int, dict[str, Any]] = {}
        tools_by_iteration: dict[int, list[dict[str, Any]]] = {}
        warnings: list[dict[str, Any]] = []
        for entry in sorted(entries, key=lambda current: current.order):
            item = copy.deepcopy(entry.item)
            if entry.kind == "iterations":
                iteration = _trace_iteration(item)
                item["iteration"] = iteration
                iterations_by_id[iteration] = item
                iteration_order.setdefault(iteration, entry.order)
            elif entry.kind == "agent_outputs":
                iteration = _trace_iteration(item)
                item.pop("iteration", None)
                agent_output_by_iteration[iteration] = item
                iteration_order.setdefault(iteration, entry.order)
            elif entry.kind == "tools":
                iteration = _trace_iteration(item)
                tool_warnings = item.pop("_warnings", [])
                item.pop("iteration", None)
                tool_name = item.pop("tool_name", None)
                if tool_name:
                    item["name"] = tool_name
                tools_by_iteration.setdefault(iteration, []).append(item)
                iteration_order.setdefault(iteration, entry.order)
                for warning in tool_warnings:
                    warning_item = {"message": warning}
                    warning_item["iteration"] = iteration
                    if tool_name:
                        warning_item["tool_name"] = tool_name
                    if item.get("tool_call_id") is not None:
                        warning_item["tool_call_id"] = item["tool_call_id"]
                    warnings.append(warning_item)

        iteration_ids = (
            set(iterations_by_id)
            | set(agent_output_by_iteration)
            | set(tools_by_iteration)
        )
        iterations: list[dict[str, Any]] = []
        for iteration in sorted(iteration_ids, key=lambda value: (value, iteration_order.get(value, 0))):
            item = iterations_by_id.get(iteration, {"iteration": iteration})
            calls = tools_by_iteration.get(iteration, [])
            agent_content = agent_output_by_iteration.get(iteration)
            agent_output = self._build_agent_output(
                item.get("agent_output"),
                agent_content,
                calls,
            )
            if agent_output:
                item["agent_output"] = agent_output
            else:
                item.pop("agent_output", None)
            for call in calls:
                call.pop("_query_summary_truncated", None)
            item.pop("tool_calls_count", None)
            item.pop("tool_names", None)
            if calls:
                failed = sum(1 for call in calls if _tool_failed(call))
                item["tools"] = {
                    "count": len(calls),
                    "succeeded": len(calls) - failed,
                    "failed": failed,
                    "calls": calls,
                }
            iterations.append(item)

        return {
            "schema_version": "5.0",
            "level": "summary",
            "truncated": bool(truncated),
            "iterations": iterations,
            "warnings": warnings,
            "totals": totals,
            "omitted": {key: value for key, value in omitted.items() if value},
        }

    def _build_agent_output(
        self,
        existing: Any,
        agent_content: Any,
        calls: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        result = self._public_agent_output(existing)
        if result.get("source") in {"final_output", "error", "cancel_reason"}:
            return result
        content_result = self._public_agent_output(agent_content)
        if content_result:
            return content_result
        if result:
            return result
        if calls:
            query_call = next(
                (
                    call
                    for call in reversed(calls)
                    if str(call.get("query_summary") or "").strip()
                ),
                None,
            )
            if query_call is not None:
                return {
                    "summary": str(query_call.get("query_summary") or "").strip(),
                    "source": "tool_query_summary",
                    "truncated": bool(query_call.get("_query_summary_truncated")),
                }
            else:
                names = [str(call.get("name") or "").strip() for call in calls]
                names = [name for name in names if name]
                unique_names = list(dict.fromkeys(names))
                if unique_names:
                    summary = "调用工具：{}".format("、".join(unique_names))
                    summary_truncated = len(summary) > self.max_text_chars
                    return {
                        "summary": summary[:self.max_text_chars],
                        "source": "tool_name",
                        "truncated": summary_truncated,
                    }
        return {}

    @staticmethod
    def _json_size(value: Mapping[str, Any]) -> int:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )


def _terminal_summary_source(
    payload: Mapping[str, Any],
    status: str,
) -> tuple[Any, str]:
    fields = (
        ("final_output", "final_output"),
        ("error", "error"),
        ("cancel_reason", "cancel_reason"),
    )
    if status not in {"", "success", "awaiting_input"}:
        fields = (fields[1], fields[2], fields[0])
    for field, source in fields:
        value = payload.get(field)
        if value is not None and str(value).strip():
            return value, source
    return "", "final_output"


def _legacy_agent_output_source(value: Any) -> str:
    output_type = str(value or "").strip().lower()
    return {
        "final_output": "final_output",
        "error": "error",
        "awaiting_input": "final_output",
        "tool_decision": "iteration_event",
    }.get(output_type, "")


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(_redact_value(value), ensure_ascii=False, separators=(",", ":"))
    text = str(value).strip()
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _JWT_RE.sub("[REDACTED_JWT]", text)
    text = _URI_CREDENTIAL_RE.sub(r"\1[REDACTED]@", text)
    return _SECRET_ASSIGNMENT_RE.sub(r"\1=[REDACTED]", text)


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if any(marker in normalized_key for marker in _SENSITIVE_KEYS):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = _redact_value(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value[:16]]
    return value


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and math.isfinite(value):
        return max(0, int(value))
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def _trace_iteration(item: Mapping[str, Any]) -> int:
    iteration = _non_negative_int(item.get("iteration"))
    return iteration if iteration is not None else 1


def _tool_failed(item: Mapping[str, Any]) -> bool:
    status = str(item.get("status") or "").strip().lower()
    return status in {"error", "cancelled", "canceled", "failed"} or bool(
        item.get("error_code") or item.get("error_summary")
    )


def _compact_iteration_timing(payload: Mapping[str, Any]) -> dict[str, int]:
    total_ms = _non_negative_int(payload.get("total_elapsed_ms")) or 0
    model_ms = _non_negative_int(payload.get("model_elapsed_ms"))
    if model_ms is None:
        model_ms = _non_negative_int(payload.get("llm_wall_elapsed_ms"))
    if model_ms is None:
        model_ms = _non_negative_int(payload.get("llm_elapsed_ms")) or 0
    tools_ms = _non_negative_int(payload.get("tools_elapsed_ms")) or 0
    setup_ms = _non_negative_int(payload.get("setup_elapsed_ms"))
    if setup_ms is None:
        setup_ms = max(0, total_ms - model_ms - tools_ms)
    values = {
        "total_elapsed_ms": total_ms,
        "model_elapsed_ms": model_ms,
        "tools_elapsed_ms": tools_ms,
        "setup_elapsed_ms": setup_ms,
    }
    return {key: values[key] for key in _TIMING_FIELDS}


def _query_digest(query: str) -> str:
    return "sha256:{}".format(hashlib.sha256(query.encode("utf-8")).hexdigest())


def _dedupe_text_list(values: list[Any], limit: int) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result
