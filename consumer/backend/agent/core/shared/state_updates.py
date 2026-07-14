"""Shared agent state update helpers."""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.tools.base import ToolBatchResult
from utils.read_docs import merge_read_documents_registry


def apply_tool_batch_result_to_state(state: Dict[str, Any], batch_result: Any) -> None:
    messages = list(getattr(batch_result, "messages", None) or [])
    if messages:
        state.setdefault("messages", [])
        state["messages"].extend(messages)

    updates = getattr(batch_result, "state_updates", None) or {}
    if updates:
        apply_state_updates_to_state(state, dict(updates))


def apply_state_updates_to_state(state: Dict[str, Any], updates: Dict[str, Any]) -> None:
    for key, value in (updates or {}).items():
        if key == "results" and isinstance(value, dict):
            state["results"] = {**dict(state.get("results") or {}), **value}
        elif key in {"completed_ids", "failed_ids", "errors"} and isinstance(value, list):
            existing = list(state.get(key) or [])
            existing.extend(value)
            state[key] = existing
        elif key == "messages" and isinstance(value, list):
            state.setdefault("messages", [])
            state["messages"].extend(value)
        elif key == "read_document_records" and isinstance(value, list):
            state["read_documents"] = merge_read_documents_registry(
                state.get("read_documents"),
                value,
            )
        elif key == "evolution_recall_records" and isinstance(value, list):
            existing = list(state.get("evolution_recall_records") or [])
            existing.extend(value)
            state["evolution_recall_records"] = existing
        else:
            state[key] = value


def merge_tool_state_updates(
    existing: Optional[Dict[str, Any]],
    incoming: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(existing or {})
    for key, value in (incoming or {}).items():
        if key == "results" and isinstance(value, dict):
            merged[key] = {**dict(merged.get(key) or {}), **value}
        elif key in {"completed_ids", "failed_ids", "errors"} and isinstance(value, list):
            items = list(merged.get(key) or [])
            items.extend(value)
            merged[key] = items
        elif key == "messages" and isinstance(value, list):
            messages = list(merged.get(key) or [])
            messages.extend(value)
            merged[key] = messages
        elif key == "read_document_records" and isinstance(value, list):
            records = list(merged.get(key) or [])
            records.extend(value)
            merged[key] = records
        elif key == "evolution_recall_records" and isinstance(value, list):
            records = list(merged.get(key) or [])
            records.extend(value)
            merged[key] = records
        else:
            merged[key] = value
    return merged


def merge_tool_batch_results(
    existing: ToolBatchResult,
    incoming: ToolBatchResult,
) -> ToolBatchResult:
    return ToolBatchResult(
        messages=[*(existing.messages or []), *(incoming.messages or [])],
        state_updates=merge_tool_state_updates(existing.state_updates, incoming.state_updates),
        terminal=bool(existing.terminal or incoming.terminal),
        tool_intervals_ms=[
            *(existing.tool_intervals_ms or []),
            *(incoming.tool_intervals_ms or []),
        ],
    )
