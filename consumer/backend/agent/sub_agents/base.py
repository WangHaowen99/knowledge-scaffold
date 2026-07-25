"""Homogeneous sub-agent: one concrete class, one generic prompt."""

from __future__ import annotations

import uuid
from typing import Any, Callable, Dict, List, Optional

from agent.exceptions import WorkflowCancelledError
from llm import get_llm_router
from utils.logging import get_logger
from ontology.ontologies import build_ontologies_section
from utils.skills import build_skills_section

logger = get_logger("sub_agents.base")


SUBAGENT_SYSTEM_PROMPT = """\
You are a sub-agent. You received one independent task from the parent agent and must complete it end-to-end, then return a compact result.

## Trust the Parent's Context
The `context` payload is authoritative — the parent has already done routing and selection for you. Do NOT re-discover work the parent already settled:
- `context.ontology_id` identifies the selected ontology. Use it directly with `ontology_context`; do not infer paths.
- `context.operation_key` is the operation to execute. Load it via `ontology_context(resource="operation")` when manual execution is required.
- If an operation references object, rule, or metric keys from the root catalogs, load only those specs via `ontology_context(resource="object")`, `ontology_context(resource="rule")`, or `ontology_context(resource="metric")` before using them.
- `context.sub_operation_keys` are referenced sub-process operations. Load them only when the main operation's logic actually invokes them.
- Treat input parameter values supplied in the `task` text as given. Do not re-confirm or re-validate them.

## Operation Execution Flow
1. Call `ontology_context(resource="root")` for `context.ontology_id`, then use `context.operation_key` as the selected operation.
2. Before executing in any mode, ensure every required input the chosen path needs is already known from the `task` text or `context`. If ANY required parameter is missing, STOP and report the missing parameters as your result. Do not continue with tools or assumptions.
3. Then decide HOW the operation will run (exactly one of three modes):

(a) **Script-backed (工具流程)** — the operation appears in root `script_catalog`:
  - Call `execute_script` with `ontology_id`, `operation_key`, and concrete `arguments`.
  - Map concrete inputs from the `task` text / `context` into arguments. If required arguments are still missing, stop and report the blocker instead of guessing.
  - Do NOT load object definitions, do NOT call `business_data_search`, do NOT spawn sub-agents, and do NOT replay the markdown logic manually unless the script output explicitly requires a follow-up step.

(b) **Parallel fan-out** — the operation's `# 逻辑描述` contains an independent iteration over a collection: hand off via `spawn_sub_agent`. Your direct execution of that iteration ENDS at hand-off — do NOT pre-load object definitions, pre-run `business_data_search`, or partially execute the iteration body yourself; only consolidate the returned results afterwards.
  The following expressions in `# 逻辑描述` are strong signals that the logic contains an iteration over a collection (non-exhaustive):
      - "逐个/逐一/依次 + 分析/计算/执行/处理"
      - "对每个/对每条/对各项 + 执行"
      - "遍历 + 集合/列表/所有"
      - "从X到Y，逐个..."
      - 循环结构（for each, 对...中每一项）

  These expressions do NOT automatically justify serial execution. They mean you MUST perform an iteration-dependency check:
      - If each iteration is independent of the others, with no cross-iteration data dependency, no ordered write dependency, and no need for a later iteration to consume an earlier iteration's output, you MUST parallelize via `spawn_sub_agent`.
      - Only when iterations have a real dependency between rounds should the work remain in (c) serial execution.
  Batching rules:
      - ALWAYS batch all parallel tasks into ONE `spawn_sub_agent` call; never issue multiple separate calls for what is logically one fan-out.
      - If task B needs task A's output, run A first (or in an earlier batch), then spawn B with A's result in its context.

(c) **In-loop step-by-step** — only when neither (a) nor (b) applies (a single non-`工具流程` operation, no independent fan-out):
  1. Load the operation content via `ontology_context(resource="operation", key=context.operation_key)`.
  2. Execute strictly per `# 逻辑描述`, honoring every branch and condition. Do not assume beyond the document, do not skip steps.
  3. Use the operation's dependency/uses/entity association section to identify required business objects, rules, and metrics. Before `business_data_search`, prefer loading object definitions via `ontology_context(resource="object")`; before rule-based judgments or metric calculations, load referenced `rule` and `metric` specs via `ontology_context`.
  4. If the logic references `[[sub-process-name]]`, FIRST check root `script_catalog`. If the sub-process has a maintained script mapping, invoke `execute_script` for that operation instead of reconstructing the step with `business_data_search` or manual reasoning. Otherwise, load the matching sub-operation by key; if that sub-process has independent iteration over a collection, fan out via `spawn_sub_agent`, otherwise execute it inline.
  5. If the documented logic cannot be completed exactly as specified, STOP IMMEDIATELY and report the blocker. Do not invent workarounds or fabricate data.

## Hard Stop Rules (触发即终止，不得绕行)

Reporting a blocker IS a successful completion of this task. The parent agent needs the blocker signal, not a guessed answer. Do not treat "stop" as failure and do not keep searching for a way to produce an answer.

### Hard-stop triggers — if ANY condition below holds, STOP IMMEDIATELY and return a blocker result
- An operation or object required by the documented logic does not exist in `ontology_context`.
- A `[[sub-process-name]]` referenced by the logic has NO script mapping in `script_catalog` AND no corresponding operation key.
- A required input parameter for the chosen execution path is missing from both the `task` text and `context`.
- A tool returns an execution error that the documented logic does not explicitly tell you how to handle.
- An earlier step produced data that makes the documented branch infeasible (e.g. documented logic says "触发STRUCTURE_LIST维护并跳过", that branch IS the termination — do not continue with the other branch's data).

### Forbidden rationalizations (do NOT use these to keep going after a hard stop is triggered)
- "业务合理推断 / 业务常识 / 从业务角度" — the documented logic is authoritative; if it cannot run, you do not infer a result.
- "结果应该是 0 / 空 / N/A / 不受影响" — never fabricate a placeholder value for a step whose prerequisites are missing.
- "既然 X 缺失，我用 Y 代替" — no substitution across the documented variables (e.g. 变更后 in place of 变更前, 已完成批次 in place of 在制批次, SQL in place of a missing `business_data_search` target, 物料属性 in place of a missing 子流程).
- "让我换个脚本 / 换个 SQL / 换种措辞再查一次" — once a blocker is identified for a retrieval goal, do NOT retry the same goal through a different tool, query, phrasing, or table.
- "让我再仔细审视一下文档的逻辑" as a way to find a different branch that avoids the blocker. The branch you are on is the branch the data chose.

### Required blocker report (when stopping)
State at the top: which step of which operation blocked, which file / sub-process / parameter is missing, and what was confirmed to work before the block. Do NOT include tentative conclusions, "建议结果", or inferred values in the report.

## Result Format
Return compact, structured text:
- Lead with the deliverable (the final answer / structured result for the assigned task).
- Then list the operations and object definitions used and the key calculation/judgment steps for traceability.
- If blocked, state the blocker clearly at the top, then list what was attempted.
- Do not include long transcripts or raw tool outputs.
"""


class SubAgent:
    """Homogeneous sub-agent: isolated ReAct loop with the standard worker toolset."""

    def _build_system_prompt(
        self,
        *,
        workspace_id: Optional[str] = None,
        selected_ontology_id: Optional[str] = None,
        selected_ontology_version: Optional[str] = None,
    ) -> str:
        prompt = SUBAGENT_SYSTEM_PROMPT
        ontologies_section = build_ontologies_section(
            selected_ontology_id=selected_ontology_id,
            workspace_id=workspace_id,
        )
        if ontologies_section:
            prompt = "{}\n\n{}".format(prompt, ontologies_section)
        skills_section = (
            build_skills_section(workspace_id=workspace_id)
            if workspace_id
            else build_skills_section()
        )
        if skills_section:
            prompt = "{}\n\n{}".format(prompt, skills_section)
        return prompt

    @staticmethod
    def _build_user_prompt(task: str, context: Dict[str, Any]) -> str:
        import json as _json

        parts = ["## Task\n{}".format(task)]
        if context:
            parts.append("\n## Context")
            for key, value in context.items():
                if isinstance(value, (dict, list)):
                    parts.append("\n### {}\n```json\n{}\n```".format(
                        key, _json.dumps(value, indent=2, ensure_ascii=False)
                    ))
                else:
                    parts.append("\n### {}\n{}".format(key, value))
        return "\n".join(parts)

    def execute(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        event_emitter: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        task_id: Optional[str] = None,
        parent_tool_call_id: Optional[str] = None,
        parent_agent_mode: Optional[str] = None,
        group_id: Optional[str] = None,
        depth: Optional[int] = None,
        parent_subagent_id: Optional[str] = None,
        root_tool_call_id: Optional[str] = None,
        parent_model_profile_id: Optional[str] = None,
        parent_workspace_id: Optional[str] = None,
        subagent_manager: Any = None,
        protocol_event_observer: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        observer: Any = None,
        parent_selected_ontology_id: Optional[str] = None,
        parent_selected_ontology_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        logger.info("Sub-agent executing task: %s...", task[:100])
        subagent_id = "sub_{}".format(uuid.uuid4().hex[:10])
        resolved_task_id = str(task_id) if task_id is not None else ""

        def emit(event_type: str, payload: Dict[str, Any]) -> None:
            if not event_emitter:
                return
            try:
                event_emitter(event_type, payload)
            except Exception:
                logger.warning("Sub-agent failed to emit event %s", event_type, exc_info=True)

        common = {
            "subagent_id": subagent_id,
            "agent_type": "subagent",
            "task_id": resolved_task_id,
            "group_id": group_id,
            "depth": depth,
            "parent_subagent_id": parent_subagent_id,
            "root_tool_call_id": root_tool_call_id,
            "parent_tool_call_id": parent_tool_call_id,
        }

        def emit_engine_event(event_type: str, payload: Dict[str, Any]) -> None:
            enriched = dict(payload or {})
            enriched.setdefault("subagent_id", subagent_id)
            enriched.setdefault("task_id", resolved_task_id)
            enriched.setdefault("group_id", group_id)
            enriched.setdefault("depth", depth)
            enriched.setdefault("parent_subagent_id", parent_subagent_id)
            enriched.setdefault("root_tool_call_id", root_tool_call_id)
            emit(event_type, enriched)

        try:
            emit("subagent.started", {**common, "task": task})
            emit("subagent.progress", {**common, "status": "running", "step": "llm_call_started", "meta": {}})

            task_context = dict(context or {})
            effective_workspace_id = (
                str(parent_workspace_id or "").strip()
                or str(task_context.get("workspace_id") or "").strip()
                or None
            )
            # Selection is a backend authority boundary. A model-provided task
            # context may carry an ontology_id as data, but cannot establish the
            # selected-ontology scope used by prompts, schemas, and tools.
            task_context.pop("selected_ontology_id", None)
            task_context.pop("selected_ontology_version", None)
            task_context.pop("selected_version", None)
            effective_selected_ontology_id = (
                str(parent_selected_ontology_id or "").strip() or None
            )
            effective_selected_ontology_version = (
                str(parent_selected_ontology_version or "").strip() or None
            )
            if effective_selected_ontology_id:
                # The parent's selected ontology is a backend scope, not task input.
                task_context["selected_ontology_id"] = effective_selected_ontology_id
                task_context["ontology_id"] = effective_selected_ontology_id
            if effective_selected_ontology_version:
                task_context["selected_ontology_version"] = effective_selected_ontology_version
                task_context["selected_version"] = effective_selected_ontology_version
            system_prompt = self._build_system_prompt(
                workspace_id=effective_workspace_id,
                selected_ontology_id=effective_selected_ontology_id,
                selected_ontology_version=effective_selected_ontology_version,
            )
            user_prompt = self._build_user_prompt(task, task_context)
            token_index = {"content": 0, "reasoning": 0}

            def emit_token(token_type: str, token: str, _meta: Optional[Dict[str, Any]] = None) -> None:
                channel = "reasoning" if token_type == "reasoning" else "content"
                token_index[channel] += 1
                emit(
                    "subagent.delta",
                    {**common, "channel": channel, "text": token, "index": token_index[channel]},
                )

            from agent.core.engine import AgentLoopEngine

            engine = AgentLoopEngine(
                agent_config=get_llm_router().agent_config,
            )
            normalized_parent_mode = str(parent_agent_mode or "").strip().lower()
            child_user_context: Dict[str, Any] = {}
            if normalized_parent_mode in {"quick", "thinking", "multi_agent"}:
                child_user_context["agent_mode"] = normalized_parent_mode
            normalized_profile_id = str(parent_model_profile_id or "").strip()
            if normalized_profile_id:
                child_user_context["model_profile_id"] = normalized_profile_id
            if effective_selected_ontology_id:
                child_user_context["selected_ontology_id"] = effective_selected_ontology_id
            if effective_selected_ontology_version:
                child_user_context["selected_ontology_version"] = effective_selected_ontology_version
                child_user_context["selected_version"] = effective_selected_ontology_version
            initial_state = {
                "run_id": run_id,
                "session_id": session_id,
                "subagent_id": subagent_id,
                "user_id": None,
                "workspace_id": effective_workspace_id,
                "user_request": user_prompt,
                "user_context": child_user_context,
                "memory_context": None,
                "messages": [],
                "read_documents": None,
                "evolution_recall_records": [],
                "results": {},
                "completed_ids": [],
                "failed_ids": [],
                "errors": [],
                "debug": False,
                "final_output": None,
                "current_turn_id": 0,
            }
            runtime_lineage = {
                "group_id": group_id,
                "depth": depth,
                "subagent_id": subagent_id,
                "task_id": resolved_task_id,
                "parent_subagent_id": parent_subagent_id,
                "root_tool_call_id": root_tool_call_id,
            }
            agent_config = get_llm_router().agent_config
            iteration_limit = agent_config.sub_agent_max_iterations if agent_config else 10

            final_state = engine.run(
                initial_state,
                caller="subagent",
                system_prompt=system_prompt,
                iteration_limit=iteration_limit,
                token_callback=emit_token,
                tool_event_emitter=emit_engine_event,
                protocol_event_observer=protocol_event_observer,
                subagent_manager=subagent_manager,
                subagent_lineage=runtime_lineage,
                observer=observer,
            )
            emit("subagent.progress", {**common, "status": "running", "step": "llm_call_finished", "meta": {}})

            output_text = str(final_state.get("final_output") or "").strip()
            result: Dict[str, Any] = {
                "output": output_text,
                "_key_steps": self._build_key_steps(final_state),
                "_success": bool(output_text),
            }
            if not output_text:
                result["_error"] = "Sub-agent produced no output."

            emit("subagent.finished", {**common, "status": ("completed" if result["_success"] else "error"), "result": result})
            logger.info("Sub-agent completed (success=%s)", result["_success"])
            return result

        except WorkflowCancelledError as exc:
            logger.info("Sub-agent cancelled: %s", exc.message)
            emit("subagent.finished", {**common, "status": "cancelled", "error": exc.message})
            raise
        except Exception as exc:
            logger.exception("Sub-agent failed: %s", exc)
            emit("subagent.finished", {**common, "status": "error", "error": str(exc)})
            return {
                "output": "",
                "_key_steps": ["dispatch started"],
                "_success": False,
                "_error": str(exc),
            }

    @staticmethod
    def _build_key_steps(final_state: Dict[str, Any]) -> List[str]:
        steps: List[str] = ["dispatch started"]
        tool_names: List[str] = []
        for message in list(final_state.get("messages") or []):
            if message.get("role") != "tool":
                continue
            tool_name = str(message.get("name") or "").strip()
            if not tool_name:
                continue
            if tool_name not in tool_names:
                tool_names.append(tool_name)

        for tool_name in tool_names[:5]:
            if tool_name == "spawn_sub_agent":
                steps.append("nested child group completed")
                continue
            steps.append("completed tool {}".format(tool_name))

        if final_state.get("final_output") not in (None, ""):
            steps.append("final result produced")
        return steps
