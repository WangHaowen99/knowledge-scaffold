"""Tests for operation-evolution patcher behavior."""

from __future__ import annotations

from agent.evolution.models import Aggregate
from agent.evolution import registry
from agent.evolution.patcher import OperationPatcher
from agent.evolution.patcher import PatcherConfig
from agent.evolution.patcher import StagedMergeOutput
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

    merged = OperationPatcher._normalize_merged_fps(
        ["abcdef123456"],
        [agg],
    )

    assert merged == [agg.fingerprint]


def test_normalize_merged_fps_rejects_ambiguous_prefixes():
    first = _aggregate("abcdef1234567890abcdef1234567890abcdef12")
    second = _aggregate("abcdef1234560000abcdef1234567890abcdef12")

    merged = OperationPatcher._normalize_merged_fps(
        ["abcdef123456"],
        [first, second],
    )

    assert merged == []


def test_llm_merge_requests_json_object_response(monkeypatch):
    captured = {}

    class _TaskLLM:
        is_configured = True

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

    result = patcher._llm_merge(
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

    assert patcher._collect_actionable("example", "OP_A") == []

    patcher.update_config(
        PatcherConfig(
            min_occurrences=1,
            min_applied=0,
        )
    )

    assert patcher._collect_actionable("example", "OP_A") == [agg]


def test_init_operation_patcher_updates_existing_singleton_config(monkeypatch):
    registry.reset_operation_patcher()

    class _Buffer:
        def set_change_listener(self, listener):
            self.listener = listener

    class _Patcher:
        def __init__(self, *, buffer, config):
            self.buffer = buffer
            self.config = config
            self.updated = []

        def on_buffer_change(self, *args, **kwargs):
            del args, kwargs

        def start(self):
            self.started = True

        def shutdown(self, wait=False):
            self.shutdown_wait = wait

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


def test_patcher_does_not_stage_sidecars_inside_deterministic_version(
    monkeypatch,
    tmp_path,
):
    version_dir = tmp_path / "ontology" / "versions" / "1.0.0"
    operations_dir = version_dir / "operations"
    operations_dir.mkdir(parents=True)
    op_path = operations_dir / "OP_A.md"
    op_path.write_text("# OP_A\n", encoding="utf-8")
    (version_dir / ".deterministic-lock.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "agent.evolution.patcher.resolve_operation_md",
        lambda *_args, **_kwargs: op_path,
    )
    patcher = OperationPatcher(buffer=object(), config=PatcherConfig())
    monkeypatch.setattr(
        patcher,
        "_llm_merge",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("deterministic package must be rejected before LLM")
        ),
    )

    patcher._scan("example", "OP_A", workspace_id="workspace-a")

    assert not op_path.with_suffix(".md.staged").exists()
    assert not op_path.with_suffix(".md.staged.meta.json").exists()
