"""Tool-level SSE event emission helpers."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from integration.observability.events import RuntimeEvent, RuntimeEventType
from utils.streaming import emit_agent_event
from utils.logging import get_logger
from utils.security.redaction import redact_sensitive_text, redact_sensitive_value
from agent.tools.base import ToolContext

logger = get_logger("tools.events")

ToolOutputArtifactWriter = Callable[[Optional[str], str], Optional[Dict[str, Any]]]
_tool_output_artifact_writer_var: ContextVar[Optional[ToolOutputArtifactWriter]] = ContextVar(
    "tool_output_artifact_writer",
    default=None,
)

_SUBAGENT_LINEAGE_FIELDS = (
    "subagent_id",
    "task_id",
    "group_id",
    "depth",
    "parent_subagent_id",
    "root_tool_call_id",
)

_WEB_SEARCH_RESULT_LIMIT = 8
_WEB_SEARCH_SCAN_LIMIT = 32
_WEB_SEARCH_URL_LIMIT = 2048
_BUSINESS_EXECUTED_STEP_LIMIT = 8
_BUSINESS_EXECUTED_STEP_SUMMARY_LIMIT = 300
_FULL_TRACE_OUTPUT_TOOLS = {"ontology_context", "business_data_search"}


def _bounded_text(value: Any, limit: int) -> str:
    return redact_sensitive_text(value).strip()[:limit]


def _finite_number(value: Any) -> Optional[Any]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(float(value)) else None


def _web_search_url_key(parsed_url: Any) -> Optional[tuple]:
    scheme = str(parsed_url.scheme or "").lower()
    hostname = str(parsed_url.hostname or "").lower()
    try:
        port = parsed_url.port
    except ValueError:
        return None
    if (scheme, port) in {("http", 80), ("https", 443)}:
        port = None
    return (
        scheme,
        hostname,
        port,
        parsed_url.path or "/",
        parsed_url.query or "",
    )


def _build_web_search_projection(raw_output: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(raw_output)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
        return None

    results = []
    seen_urls = set()
    for item in parsed["results"][:_WEB_SEARCH_SCAN_LIMIT]:
        if len(results) >= _WEB_SEARCH_RESULT_LIMIT:
            break
        if not isinstance(item, dict):
            continue
        url = _bounded_text(item.get("url") or item.get("link"), _WEB_SEARCH_URL_LIMIT + 1)
        if not url or len(url) > _WEB_SEARCH_URL_LIMIT:
            continue
        try:
            parsed_url = urlsplit(url)
        except ValueError:
            continue
        if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.hostname:
            continue
        url_key = _web_search_url_key(parsed_url)
        if url_key is None or url_key in seen_urls:
            continue
        seen_urls.add(url_key)
        domain = _bounded_text(item.get("domain"), 160)
        if not domain:
            domain = _bounded_text(parsed_url.hostname, 160)
        results.append({
            "title": _bounded_text(item.get("title") or domain or url, 200),
            "url": url,
            "domain": domain,
            "snippet": _bounded_text(
                item.get("snippet") or item.get("content") or item.get("description"),
                320,
            ),
            "published_at": _bounded_text(
                item.get("published_at") or item.get("published_date"),
                80,
            ),
        })

    return {
        "ok": parsed.get("ok") is True,
        "query": _bounded_text(parsed.get("query"), 500),
        "provider": _bounded_text(parsed.get("provider"), 100),
        "response_time": _finite_number(parsed.get("response_time")),
        "result_count": len(parsed["results"]),
        "results": results,
    }


def _build_business_data_search_projection(raw_output: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(raw_output)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None

    # lookup_values action produces a structured response distinct from the
    # plain query action.  Project it separately so the trace shows the right
    # summary fields.
    if parsed.get("action") == "lookup_values":
        return _build_lookup_values_projection(parsed)

    projection: Dict[str, Any] = {
        "success": parsed.get("success") is True,
        "provider": _bounded_text(parsed.get("provider"), 100),
        "entity": _bounded_text(parsed.get("entity"), 240),
        "query": _bounded_text(parsed.get("query"), 500),
    }
    result_count = parsed.get("result_count")
    if isinstance(result_count, int) and not isinstance(result_count, bool):
        projection["result_count"] = max(0, result_count)

    executed_steps = []
    raw_steps = parsed.get("executed_steps")
    if isinstance(raw_steps, list):
        for raw_step in raw_steps[:_BUSINESS_EXECUTED_STEP_LIMIT]:
            if not isinstance(raw_step, dict):
                continue
            step = {
                "tool": _bounded_text(raw_step.get("tool"), 120),
                "summary": _bounded_text(
                    raw_step.get("summary"),
                    _BUSINESS_EXECUTED_STEP_SUMMARY_LIMIT,
                ),
            }
            step = {key: value for key, value in step.items() if value}
            if step:
                executed_steps.append(step)
    if executed_steps:
        projection["executed_steps"] = executed_steps

    evidence = parsed.get("evidence")
    if isinstance(evidence, list) and evidence:
        projection["has_evidence"] = True
        query_digest = next(
            (
                _bounded_text(item.get("query_digest"), 200)
                for item in evidence
                if isinstance(item, dict) and item.get("query_digest")
            ),
            "",
        )
        if query_digest:
            projection["query_digest"] = query_digest

    error_code = _bounded_text(parsed.get("error_code"), 160)
    if error_code:
        projection["error_code"] = error_code
    warnings = parsed.get("warnings")
    if isinstance(warnings, list):
        projected_warnings = [
            _bounded_text(warning, _BUSINESS_EXECUTED_STEP_SUMMARY_LIMIT)
            for warning in warnings[:8]
        ]
        projected_warnings = [warning for warning in projected_warnings if warning]
        if projected_warnings:
            projection["warnings"] = projected_warnings
    return {key: value for key, value in projection.items() if value not in ("", None)}


def _build_lookup_values_projection(parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Project a ``lookup_values`` response for trace/observability."""
    projection: Dict[str, Any] = {
        "action": "lookup_values",
        "entity": _bounded_text(parsed.get("entity"), 240),
        "status": _bounded_text(parsed.get("status"), 60) or "unknown",
    }
    value_results = parsed.get("value_results")
    if isinstance(value_results, list):
        projection["value_result_count"] = len(value_results)
        binding_keys = [
            _bounded_text(item.get("binding_key"), 120)
            for item in value_results
            if isinstance(item, dict) and item.get("binding_key")
        ]
        binding_keys = [key for key in binding_keys if key]
        if binding_keys:
            projection["binding_keys"] = binding_keys[:8]
    errors = parsed.get("errors")
    provider_error_count = 0
    if isinstance(errors, list) and errors:
        projection["error_count"] = len(errors)
        error_codes = [
            _bounded_text(item.get("code"), 160)
            for item in errors
            if isinstance(item, dict) and item.get("code")
        ]
        error_codes = [code for code in error_codes if code]
        provider_error_count = 0
        for item in errors:
            if isinstance(item, dict) and item.get("code") == "VALUE_PROVIDER_FAILED":
                provider_error_count += 1
        if error_codes:
            projection["error_codes"] = error_codes[:8]
    projection["provider_request_count"] = (
        int(projection.get("value_result_count") or 0) + provider_error_count
    )
    return {key: value for key, value in projection.items() if value not in ("", None)}


def _build_output_projection(tool_name: str, raw_output: str) -> Optional[Dict[str, Any]]:
    normalized_name = str(tool_name or "").strip().lower()
    if normalized_name == "web_search":
        return _build_web_search_projection(raw_output)
    if normalized_name == "business_data_search":
        return _build_business_data_search_projection(raw_output)
    return None


@contextmanager
def tool_output_artifact_writer_context(
    writer: Optional[ToolOutputArtifactWriter],
):
    """Bind the durable-output writer for the current run and child contexts."""
    token = _tool_output_artifact_writer_var.set(writer)
    try:
        yield
    finally:
        _tool_output_artifact_writer_var.reset(token)


def _inject_subagent_lineage(context: ToolContext, payload: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(payload or {})
    if enriched.get("caller") in (None, ""):
        enriched["caller"] = str(context.caller or "main")
    if context.agent_iteration is not None and enriched.get("iteration") in (None, ""):
        enriched["iteration"] = int(context.agent_iteration)
    lineage = context.subagent_lineage if isinstance(context.subagent_lineage, dict) else {}
    if context.caller != "subagent" or not lineage:
        return enriched

    for key in _SUBAGENT_LINEAGE_FIELDS:
        current = enriched.get(key)
        if current not in (None, ""):
            continue
        value = lineage.get(key)
        if value not in (None, ""):
            enriched[key] = value
    return enriched


def emit_protocol_event(
    *,
    context: ToolContext,
    event_type: str,
    payload: Dict[str, Any],
    source: Dict[str, str],
    writer: Optional[Callable[[Dict[str, Any]], None]] = None,
    observability_payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit the bounded protocol payload and optional observer-only detail."""
    if not context.run_id:
        return

    scoped_payload = redact_sensitive_value(
        _inject_subagent_lineage(context, payload)
    )
    scoped_observability_payload = (
        redact_sensitive_value(
            _inject_subagent_lineage(context, observability_payload)
        )
        if observability_payload is not None
        else scoped_payload
    )

    if context.caller == "subagent" and context.event_emitter and writer is None:
        context.event_emitter(event_type, scoped_payload)
        if context.protocol_event_observer:
            context.protocol_event_observer(event_type, scoped_payload)
        _emit_tool_observability_event(
            context=context,
            event_type=event_type,
            payload=scoped_observability_payload,
        )
        return

    emit_agent_event(
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        event_type=event_type,
        source=source,
        payload=scoped_payload,
        writer=writer,
        span_key=_tool_span_key(context, scoped_payload),
        parent_key=_tool_sse_parent_key(context),
    )
    if context.protocol_event_observer:
        context.protocol_event_observer(event_type, scoped_payload)
    _emit_tool_observability_event(
        context=context,
        event_type=event_type,
        payload=scoped_observability_payload,
    )


def emit_tool_started(
    *,
    context: ToolContext,
    tool_name: str,
    tool_call_id: Optional[str],
    tool_input: Optional[Dict[str, Any]],
    message: Optional[str],
) -> None:
    emit_protocol_event(
        context=context,
        event_type="tool.started",
        source={"node": "agent_loop", "component": "tool"},
        payload={
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "input": tool_input or {},
            "message": message or "调用工具 {}".format(tool_name),
        },
    )


def emit_tool_finished(
    *,
    context: ToolContext,
    tool_name: str,
    tool_call_id: Optional[str],
    status: str,
    raw_output: str,
    error: Optional[str],
    elapsed_ms: int,
    error_code: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
) -> None:
    payload = build_tool_finished_payload(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        status=status,
        raw_output=raw_output,
        error=error,
        elapsed_ms=elapsed_ms,
        error_code=error_code,
        timeout_seconds=timeout_seconds,
    )
    observability_payload = dict(payload)
    if tool_name in _FULL_TRACE_OUTPUT_TOOLS:
        observability_payload["output_full"] = str(raw_output or "")
    emit_protocol_event(
        context=context,
        event_type="tool.finished",
        source={"node": "agent_loop", "component": "tool"},
        payload=payload,
        observability_payload=observability_payload,
    )


def build_tool_finished_payload(
    *,
    tool_name: str,
    tool_call_id: Optional[str],
    status: str,
    raw_output: str,
    error: Optional[str],
    elapsed_ms: int,
    error_code: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """Build the bounded public payload and persist its complete output."""
    text = redact_sensitive_text(raw_output)
    preview, truncated = _truncate_text(text, limit=4096)
    normalized_status = str(status or "").strip().lower()
    if normalized_status in {"cancelled", "canceled"}:
        normalized_status = "cancelled"
    elif normalized_status == "error":
        normalized_status = "error"
    else:
        normalized_status = "completed"
    payload: Dict[str, Any] = {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "status": normalized_status,
        "output_preview": preview,
        "output_truncated": truncated,
        "output_size": len(text.encode("utf-8")),
        "error": redact_sensitive_text(error),
        "elapsed_ms": elapsed_ms,
    }
    if error_code:
        payload["error_code"] = str(error_code).strip()[:160]
    if timeout_seconds is not None:
        payload["timeout_seconds"] = max(0, float(timeout_seconds))
    output_projection = _build_output_projection(tool_name, text)
    if output_projection is not None:
        payload["output_projection"] = output_projection

    artifact_writer = _tool_output_artifact_writer_var.get()
    if truncated and artifact_writer is not None:
        try:
            artifact = artifact_writer(tool_call_id, text)
        except Exception:
            logger.warning(
                "Failed to persist complete tool output tool=%s call=%s",
                tool_name,
                tool_call_id,
                exc_info=True,
            )
            artifact = None
        if isinstance(artifact, dict):
            output_ref = str(artifact.get("output_ref") or "").strip()
            try:
                output_size = int(artifact.get("output_size"))
            except (TypeError, ValueError):
                output_size = -1
            if output_ref and output_size == payload["output_size"]:
                payload["output_ref"] = output_ref
            elif output_ref:
                logger.warning(
                    "Ignored tool output artifact with mismatched size tool=%s call=%s",
                    tool_name,
                    tool_call_id,
                )

    return payload


def _emit_tool_observability_event(
    *,
    context: ToolContext,
    event_type: str,
    payload: Dict[str, Any],
) -> None:
    runtime_type = {
        "tool.started": RuntimeEventType.TOOL_STARTED,
        "tool.progress": RuntimeEventType.TOOL_PROGRESS,
        "tool.finished": RuntimeEventType.TOOL_FINISHED,
    }.get(event_type)
    if not runtime_type:
        return
    observer = context.observer
    if not observer or not getattr(observer, "enabled", True) or not context.run_id:
        return

    observer.emit(
        RuntimeEvent(
            type=runtime_type,
            run_id=str(context.run_id or ""),
            session_id=context.session_id,
            turn_id=context.turn_id,
            span_key=_tool_span_key(context, payload),
            parent_key=_tool_batch_span_key(context),
            payload=dict(payload or {}),
        )
    )


def _tool_span_key(context: ToolContext, payload: Dict[str, Any]) -> str:
    tool_call_id = str(payload.get("tool_call_id") or "").strip()
    tool_name = str(payload.get("tool_name") or "unknown_tool").strip() or "unknown_tool"
    suffix = "{}:{}".format(tool_name, tool_call_id or "no_id")
    return "tool:{}:{}:{}".format(_caller_suffix(context), context.agent_iteration, suffix)


def _tool_batch_span_key(context: ToolContext) -> str:
    base_key = "tools:{}:{}".format(_caller_suffix(context), context.agent_iteration)
    batch_index = getattr(context, "tool_batch_index", None)
    if batch_index is None:
        return base_key
    return "{}:{}".format(base_key, int(batch_index))


def _tool_sse_parent_key(context: ToolContext) -> str:
    """Attach streamed tool nodes to the iteration node visible in the UI."""

    return "iteration:{}:{}".format(_caller_suffix(context), context.agent_iteration)


def _caller_suffix(context: ToolContext) -> str:
    if context.caller != "subagent":
        return str(context.caller or "main")
    lineage = context.subagent_lineage if isinstance(context.subagent_lineage, dict) else {}
    subagent_id = str(lineage.get("subagent_id") or "").strip()
    return "subagent:{}".format(subagent_id) if subagent_id else "subagent"


def _truncate_text(text: str, *, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True
