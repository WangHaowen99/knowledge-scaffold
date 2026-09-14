# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Tests for operation-evolution patcher behavior."""

from __future__ import annotations

from internal_access import internal
import asyncio

from agent.evolution import registry
from agent.evolution.models import Aggregate
from agent.evolution.patcher import OperationPatcher, PatcherConfig, StagedMergeOutput
from agent.runtime.async_utils import (
    clear_bounded_offload_pools,
    configure_bounded_offload_pools,
)
from agent.runtime.offload import BoundedOffloadPool
from agent.runtime.supervisor import RunTaskSupervisor
from llm import LLMPurpose


def _aggregate(fp: str) -> Aggregate:
    return Aggregate(
        fingerprint=fp,
        workspace_id=None,
        ontology_id="example",
        operation_key="OP_A",
        kind="procedure",
        canonical_content="先读 operation，再执行脚本",
        occurrences=1,
        active_endorsements=1,
        applied_count=0,
        rejection_count=0,
        first_seen=1.0,
        last_seen=1.0,
    )


def test_normalize_merged_fps_accepts_unique_visible_prefixes():
    agg = _aggregate("abcdef1234567890abcdef1234567890abcdef12")

    merged = internal(OperationPatcher).normalize_merged_fps(
        ["abcdef123456"],
        [agg],
    )

    assert merged == [agg.fingerprint]


def test_normalize_merged_fps_rejects_ambiguous_prefixes():
    first = _aggregate("abcdef1234567890abcdef1234567890abcdef12")
    second = _aggregate("abcdef1234560000abcdef1234567890abcdef12")

    merged = internal(OperationPatcher).normalize_merged_fps(
        ["abcdef123456"],
        [first, second],
    )

    assert merged == []


def test_llm_merge_requests_json_object_response(monkeypatch):
    captured = {}

    class _TaskLLM:
        is_configured = True

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def call(self, **kwargs):
            captured.update(kwargs)

            class _Response:
                data = StagedMergeOutput(
                    new_markdown="# OP_A\n\nupdated",
                    change_summary="合并测试 proposal",
                    incorporated_fingerprints=["abcdef1234567890abcdef1234567890abcdef12"],
                )

            return _Response()

    class _Router:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def get_service(self, purpose):
            assert purpose == LLMPurpose.TASK
            return _TaskLLM()

    monkeypatch.setattr(
        "agent.evolution.patcher.get_llm_router",
        lambda: _Router(),
    )
    patcher = OperationPatcher(
        buffer=object(),
        config=PatcherConfig(),
    )
    agg = _aggregate("abcdef1234567890abcdef1234567890abcdef12")

    result = internal(patcher).llm_merge(
        ontology_id="example",
        operation_key="OP_A",
        current_md="# OP_A",
        actionable=[agg],
    )

    assert result is not None
    assert captured["response_model"] is StagedMergeOutput
    assert captured["response_format"] == {"type": "json_object"}


def test_update_config_changes_threshold_decisions():
    agg = _aggregate("abcdef1234567890abcdef1234567890abcdef12")
    agg.sources = {"passive": 1}
    agg.active_endorsements = 0

    class _Buffer:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def list_aggregates(self, *args, **kwargs):
            del args, kwargs
            return [agg]

    patcher = OperationPatcher(
        buffer=_Buffer(),
        config=PatcherConfig(
            min_occurrences=3,
            min_applied=2,
        ),
    )

    assert internal(patcher).collect_actionable("example", "OP_A") == []

    patcher.update_config(
        PatcherConfig(
            min_occurrences=1,
            min_applied=0,
        )
    )

    assert internal(patcher).collect_actionable("example", "OP_A") == [agg]


def test_init_operation_patcher_updates_existing_singleton_config(monkeypatch):
    registry.reset_operation_patcher()

    class _Buffer:
        def __init__(self):
            self.listener = None

        def set_change_listener(self, listener):
            self.listener = listener

    class _Patcher:
        def __init__(self, *, buffer, config):
            self.buffer = buffer
            self.config = config
            self.updated = []
            self.cancelled = False

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def on_buffer_change(self, *args, **kwargs):
            del args, kwargs

        def cancel_nowait(self):
            self.cancelled = True

        def update_config(self, config):
            self.updated.append(config)

    buffer = _Buffer()
    monkeypatch.setattr(registry, "get_proposal_buffer", lambda project_root=None: buffer)
    monkeypatch.setattr(registry, "OperationPatcher", _Patcher)

    first = registry.init_operation_patcher(config=PatcherConfig(min_occurrences=3))
    second = registry.init_operation_patcher(config=PatcherConfig(min_occurrences=7))

    assert second is first
    assert first.config.min_occurrences == 3
    assert [cfg.min_occurrences for cfg in first.updated] == [7]

    registry.reset_operation_patcher()


def test_patcher_close_cancels_sleeping_sweep_without_consuming_deadline():
    class _Buffer:
        @staticmethod
        def iter_op_dirs():
            return []

    async def scenario():
        pools = [
            BoundedOffloadPool(
                name=f"patcher-close-{name}",
                max_workers=1,
                max_pending=2,
                capacity_wait_seconds=0.05,
                operation_timeout_seconds=1.0,
            )
            for name in ("short", "external", "tool")
        ]
        configure_bounded_offload_pools(
            short_io=pools[0],
            external_io=pools[1],
            tool_io=pools[2],
        )
        supervisor = RunTaskSupervisor()
        patcher = OperationPatcher(buffer=_Buffer(), config=PatcherConfig())
        patcher.bind_supervisor(supervisor)
        loop = asyncio.get_running_loop()
        try:
            assert await patcher.start()
            await asyncio.sleep(0)

            closed = await patcher.close(deadline=loop.time() + 0.05)

            assert closed is True
            assert supervisor.active_derived_count == 0
        finally:
            await supervisor.close(deadline=loop.time() + 0.2)
            for pool in pools:
                await pool.close(deadline=loop.time() + 0.2)
            clear_bounded_offload_pools()

    asyncio.run(scenario())
