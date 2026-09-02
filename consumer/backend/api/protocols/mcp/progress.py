"""Coarse, public MCP chat progress projection."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from typing import Any

from utils.security.redaction import REDACTED, redact_sensitive_text


PROGRESS_SCHEMA_VERSION = "1.0"

_KNOWN_PROVIDERS = frozenset(
    {
        "dataagent",
        "hugegraph",
        "mindsdb",
        "neo4j",
        "nl2sql_http",
        "nl2sql_template",
    }
)
_TOOL_SUMMARIES = {
    "ontology_context": ("ontology_context", "正在读取知识包内容", "知识包内容读取完成"),
    "business_data_search": ("business_data_search", "正在查询业务数据", "业务数据查询完成"),
    "execute_script": ("business_rule", "正在执行业务规则", "业务规则执行完成"),
    "web_search": ("web_search", "正在检索公开信息", "公开信息检索完成"),
    "spawn_sub_agent": ("specialized_analysis", "正在进行专项分析", "专项分析完成"),
}
_ONTOLOGY_RESOURCE_LABELS = {
    "root": "知识包根上下文",
    "operation": "业务操作",
    "object": "业务对象",
    "rule": "业务规则",
    "metric": "业务指标",
    "script": "业务脚本",
    "asset": "知识包资源",
}
_PROVIDER_LABELS = {
    "dataagent": "DataAgent",
    "hugegraph": "HugeGraph",
    "mindsdb": "MindsDB",
    "neo4j": "Neo4j",
    "nl2sql_http": "NL2SQL HTTP",
    "nl2sql_template": "NL2SQL Template",
}
_SQL_RE = re.compile(
    r"(?i)(?:^|[\s;(])(?:select|insert|update|delete|merge|match|call|create|drop|alter)\b"
)
_URL_RE = re.compile(r"(?i)\b(?:https?|bolt|neo4j|ftp)://")
_PATH_RE = re.compile(r"(?:^|\s)(?:/[A-Za-z0-9_.-]+){2,}(?:/|\b)|[A-Za-z]:\\")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def build_public_summary(
    *,
    template: str,
    candidate: str = "",
    candidate_allowed: bool = False,
    preview_limit: int = 100,
    summary_limit: int = 200,
) -> str:
    """Build a bounded summary from a fixed template and trusted preview."""

    fixed = _bounded_text(template, summary_limit)
    if not candidate_allowed or not str(candidate or "").strip():
        return fixed
    safe_text = redact_sensitive_text(candidate)
    safe_text = _CONTROL_RE.sub(" ", safe_text)
    safe_text = " ".join(safe_text.split())
    if not safe_text or REDACTED in safe_text:
        return fixed
    if _SQL_RE.search(safe_text) or _URL_RE.search(safe_text) or _PATH_RE.search(safe_text):
        return fixed
    preview = _bounded_text(safe_text, preview_limit, ellipsis=True)
    if not preview:
        return fixed
    return _bounded_text("{}：{}".format(fixed, preview), summary_limit, ellipsis=True)


class MCPChatProgressProjector:
    """Project canonical public events into a stable, low-volume MCP schema."""

    def __init__(
        self,
        *,
        max_events: int = 100,
        max_summary_chars: int = 200,
        max_preview_chars: int = 100,
    ) -> None:
        self.max_events = max(1, int(max_events))
        self.max_summary_chars = max(32, int(max_summary_chars))
        self.max_preview_chars = max(16, int(max_preview_chars))
        self._seq = 0
        self._started_at = time.monotonic()
        self._emitted: set[str] = set()
        self._tools: dict[str, dict[str, Any]] = {}
        self._business_data_completed = False

    def emit_run_started(self, *, run_id: str) -> dict[str, Any] | None:
        return self._once(
            "run.started",
            event="run.started",
            stage="request",
            status="running",
            summary="已接收请求，开始处理",
            run_id=run_id,
        )

    def emit_ontology_ready(
        self,
        *,
        run_id: str,
        elapsed_ms: int | None = None,
    ) -> dict[str, Any] | None:
        return self._once(
            "ontology.ready",
            event="ontology.ready",
            stage="ontology_context",
            status="completed",
            summary="知识包上下文准备完成",
            run_id=run_id,
            elapsed_ms=elapsed_ms,
        )

    def emit_terminal(
        self,
        *,
        run_id: str,
        status: Any,
    ) -> dict[str, Any] | None:
        normalized = str(getattr(status, "value", status) or "").strip().lower()
        completed = normalized in {"completed", "success"}
        return self._once(
            "run.terminal",
            event="run.completed" if completed else "run.failed",
            stage="result",
            status="completed" if completed else "failed",
            summary="处理完成" if completed else "处理失败",
            run_id=run_id,
            elapsed_ms=max(0, int((time.monotonic() - self._started_at) * 1000)),
            critical=True,
        )

    def consume(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any] | None,
        run_id: str,
    ) -> list[dict[str, Any]]:
        values = payload if isinstance(payload, Mapping) else {}
        normalized_type = str(event_type or "").strip().lower()
        if normalized_type == "model.started":
            if self._business_data_completed and not self._tools:
                item = self._once(
                    "answer.preparing",
                    event="answer.preparing",
                    stage="answer_generation",
                    status="running",
                    summary="正在整理最终回答",
                    run_id=run_id,
                )
            else:
                item = self._once(
                    "analysis.started",
                    event="analysis.started",
                    stage="query_analysis",
                    status="running",
                    summary="正在分析问题并选择处理方式",
                    run_id=run_id,
                )
            return [item] if item is not None else []
        if normalized_type == "tool.started":
            item = self._tool_started(values, run_id=run_id)
            return [item] if item is not None else []
        if normalized_type == "tool.progress":
            item = self._tool_progress(values, run_id=run_id)
            return [item] if item is not None else []
        if normalized_type == "tool.finished":
            item = self._tool_finished(values, run_id=run_id)
            return [item] if item is not None else []
        if normalized_type == "token.delta":
            return []
        if normalized_type == "turn.finished":
            item = self.emit_terminal(
                run_id=run_id,
                status=values.get("status"),
            )
            return [item] if item is not None else []
        return []

    def _tool_started(
        self,
        payload: Mapping[str, Any],
        *,
        run_id: str,
    ) -> dict[str, Any] | None:
        tool_name = _known_tool_name(payload)
        tool_call_id = _identifier(payload.get("tool_call_id"))
        key = tool_call_id or "tool:{}:{}".format(tool_name or "other", self._seq + 1)
        stage, started_summary, _completed_summary = _tool_presentation(tool_name)
        candidate, allowed = _public_summary_candidate(payload)
        resource, requested_key = _ontology_context_request(payload, tool_name)
        if resource:
            candidate = _ontology_context_summary(
                resource=resource,
                requested_key=requested_key,
                resolved_key="",
            )
            allowed = bool(candidate)
        self._tools[key] = {
            "tool": tool_name,
            "tool_call_id": tool_call_id,
            "stage": stage,
            "provider": _known_provider(payload.get("database_provider")),
            "public_summary": candidate,
            "public_summary_allowed": allowed,
            "record_count": _record_count(payload),
            "truncated": _truncated(payload),
            "resource": resource,
            "requested_key": requested_key,
            "last_progress_summary": "",
        }
        return self._event(
            event="tool.started",
            stage=stage,
            status="running",
            summary=build_public_summary(
                template=started_summary,
                candidate=candidate,
                candidate_allowed=allowed,
                preview_limit=self.max_preview_chars,
                summary_limit=self.max_summary_chars,
            ),
            run_id=run_id,
            tool=tool_name,
            tool_call_id=tool_call_id,
            provider=self._tools[key]["provider"],
        )

    def _tool_progress(
        self,
        payload: Mapping[str, Any],
        *,
        run_id: str,
    ) -> dict[str, Any] | None:
        tool_name = _known_tool_name(payload)
        tool_call_id = _identifier(payload.get("tool_call_id"))
        key = _find_tool_key(
            self._tools,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
        if key is None:
            key = tool_call_id or "tool:{}:{}".format(tool_name or "other", self._seq + 1)
            stage, _started_summary, _completed_summary = _tool_presentation(tool_name)
            self._tools[key] = {
                "tool": tool_name,
                "tool_call_id": tool_call_id,
                "stage": stage,
                "provider": "",
                "public_summary": "",
                "public_summary_allowed": False,
                "record_count": None,
                "truncated": False,
                "resource": "",
                "requested_key": "",
                "last_progress_summary": "",
            }
        state = self._tools[key]
        provider = _known_provider(payload.get("database_provider"))
        state["provider"] = provider or state["provider"]
        state["record_count"] = _record_count(payload, fallback=state["record_count"])
        state["truncated"] = bool(state["truncated"] or _truncated(payload))

        progress = payload.get("progress")
        if not isinstance(progress, Mapping):
            return self._provider_progress_event(state, run_id=run_id)
        business_event = progress.get("business_data_search_event")
        if not isinstance(business_event, Mapping):
            return self._provider_progress_event(state, run_id=run_id)
        candidate, allowed = _public_summary_candidate(business_event)
        if allowed:
            state["public_summary"] = candidate
            state["public_summary_allowed"] = True
        state["record_count"] = _record_count(
            business_event,
            fallback=state["record_count"],
        )
        state["truncated"] = bool(state["truncated"] or _truncated(business_event))
        return self._provider_progress_event(state, run_id=run_id)

    def _provider_progress_event(
        self,
        state: dict[str, Any],
        *,
        run_id: str,
    ) -> dict[str, Any] | None:
        if state.get("tool") != "business_data_search":
            return None
        provider = str(state.get("provider") or "")
        candidate = str(state.get("public_summary") or "")
        candidate_allowed = state.get("public_summary_allowed") is True
        last_summary = str(state.get("last_progress_summary") or "")
        if not provider and not candidate_allowed:
            return None
        provider_label = _PROVIDER_LABELS.get(provider, provider)
        template = (
            "已选择 {} 数据源，正在查询业务数据".format(provider_label)
            if provider_label
            else "正在查询业务数据"
        )
        summary = build_public_summary(
            template=template,
            candidate=candidate,
            candidate_allowed=candidate_allowed,
            preview_limit=self.max_preview_chars,
            summary_limit=self.max_summary_chars,
        )
        if summary == last_summary:
            return None
        state["last_progress_summary"] = summary
        return self._event(
            event="tool.progress",
            stage=str(state.get("stage") or "business_data_search"),
            status="running",
            summary=summary,
            run_id=run_id,
            tool=str(state.get("tool") or ""),
            tool_call_id=str(state.get("tool_call_id") or ""),
            provider=provider,
        )

    def _tool_finished(
        self,
        payload: Mapping[str, Any],
        *,
        run_id: str,
    ) -> dict[str, Any] | None:
        tool_name = _known_tool_name(payload)
        tool_call_id = _identifier(payload.get("tool_call_id"))
        key = _find_tool_key(self._tools, tool_call_id=tool_call_id, tool_name=tool_name)
        state = self._tools.pop(key, {}) if key is not None else {}
        tool_name = tool_name or str(state.get("tool") or "")
        tool_call_id = tool_call_id or str(state.get("tool_call_id") or "")
        stage, _started_summary, completed_summary = _tool_presentation(tool_name)
        stage = str(state.get("stage") or stage)
        provider = _known_provider(payload.get("database_provider")) or str(
            state.get("provider") or ""
        )
        record_count = _record_count(payload, fallback=state.get("record_count"))
        truncated = bool(state.get("truncated") or _truncated(payload))
        status = str(payload.get("status") or "completed").strip().lower()
        failed = status in {"error", "failed", "cancelled", "canceled"}
        elapsed_ms = _non_negative_int(payload.get("elapsed_ms"))
        if tool_name == "ontology_context":
            output_projection = payload.get("output_projection")
            projection = (
                output_projection
                if isinstance(output_projection, Mapping)
                else {}
            )
            resource = str(
                projection.get("resource") or state.get("resource") or ""
            ).strip()
            candidate = _ontology_context_summary(
                resource=resource,
                requested_key=str(state.get("requested_key") or ""),
                resolved_key=str(projection.get("key") or ""),
            )
            if candidate:
                state["public_summary"] = candidate
                state["public_summary_allowed"] = True
        if failed:
            fixed_summary = "{}失败".format(completed_summary.removesuffix("完成"))
        else:
            fixed_summary = completed_summary
            if record_count is not None and tool_name == "business_data_search":
                fixed_summary = "{}，命中 {} 条记录".format(fixed_summary, record_count)
            if elapsed_ms is not None:
                fixed_summary = "{}，用时 {} ms".format(fixed_summary, elapsed_ms)
        warning_code = ""
        if truncated:
            warning_code = "RESULT_TRUNCATED"
        elif failed and tool_name == "business_data_search":
            warning_code = "BUSINESS_DATA_QUERY_FAILED"
        if tool_name == "business_data_search" and not failed:
            self._business_data_completed = True
        completed_candidate = (
            ""
            if tool_name == "business_data_search"
            else str(state.get("public_summary") or "")
        )
        completed_candidate_allowed = (
            tool_name != "business_data_search"
            and state.get("public_summary_allowed") is True
        )
        return self._event(
            event="tool.completed",
            stage=stage,
            status="failed" if failed else "completed",
            summary=build_public_summary(
                template=fixed_summary,
                candidate=completed_candidate,
                candidate_allowed=completed_candidate_allowed,
                preview_limit=self.max_preview_chars,
                summary_limit=self.max_summary_chars,
            ),
            run_id=run_id,
            tool=tool_name,
            tool_call_id=tool_call_id,
            provider=provider,
            record_count=record_count,
            elapsed_ms=elapsed_ms,
            warning_code=warning_code,
            truncated=truncated,
        )

    def _once(self, key: str, **values: Any) -> dict[str, Any] | None:
        if key in self._emitted:
            return None
        self._emitted.add(key)
        return self._event(**values)

    def _event(
        self,
        *,
        event: str,
        stage: str,
        status: str,
        summary: str,
        run_id: str,
        tool: str = "",
        tool_call_id: str = "",
        provider: str = "",
        record_count: int | None = None,
        elapsed_ms: int | None = None,
        warning_code: str = "",
        truncated: bool = False,
        critical: bool = False,
    ) -> dict[str, Any] | None:
        if self._seq >= self.max_events and not critical:
            return None
        self._seq += 1
        return {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "seq": self._seq,
            "event": str(event),
            "stage": str(stage),
            "status": str(status),
            "summary": _bounded_text(summary, self.max_summary_chars, ellipsis=True),
            "run_id": _identifier(run_id),
            "tool": _identifier(tool),
            "tool_call_id": _identifier(tool_call_id),
            "provider": _known_provider(provider),
            "record_count": _non_negative_int(record_count),
            "elapsed_ms": _non_negative_int(elapsed_ms),
            "warning_code": str(warning_code or "")[:64],
            "truncated": bool(truncated),
        }


def _tool_presentation(tool_name: str) -> tuple[str, str, str]:
    return _TOOL_SUMMARIES.get(
        tool_name,
        ("tool_execution", "正在执行相关能力", "相关能力执行完成"),
    )


def _known_tool_name(payload: Mapping[str, Any]) -> str:
    name = str(payload.get("name") or payload.get("tool_name") or "").strip()
    return name if name in _TOOL_SUMMARIES else ""


def _known_provider(value: Any) -> str:
    provider = str(value or "").strip().lower()
    return provider if provider in _KNOWN_PROVIDERS else ""


def _ontology_context_request(
    payload: Mapping[str, Any],
    tool_name: str,
) -> tuple[str, str]:
    if tool_name != "ontology_context":
        return "", ""
    input_preview = payload.get("input_preview")
    if not isinstance(input_preview, Mapping):
        return "", ""
    resource = str(input_preview.get("resource") or "").strip().lower()
    if resource not in _ONTOLOGY_RESOURCE_LABELS:
        return "", ""
    return resource, str(input_preview.get("key") or "").strip()


def _ontology_context_summary(
    *,
    resource: str,
    requested_key: str,
    resolved_key: str,
) -> str:
    normalized_resource = str(resource or "").strip().lower()
    resource_label = _ONTOLOGY_RESOURCE_LABELS.get(normalized_resource, "")
    if not resource_label:
        return ""
    requested = str(requested_key or "").strip()
    resolved = str(resolved_key or "").strip() or requested
    if not requested and not resolved:
        return resource_label
    if normalized_resource == "object":
        name_label = "对象名"
    else:
        name_label = "名称"
    if requested and resolved:
        return "{}，{}={}，key={}".format(
            resource_label,
            name_label,
            requested,
            resolved,
        )
    return "{}，key={}".format(resource_label, resolved or requested)


def _public_summary_candidate(payload: Mapping[str, Any]) -> tuple[str, bool]:
    allowed = payload.get("public_summary_allowed") is True
    candidate = str(payload.get("public_summary") or "").strip()
    return candidate, bool(allowed and candidate)


def _find_tool_key(
    tools: Mapping[str, Mapping[str, Any]],
    *,
    tool_call_id: str,
    tool_name: str,
) -> str | None:
    if tool_call_id:
        for key, state in reversed(list(tools.items())):
            if str(state.get("tool_call_id") or "") == tool_call_id:
                return key
    if tool_name:
        for key, state in reversed(list(tools.items())):
            if str(state.get("tool") or "") == tool_name:
                return key
    return None


def _record_count(payload: Mapping[str, Any], fallback: Any = None) -> int | None:
    direct = _first_non_negative_int(
        payload.get("record_count"),
        payload.get("row_count"),
        payload.get("result_count"),
    )
    if direct is not None:
        return direct
    for key in ("output_projection", "result", "data"):
        value = payload.get(key)
        count = _record_count_from_value(value)
        if count is not None:
            return count
    content = payload.get("content")
    if isinstance(content, str) and content.lstrip().startswith(("{", "[")):
        try:
            count = _record_count_from_value(json.loads(content))
        except (TypeError, ValueError):
            count = None
        if count is not None:
            return count
    return _non_negative_int(fallback)


def _record_count_from_value(value: Any, depth: int = 0) -> int | None:
    if depth > 2:
        return None
    if isinstance(value, list):
        return len(value)
    if not isinstance(value, Mapping):
        return None
    direct = _first_non_negative_int(
        value.get("record_count"),
        value.get("row_count"),
        value.get("result_count"),
    )
    if direct is not None:
        return direct
    rows = value.get("rows")
    if isinstance(rows, list):
        return len(rows)
    data = value.get("data")
    if isinstance(data, list):
        return len(data)
    for key in ("result", "data"):
        nested = value.get(key)
        count = _record_count_from_value(nested, depth + 1)
        if count is not None:
            return count
    return None


def _truncated(payload: Mapping[str, Any]) -> bool:
    if payload.get("truncated") is True:
        return True
    for key in ("output_projection", "result"):
        value = payload.get(key)
        if isinstance(value, Mapping) and value.get("truncated") is True:
            return True
    return False


def _first_non_negative_int(*values: Any) -> int | None:
    for value in values:
        normalized = _non_negative_int(value)
        if normalized is not None:
            return normalized
    return None


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _identifier(value: Any, limit: int = 256) -> str:
    text = str(value or "").strip()
    if not text or _CONTROL_RE.search(text):
        return ""
    return text[: max(1, int(limit))]


def _bounded_text(value: Any, limit: int, *, ellipsis: bool = False) -> str:
    text = str(value or "").strip()
    max_length = max(0, int(limit))
    if len(text) <= max_length:
        return text
    if not ellipsis or max_length <= 3:
        return text[:max_length]
    return "{}...".format(text[: max_length - 3])
