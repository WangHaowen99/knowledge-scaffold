"""Native-runtime integration tests for durable ontology version stamps."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent.core.orchestrator import AgenticOrchestrator
from agent.runtime.contracts import RuntimeInvocationContext, RuntimeResult
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
