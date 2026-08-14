"""Run-scoped orchestration for synchronous sub-agent task groups."""

from __future__ import annotations

import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from agent.exceptions import WorkflowCancelledError
from agent.runtime.async_utils import find_group_exception
from utils.logging import get_logger
from agent.sub_agents.base import SubAgent

logger = get_logger("sub_agents.manager")

_MANAGER_LOCK = threading.Lock()
_RUN_MANAGERS: Dict[str, "SubAgentManager"] = {}


@dataclass(frozen=True)
class SubAgentTaskSpec:
    id: str
    task: str
    context: Dict[str, Any]


class SubAgentManager:
    """Coordinate synchronous sub-agent fan-out/fan-in for a single run."""

    def __init__(self, *, max_concurrency: int, max_depth: int) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self.max_depth = max(1, int(max_depth))
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)
        self._async_semaphore: asyncio.BoundedSemaphore | None = None
        self._async_owner_loop: asyncio.AbstractEventLoop | None = None

    async def aexecute_group(
        self,
        *,
        run_id: Optional[str],
        session_id: Optional[str],
        parent_tool_call_id: Optional[str],
        parent_agent_mode: Optional[str],
        parent_model_profile_id: Optional[str],
        parent_workspace_id: Optional[str],
        event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
        lineage: Optional[Dict[str, Any]],
        tasks: List[SubAgentTaskSpec],
        observer: Any = None,
        parent_selected_ontology_id: Optional[str] = None,
        parent_selected_ontology_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run one sub-agent fan-out as an owned asyncio task group."""
        group_id = "group_{}".format(uuid.uuid4().hex[:10])
        lineage = dict(lineage or {})
        parent_depth = int(lineage.get("depth") or 0)
        child_depth = parent_depth + 1
        root_tool_call_id = (
            str(lineage.get("root_tool_call_id") or "").strip()
            or str(parent_tool_call_id or "").strip()
            or None
        )
        parent_subagent_id = str(lineage.get("subagent_id") or "").strip() or None

        if child_depth > self.max_depth:
            return {
                "group_id": group_id,
                "status": "error",
                "tasks": [],
                "summary": (
                    "spawn_sub_agent max depth exceeded: requested depth {}, limit {}."
                ).format(child_depth, self.max_depth),
            }

        task_index_by_id = {task.id: idx for idx, task in enumerate(tasks)}
        self._emit_group_started(
            event_emitter,
            group_id=group_id,
            depth=child_depth,
            parent_subagent_id=parent_subagent_id,
            root_tool_call_id=root_tool_call_id,
            parent_tool_call_id=parent_tool_call_id,
            tasks=tasks,
        )

        semaphore = self._get_async_semaphore()
        results_by_id: Dict[str, Dict[str, Any]] = {}

        async def execute_task(task_spec: SubAgentTaskSpec) -> None:
            results_by_id[task_spec.id] = await self._aexecute_single_task(
                run_id=run_id,
                session_id=session_id,
                parent_tool_call_id=parent_tool_call_id,
                parent_agent_mode=parent_agent_mode,
                parent_model_profile_id=parent_model_profile_id,
                parent_workspace_id=parent_workspace_id,
                root_tool_call_id=root_tool_call_id,
                parent_subagent_id=parent_subagent_id,
                group_id=group_id,
                depth=child_depth,
                event_emitter=event_emitter,
                task_spec=task_spec,
                task_index=task_index_by_id.get(task_spec.id, 0),
                observer=observer,
                parent_selected_ontology_id=parent_selected_ontology_id,
                parent_selected_ontology_version=parent_selected_ontology_version,
            )

        async def run_root_task(task_spec: SubAgentTaskSpec) -> None:
            async with semaphore:
                await execute_task(task_spec)

        async def run_nested_bucket(bucket: List[SubAgentTaskSpec]) -> None:
            for task_spec in bucket:
                await execute_task(task_spec)

        reserved_slots = 0

        async def run_group_tasks(task_group: asyncio.TaskGroup) -> None:
            nonlocal reserved_slots
            if parent_subagent_id is None:
                for task_spec in tasks:
                    task_group.create_task(
                        run_root_task(task_spec),
                        name="subagent-{}".format(task_spec.id),
                    )
                return

            # A nested group must be able to reuse its waiting parent's slot.
            # Reserve only slots that are free now, then run the remaining work
            # sequentially on the borrowed slot. This preserves the global
            # concurrency cap and cannot deadlock when the cap is one.
            available_slots = max(0, int(getattr(semaphore, "_value", 0)))
            slots_to_reserve = min(max(0, len(tasks) - 1), available_slots)
            for _ in range(slots_to_reserve):
                await semaphore.acquire()
                reserved_slots += 1
            worker_count = min(len(tasks), reserved_slots + 1) if tasks else 0
            buckets = [tasks[index::worker_count] for index in range(worker_count)]
            for index, bucket in enumerate(buckets):
                task_group.create_task(
                    run_nested_bucket(bucket),
                    name="subagent-nested-worker-{}".format(index),
                )

        try:
            async with asyncio.TaskGroup() as task_group:
                await run_group_tasks(task_group)
        except BaseExceptionGroup as exc_group:
            cancellation = find_group_exception(exc_group, WorkflowCancelledError)
            if cancellation is None:
                raise
            self._emit_group_finished(
                event_emitter,
                group_id=group_id,
                depth=child_depth,
                status="cancelled",
                task_count=len(tasks),
                completed_count=len(results_by_id),
            )
            raise cancellation from exc_group
        finally:
            for _ in range(reserved_slots):
                semaphore.release()

        ordered_results = [results_by_id[task.id] for task in tasks]
        group_status = (
            "completed"
            if all(item["status"] == "completed" for item in ordered_results)
            else "error"
        )
        self._emit_group_finished(
            event_emitter,
            group_id=group_id,
            depth=child_depth,
            status=group_status,
            task_count=len(ordered_results),
            completed_count=sum(
                1 for item in ordered_results if item["status"] == "completed"
            ),
        )
        return {
            "group_id": group_id,
            "status": group_status,
            "tasks": ordered_results,
            "summary": self._build_group_summary(ordered_results),
        }

    def execute_group(
        self,
        *,
        run_id: Optional[str],
        session_id: Optional[str],
        parent_tool_call_id: Optional[str],
        parent_agent_mode: Optional[str],
        parent_model_profile_id: Optional[str],
        parent_workspace_id: Optional[str],
        event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
        lineage: Optional[Dict[str, Any]],
        tasks: List[SubAgentTaskSpec],
        observer: Any = None,
        parent_selected_ontology_id: Optional[str] = None,
        parent_selected_ontology_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        group_id = "group_{}".format(uuid.uuid4().hex[:10])
        lineage = dict(lineage or {})
        parent_depth = int(lineage.get("depth") or 0)
        child_depth = parent_depth + 1
        root_tool_call_id = (
            str(lineage.get("root_tool_call_id") or "").strip()
            or str(parent_tool_call_id or "").strip()
            or None
        )
        parent_subagent_id = str(lineage.get("subagent_id") or "").strip() or None

        if child_depth > self.max_depth:
            return {
                "group_id": group_id,
                "status": "error",
                "tasks": [],
                "summary": (
                    "spawn_sub_agent max depth exceeded: requested depth {}, limit {}."
                ).format(child_depth, self.max_depth),
            }

        available_workers = self.max_concurrency
        borrow_parent_slot = parent_subagent_id is not None
        if borrow_parent_slot:
            available_workers = max(1, self._available_capacity() + 1)
        worker_count = max(1, min(len(tasks), available_workers))

        results_by_id: Dict[str, Dict[str, Any]] = {}
        borrowed_slot = threading.BoundedSemaphore(1) if borrow_parent_slot else None

        task_index_by_id: Dict[str, int] = {task.id: idx for idx, task in enumerate(tasks)}

        if event_emitter:
            try:
                event_emitter(
                    "subagent_group.started",
                    {
                        "group_id": group_id,
                        "depth": child_depth,
                        "parent_subagent_id": parent_subagent_id,
                        "root_tool_call_id": root_tool_call_id,
                        "parent_tool_call_id": parent_tool_call_id,
                        "task_count": len(tasks),
                        "tasks": [
                            {
                                "task_id": task.id,
                                "task": task.task,
                                "task_index": idx,
                            }
                            for idx, task in enumerate(tasks)
                        ],
                    },
                )
            except Exception:
                logger.warning("Failed to emit subagent_group.started", exc_info=True)

        def run_task(task_spec: SubAgentTaskSpec) -> Dict[str, Any]:
            using_borrowed_slot = bool(borrowed_slot and borrowed_slot.acquire(blocking=False))

            if not using_borrowed_slot:
                self._semaphore.acquire()

            try:
                return self._execute_single_task(
                    run_id=run_id,
                    session_id=session_id,
                    parent_tool_call_id=parent_tool_call_id,
                    parent_agent_mode=parent_agent_mode,
                    parent_model_profile_id=parent_model_profile_id,
                    parent_workspace_id=parent_workspace_id,
                    root_tool_call_id=root_tool_call_id,
                    parent_subagent_id=parent_subagent_id,
                    group_id=group_id,
                    depth=child_depth,
                    event_emitter=event_emitter,
                    task_spec=task_spec,
                    task_index=task_index_by_id.get(task_spec.id, 0),
                    observer=observer,
                    parent_selected_ontology_id=parent_selected_ontology_id,
                    parent_selected_ontology_version=parent_selected_ontology_version,
                )
            finally:
                if using_borrowed_slot and borrowed_slot:
                    borrowed_slot.release()
                else:
                    self._semaphore.release()

        cancelled_exc: Optional[WorkflowCancelledError] = None

        def run_task_with_context(captured_context, task: SubAgentTaskSpec) -> Dict[str, Any]:
            return captured_context.run(run_task, task)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(run_task_with_context, copy_context(), task): task
                for task in tasks
            }
            for future in as_completed(future_map):
                task = future_map[future]
                try:
                    results_by_id[task.id] = future.result()
                except WorkflowCancelledError as exc:
                    cancelled_exc = exc
                    logger.info(
                        "Sub-agent task group %s cancelled while waiting for task '%s'",
                        group_id,
                        task.id,
                    )
                    break
                except Exception as exc:
                    logger.exception("Sub-agent task '%s' failed in task-group executor: %s", task.id, exc)
                    results_by_id[task.id] = {
                        "id": task.id,
                        "status": "error",
                        "key_steps": ["dispatch started"],
                        "result": None,
                        "error": str(exc),
                    }

        if cancelled_exc is not None:
            if event_emitter:
                try:
                    event_emitter(
                        "subagent_group.finished",
                        {
                            "group_id": group_id,
                            "depth": child_depth,
                            "status": "cancelled",
                            "task_count": len(tasks),
                            "completed_count": len(results_by_id),
                            "error_count": max(1, len(tasks) - len(results_by_id)),
                        },
                    )
                except Exception:
                    logger.warning("Failed to emit subagent_group.finished", exc_info=True)
            raise cancelled_exc

        ordered_results = [results_by_id[task.id] for task in tasks]
        group_status = "completed" if all(item["status"] == "completed" for item in ordered_results) else "error"

        if event_emitter:
            try:
                completed_count = sum(1 for item in ordered_results if item["status"] == "completed")
                error_count = len(ordered_results) - completed_count
                event_emitter(
                    "subagent_group.finished",
                    {
                        "group_id": group_id,
                        "depth": child_depth,
                        "status": group_status,
                        "task_count": len(ordered_results),
                        "completed_count": completed_count,
                        "error_count": error_count,
                    },
                )
            except Exception:
                logger.warning("Failed to emit subagent_group.finished", exc_info=True)

        return {
            "group_id": group_id,
            "status": group_status,
            "tasks": ordered_results,
            "summary": self._build_group_summary(ordered_results),
        }

    def _execute_single_task(
        self,
        *,
        run_id: Optional[str],
        session_id: Optional[str],
        parent_tool_call_id: Optional[str],
        parent_agent_mode: Optional[str],
        parent_model_profile_id: Optional[str],
        parent_workspace_id: Optional[str],
        root_tool_call_id: Optional[str],
        parent_subagent_id: Optional[str],
        group_id: str,
        depth: int,
        event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
        task_spec: SubAgentTaskSpec,
        task_index: int = 0,
        observer: Any = None,
        parent_selected_ontology_id: Optional[str] = None,
        parent_selected_ontology_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        key_steps, record_step, protocol_event_observer, scoped_event_emitter = (
            self._build_task_execution_context(
                event_emitter=event_emitter,
                task_spec=task_spec,
                task_index=task_index,
                group_id=group_id,
                depth=depth,
                parent_subagent_id=parent_subagent_id,
                root_tool_call_id=root_tool_call_id,
            )
        )

        try:
            sub_agent = SubAgent()
            raw_result = sub_agent.execute(
                task_spec.task,
                task_spec.context,
                run_id=run_id,
                session_id=session_id,
                event_emitter=scoped_event_emitter,
                task_id=task_spec.id,
                parent_tool_call_id=parent_tool_call_id,
                parent_agent_mode=parent_agent_mode,
                group_id=group_id,
                depth=depth,
                parent_subagent_id=parent_subagent_id,
                root_tool_call_id=root_tool_call_id,
                parent_model_profile_id=parent_model_profile_id,
                parent_workspace_id=parent_workspace_id,
                parent_selected_ontology_id=parent_selected_ontology_id,
                parent_selected_ontology_version=parent_selected_ontology_version,
                subagent_manager=self,
                protocol_event_observer=protocol_event_observer,
                observer=observer,
            )
        except WorkflowCancelledError:
            raise
        except Exception as exc:
            return self._failed_task_result(task_spec, key_steps, record_step, exc)

        return self._normalize_task_result(
            task_spec,
            raw_result,
            key_steps,
            record_step,
        )

    async def _aexecute_single_task(
        self,
        *,
        run_id: Optional[str],
        session_id: Optional[str],
        parent_tool_call_id: Optional[str],
        parent_agent_mode: Optional[str],
        parent_model_profile_id: Optional[str],
        parent_workspace_id: Optional[str],
        root_tool_call_id: Optional[str],
        parent_subagent_id: Optional[str],
        group_id: str,
        depth: int,
        event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
        task_spec: SubAgentTaskSpec,
        task_index: int = 0,
        observer: Any = None,
        parent_selected_ontology_id: Optional[str] = None,
        parent_selected_ontology_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        key_steps, record_step, protocol_event_observer, scoped_event_emitter = (
            self._build_task_execution_context(
                event_emitter=event_emitter,
                task_spec=task_spec,
                task_index=task_index,
                group_id=group_id,
                depth=depth,
                parent_subagent_id=parent_subagent_id,
                root_tool_call_id=root_tool_call_id,
            )
        )
        try:
            raw_result = await SubAgent().aexecute(
                task_spec.task,
                task_spec.context,
                run_id=run_id,
                session_id=session_id,
                event_emitter=scoped_event_emitter,
                task_id=task_spec.id,
                parent_tool_call_id=parent_tool_call_id,
                parent_agent_mode=parent_agent_mode,
                group_id=group_id,
                depth=depth,
                parent_subagent_id=parent_subagent_id,
                root_tool_call_id=root_tool_call_id,
                parent_model_profile_id=parent_model_profile_id,
                parent_workspace_id=parent_workspace_id,
                parent_selected_ontology_id=parent_selected_ontology_id,
                parent_selected_ontology_version=parent_selected_ontology_version,
                subagent_manager=self,
                protocol_event_observer=protocol_event_observer,
                observer=observer,
            )
        except WorkflowCancelledError:
            raise
        except Exception as exc:
            return self._failed_task_result(task_spec, key_steps, record_step, exc)

        return self._normalize_task_result(
            task_spec,
            raw_result,
            key_steps,
            record_step,
        )

    @staticmethod
    def _build_task_execution_context(
        *,
        event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
        task_spec: SubAgentTaskSpec,
        task_index: int,
        group_id: str,
        depth: int,
        parent_subagent_id: Optional[str],
        root_tool_call_id: Optional[str],
    ) -> tuple[
        List[str],
        Callable[[str], None],
        Callable[[str, Dict[str, Any]], None],
        Callable[[str, Dict[str, Any]], None],
    ]:
        key_steps: List[str] = ["dispatch started"]

        def record_step(step: str) -> None:
            normalized = str(step or "").strip()
            if not normalized:
                return
            if key_steps and key_steps[-1] == normalized:
                return
            if normalized in key_steps:
                return
            if len(key_steps) >= 8:
                return
            key_steps.append(normalized)

        def protocol_event_observer(event_type: str, payload: Dict[str, Any]) -> None:
            if event_type != "tool.finished":
                return
            tool_name = str(payload.get("tool_name") or "").strip()
            if tool_name == "spawn_sub_agent":
                record_step("nested child group completed")
            elif tool_name:
                record_step("completed tool {}".format(tool_name))

        def scoped_event_emitter(event_type: str, payload: Dict[str, Any]) -> None:
            if event_type == "subagent.progress":
                step_name = str(payload.get("step") or "").strip()
                if step_name == "llm_call_started":
                    record_step("llm call started")
                elif step_name == "llm_call_finished":
                    record_step("llm call finished")
            if not event_emitter:
                return
            enriched = dict(payload or {})
            enriched.setdefault("task_id", task_spec.id)
            enriched.setdefault("task_index", task_index)
            enriched.setdefault("group_id", group_id)
            enriched.setdefault("depth", depth)
            enriched.setdefault("parent_subagent_id", parent_subagent_id)
            enriched.setdefault("root_tool_call_id", root_tool_call_id)
            event_emitter(event_type, enriched)

        return key_steps, record_step, protocol_event_observer, scoped_event_emitter

    @staticmethod
    def _failed_task_result(
        task_spec: SubAgentTaskSpec,
        key_steps: List[str],
        record_step: Callable[[str], None],
        exc: Exception,
    ) -> Dict[str, Any]:
        logger.exception("Sub-agent task '%s' failed before completion: %s", task_spec.id, exc)
        record_step("final result produced")
        return {
            "id": task_spec.id,
            "status": "error",
            "key_steps": key_steps,
            "result": None,
            "error": str(exc),
        }

    def _normalize_task_result(
        self,
        task_spec: SubAgentTaskSpec,
        raw_result: Optional[Dict[str, Any]],
        key_steps: List[str],
        record_step: Callable[[str], None],
    ) -> Dict[str, Any]:
        raw_key_steps = [
            str(step).strip()
            for step in list(dict(raw_result or {}).get("_key_steps") or [])
            if str(step).strip()
        ]
        if raw_key_steps:
            key_steps = raw_key_steps
        else:
            record_step("final result produced")
        result_payload = {
            key: value
            for key, value in dict(raw_result or {}).items()
            if not str(key).startswith("_")
        }
        return {
            "id": task_spec.id,
            "status": self._resolve_task_status(raw_result),
            "key_steps": key_steps,
            "result": result_payload,
            "error": self._resolve_task_error(raw_result),
        }

    def _available_capacity(self) -> int:
        return max(0, int(getattr(self._semaphore, "_value", 0)))

    def _get_async_semaphore(self) -> asyncio.BoundedSemaphore:
        loop = asyncio.get_running_loop()
        if self._async_owner_loop is None:
            self._async_owner_loop = loop
            self._async_semaphore = asyncio.BoundedSemaphore(self.max_concurrency)
        elif self._async_owner_loop is not loop:
            if self._async_owner_loop.is_closed():
                self._async_owner_loop = loop
                self._async_semaphore = asyncio.BoundedSemaphore(self.max_concurrency)
            else:
                raise RuntimeError("SubAgentManager is bound to a different event loop")
        semaphore = self._async_semaphore
        if semaphore is None:
            raise RuntimeError("SubAgentManager semaphore initialization failed")
        return semaphore

    @staticmethod
    def _emit_group_started(
        event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
        *,
        group_id: str,
        depth: int,
        parent_subagent_id: Optional[str],
        root_tool_call_id: Optional[str],
        parent_tool_call_id: Optional[str],
        tasks: List[SubAgentTaskSpec],
    ) -> None:
        if not event_emitter:
            return
        try:
            event_emitter(
                "subagent_group.started",
                {
                    "group_id": group_id,
                    "depth": depth,
                    "parent_subagent_id": parent_subagent_id,
                    "root_tool_call_id": root_tool_call_id,
                    "parent_tool_call_id": parent_tool_call_id,
                    "task_count": len(tasks),
                    "tasks": [
                        {
                            "task_id": task.id,
                            "task": task.task,
                            "task_index": index,
                        }
                        for index, task in enumerate(tasks)
                    ],
                },
            )
        except Exception:
            logger.warning("Failed to emit subagent_group.started", exc_info=True)

    @staticmethod
    def _emit_group_finished(
        event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
        *,
        group_id: str,
        depth: int,
        status: str,
        task_count: int,
        completed_count: int,
    ) -> None:
        if not event_emitter:
            return
        try:
            event_emitter(
                "subagent_group.finished",
                {
                    "group_id": group_id,
                    "depth": depth,
                    "status": status,
                    "task_count": task_count,
                    "completed_count": completed_count,
                    "error_count": max(0, task_count - completed_count),
                },
            )
        except Exception:
            logger.warning("Failed to emit subagent_group.finished", exc_info=True)

    @staticmethod
    def _resolve_task_status(result: Optional[Dict[str, Any]]) -> str:
        payload = dict(result or {})
        if payload.get("_success") is False:
            return "error"
        if payload.get("_error"):
            return "error"
        return "completed"

    @staticmethod
    def _resolve_task_error(result: Optional[Dict[str, Any]]) -> Optional[str]:
        payload = dict(result or {})
        if payload.get("_error"):
            return str(payload.get("_error"))
        return None

    @staticmethod
    def _build_group_summary(task_results: List[Dict[str, Any]]) -> str:
        total = len(task_results)
        failures = sum(1 for item in task_results if item.get("status") != "completed")
        successes = total - failures
        if failures == 0:
            return "Completed {} sub-agent task(s).".format(total)
        return "Completed {} sub-agent task(s) with {} failure(s).".format(successes, failures)



def get_subagent_manager(run_id: Optional[str], agent_config: Any) -> SubAgentManager:
    key = str(run_id or "__default__")
    with _MANAGER_LOCK:
        manager = _RUN_MANAGERS.get(key)
        if manager is None:
            manager = SubAgentManager(
                max_concurrency=getattr(agent_config, "sub_agent_max_concurrency", 4),
                max_depth=getattr(agent_config, "sub_agent_max_depth", 2),
            )
            _RUN_MANAGERS[key] = manager
        return manager


def clear_subagent_manager(run_id: Optional[str]) -> None:
    key = str(run_id or "__default__")
    with _MANAGER_LOCK:
        _RUN_MANAGERS.pop(key, None)


def clear_subagent_manager_registry() -> None:
    with _MANAGER_LOCK:
        _RUN_MANAGERS.clear()
