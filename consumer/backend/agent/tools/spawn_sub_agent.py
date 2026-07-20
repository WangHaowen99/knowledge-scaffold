"""Tool for synchronous sub-agent task-group orchestration."""

import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from agent.tools.base import ToolCaller, ToolContext, ToolOutcome
from config import AgentConfig
from agent.exceptions import WorkflowCancelledError
from agent.sub_agents.manager import SubAgentTaskSpec, get_subagent_manager
from utils.logging import get_logger

logger = get_logger("tools.spawn_sub_agent")


SPAWN_SUB_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn_sub_agent",
        "description": (
            "Dispatch independent sub-agent tasks in parallel and return an aggregated result. "
            "Use this for independent fan-out work; put all tasks from the same logical fan-out "
            "in one call. Do not use this for dependent or sequential tasks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": (
                        "Independent tasks to dispatch in parallel. Each item gets its own sub-agent. "
                        "All items in this array must be mutually independent; if one task needs "
                        "another task's output, run it in a later spawn_sub_agent call with the "
                        "earlier result in context."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": (
                                    "Stable, unique identifier within this batch, used to correlate "
                                    "each sub-agent task with its aggregated result."
                                ),
                            },
                            "task": {
                                "type": "string",
                                "description": (
                                    "Self-contained task description for this sub-agent. Include the "
                                    "concrete goal, required input values, execution constraints, and "
                                    "expected output format. The sub-agent only sees this task plus "
                                    "its context, so omit nothing required for strict execution."
                                ),
                            },
                            "context": {
                                "type": "object",
                                "description": (
                                    "Explicit context payload for this task only. For ontology "
                                    "execution tasks, include ontology_id and operation_key; include "
                                    "sub_operation_keys when known child operations are needed. Put "
                                    "any prior result or task-local data the sub-agent must rely on "
                                    "here instead of assuming shared hidden state."
                                ),
                            },
                        },
                        "required": ["id", "task"],
                    },
                    "minItems": 1,
                },
            },
            "required": ["tasks"],
        },
    },
}


def _build_error_payload(summary: str) -> Dict[str, Any]:
    return {
        "group_id": "group_{}".format(uuid.uuid4().hex[:10]),
        "status": "error",
        "tasks": [],
        "summary": summary,
    }


def normalize_spawn_sub_agent_args(
    args: Optional[Dict[str, Any]],
) -> Tuple[Optional[List[SubAgentTaskSpec]], Optional[str]]:
    payload = dict(args or {})
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return None, "`tasks` must be a non-empty array."

    normalized: List[SubAgentTaskSpec] = []
    seen_ids: Set[str] = set()
    for index, raw_task in enumerate(raw_tasks):
        if not isinstance(raw_task, dict):
            return None, "tasks[{}] must be an object.".format(index)

        if "dependency" in raw_task or "dependencies" in raw_task:
            return None, (
                "tasks[{}] includes a dependency field. Batch `spawn_sub_agent` tasks must be independent."
            ).format(index)

        task_id = str(raw_task.get("id") or "").strip()
        task = str(raw_task.get("task") or "").strip()
        task_context = raw_task.get("context") or {}

        if not task_id:
            return None, "tasks[{}].id is required.".format(index)
        if task_id in seen_ids:
            return None, "Duplicate task id `{}` in `tasks`.".format(task_id)
        if not task:
            return None, "tasks[{}].task is required.".format(index)
        if not isinstance(task_context, dict):
            return None, "tasks[{}].context must be an object.".format(index)

        seen_ids.add(task_id)
        normalized.append(
            SubAgentTaskSpec(id=task_id, task=task, context=dict(task_context))
        )

    return normalized, None


def handle_spawn_sub_agent(
    *,
    args: Dict[str, Any],
    agent_config: Optional[AgentConfig],
    run_id: Optional[str],
    session_id: Optional[str],
    event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
    parent_tool_call_id: Optional[str],
    lineage: Optional[Dict[str, Any]] = None,
    manager: Any = None,
    parent_agent_mode: Optional[str] = None,
    parent_model_profile_id: Optional[str] = None,
    parent_workspace_id: Optional[str] = None,
    observer: Any = None,
    parent_selected_ontology_id: Optional[str] = None,
) -> str:
    tasks, validation_error = normalize_spawn_sub_agent_args(args)
    if validation_error:
        return json.dumps(_build_error_payload(validation_error), ensure_ascii=False)

    runtime_manager = manager or get_subagent_manager(
        run_id,
        agent_config or AgentConfig.from_env(),
    )

    try:
        result = runtime_manager.execute_group(
            run_id=run_id,
            session_id=session_id,
            parent_tool_call_id=parent_tool_call_id,
            parent_agent_mode=parent_agent_mode,
            parent_model_profile_id=parent_model_profile_id,
            parent_workspace_id=parent_workspace_id,
            event_emitter=event_emitter,
            lineage=lineage,
            tasks=tasks or [],
            observer=observer,
            parent_selected_ontology_id=parent_selected_ontology_id,
        )
        return json.dumps(result, ensure_ascii=False)
    except WorkflowCancelledError:
        raise
    except Exception as exc:
        logger.exception("Failed to execute spawn_sub_agent group: %s", exc)
        return json.dumps(
            _build_error_payload("spawn_sub_agent failed: {}".format(str(exc))),
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class SpawnSubAgentTool:
    name: str = "spawn_sub_agent"
    schema: Dict[str, Any] = None  # type: ignore[assignment]
    allowed_callers: Set[ToolCaller] = None  # type: ignore[assignment]
    concurrency_safe: bool = False
    max_parallelism: Optional[int] = None
    category: str = "orchestration"

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", SPAWN_SUB_AGENT_SCHEMA)
        object.__setattr__(self, "allowed_callers", {"main", "subagent", "acp"})

    def execute(
        self,
        *,
        args: Dict[str, Any],
        context: ToolContext,
        tool_call_id: Optional[str] = None,
        event_emitter: Optional[Callable[..., Any]] = None,
    ) -> ToolOutcome:
        return ToolOutcome(
            content=handle_spawn_sub_agent(
                args=args,
                agent_config=context.agent_config,
                run_id=context.run_id,
                session_id=context.session_id,
                event_emitter=event_emitter or context.event_emitter,
                parent_tool_call_id=tool_call_id,
                lineage=context.subagent_lineage,
                manager=context.subagent_manager,
                parent_agent_mode=context.agent_mode,
                parent_model_profile_id=context.model_profile_id,
                parent_workspace_id=context.workspace_id,
                observer=context.observer,
                parent_selected_ontology_id=context.selected_ontology_id,
            ),
        )
