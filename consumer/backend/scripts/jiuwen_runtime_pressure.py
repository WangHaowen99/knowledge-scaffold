#!/usr/bin/env python3
"""Offline pressure check for the owned OpenJiuwen runtime lifecycle.

The script uses the real OpenJiuwen 0.1.14 ReActAgent with an in-process
OfflineModel. It never opens a network connection. Defaults exercise 500 short
runs in 25 batches of 20 concurrent model calls.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any

from openjiuwen.core.foundation.llm.schema.message import AssistantMessage
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.checkpointer import CheckpointerFactory

import agent.core.jiuwen_engine as jiuwen_engine
from agent.runtime.lifecycle import get_jiuwen_runner_lifecycle


class _Observer:
    enabled = True

    def emit(self, event: Any) -> bool:
        del event
        return True


class _BatchBarrier:
    def __init__(self, size: int) -> None:
        self.size = size
        self.condition: asyncio.Condition | None = None
        self.arrived = 0
        self.generation = 0
        self.generations = 0
        self.active = 0
        self.max_active = 0
        self.peak: dict[str, int] | None = None
        self.loop_thread_ids: set[int] = set()

    async def arrive(self) -> None:
        if self.condition is None:
            self.condition = asyncio.Condition()
        async with self.condition:
            generation = self.generation
            self.arrived += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.loop_thread_ids.add(threading.get_ident())
            if self.arrived == self.size:
                self.peak = _snapshot()
                self.arrived = 0
                self.generation += 1
                self.generations += 1
                self.condition.notify_all()
                return
            await asyncio.wait_for(
                self.condition.wait_for(lambda: self.generation != generation),
                timeout=30,
            )

    async def leave(self) -> None:
        assert self.condition is not None
        async with self.condition:
            self.active -= 1


def _checkpointer_stats() -> dict[str, int]:
    checkpointer = CheckpointerFactory.get_checkpointer()
    stores = getattr(checkpointer, "_agent_stores", {})
    blobs = [
        blob
        for store in stores.values()
        for blob in getattr(store, "state_blobs", {}).values()
    ]
    return {
        "agent_stores": len(stores),
        "agent_state_blobs": len(blobs),
        "agent_state_blob_bytes": sum(
            len(blob[1])
            for blob in blobs
            if isinstance(blob, tuple)
            and len(blob) > 1
            and isinstance(blob[1], (bytes, bytearray))
        ),
        "workflow_stores": len(getattr(checkpointer, "_workflow_stores", {})),
        "agent_team_stores": len(getattr(checkpointer, "_agent_team_stores", {})),
    }


def _snapshot() -> dict[str, int]:
    manager = Runner.resource_mgr
    tag_manager = manager._tag_mgr
    tool_manager = manager._resource_registry.tool()
    callbacks = Runner.callback_framework
    callback_lists = list(callbacks._callbacks.values())
    return {
        "resource_cards": len(manager._id_to_card),
        "tool_registry": len(tool_manager._tools),
        "tag_resources": len(tag_manager._resource_tags),
        "tag_keys": len(tag_manager._tag_to_resource),
        "tag_links": sum(len(value) for value in tag_manager._tag_to_resource.values()),
        "callback_event_keys": len(callbacks._callbacks),
        "callback_nonempty_events": sum(bool(value) for value in callback_lists),
        "callbacks_total": sum(len(value) for value in callback_lists),
        "callback_metrics": len(callbacks._metrics),
        "callback_locks": len(callbacks._locks),
        **_checkpointer_stats(),
    }


def _agent_config() -> Any:
    return SimpleNamespace(
        main_agent_max_iterations=1,
        sub_agent_max_iterations=1,
        context_max_rounds=10,
        web_search=SimpleNamespace(enabled=False),
        limits=SimpleNamespace(model_output_token_limit=4096),
    )


def _llm_config() -> Any:
    return SimpleNamespace(
        provider="openai",
        model="offline",
        api_key="",
        base_url="http://127.0.0.1/unused",
        timeout=1,
        temperature=0,
        max_tokens=64,
    )


async def _loop_probe() -> dict[str, int]:
    current = asyncio.current_task()
    loop = asyncio.get_running_loop()
    return {
        "thread_id": threading.get_ident(),
        "pending_tasks_excluding_probe": sum(
            task is not current and not task.done()
            for task in asyncio.all_tasks()
        ),
        "ready_handles": sum(
            not handle.cancelled() for handle in loop._ready
        ),
        "scheduled_handles": sum(
            not handle.cancelled() for handle in loop._scheduled
        ),
    }


def _cleanup_fields(snapshot: dict[str, int]) -> dict[str, int]:
    fields = (
        "resource_cards",
        "tool_registry",
        "tag_resources",
        "tag_links",
        "callback_event_keys",
        "callback_nonempty_events",
        "callbacks_total",
        "callback_metrics",
        "agent_stores",
        "agent_state_blobs",
        "agent_state_blob_bytes",
        "workflow_stores",
        "agent_team_stores",
    )
    return {field: snapshot[field] for field in fields}


async def _run_pressure(*, runs: int, workers: int) -> dict[str, Any]:
    if runs < workers or runs % workers:
        raise ValueError("runs must be a positive multiple of workers")

    logging.disable(logging.CRITICAL)
    observer = _Observer()
    barrier = _BatchBarrier(workers)

    class OfflineModel:
        def supports_kv_cache_release(self) -> bool:
            return False

        async def invoke(self, *, model, messages, tools=None, **kwargs):
            del model, messages, tools, kwargs
            await barrier.arrive()
            try:
                return AssistantMessage(content="ok-offline", tool_calls=[])
            finally:
                await barrier.leave()

    original_create = jiuwen_engine.create_default_jiuwen_agent
    original_resolve = jiuwen_engine._resolve_jiuwen_llm_config

    def create_offline_agent(**kwargs):
        agent = original_create(**kwargs)
        agent.set_llm(OfflineModel())
        return agent

    before = _snapshot()
    threads_before = [
        (thread.ident, thread.is_alive())
        for thread in threading.enumerate()
        if thread.name == "jiuwen-loop"
    ]
    expected_tools_per_run = len(
        jiuwen_engine.get_jiuwen_tool_infos(
            "main",
            include_orchestration=False,
            include_web_search=False,
        )
    )
    errors: list[str] = []
    started_at = time.perf_counter()

    semaphore = asyncio.Semaphore(workers)

    async def run_one(index: int) -> int:
        state = {
            "run_id": "pressure-real-{}".format(index),
            "session_id": "pressure-shared-business-session",
            "current_turn_id": index + 1,
            "user_request": "offline pressure run",
            "messages": [],
            "results": {},
            "user_context": {
                "agent_mode": "single_agent",
                "llm_request_mode": "sync",
            },
        }
        async with semaphore:
            result = await jiuwen_engine.JiuwenReactLoopEngine(
                agent_config=_agent_config()
            ).arun(
                state,
                caller="main",
                system_prompt="offline pressure prompt",
                observer=observer,
            )
        if result.get("final_output") != "ok-offline":
            raise AssertionError(
                "unexpected result for {}: {!r}".format(
                    index,
                    result.get("final_output"),
                )
            )
        return index

    jiuwen_engine._resolve_jiuwen_llm_config = lambda *args, **kwargs: _llm_config()
    jiuwen_engine.create_default_jiuwen_agent = create_offline_agent
    lifecycle = get_jiuwen_runner_lifecycle()
    started_here = False
    try:
        started_here = await lifecycle.start(mode="async")
        results = await asyncio.gather(
            *(run_one(index) for index in range(runs)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                errors.append(
                    "{}: {}".format(type(result).__name__, result)
                )
    finally:
        jiuwen_engine.create_default_jiuwen_agent = original_create
        jiuwen_engine._resolve_jiuwen_llm_config = original_resolve
        if started_here:
            await lifecycle.stop()

    elapsed_seconds = time.perf_counter() - started_at
    gc.collect()
    after = _snapshot()
    loop_probe = await _loop_probe()
    threads_after = [
        (thread.ident, thread.is_alive())
        for thread in threading.enumerate()
        if thread.name == "jiuwen-loop"
    ]
    peak = barrier.peak or {}
    owned_before = _cleanup_fields(before)
    owned_after = _cleanup_fields(after)
    clean = owned_after == owned_before
    owned_resources_zero = all(value == 0 for value in owned_after.values())
    peak_matches = (
        peak.get("resource_cards") == expected_tools_per_run * workers
        and peak.get("tool_registry") == expected_tools_per_run * workers
        and peak.get("callbacks_total") == 4 * workers
    )
    loop_clean = (
        loop_probe["pending_tasks_excluding_probe"] == 0
        and loop_probe["ready_handles"] == 0
        and loop_probe["scheduled_handles"] == 0
    )
    result = {
        "runs": runs,
        "workers": workers,
        "successes": runs - len(errors),
        "errors": errors[:10],
        "elapsed_seconds": round(elapsed_seconds, 3),
        "expected_tools_per_run": expected_tools_per_run,
        "barrier_generations": barrier.generations,
        "max_simultaneous_model_calls": barrier.max_active,
        "loop_thread_ids_seen": sorted(barrier.loop_thread_ids),
        "before": before,
        "peak": peak,
        "after": after,
        "owned_resources_after": owned_after,
        "loop_threads_before": threads_before,
        "loop_threads_after": threads_after,
        "loop_probe": loop_probe,
        "clean": clean,
        "owned_resources_zero": owned_resources_zero,
        "peak_matches": peak_matches,
        "loop_clean": loop_clean,
    }
    if (
        errors
        or barrier.generations != runs // workers
        or barrier.max_active != workers
        or not clean
        or not owned_resources_zero
        or not peak_matches
        or not loop_clean
    ):
        raise RuntimeError(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def run_pressure(*, runs: int, workers: int) -> dict[str, Any]:
    """Run the pressure experiment on one owned native event loop."""

    return asyncio.run(_run_pressure(runs=runs, workers=workers))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=500)
    parser.add_argument("--workers", type=int, default=20)
    args = parser.parse_args()
    result = run_pressure(runs=args.runs, workers=args.workers)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
