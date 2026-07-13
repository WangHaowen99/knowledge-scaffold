"""Integration tests for per-turn ontology_version stamping by the orchestrator.

These cover the stamping paths that the unit tests in test_skill_evolution_service
bypass (they construct turns directly via FakeExecutionStore). 坑 2 (locked
sessions got None stamps) and 坑 4 (tool-based sessions got no stamps) were both
orchestrator-wiring bugs in exactly this code — the helper-only unit test does
NOT guard the recurrence surface. These tests drive orchestrator.run() with a
real ExecutionStore + a real on-disk manifest and assert the stamp lands on the
persisted turn.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.core.orchestrator import AgenticOrchestrator
from config import AgentConfig, AppConfig
from llm import LLMRouter, configure_llm_router, reset_llm_router_for_tests
from persistence.chat_store import ChatStore
from persistence.execution_store import ExecutionStore
from persistence.run_store import RunStore
from utils.read_docs import build_read_document_record, merge_read_documents_registry
from workspace.workspaces import create_workspace, get_workspace_ontologies_root


WORKSPACE_ID = "test-workspace"
ONTOLOGY_ID = "demo-onto"
ACTIVE_VERSION = "1.0.0"


@pytest.fixture(autouse=True)
def _configure_runtime_llm(monkeypatch, tmp_path):
    import config.system_config as system_config_module
    from integration.observability.provider import reset_observer

    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("ONTO_PLATFORM_ENABLED", "false")
    monkeypatch.setenv("ONTO_PLATFORM_CONFIG_PATH", str(runtime_root / "config" / "platform_config.json"))
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
        "base_url": "https://default.example/v1",
        "model": "default-model",
        "api_key": "default-secret",
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
    create_workspace("Test Workspace", workspace_id=WORKSPACE_ID, set_as_default=True)
    configure_llm_router(
        LLMRouter(
            AppConfig.from_env(),
            agent_config=AgentConfig.from_env(),
        )
    )
    reset_observer(timeout=0)
    yield
    reset_llm_router_for_tests()
    reset_observer(timeout=0)


class _FakeMemoryFactory:
    class _Manager:
        def build_memory_context(self, query):
            return ""

    def __init__(self):
        self.manager = self._Manager()

    def for_user(self, user_id):
        return self.manager


def _write_ontology_manifest(workspace_id: str, ontology_id: str, active_version: str) -> None:
    """Write a minimal on-disk ontology (manifest + version dir) so both the
    registry path (turn 1) and the manifest-read path (locked turn 2) resolve
    the same active_version."""
    onto_dir = get_workspace_ontologies_root(workspace_id) / ontology_id
    version_dir = onto_dir / "versions" / active_version / "operations"
    version_dir.mkdir(parents=True, exist_ok=True)
    (onto_dir / "manifest.yaml").write_text(
        "kind: ontology\n"
        "id: {}\n"
        "name: 测试本体\n"
        "description: test\n"
        "version: {active}\n"
        "active_version: {active}\n"
        "versions: [{active}]\n"
        "language: zh-CN\n"
        "enabled: true\n".format(ontology_id, active=active_version),
        encoding="utf-8",
    )


def _make_orchestrator(tmp_path, monkeypatch, *, registry):
    """Build an orchestrator wired to real stores + a fake ontology registry.

    The fake registry serves turn-1 (requested selected_ontology_id); the
    locked turn-2 branch reads the manifest from disk (real file). Both must
    resolve active_version — that is the integration contract under test."""
    monkeypatch.setattr("agent.core.orchestrator.get_ontology_registry", lambda: registry)
    return AgenticOrchestrator(
        run_store=RunStore(),
        chat_store=ChatStore(str(tmp_path / "chats")),
        execution_store=ExecutionStore(str(tmp_path / "executions")),
        memory_factory=_FakeMemoryFactory(),
    )


def _single_pass_stream_agent_loop(monkeypatch, *, read_documents=None):
    """Patch _stream_agent_loop to a one-iteration loop that optionally seeds
    read_documents (tool-based flow). Returns the captured initial states."""
    captured = []

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        captured.append(dict(initial_state))
        state = dict(initial_state)
        state["messages"] = list(state.get("messages") or []) + [
            {"role": "assistant", "content": "done"},
        ]
        state["final_output"] = "done"
        if read_documents is not None:
            state["read_documents"] = read_documents
        yield ("values", state)

    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)
    return captured


def test_ui_selected_flow_stamps_version_on_first_and_locked_turns(tmp_path, monkeypatch):
    """坑 2 regression: the locked branch (turn 2+) must read active_version
    from the manifest and stamp the turn — not leave it None."""
    _write_ontology_manifest(WORKSPACE_ID, ONTOLOGY_ID, ACTIVE_VERSION)

    class _Registry:
        def get_ontology(self, ontology_id):
            if ontology_id == ONTOLOGY_ID:
                return SimpleNamespace(
                    id=ontology_id,
                    name="测试本体",
                    enabled=True,
                    active_version=ACTIVE_VERSION,
                )
            return None

    orchestrator = _make_orchestrator(tmp_path, monkeypatch, registry=_Registry())
    _single_pass_stream_agent_loop(monkeypatch)

    list(
        orchestrator.run(
            user_request="第一轮",
            session_id="sess-stamp-ui",
            user_context={"selected_ontology_id": ONTOLOGY_ID},
            workspace_id=WORKSPACE_ID,
        )
    )
    list(
        orchestrator.run(
            user_request="第二轮",
            session_id="sess-stamp-ui",
            user_context={},
            workspace_id=WORKSPACE_ID,
        )
    )

    turns = orchestrator.execution_store.get_session_history("sess-stamp-ui")["turns"]
    assert [t["ontology_version"] for t in turns] == [ACTIVE_VERSION, ACTIVE_VERSION], (
        "both the requested (turn 1) and locked (turn 2) branches must stamp active_version"
    )


def test_tool_based_flow_stamps_version_at_turn_completion(tmp_path, monkeypatch):
    """坑 4 regression: a session with no session-level selected ontology (the
    agent loaded it via ontology_context) must still get a version stamp at
    turn completion, inferred from read_documents."""
    _write_ontology_manifest(WORKSPACE_ID, ONTOLOGY_ID, ACTIVE_VERSION)

    read_documents = merge_read_documents_registry(
        None,
        [
            build_read_document_record(
                file_path="ontologies/{}/objects/主CHANGE_ORDER.md".format(ONTOLOGY_ID),
                content="# 主CHANGE_ORDER",
                source="ontology_context",
                turn_id=1,
                ontology_version=ACTIVE_VERSION,
            )
        ],
    )

    class _Registry:
        def get_ontology(self, ontology_id):
            return None  # tool-based flow never resolves a selected ontology

    orchestrator = _make_orchestrator(tmp_path, monkeypatch, registry=_Registry())
    _single_pass_stream_agent_loop(monkeypatch, read_documents=read_documents)

    list(
        orchestrator.run(
            user_request="用工具加载本体",
            session_id="sess-stamp-tool",
            user_context={},
            workspace_id=WORKSPACE_ID,
        )
    )

    turns = orchestrator.execution_store.get_session_history("sess-stamp-tool")["turns"]
    assert len(turns) == 1
    assert turns[0]["ontology_version"] == ACTIVE_VERSION, (
        "tool-based sessions must be stamped at turn completion from read_documents"
    )


def test_tool_based_flow_without_ontology_read_leaves_stamp_empty(tmp_path, monkeypatch):
    """Negative guard: a tool-based session that read no ontology (ambiguous /
    none) must leave the stamp empty rather than crash or fabricate a version."""
    _write_ontology_manifest(WORKSPACE_ID, ONTOLOGY_ID, ACTIVE_VERSION)

    class _Registry:
        def get_ontology(self, ontology_id):
            return None

    orchestrator = _make_orchestrator(tmp_path, monkeypatch, registry=_Registry())
    _single_pass_stream_agent_loop(monkeypatch, read_documents={"by_path": {}, "recent_paths": [], "active_object_docs": []})

    list(
        orchestrator.run(
            user_request="没读本体",
            session_id="sess-stamp-none",
            user_context={},
            workspace_id=WORKSPACE_ID,
        )
    )

    turns = orchestrator.execution_store.get_session_history("sess-stamp-none")["turns"]
    assert len(turns) == 1
    assert turns[0].get("ontology_version") in (None, "")
