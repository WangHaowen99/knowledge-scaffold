"""Async reflection worker that derives procedural memories from completed turns.

Design invariants (see plan section 8.1):

- The worker is strictly fire-and-forget. It never blocks the SSE stream or the
  next user turn.
- Submissions take a snapshot of the turn state at submission time; subsequent
  session mutations cannot affect the in-flight reflection.
- Per-user serialization ensures that two concurrent turns for the same user do
  not race on memory writes.
- Any failure is logged and swallowed; reflection must never surface errors to
  the user.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from llm import LLMPurpose, get_llm_router
from utils.logging import get_logger
from agent.evolution.proposal_writer import append_operation_proposal_with_relations
from agent.evolution.registry import get_proposal_buffer

logger = get_logger("evolution.reflection")


_EXECUTION_SUMMARY_PATTERN = re.compile(
    r"<execution_summary>\s*(.*?)\s*</execution_summary>",
    re.DOTALL | re.IGNORECASE,
)

_SUMMARY_FIELD_PATTERN = re.compile(
    r"^\s*(?P<key>operation|status|evidence)\s*[:：]\s*(?P<value>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_REFLECTION_MAX_TOOL_EVENTS = 40
_REFLECTION_MAX_TOOL_ARG_CHARS = 400
_REFLECTION_MAX_TOOL_RESULT_CHARS = 600
_REFLECTION_MAX_FINAL_OUTPUT_CHARS = 2000
_REFLECTION_MAX_OUTPUT_TOKENS = 1500


class ReflectionSchemaEntry(BaseModel):
    entity_key: str = Field(..., description="Entity name from objects-list.md")
    content: str = Field(..., description="Concrete schema facts (actual field names, value formats).")


class ReflectionOutput(BaseModel):
    operation_executed: Optional[str] = Field(
        None,
        description="Canonical operation name from operations-list.md, or null if none.",
    )
    compliance_score: float = Field(0.0, ge=0.0, le=1.0)
    efficiency_score: float = Field(0.0, ge=0.0, le=1.0)
    new_procedure: Optional[str] = Field(
        None,
        description="Reusable success template; null if the turn did not converge cleanly.",
    )
    new_pitfalls: List[str] = Field(
        default_factory=list,
        description="Concrete mistakes or dead-ends from this turn that future runs should avoid.",
    )
    new_schemas: List[ReflectionSchemaEntry] = Field(default_factory=list)
    notes: Optional[str] = None


@dataclass
class ReflectionSnapshot:
    """Immutable input handed to the worker. Never mutated after submission."""

    run_id: str
    session_id: str
    turn_id: int
    user_id: str
    workspace_id: Optional[str]
    ontology_id: Optional[str]
    operation_hint: Optional[str]
    status: str
    user_request: str
    final_output: str
    execution_summary: Optional[Dict[str, str]]
    tool_events: List[Dict[str, Any]]
    iteration_count: int
    duration_ms: int
    feedback: Optional[Dict[str, Any]] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def parse_execution_summary(text: Optional[str]) -> Optional[Dict[str, str]]:
    """Extract the ``<execution_summary>`` block from the agent's final output.

    Returns a dict with keys among {operation, status, evidence} or None if the
    block is absent or empty.
    """

    if not text:
        return None
    match = _EXECUTION_SUMMARY_PATTERN.search(text)
    if not match:
        return None
    body = match.group(1).strip()
    if not body:
        return None
    out: Dict[str, str] = {}
    for m in _SUMMARY_FIELD_PATTERN.finditer(body):
        key = m.group("key").lower()
        value = m.group("value").strip()
        if value:
            out[key] = value
    if not out:
        out["raw"] = body
    return out


def collect_tool_events(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Walk the OpenAI-format history and emit a compact (call, result) list."""

    if not messages:
        return []

    pending: Dict[str, Dict[str, Any]] = {}
    events: List[Dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for call in msg.get("tool_calls") or []:
                call_id = str(call.get("id") or "").strip()
                if not call_id:
                    continue
                fn = (call.get("function") or {})
                args_raw = fn.get("arguments")
                if isinstance(args_raw, (dict, list)):
                    try:
                        args_text = json.dumps(args_raw, ensure_ascii=False)
                    except Exception:
                        args_text = str(args_raw)
                else:
                    args_text = str(args_raw or "")
                if len(args_text) > _REFLECTION_MAX_TOOL_ARG_CHARS:
                    args_text = args_text[:_REFLECTION_MAX_TOOL_ARG_CHARS] + "…"
                pending[call_id] = {
                    "name": str(fn.get("name") or "").strip(),
                    "arguments": args_text,
                    "result": None,
                    "error": None,
                }
        elif role == "tool":
            call_id = str(msg.get("tool_call_id") or "").strip()
            if not call_id:
                continue
            content = msg.get("content")
            if isinstance(content, (dict, list)):
                try:
                    content_text = json.dumps(content, ensure_ascii=False)
                except Exception:
                    content_text = str(content)
            else:
                content_text = str(content or "")
            if len(content_text) > _REFLECTION_MAX_TOOL_RESULT_CHARS:
                content_text = content_text[:_REFLECTION_MAX_TOOL_RESULT_CHARS] + "…"
            entry = pending.pop(call_id, None)
            if entry is None:
                entry = {"name": "", "arguments": "", "result": None, "error": None}
            entry["result"] = content_text
            events.append(entry)

    for entry in pending.values():
        events.append(entry)

    if len(events) > _REFLECTION_MAX_TOOL_EVENTS:
        events = events[-_REFLECTION_MAX_TOOL_EVENTS:]
    return events


def build_snapshot(
    *,
    run_id: str,
    session_id: str,
    turn_id: int,
    user_id: str,
    ontology_id: Optional[str],
    status: str,
    user_request: str,
    final_output: Optional[str],
    messages: List[Dict[str, Any]],
    iteration_count: int,
    duration_ms: int,
    operation_hint: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> ReflectionSnapshot:
    final_output_text = str(final_output or "")
    summary = parse_execution_summary(final_output_text)
    op_hint = operation_hint
    if not op_hint and summary:
        op_hint = summary.get("operation") or None
    events = collect_tool_events(messages)
    truncated_final = final_output_text
    if len(truncated_final) > _REFLECTION_MAX_FINAL_OUTPUT_CHARS:
        truncated_final = truncated_final[:_REFLECTION_MAX_FINAL_OUTPUT_CHARS] + "…"
    return ReflectionSnapshot(
        run_id=str(run_id or ""),
        session_id=str(session_id or ""),
        turn_id=int(turn_id or 0),
        user_id=str(user_id or "anonymous"),
        workspace_id=(str(workspace_id).strip() or None) if workspace_id else None,
        ontology_id=(str(ontology_id).strip() or None) if ontology_id else None,
        operation_hint=(str(op_hint).strip() or None) if op_hint else None,
        status=str(status or "").strip() or "unknown",
        user_request=str(user_request or "")[:1000],
        final_output=truncated_final,
        execution_summary=summary,
        tool_events=events,
        iteration_count=int(iteration_count or 0),
        duration_ms=int(duration_ms or 0),
    )


REFLECTION_SYSTEM_PROMPT = """\
You distill procedural knowledge from one completed agent turn.

The agent executes ontology operations defined in markdown (`# 逻辑描述`). Your
job is to extract reusable lessons that will help future runs of the SAME
operation improve compliance (follow the spec exactly) and efficiency (fewer
tool calls, fewer iterations).

Rules:
- Only output knowledge that is concrete, reusable, and grounded in this turn.
- `operation_executed` must be the canonical operation name from
  operations-list.md when identifiable.
- `new_procedure` captures a successful, repeatable pattern (e.g., "this op is
  script-backed — call execute_script with args X").
- `new_procedure` MUST be a string or null. Do not use an array for this field.
- `new_pitfalls` captures specific dead-ends actually observed (e.g.,
  "business_data_search returned empty when using field `cycle`; the real
  column is `cycle_no`").
- `new_schemas` captures corrected entity schema facts the turn discovered
  (actual field names, value formats).
- `new_schemas` MUST be an array of objects:
  `{"entity_key": "TargetItem", "content": "TargetItem 实体包含字段..."}`.
  Do not use bare strings for this field.
- Do not use markdown fences. Output raw JSON only.
- Field types are strict. Invalid types such as `new_procedure: []` or
  `new_schemas: ["..."]` are invalid output.
- Do NOT invent anything. If no durable lesson was learned, return empty
  arrays / null.
- Keep each field terse. One paragraph or a short bullet list max.
- Write in the same language the agent used (typically Chinese).

Return JSON with exactly this shape:
{
  "operation_executed": "操作名称或 null",
  "compliance_score": 0.0,
  "efficiency_score": 0.0,
  "new_procedure": "string or null",
  "new_pitfalls": ["string"],
  "new_schemas": [{"entity_key": "实体名", "content": "schema fact"}],
  "notes": "string or null"
}
"""


REFLECTION_USER_TEMPLATE = """\
[Turn context]
status: {status}
iteration_count: {iteration_count}
duration_ms: {duration_ms}
ontology_id: {ontology_id}
operation_hint: {operation_hint}
user_feedback: {feedback}

[User request]
{user_request}

[Agent execution_summary]
{execution_summary}

[Final output]
{final_output}

[Tool events ({event_count})]
{tool_events}

Respond ONLY with the structured JSON matching the required schema.
"""


def _format_tool_events(events: List[Dict[str, Any]]) -> str:
    if not events:
        return "(none)"
    lines: List[str] = []
    for idx, event in enumerate(events, 1):
        lines.append(
            "{idx}. {name}({args}) -> {result}".format(
                idx=idx,
                name=event.get("name") or "?",
                args=event.get("arguments") or "",
                result=event.get("result") or "<no result>",
            )
        )
    return "\n".join(lines)


def _format_execution_summary(summary: Optional[Dict[str, str]]) -> str:
    if not summary:
        return "(not provided)"
    try:
        return json.dumps(summary, ensure_ascii=False)
    except Exception:
        return str(summary)


class ReflectionWorker:
    """Background reflector. One instance per orchestrator.

    Uses a shared thread pool for concurrency across users and a per-user lock to
    serialize writes for any single user.
    """

    def __init__(
        self,
        *,
        max_workers: int = 2,
    ) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="reflection",
        )
        self._user_locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def submit(self, snapshot: ReflectionSnapshot) -> None:
        """Fire-and-forget reflection submission. Never raises."""

        if snapshot is None:
            return
        try:
            self._executor.submit(self._safe_run, snapshot)
        except Exception:
            logger.exception(
                "Failed to submit reflection for run=%s turn=%s",
                snapshot.run_id,
                snapshot.turn_id,
            )

    def shutdown(self, wait: bool = False) -> None:
        try:
            self._executor.shutdown(wait=wait, cancel_futures=True)
        except TypeError:  # Python < 3.9
            self._executor.shutdown(wait=wait)
        except Exception:
            logger.exception("reflection worker shutdown failed")

    def _user_lock(self, user_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._user_locks.get(user_id)
            if lock is None:
                lock = threading.Lock()
                self._user_locks[user_id] = lock
            return lock

    def _safe_run(self, snapshot: ReflectionSnapshot) -> None:
        started = time.perf_counter()
        logger.info(
            "reflection started run=%s session=%s turn=%s user=%s workspace=%s ontology=%s operation_hint=%s status=%s",
            snapshot.run_id,
            snapshot.session_id,
            snapshot.turn_id,
            snapshot.user_id,
            snapshot.workspace_id,
            snapshot.ontology_id,
            snapshot.operation_hint,
            snapshot.status,
        )
        try:
            with self._user_lock(snapshot.user_id):
                result = self._reflect_and_store(snapshot)
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "reflection finished run=%s session=%s turn=%s outcome=%s written=%s duration_ms=%s",
                snapshot.run_id,
                snapshot.session_id,
                snapshot.turn_id,
                result.get("outcome"),
                result.get("written"),
                duration_ms,
            )
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "reflection failed run=%s turn=%s: %s",
                snapshot.run_id,
                snapshot.turn_id,
                exc,
            )
            logger.info(
                "reflection finished run=%s session=%s turn=%s outcome=error duration_ms=%s",
                snapshot.run_id,
                snapshot.session_id,
                snapshot.turn_id,
                duration_ms,
            )

    def _reflect_and_store(self, snapshot: ReflectionSnapshot) -> Dict[str, Any]:
        try:
            task_llm = get_llm_router().get_service(LLMPurpose.TASK)
        except Exception:
            logger.warning(
                "reflection skipped run=%s session=%s turn=%s reason=task_llm_unavailable",
                snapshot.run_id,
                snapshot.session_id,
                snapshot.turn_id,
                exc_info=True,
            )
            return {"outcome": "skipped_task_llm_unavailable", "written": 0}
        if not task_llm or not getattr(task_llm, "is_configured", True):
            logger.info(
                "reflection skipped run=%s session=%s turn=%s reason=task_llm_not_configured",
                snapshot.run_id,
                snapshot.session_id,
                snapshot.turn_id,
            )
            return {"outcome": "skipped_task_llm_not_configured", "written": 0}
        logger.debug(
            "reflection preparing prompt run=%s session=%s turn=%s ontology=%s operation_hint=%s tool_events=%s has_summary=%s final_output_chars=%s feedback=%s",
            snapshot.run_id,
            snapshot.session_id,
            snapshot.turn_id,
            snapshot.ontology_id,
            snapshot.operation_hint,
            len(snapshot.tool_events or []),
            bool(snapshot.execution_summary),
            len(snapshot.final_output or ""),
            bool(snapshot.feedback),
        )

        messages = [
            {"role": "system", "content": REFLECTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": REFLECTION_USER_TEMPLATE.format(
                    status=snapshot.status,
                    iteration_count=snapshot.iteration_count,
                    duration_ms=snapshot.duration_ms,
                    ontology_id=snapshot.ontology_id or "(none)",
                    operation_hint=snapshot.operation_hint or "(none)",
                    feedback=(
                        json.dumps(snapshot.feedback, ensure_ascii=False)
                        if snapshot.feedback
                        else "(none)"
                    ),
                    user_request=snapshot.user_request or "(empty)",
                    execution_summary=_format_execution_summary(snapshot.execution_summary),
                    final_output=snapshot.final_output or "(empty)",
                    tool_events=_format_tool_events(snapshot.tool_events),
                    event_count=len(snapshot.tool_events),
                ),
            },
        ]

        try:
            logger.debug(
                "reflection calling task_llm run=%s response_model=%s max_tokens=%s",
                snapshot.run_id,
                ReflectionOutput.__name__,
                _REFLECTION_MAX_OUTPUT_TOKENS,
            )
            structured = task_llm.call(
                messages=messages,
                response_model=ReflectionOutput,
                enable_reasoning=False,
                temperature=0.2,
                max_tokens=_REFLECTION_MAX_OUTPUT_TOKENS,
                request_label="reflection",
            )
        except Exception as exc:
            logger.warning(
                "reflection LLM call failed run=%s: %s",
                snapshot.run_id,
                exc,
            )
            return {"outcome": "skipped_llm_call_failed", "written": 0}

        output = getattr(structured, "data", None)
        if not isinstance(output, ReflectionOutput):
            logger.info(
                "reflection skipped run=%s session=%s turn=%s reason=no_structured_output",
                snapshot.run_id,
                snapshot.session_id,
                snapshot.turn_id,
            )
            return {"outcome": "skipped_no_structured_output", "written": 0}

        logger.debug(
            "reflection output parsed run=%s operation=%s compliance=%.2f efficiency=%.2f procedure=%s pitfalls=%s schemas=%s",
            snapshot.run_id,
            output.operation_executed,
            output.compliance_score,
            output.efficiency_score,
            bool(output.new_procedure),
            len(output.new_pitfalls or []),
            len(output.new_schemas or []),
        )

        written = self._persist_outputs(snapshot, output)
        return {"outcome": "completed", "written": written}

    def _persist_outputs(
        self,
        snapshot: ReflectionSnapshot,
        output: ReflectionOutput,
    ) -> int:
        op_key = (output.operation_executed or snapshot.operation_hint or "").strip() or None
        ontology_id = snapshot.ontology_id

        written = 0
        logger.debug(
            "reflection persisting output run=%s ontology=%s operation=%s procedure=%s pitfalls=%s schemas=%s",
            snapshot.run_id,
            ontology_id,
            op_key,
            bool(output.new_procedure),
            len(output.new_pitfalls or []),
            len(output.new_schemas or []),
        )
        if output.new_procedure and ontology_id and op_key:
            written += self._safe_store(
                snapshot,
                content=output.new_procedure,
                kind="procedure",
                ontology_id=ontology_id,
                operation_key=op_key,
            )

        if ontology_id and op_key:
            for pitfall in output.new_pitfalls or []:
                pitfall = (pitfall or "").strip()
                if not pitfall:
                    continue
                written += self._safe_store(
                    snapshot,
                    content=pitfall,
                    kind="pitfall",
                    ontology_id=ontology_id,
                    operation_key=op_key,
                )

        if ontology_id and op_key:
            for schema_entry in output.new_schemas or []:
                entity = (schema_entry.entity_key or "").strip()
                body = (schema_entry.content or "").strip()
                if not entity or not body:
                    continue
                # Schema corrections are scoped to (ontology, operation, entity).
                # operation_key partitions storage so an entity used by many ops
                # accumulates separate aggregates per op (its meaning may differ
                # by context).
                written += self._safe_store(
                    snapshot,
                    content=body,
                    kind="schema",
                    ontology_id=ontology_id,
                    operation_key=op_key,
                    entity_key=entity,
                )

        logger.debug(
            "reflection persist finished run=%s ontology=%s operation=%s written=%s",
            snapshot.run_id,
            ontology_id,
            op_key,
            written,
        )
        return written

    def _safe_store(
        self,
        snapshot: ReflectionSnapshot,
        *,
        content: str,
        kind: str,
        ontology_id: Optional[str] = None,
        operation_key: Optional[str] = None,
        entity_key: Optional[str] = None,
    ) -> int:
        """Append a passive proposal to the operation-evolution buffer."""
        if kind not in {"procedure", "pitfall", "schema"}:
            logger.debug(
                "reflection refused store run=%s kind=%s reason=unsupported_kind",
                snapshot.run_id,
                kind,
            )
            return 0
        normalized = (content or "").strip()
        if len(normalized) < 8:
            logger.debug(
                "reflection refused store run=%s kind=%s reason=content_too_short chars=%s",
                snapshot.run_id,
                kind,
                len(normalized),
            )
            return 0
        if not (ontology_id and operation_key):
            logger.debug(
                "reflection refused store run=%s kind=%s reason=missing_scope ontology=%s operation=%s",
                snapshot.run_id,
                kind,
                ontology_id,
                operation_key,
            )
            return 0
        try:
            buffer = get_proposal_buffer()
            append_operation_proposal_with_relations(
                buffer=buffer,
                workspace_id=snapshot.workspace_id,
                ontology_id=ontology_id,
                operation_key=operation_key,
                kind=kind,
                content=normalized,
                source="passive",
                entity_key=entity_key,
                run_id=snapshot.run_id,
                user_id=snapshot.user_id,
            )
            logger.debug(
                "reflection appended proposal run=%s ontology=%s operation=%s entity=%s kind=%s chars=%s",
                snapshot.run_id,
                ontology_id,
                operation_key,
                entity_key,
                kind,
                len(normalized),
            )
            return 1
        except Exception as exc:
            logger.warning(
                "reflection buffer.append failed kind=%s op=%s entity=%s: %s",
                kind,
                operation_key,
                entity_key,
                exc,
            )
            return 0
