"""Synchronous adapters used only by legacy-shaped unit test bodies."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional, Sequence

from agent.tools.base import ToolBatchResult, ToolContext, ToolInvocation
from agent.tools.executor import _aexecute_single_task, aexecute_tool_batch


def execute_tool_batch(
    tool_calls: Sequence[ToolInvocation],
    context: ToolContext,
) -> ToolBatchResult:
    return asyncio.run(aexecute_tool_batch(tool_calls, context))


def execute_single_task(
    task: Dict[str, Any],
    context: ToolContext,
    subagent_event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
) -> Dict[str, Any]:
    return asyncio.run(
        _aexecute_single_task(task, context, subagent_event_emitter)
    )
