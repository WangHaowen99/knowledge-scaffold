"""Output formatting for graph search results."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from .models import GraphAgentResult, GraphEntityMapping, GraphScope

ENGINE_NAME = "graph_search_runtime"


def build_success_result(
    *,
    provider: str,
    entity: str,
    query: str,
    mapping: GraphEntityMapping,
    agent_result: GraphAgentResult,
    scope: GraphScope,
) -> Dict[str, Any]:
    backend_result = agent_result.backend_result
    records = list(backend_result.records or [])
    total = backend_result.total if backend_result.total is not None else len(records)
    warnings = list(agent_result.warnings or []) + list(backend_result.warnings or [])
    summary = (
        "命中 {} 条图谱业务数据。".format(total)
        if total
        else "未检索到满足条件的实例。"
    )
    return {
        "success": True,
        "provider": provider,
        "engine": ENGINE_NAME,
        "entity": entity,
        "query": query,
        "summary": summary,
        "result_count": total,
        "records": records,
        "evidence": [
            {
                "evidence_id": "graph_ev_001",
                "provider": provider,
                "entity": entity,
                "tool": agent_result.tool_name,
                "mapping_source": mapping.source,
                "scope": {
                    "workspace_property": scope.workspace_property,
                    "ontology_property": scope.ontology_property,
                },
                "record_count": total,
                "query_digest": _digest_tool_call(agent_result.tool_name, agent_result.tool_args),
            }
        ],
        "executed_steps": agent_result.executed_steps or [
            {
                "tool": agent_result.tool_name,
                "summary": "按映射 {} 执行内部图工具 {}".format(
                    mapping.source,
                    agent_result.tool_name,
                ),
            }
        ],
        "truncated": bool(backend_result.truncated),
        "warnings": warnings,
    }


def build_error_result(
    *,
    provider: str,
    entity: str = "",
    query: str = "",
    error_code: str,
    error: str,
    retryable: bool = False,
    warnings: list[str] | None = None,
) -> Dict[str, Any]:
    return {
        "success": False,
        "provider": provider,
        "engine": ENGINE_NAME,
        "entity": entity,
        "query": query,
        "error_code": error_code,
        "error": error,
        "retryable": retryable,
        "warnings": warnings or [],
    }


def _digest_tool_call(tool_name: str, tool_args: Dict[str, Any]) -> str:
    payload = {
        "tool_name": tool_name,
        "tool_args": tool_args,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return "sha256:{}".format(hashlib.sha256(raw).hexdigest())
