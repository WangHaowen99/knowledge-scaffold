"""Native-runtime integration tests for durable ontology version stamps."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent.core.orchestrator import AgenticOrchestrator
from agent.runtime.contracts import (
    RunPreflightSnapshot,
    RuntimeInvocationContext,
    RuntimeResult,
)
from agent.runtime.service import OrchestratorSessionAdapter
from config import AgentConfig, AppConfig
from llm import LLMRouter, configure_llm_router, reset_llm_router_for_tests
from persistence.chat_store import ChatStore
from persistence.execution_store import ExecutionStore
from utils.read_docs import (
    build_read_document_record,
    merge_read_documents_registry,
)
from workspace.workspaces import (
    create_workspace,
    get_workspace_ontologies_root,
)


WORKSPACE_ID = "workspace_demo"
ONTOLOGY_ID = "ontology_demo"
ACTIVE_VERSION = "1.0.0"


@pytest.fixture(autouse=True)
def _configure_runtime_llm(monkeypatch, tmp_path):
    import config.system_config as system_config_module
    from integration.observability.provider import reset_observer
    from system_config_migration_helpers import apply_test_system_config_migrations

    runtime_root = tmp_path / ".runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("ONTO_PLATFORM_ENABLED", "false")
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "llm_profiles": {
    "default_profile_id": "default",
    "profiles": [
      {
        "id": "default",
        "name": "Default Model",
        "provider": "openai_compatible",
        "base_url": "https://service.example.net/v1",
        "model": "model-demo",
        "api_key": "your-api-key-here",
        "enabled": true
      }
    ]
  }
}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    apply_test_system_config_migrations(
        config_path,
        monkeypatch,
        runtime_root=runtime_root,
    )
    create_workspace(
        "Workspace Demo",
        workspace_id=WORKSPACE_ID,
        set_as_default=True,
    )
    configure_llm_router(
        LLMRouter(AppConfig.from_env(), agent_config=AgentConfig.from_env())
    )
    reset_observer(timeout=0)
    yield
    reset_llm_router_for_tests()
    reset_observer(timeout=0)


class _MemoryFactory:
    class _Manager:
        @staticmethod
        def build_memory_context(*, query):
            del query
            return ""

    def for_user(self, user_id):
        del user_id
        return self._Manager()


def _write_ontology_manifest() -> None:
    ontology_dir = get_workspace_ontologies_root(WORKSPACE_ID) / ONTOLOGY_ID
    version_dir = ontology_dir / "versions" / ACTIVE_VERSION / "operations"
    version_dir.mkdir(parents=True, exist_ok=True)
    (ontology_dir / "manifest.yaml").write_text(
        "kind: ontology-package\n"
        "schema_version: 0.1\n"
        f"id: {ONTOLOGY_ID}\n"
        "name: Ontology Demo\n"
        "description: synthetic test ontology\n"
        f"version: {ACTIVE_VERSION}\n"
        f"active_version: {ACTIVE_VERSION}\n"
        f"versions: [{ACTIVE_VERSION}]\n"
        "language: zh-CN\n"
        "enabled: true\n",
        encoding="utf-8",
    )


def _services(tmp_path) -> tuple[AgenticOrchestrator, OrchestratorSessionAdapter]:
    orchestrator = AgenticOrchestrator(
        chat_store=ChatStore(str(tmp_path / "chats")),
        execution_store=ExecutionStore(str(tmp_path / "executions")),
        memory_factory=_MemoryFactory(),
    )
    return orchestrator, OrchestratorSessionAdapter(lambda: orchestrator)


def _context(
    *,
    run_id: str,
    session_id: str,
    turn_id: int,
    arguments: dict | None = None,
) -> RuntimeInvocationContext:
    return RuntimeInvocationContext(
        run_id=run_id,
        session_id=session_id,
        turn_id=turn_id,
        workspace_id=WORKSPACE_ID,
        username="user_demo",
        query="synthetic request",
        arguments=arguments,
    )


def _prepare_and_finalize(
    adapter: OrchestratorSessionAdapter,
    context: RuntimeInvocationContext,
    *,
    read_documents: dict | None = None,
) -> RuntimeInvocationContext:
    async def run() -> RuntimeInvocationContext:
        prepared = await adapter.prepare(context)
        await adapter.finalize(
            prepared,
            RuntimeResult(
                status="completed",
                content="synthetic answer",
                read_documents=read_documents,
            ),
        )
        return prepared

    return asyncio.run(run())


def test_selected_ontology_stamps_first_and_locked_turns(
    tmp_path,
    monkeypatch,
):
    _write_ontology_manifest()

    class _Registry:
        @staticmethod
        def get_ontology(ontology_id):
            if ontology_id != ONTOLOGY_ID:
                return None
            return SimpleNamespace(
                id=ontology_id,
                name="Ontology Demo",
                enabled=True,
                active_version=ACTIVE_VERSION,
            )

    monkeypatch.setattr(
        "ontology.ontologies.get_ontology_registry",
        lambda **_kwargs: _Registry(),
    )
    orchestrator, adapter = _services(tmp_path)
    first = _prepare_and_finalize(
        adapter,
        _context(
            run_id="run-1",
            session_id="session-selected",
            turn_id=1,
            arguments={"selected_ontology_id": ONTOLOGY_ID},
        ),
    )
    second = _prepare_and_finalize(
        adapter,
        _context(
            run_id="run-2",
            session_id="session-selected",
            turn_id=2,
        ),
    )

    assert first.arguments["selected_ontology_version"] == ACTIVE_VERSION
    assert second.arguments["selected_ontology_version"] == ACTIVE_VERSION
    turns = orchestrator.execution_store.get_session_history(
        "session-selected"
    )["turns"]
    assert [turn["ontology_version"] for turn in turns] == [
        ACTIVE_VERSION,
        ACTIVE_VERSION,
    ]


def test_requested_ontology_version_skips_active_registry_resolution(
    tmp_path,
    monkeypatch,
):
    _write_ontology_manifest()

    def fail_registry_lookup(**_kwargs):
        raise AssertionError("an explicit ontology version must not rescan active state")

    monkeypatch.setattr(
        "ontology.ontologies.get_ontology_registry",
        fail_registry_lookup,
    )
    _orchestrator, adapter = _services(tmp_path)
    prepared = _prepare_and_finalize(
        adapter,
        _context(
            run_id="run-explicit-version",
            session_id="session-explicit-version",
            turn_id=1,
            arguments={
                "selected_ontology_id": ONTOLOGY_ID,
                "selected_ontology_version": ACTIVE_VERSION,
            },
        ),
    )

    assert prepared.arguments["selected_ontology_id"] == ONTOLOGY_ID
    assert prepared.arguments["selected_ontology_version"] == ACTIVE_VERSION


def test_prepare_reuses_matching_preflight_manifest_and_profile(tmp_path, monkeypatch):
    _orchestrator, adapter = _services(tmp_path)
    monkeypatch.setattr(
        "ontology.ontologies.get_runtime_ontology_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("matching preflight must avoid a second manifest read")
        ),
    )
    monkeypatch.setattr(
        "ontology.ontologies.get_ontology_registry",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("matching preflight must avoid a registry scan")
        ),
    )
    monkeypatch.setattr(
        "config.system_config.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("matching preflight must avoid a second profile read")
        ),
    )
    snapshot = RunPreflightSnapshot(
        workspace_id=WORKSPACE_ID,
        workspace_config_generation=1,
        system_config_generation=1,
        ontology_id=ONTOLOGY_ID,
        ontology_version=ACTIVE_VERSION,
        mutation_revision=1,
        ontology_manifest={
            "id": ONTOLOGY_ID,
            "name": "Ontology Demo",
            "version": ACTIVE_VERSION,
            "enabled": True,
        },
        root_payload={},
        model_profile={
            "id": "default",
            "provider": "openai_compatible",
            "model_profile_ref": {"scope": "global", "id": "default"},
        },
        deterministic_candidate=False,
    )
    context = _context(
        run_id="run-preflight-reuse",
        session_id="session-preflight-reuse",
        turn_id=1,
        arguments={
            "selected_ontology_id": ONTOLOGY_ID,
            "selected_ontology_version": ACTIVE_VERSION,
        },
    ).model_copy(
        update={
            "model_profile_id": "default",
            "preflight_snapshot": snapshot,
        }
    )

    prepared = _prepare_and_finalize(adapter, context)

    assert prepared.preflight_snapshot is snapshot
    assert prepared.model_profile_id == "default"
    assert prepared.arguments["selected_ontology_version"] == ACTIVE_VERSION


def test_prepare_reloads_profile_when_session_changed_after_preflight(
    tmp_path,
    monkeypatch,
):
    orchestrator, adapter = _services(tmp_path)
    orchestrator.chat_store.create_session(
        session_id="session-profile-race",
        metadata={
            "agent_mode": "quick",
            "llm_request_mode": "stream",
            "model_profile_id": "profile-a",
            "selected_ontology_id": ONTOLOGY_ID,
            "selected_ontology_name": "Ontology Demo",
            "selected_ontology_version": ACTIVE_VERSION,
        },
    )
    profile_reads = []

    def resolve_profile(profile_ref, **_kwargs):
        profile_reads.append(profile_ref)
        return {
            "id": "profile-a",
            "provider": "openai_compatible",
            "model_profile_ref": {"scope": "global", "id": "profile-a"},
        }

    monkeypatch.setattr(
        "config.system_config.resolve_effective_llm_profile_config",
        resolve_profile,
    )
    snapshot = RunPreflightSnapshot(
        workspace_id=WORKSPACE_ID,
        workspace_config_generation=1,
        system_config_generation=1,
        ontology_id=ONTOLOGY_ID,
        ontology_version=ACTIVE_VERSION,
        mutation_revision=1,
        ontology_manifest={
            "id": ONTOLOGY_ID,
            "name": "Ontology Demo",
            "version": ACTIVE_VERSION,
            "enabled": True,
        },
        root_payload={},
        model_profile={
            "id": "profile-b",
            "provider": "openai_compatible",
            "model_profile_ref": {"scope": "global", "id": "profile-b"},
        },
        deterministic_candidate=False,
    )
    context = _context(
        run_id="run-profile-race",
        session_id="session-profile-race",
        turn_id=1,
    ).model_copy(update={"preflight_snapshot": snapshot})

    prepared = _prepare_and_finalize(adapter, context)

    assert profile_reads == ["profile-a"]
    assert prepared.model_profile_id == "profile-a"


def test_orchestrator_preflight_caches_package_but_resolves_profile_per_request(
    tmp_path,
    monkeypatch,
):
    _write_ontology_manifest()
    orchestrator, _adapter = _services(tmp_path)
    package_loads = []
    profile_loads = []
    monkeypatch.setattr(
        "agent.direct_execution.service.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "ontology.ontology_catalog.build_root_payload",
        lambda **_kwargs: package_loads.append("package")
        or {"operations_catalog": [], "solver_catalog": []},
    )
    monkeypatch.setattr(
        "config.system_config.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: profile_loads.append("profile")
        or {
            "id": "default",
            "provider": "openai_compatible",
            "model_profile_ref": {"scope": "global", "id": "default"},
        },
    )
    kwargs = {
        "user_request": "synthetic request",
        "user_context": {
            "selected_ontology_id": ONTOLOGY_ID,
            "selected_ontology_version": ACTIVE_VERSION,
        },
        "arguments": {
            "selected_ontology_id": ONTOLOGY_ID,
            "selected_ontology_version": ACTIVE_VERSION,
        },
        "session_id": "session-preflight-cache",
        "model_profile_id": "default",
        "user_id": "user_demo",
        "workspace_id": WORKSPACE_ID,
    }

    first = orchestrator.build_run_preflight_snapshot(**kwargs)
    second = orchestrator.build_run_preflight_snapshot(**kwargs)

    assert first.root_payload is second.root_payload
    assert package_loads == ["package"]
    assert profile_loads == ["profile", "profile"]


def test_orchestrator_reloads_manifest_when_revision_changes_during_preflight(
    tmp_path,
    monkeypatch,
):
    _write_ontology_manifest()
    orchestrator, _adapter = _services(tmp_path)
    from ontology.ontologies import get_runtime_ontology_manifest as real_loader

    manifest = real_loader(
        ONTOLOGY_ID,
        version=ACTIVE_VERSION,
        workspace_id=WORKSPACE_ID,
    )
    assert manifest is not None
    manifests = [
        manifest.model_copy(update={"name": "stale-manifest"}),
        manifest.model_copy(update={"name": "current-manifest"}),
    ]
    manifest_reads = []

    def load_manifest(*_args, **_kwargs):
        selected = manifests[min(len(manifest_reads), len(manifests) - 1)]
        manifest_reads.append(selected.name)
        return selected

    revisions = iter((7, 8, 8, 8))
    monkeypatch.setattr(
        "ontology.ontologies.get_runtime_ontology_manifest",
        load_manifest,
    )
    monkeypatch.setattr(
        "ontology.view_cache.get_ontology_mutation_revision",
        lambda *_args, **_kwargs: next(revisions),
    )
    monkeypatch.setattr(
        "ontology.ontology_catalog.build_root_payload",
        lambda *, ontology, **_kwargs: {"marker": ontology.name},
    )
    monkeypatch.setattr(
        "agent.direct_execution.service.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, ontology_manifest=None, **_kwargs: (
            ontology_manifest.name == "current-manifest"
        ),
    )

    snapshot = orchestrator.build_run_preflight_snapshot(
        user_request="synthetic request",
        user_context={
            "selected_ontology_id": ONTOLOGY_ID,
            "selected_ontology_version": ACTIVE_VERSION,
        },
        arguments={
            "selected_ontology_id": ONTOLOGY_ID,
            "selected_ontology_version": ACTIVE_VERSION,
        },
        session_id="session-preflight-manifest-race",
        model_profile_id="default",
        user_id="user_demo",
        workspace_id=WORKSPACE_ID,
    )

    assert manifest_reads == ["stale-manifest", "current-manifest"]
    assert snapshot.mutation_revision == 8
    assert snapshot.ontology_manifest["name"] == "current-manifest"
    assert snapshot.root_payload["marker"] == "current-manifest"
    assert snapshot.deterministic_candidate is True


def test_requested_ontology_version_rejects_undeclared_installed_revision(
    tmp_path,
):
    _write_ontology_manifest()
    ontology_dir = get_workspace_ontologies_root(WORKSPACE_ID) / ONTOLOGY_ID
    residual_version = ontology_dir / "versions" / "0.9.0"
    residual_version.mkdir(parents=True)

    _orchestrator, adapter = _services(tmp_path)
    with pytest.raises(ValueError, match="Invalid selected ontology version"):
        _prepare_and_finalize(
            adapter,
            _context(
                run_id="run-undeclared-version",
                session_id="session-undeclared-version",
                turn_id=1,
                arguments={
                    "selected_ontology_id": ONTOLOGY_ID,
                    "selected_ontology_version": "0.9.0",
                },
            ),
        )


def test_tool_loaded_ontology_stamps_version_at_finalize(tmp_path):
    _write_ontology_manifest()
    read_documents = merge_read_documents_registry(
        None,
        [
            build_read_document_record(
                file_path=(
                    f"ontologies/{ONTOLOGY_ID}/objects/object_demo.md"
                ),
                content="# Object Demo",
                source="ontology_context",
                turn_id=1,
                ontology_version=ACTIVE_VERSION,
            )
        ],
    )
    orchestrator, adapter = _services(tmp_path)
    _prepare_and_finalize(
        adapter,
        _context(
            run_id="run-tool",
            session_id="session-tool",
            turn_id=1,
        ),
        read_documents=read_documents,
    )

    turn = orchestrator.execution_store.get_session_history("session-tool")[
        "turns"
    ][0]
    assert turn["ontology_version"] == ACTIVE_VERSION


def test_no_ontology_read_leaves_version_empty(tmp_path):
    _write_ontology_manifest()
    orchestrator, adapter = _services(tmp_path)
    _prepare_and_finalize(
        adapter,
        _context(
            run_id="run-none",
            session_id="session-none",
            turn_id=1,
        ),
        read_documents={
            "by_path": {},
            "recent_paths": [],
            "active_object_docs": [],
        },
    )

    turn = orchestrator.execution_store.get_session_history("session-none")[
        "turns"
    ][0]
    assert turn.get("ontology_version") in (None, "")
