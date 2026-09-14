# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Public web search tool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set

from agent.tools.base import ToolCaller, ToolContext, ToolOutcome
from agent.tools.web_search.service import WebSearchEventEmitter, query_web_search

WEB_SEARCH_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the public web for current or external information. "
            "Use this only when the answer depends on public internet sources, recent events, "
            "or facts outside the selected ontology and business data. "
            "Do not use this for enterprise ontology definitions or bound business records; use "
            "ontology_context and business_data_search for those. "
            "Search results are external evidence; cite source URLs in the final answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A precise natural-language web search query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": (
                        "Maximum number of results to return. The backend enforces a configured upper limit."
                    ),
                },
                "freshness": {
                    "type": "string",
                    "enum": ["any", "day", "week", "month", "year"],
                    "description": "Optional publish/update time window. Use any when recency is not required.",
                },
                "include_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional public domains to restrict results to, e.g. ['example.com']. Do not pass URLs."
                    ),
                },
                "exclude_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional public domains to exclude, e.g. ['example.com']. Do not pass URLs.",
                },
            },
            "required": ["query"],
        },
    },
}


def handle_web_search(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
    query: str,
    max_results: Optional[int] = None,
    freshness: str = "any",
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    event_emitter: Optional[WebSearchEventEmitter] = None,
    tool_call_id: Optional[str] = None,
) -> str:
    return query_web_search(
        query=query,
        max_results=max_results,
        freshness=freshness,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        event_emitter=event_emitter,
        tool_call_id=tool_call_id,
    )


@dataclass(frozen=True)
class WebSearchTool:
    name: str = "web_search"
    schema: Dict[str, Any] = None  # type: ignore[assignment]
    allowed_callers: Set[ToolCaller] = None  # type: ignore[assignment]
    concurrency_safe: bool = True
    max_parallelism: Optional[int] = 3
    category: str = "web_search"

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", WEB_SEARCH_TOOL_SCHEMA)
        object.__setattr__(self, "allowed_callers", {"main", "subagent"})

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def execute(
        self,
        *,
        args: Dict[str, Any],
        context: ToolContext,
        tool_call_id: Optional[str] = None,
        event_emitter: Optional[Callable[..., Any]] = None,
    ) -> ToolOutcome:
        content = handle_web_search(
            **args,
            event_emitter=event_emitter,
            tool_call_id=tool_call_id,
        )
        return ToolOutcome(content=content)
