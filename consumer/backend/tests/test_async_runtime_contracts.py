# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Phase-0 contract tests for the A/B async Runtime boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from agent.runtime import (
    PublicEvent,
    PublicEventAdapter,
    PublicEventDraft,
    RuntimeErrorInfo,
    RuntimeEvent,
    RuntimeHistoryMessage,
    RuntimeInvocationContext,
    RuntimeRecoveryAction,
    RuntimeResult,
)
from agent.runtime.contracts import RunPreflightSnapshot
from agent.runtime.mock import FixedAsyncAgentRuntime, ListRuntimeEventSink
from agent.runtime.preflight import (
    build_run_preflight_snapshot,
    ontology_preflight_revision_matches,
)
from agent.runtime.preflight_cache import ImmutablePreflightCache
from backend_synthetic_credentials import synthetic_invalid_password, synthetic_secret
from ontology.view_cache import (
    build_package_content_revision,
    build_package_revision_source_signature,
    get_ontology_mutation_revision,
)
from pydantic import ValidationError
from internal_access import internal


def _context(run_id: str = "run-01", **arguments) -> RuntimeInvocationContext:
    return RuntimeInvocationContext(
        run_id=run_id,
        session_id="session-01",
        turn_id=1,
        workspace_id="workspace-01",
        username="alice",
        query="请执行阶段 0 测试",
        agent_mode="thinking",
        llm_request_mode="stream",
        arguments=arguments or None,
    )


def test_run_preflight_snapshot_is_immutable_and_excluded_from_public_context():
    snapshot = RunPreflightSnapshot(
        workspace_id="workspace-a",
        workspace_config_generation=1,
        system_config_generation=2,
        ontology_id="ontology-a",
        ontology_version="1.0.0",
        mutation_revision=3,
        ontology_manifest={"id": "ontology-a", "version": "1.0.0"},
        root_payload={"operations_catalog": [{"operation_key": "synthetic"}]},
        selected_ontology_context="<Selected-Ontology />",
        preload_records=({"path": "index.md"},),
        model_profile={"id": "model-a"},
        deterministic_candidate=False,
    )
    context = _context().model_copy(update={"preflight_snapshot": snapshot})

    assert context.preflight_snapshot is snapshot
    assert "preflight_snapshot" not in context.model_dump(mode="json")
    with pytest.raises(TypeError):
        snapshot.root_payload["operations_catalog"] = ()
    with pytest.raises(TypeError):
        snapshot.root_payload["operations_catalog"][0]["operation_key"] = "changed"


def test_preflight_builder_reuses_only_immutable_package_products():
    cache = ImmutablePreflightCache(max_entries=4)
    generations = [1, 2, 3]
    package_loads = []
    profile_loads = []

    class Manifest:
        id = "ontology-a"
        version = "1.0.0"

        @staticmethod
        def model_dump(mode="python"):
            assert mode == "python"
            return {"id": "ontology-a", "version": "1.0.0", "enabled": True}

    def resolve_generations(_workspace_id, _ontology_id):
        return tuple(generations)

    def load_package(_manifest, _workspace_id):
        package_loads.append(tuple(generations))
        return {
            "root_payload": {"operations_catalog": [{"operation_key": "synthetic"}]},
            "selected_ontology_context": "context",
            "preload_records": [{"path": "index.md"}],
            "ontology_source_signature": (("file", "index.md", 1, 2),),
            "ontology_content_revision": "sha256:" + "a" * 64,
            "script_bindings": {
                "synthetic": {
                    "_script_path": "scripts/synthetic.py",
                    "_expected_script_digest": "sha256:synthetic",
                }
            },
        }

    def load_profile():
        profile_loads.append("profile")
        return {"id": "model-a", "api_key": synthetic_secret("async_runtime_contracts-credential-07")}

    first = build_run_preflight_snapshot(
        workspace_id="workspace-a",
        ontology_manifest=Manifest(),
        deterministic_candidate=False,
        model_profile_loader=load_profile,
        cache=cache,
        generation_resolver=resolve_generations,
        package_loader=load_package,
    )
    second = build_run_preflight_snapshot(
        workspace_id="workspace-a",
        ontology_manifest=Manifest(),
        deterministic_candidate=True,
        model_profile_loader=None,
        cache=cache,
        generation_resolver=resolve_generations,
        package_loader=load_package,
    )

    assert first.root_payload is second.root_payload
    assert package_loads == [(1, 2, 3)]
    assert profile_loads == ["profile"]
    assert first.model_profile["id"] == "model-a"
    assert first.ontology_source_signature == (("file", "index.md", 1, 2),)
    assert first.ontology_content_revision == "sha256:" + "a" * 64
    assert second.ontology_content_revision == first.ontology_content_revision
    assert first.script_bindings["synthetic"]["_script_path"] == ("scripts/synthetic.py")
    with pytest.raises(TypeError):
        first.script_bindings["synthetic"]["_script_path"] = "changed.py"
    assert second.model_profile is None
    assert second.deterministic_candidate is True

    generations[2] = 4
    third = build_run_preflight_snapshot(
        workspace_id="workspace-a",
        ontology_manifest=Manifest(),
        deterministic_candidate=False,
        model_profile_loader=load_profile,
        cache=cache,
        generation_resolver=resolve_generations,
        package_loader=load_package,
    )
    assert third.mutation_revision == 4
    assert package_loads == [(1, 2, 3), (1, 2, 4)]


def test_default_preflight_package_loader_freezes_content_revision(
    tmp_path,
    monkeypatch,
):
    import ontology.ontologies as ontologies_module
    import ontology.ontology_catalog as catalog_module
    from agent.runtime import preflight as preflight_module

    ontology_dir = tmp_path / "ontology"
    ontology_dir.mkdir()
    (ontology_dir / "index.md").write_text("admitted", encoding="utf-8")
    manifest = SimpleNamespace(ontology_dir=str(ontology_dir))
    monkeypatch.setattr(catalog_module, "build_root_payload", lambda **_kwargs: {})
    monkeypatch.setattr(catalog_module, "load_script_catalog", lambda _root: [])
    monkeypatch.setattr(
        ontologies_module,
        "build_selected_ontology_preload_context_from_manifest",
        lambda *_args, **_kwargs: "context",
    )
    monkeypatch.setattr(
        ontologies_module,
        "build_selected_ontology_preload_records_from_manifest",
        lambda *_args, **_kwargs: (),
    )

    product = internal(preflight_module).default_package_loader(manifest, "workspace-a")

    assert product["ontology_content_revision"] == (build_package_content_revision(ontology_dir))


def test_preflight_builder_reloads_manifest_inside_generation_retry():
    generations = [1, 2, 7]
    manifest_reads = []
    package_reads = []

    def load_manifest():
        marker = "stale" if not manifest_reads else "current"
        manifest_reads.append(marker)
        return {
            "id": "ontology-a",
            "version": "1.0.0",
            "marker": marker,
        }

    def load_package(manifest, _workspace_id):
        marker = manifest["marker"]
        package_reads.append(marker)
        if marker == "stale":
            generations[2] += 1
        return {
            "root_payload": {"marker": marker},
            "selected_ontology_context": marker,
            "preload_records": (),
        }

    snapshot = build_run_preflight_snapshot(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        ontology_version="1.0.0",
        ontology_manifest_loader=load_manifest,
        deterministic_candidate_loader=lambda manifest: manifest["marker"] == "current",
        model_profile_loader=lambda: {"id": "model-a"},
        cache=ImmutablePreflightCache(max_entries=4),
        generation_resolver=lambda _workspace_id, _ontology_id: tuple(generations),
        package_loader=load_package,
    )

    assert manifest_reads == ["stale", "current"]
    assert package_reads == ["stale", "current"]
    assert snapshot.mutation_revision == 8
    assert snapshot.ontology_manifest["marker"] == "current"
    assert snapshot.root_payload["marker"] == "current"
    assert snapshot.deterministic_candidate is True


def test_preflight_builder_rejects_disappeared_selected_manifest():
    package_reads = []

    with pytest.raises(
        RuntimeError,
        match="ontology manifest became unavailable during preflight",
    ):
        build_run_preflight_snapshot(
            workspace_id="workspace-a",
            ontology_id="ontology-a",
            ontology_version="1.0.0",
            ontology_manifest_loader=lambda: None,
            deterministic_candidate_loader=lambda _manifest: False,
            model_profile_loader=lambda: {"id": "model-a"},
            cache=ImmutablePreflightCache(max_entries=4),
            generation_resolver=lambda _workspace_id, _ontology_id: (1, 2, 7),
            package_loader=lambda manifest, _workspace_id: (package_reads.append(manifest) or {}),
        )

    assert package_reads == []


def test_preflight_builder_rejects_mixed_manifest_and_cached_package_product():
    cache = ImmutablePreflightCache(max_entries=4)
    manifest = {
        "id": "ontology-a",
        "version": "1.0.0",
        "marker": "old",
    }
    package_reads = []

    def load_manifest():
        return dict(manifest)

    def load_package(current_manifest, _workspace_id):
        marker = current_manifest["marker"]
        package_reads.append(marker)
        return {
            "root_payload": {"marker": marker},
            "preload_records": (),
        }

    first = build_run_preflight_snapshot(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        ontology_version="1.0.0",
        ontology_manifest_loader=load_manifest,
        deterministic_candidate_loader=lambda _manifest: False,
        model_profile_loader=lambda: {"id": "model-a"},
        cache=cache,
        generation_resolver=lambda _workspace_id, _ontology_id: (1, 2, 7),
        package_loader=load_package,
    )
    manifest["marker"] = "new"

    with pytest.raises(
        RuntimeError,
        match="ontology manifest changed without generation advancement",
    ):
        build_run_preflight_snapshot(
            workspace_id="workspace-a",
            ontology_id="ontology-a",
            ontology_version="1.0.0",
            ontology_manifest_loader=load_manifest,
            deterministic_candidate_loader=lambda _manifest: False,
            model_profile_loader=lambda: {"id": "model-a"},
            cache=cache,
            generation_resolver=lambda _workspace_id, _ontology_id: (1, 2, 7),
            package_loader=load_package,
        )

    assert first.ontology_manifest["marker"] == "old"
    assert first.root_payload["marker"] == "old"
    assert package_reads == ["old"]


def test_execution_guard_detects_source_drift_without_warm_package_reload(tmp_path):
    workspace_id = "workspace-source-drift"
    ontology_id = "ontology-source-drift"
    ontology_dir = tmp_path / ontology_id
    ontology_dir.mkdir()
    package_file = ontology_dir / "index.md"
    package_file.write_text("old", encoding="utf-8")
    manifest = {
        "id": ontology_id,
        "version": "1.0.0",
        "ontology_dir": str(ontology_dir),
    }
    cache = ImmutablePreflightCache(max_entries=4)
    package_reads = []

    def load_package(_manifest, _workspace_id):
        package_reads.append("load")
        return {
            "root_payload": {"marker": "old"},
            "preload_records": (),
            "ontology_source_signature": (build_package_revision_source_signature(ontology_dir)),
            "ontology_content_revision": build_package_content_revision(ontology_dir),
        }

    first = build_run_preflight_snapshot(
        workspace_id=workspace_id,
        ontology_id=ontology_id,
        ontology_version="1.0.0",
        ontology_manifest_loader=lambda: dict(manifest),
        deterministic_candidate_loader=lambda _manifest: False,
        model_profile_loader=lambda: {"id": "model-a"},
        cache=cache,
        generation_resolver=lambda _workspace_id, _ontology_id: (
            1,
            2,
            get_ontology_mutation_revision(workspace_id, ontology_id),
        ),
        package_loader=load_package,
    )
    assert ontology_preflight_revision_matches(first) is True
    package_file.write_text("new package content", encoding="utf-8")

    second = build_run_preflight_snapshot(
        workspace_id=workspace_id,
        ontology_id=ontology_id,
        ontology_version="1.0.0",
        ontology_manifest_loader=lambda: dict(manifest),
        deterministic_candidate_loader=lambda _manifest: False,
        model_profile_loader=lambda: {"id": "model-a"},
        cache=cache,
        generation_resolver=lambda _workspace_id, _ontology_id: (
            1,
            2,
            get_ontology_mutation_revision(workspace_id, ontology_id),
        ),
        package_loader=load_package,
    )

    assert ontology_preflight_revision_matches(first) is False
    assert second.root_payload is first.root_payload
    assert ontology_preflight_revision_matches(second) is False
    assert package_reads == ["load"]


def test_execution_guard_rejects_content_drift_with_unchanged_stat(tmp_path):
    workspace_id = "workspace-content-drift"
    ontology_id = "ontology-content-drift"
    ontology_dir = tmp_path / ontology_id
    ontology_dir.mkdir()
    package_file = ontology_dir / "operations.json"
    package_file.write_text('{"mode":"alpha"}', encoding="utf-8")
    source_signature = build_package_revision_source_signature(ontology_dir)
    snapshot = RunPreflightSnapshot(
        workspace_id=workspace_id,
        workspace_config_generation=1,
        system_config_generation=2,
        ontology_id=ontology_id,
        ontology_version="1.0.0",
        mutation_revision=get_ontology_mutation_revision(workspace_id, ontology_id),
        ontology_manifest={
            "id": ontology_id,
            "version": "1.0.0",
            "ontology_dir": str(ontology_dir),
        },
        ontology_source_signature=source_signature,
        ontology_content_revision=build_package_content_revision(ontology_dir),
    )
    original_stat = package_file.stat()

    package_file.write_text('{"mode":"bravo"}', encoding="utf-8")
    package_file.touch()
    import os

    os.utime(
        package_file,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    assert build_package_revision_source_signature(ontology_dir) == source_signature
    assert ontology_preflight_revision_matches(snapshot) is False


def test_invocation_context_is_strict_immutable_and_normalized():
    context = _context()

    assert context.model_profile_id is None
    assert context.timeout_seconds is None
    with pytest.raises(ValidationError):
        RuntimeInvocationContext.model_validate({**context.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        RuntimeInvocationContext.model_validate({**context.model_dump(), "query": "  "})
    with pytest.raises(ValidationError):
        RuntimeInvocationContext.model_validate({**context.model_dump(), "llm_request_mode": "async"})
    with pytest.raises(ValidationError):
        context.query = "changed"


def test_history_message_contract_is_provider_neutral_and_role_safe():
    context = RuntimeInvocationContext(
        **{
            **_context().model_dump(),
            "history_messages": [
                {"role": "user", "content": "第一轮问题"},
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "需要查询工具",
                    "tool_calls": [
                        {
                            "id": "call-01",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": "查询结果",
                    "tool_call_id": "call-01",
                    "name": "lookup",
                },
            ],
        }
    )

    assert [message.role for message in context.history_messages] == [
        "user",
        "assistant",
        "tool",
    ]
    assert context.history_messages[1].reasoning_content == "需要查询工具"
    assert context.history_messages[2].tool_call_id == "call-01"
    assert isinstance(context.history_messages[0], RuntimeHistoryMessage)

    with pytest.raises(ValidationError, match="requires tool_call_id"):
        RuntimeHistoryMessage(role="tool", content="orphan tool result")
    with pytest.raises(ValidationError, match="cannot contain tool or reasoning"):
        RuntimeHistoryMessage(
            role="user",
            content="invalid",
            reasoning_content="private",
        )
    with pytest.raises(ValidationError):
        RuntimeHistoryMessage.model_validate({"role": "system", "content": "untrusted system prompt"})


def test_runtime_result_enforces_terminal_error_shape():
    error = RuntimeErrorInfo(
        error_code="MODEL_TIMEOUT",
        message="模型调用超时",
        retriable=True,
        source="model",
    )

    assert RuntimeResult(status="completed", content="ok").error is None
    assert RuntimeResult(status="failed", error=error).error == error
    recovery = RuntimeRecoveryAction(message="Continue from the preserved context.")
    recoverable = RuntimeResult(status="failed", error=error, recovery=recovery)
    assert recoverable.recovery == recovery
    assert recoverable.to_dict()["recovery"]["action"] == "continue"
    assert RuntimeResult(status="cancelled", error=error).status == "cancelled"
    with pytest.raises(ValidationError):
        RuntimeResult(status="completed", error=error)
    with pytest.raises(ValidationError):
        RuntimeResult(status="failed")
    with pytest.raises(ValidationError, match="only valid for failed"):
        RuntimeResult(status="completed", recovery=recovery)
    with pytest.raises(ValidationError, match="retriable error"):
        RuntimeResult(
            status="failed",
            error=error.model_copy(update={"retriable": False}),
            recovery=recovery,
        )


def test_draft_has_no_public_identity_but_public_event_requires_it():
    draft = PublicEventDraft(
        run_id="run-01",
        event_type="token.delta",
        status="running",
        payload={"text": "hello"},
    )

    assert draft.event_id is None
    assert draft.seq is None
    with pytest.raises(ValidationError):
        PublicEventDraft.model_validate({**draft.model_dump(), "seq": 1})
    with pytest.raises(ValidationError):
        PublicEvent(
            event_id="event-01",
            run_id="run-01",
            seq=0,
            event_type="token.delta",
            status="running",
        )

    public = PublicEvent(
        event_id="event-01",
        run_id="run-01",
        seq=1,
        event_type="token.delta",
        status="running",
        payload=draft.payload,
    )
    assert public.seq == 1


def test_public_adapter_preserves_optional_trace_identity():
    event = RuntimeEvent(
        event_type="token.delta",
        status="running",
        run_id="run-trace",
        source={"node": "agent", "component": "model"},
        payload={"text": "hello"},
        span_key="llm:main:1:1",
        parent_key="iteration:main:1",
        trace_id="a" * 32,
        span_id="b" * 16,
        parent_span_id="c" * 16,
    )

    draft = PublicEventAdapter().adapt(event)

    assert draft is not None
    assert draft.span_key == "llm:main:1:1"
    assert draft.parent_key == "iteration:main:1"
    assert draft.trace_id == "a" * 32
    assert draft.span_id == "b" * 16
    assert draft.parent_span_id == "c" * 16


def test_turn_finished_requires_one_consistent_terminal_status():
    event = RuntimeEvent(
        event_type="turn.finished",
        status="completed",
        run_id="run-01",
        payload={"status": "success", "content": "ok"},
    )
    assert event.status == "completed"

    with pytest.raises(ValidationError, match="terminal status"):
        RuntimeEvent(
            event_type="turn.finished",
            status="running",
            run_id="run-01",
            payload={"status": "completed"},
        )
    with pytest.raises(ValidationError, match="must match"):
        RuntimeEvent(
            event_type="turn.finished",
            status="failed",
            run_id="run-01",
            payload={"status": "cancelled"},
        )
    with pytest.raises(ValidationError, match="must match"):
        PublicEventDraft(
            event_type="turn.finished",
            status="completed",
            run_id="run-01",
            payload={"status": "failed"},
        )


def test_public_adapter_redacts_secrets_without_redacting_usage_tokens():
    event = RuntimeEvent(
        event_type="turn.finished",
        status="failed",
        run_id="run-01",
        payload={
            "status": "failed",
            "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
            "error": {
                "error_code": "TOOL_FAILED",
                "message": "Authorization: Bearer top-secret",
                "retriable": True,
                "source": "tool",
                "details": {
                    "access_token": synthetic_secret("async_runtime_contracts-credential-06"),
                    "model_api_key": synthetic_secret("async_runtime_contracts-credential-04"),
                    "refreshToken": synthetic_secret("async_runtime_contracts-credential-05"),
                    "password_hint": "not a credential field",
                },
            },
            "recovery": {
                "action": "continue",
                "label": "继续执行",
                "message": "Continue from the preserved context.",
            },
            "history_messages": [{"role": "user", "content": "private model context"}],
        },
    )

    draft = PublicEventAdapter().adapt(event)

    assert draft is not None
    assert draft.status == "failed"
    assert draft.payload["usage"] == {
        "input_tokens": 12,
        "output_tokens": 7,
        "total_tokens": 19,
    }
    details = draft.payload["error"]["details"]
    assert details["access_token"] == "[REDACTED]"
    assert details["model_api_key"] == "[REDACTED]"
    assert details["refreshToken"] == "[REDACTED]"
    assert details["password_hint"] == "not a credential field"
    assert "top-secret" not in draft.payload["error"]["message"]
    assert draft.payload["recovery"] == {
        "action": "continue",
        "label": "继续执行",
        "message": "Continue from the preserved context.",
    }
    assert "history_messages" not in draft.payload
    assert draft.payload["omitted"]["sensitive_fields_redacted"] == 3


def test_public_adapter_uses_tool_whitelist_and_marks_truncation():
    event = RuntimeEvent(
        event_type="tool.started",
        status="running",
        run_id="run-01",
        source={"node": "react", "component": "tool", "internal_task_id": "task-1"},
        payload={
            "tool_name": "lookup",
            "tool_call_id": "call-01",
            "iteration": 2,
            "arguments": {"password": synthetic_invalid_password("async_runtime_contracts-credential-03")},
            "request_headers": {"Authorization": f"Bearer {synthetic_secret('async_runtime_contracts-credential-01')}"},
        },
    )
    draft = PublicEventAdapter(max_text_length=32).adapt(event)

    assert draft is not None
    assert draft.payload == {
        "name": "lookup",
        "tool_call_id": "call-01",
        "iteration": 2,
    }
    assert draft.source == {"node": "react", "component": "tool"}

    delta = RuntimeEvent(
        event_type="token.delta",
        status="running",
        run_id="run-01",
        payload={"text": "x" * 40},
    )
    truncated = PublicEventAdapter(max_text_length=32).adapt(delta)
    assert truncated is not None
    assert truncated.payload["text"] == "x" * 32 + "..."
    assert truncated.payload["truncated"] is True
    assert truncated.payload["omitted"]["text_fields_truncated"] == 1


def test_public_adapter_preserves_complete_terminal_output():
    content = "x" * 5000
    terminal = RuntimeEvent(
        event_type="turn.finished",
        status="completed",
        run_id="run-long-terminal",
        session_id="session-01",
        turn_id=1,
        payload={
            "status": "completed",
            "content": content,
            "structured_content": {"text": content},
        },
    )

    draft = PublicEventAdapter(max_text_length=32).adapt(terminal)

    assert draft is not None
    assert draft.payload["content"] == content
    assert draft.payload["structured_content"]["text"] == content
    assert "truncated" not in draft.payload


def test_reasoning_and_terminal_trace_follow_include_trace_switch():
    reasoning = RuntimeEvent(
        event_type="reasoning.delta",
        status="running",
        run_id="run-01",
        payload={"text": "internal reasoning"},
    )
    terminal = RuntimeEvent(
        event_type="turn.finished",
        status="completed",
        run_id="run-01",
        payload={
            "status": "completed",
            "content": "answer",
            "trace": {"prompt": "secret prompt", "step": "model"},
        },
    )

    assert PublicEventAdapter(include_trace=False).adapt(reasoning) is None
    hidden = PublicEventAdapter(include_trace=False).adapt(terminal)
    assert hidden is not None
    assert "trace" not in hidden.payload

    visible_reasoning = PublicEventAdapter(include_trace=True).adapt(reasoning)
    visible_terminal = PublicEventAdapter(include_trace=True).adapt(terminal)
    assert visible_reasoning is not None
    assert visible_reasoning.payload["text"] == "internal reasoning"
    assert visible_terminal is not None
    assert visible_terminal.payload["trace"] == {
        "prompt": "[REDACTED]",
        "step": "model",
    }
    assert visible_terminal.payload["omitted"]["sensitive_fields_redacted"] == 1


def test_public_adapter_preserves_interaction_prompt_but_not_internal_prompts():
    interaction = {
        "id": "interaction_synthetic",
        "type": "input",
        "prompt": "Provide a synthetic region code",
        "options": [],
    }
    requested = RuntimeEvent(
        event_type="interaction.requested",
        status="awaiting_input",
        run_id="run-interaction",
        payload={
            "interaction_id": interaction["id"],
            "type": interaction["type"],
            "prompt": interaction["prompt"],
            "options": [],
        },
    )
    terminal = RuntimeEvent(
        event_type="turn.finished",
        status="awaiting_input",
        run_id="run-interaction",
        payload={
            "status": "awaiting_input",
            "structured_content": {
                "type": "user_interaction",
                "text": interaction["prompt"],
                "interaction": interaction,
            },
            "trace": {"prompt": "internal model prompt"},
        },
    )

    requested_draft = PublicEventAdapter().adapt(requested)
    terminal_draft = PublicEventAdapter(include_trace=True).adapt(terminal)

    assert requested_draft is not None
    assert requested_draft.payload["prompt"] == interaction["prompt"]
    assert terminal_draft is not None
    assert terminal_draft.payload["structured_content"]["interaction"]["prompt"] == interaction["prompt"]
    assert terminal_draft.payload["trace"]["prompt"] == "[REDACTED]"


def test_fixed_runtime_success_and_tool_failure_flows():
    async def exercise():
        runtime = FixedAsyncAgentRuntime()
        success_sink = ListRuntimeEventSink()
        success = await runtime.arun(_context("run-success"), success_sink)
        failure_sink = ListRuntimeEventSink()
        failure = await runtime.arun(
            _context("run-failure", mock_scenario="tool_failure"),
            failure_sink,
        )
        return runtime, success, success_sink.events, failure, failure_sink.events

    runtime, success, success_events, failure, failure_events = asyncio.run(exercise())

    assert success.status == "completed"
    assert [event.event_type for event in success_events] == [
        "turn.started",
        "model.started",
        "tool.started",
        "tool.finished",
        "token.delta",
        "turn.finished",
    ]
    assert failure.status == "failed"
    assert failure.error is not None
    assert failure.error.error_code == "TOOL_EXECUTION_FAILED"
    assert [event.event_type for event in failure_events][-2:] == [
        "tool.finished",
        "turn.finished",
    ]
    assert asyncio.run(runtime.cancel("run-success", "too late")) is False


def test_fixed_runtime_hitl_resume_cancel_and_release():
    async def exercise_resume():
        runtime = FixedAsyncAgentRuntime()
        sink = ListRuntimeEventSink()
        task = asyncio.create_task(
            runtime.arun(
                _context("run-hitl", mock_scenario="awaiting_input"),
                sink,
            )
        )
        for _ in range(100):
            if any(event.event_type == "interaction.requested" for event in sink.events):
                break
            await asyncio.sleep(0)
        interaction = next(event for event in sink.events if event.event_type == "interaction.requested")
        resumed = await runtime.resume_interaction(
            "run-hitl",
            interaction.payload["interaction_id"],
            "continue",
        )
        result = await task
        await runtime.release("jiuwen-session-01")
        return runtime, resumed, result, sink.events

    runtime, resumed, result, events = asyncio.run(exercise_resume())
    assert resumed is True
    assert result.status == "completed"
    assert [event.event_type for event in events] == [
        "turn.started",
        "interaction.requested",
        "token.delta",
        "turn.finished",
    ]
    assert runtime.released_session_ids == ["jiuwen-session-01"]

    async def exercise_cancel():
        cancel_runtime = FixedAsyncAgentRuntime()
        sink = ListRuntimeEventSink()
        task = asyncio.create_task(
            cancel_runtime.arun(
                _context("run-cancel", mock_scenario="awaiting_input"),
                sink,
            )
        )
        for _ in range(100):
            if any(event.event_type == "interaction.requested" for event in sink.events):
                break
            await asyncio.sleep(0)
        accepted = await cancel_runtime.cancel("run-cancel", "user requested")
        result = await task
        return accepted, result, sink.events

    accepted, cancelled, cancel_events = asyncio.run(exercise_cancel())
    assert accepted is True
    assert cancelled.status == "cancelled"
    assert cancelled.error is not None
    assert cancelled.error.message == "user requested"
    assert [event.event_type for event in cancel_events][-1] == "turn.finished"
    assert cancel_events[-1].status == "cancelled"
