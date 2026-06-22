"""Tests for memory factory backend selection and availability handling."""

import logging

from agentic.memory.factory import MemoryFactory
from agentic.memory.filesystem_manager import FileSystemMemoryManager


class _MissingConfigClient:
    attempts = 0

    def __init__(self, path: str) -> None:
        del path
        type(self).attempts += 1
        raise FileNotFoundError("OpenViking configuration file not found.")


def test_factory_defaults_to_filesystem_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_MODE", raising=False)
    monkeypatch.delenv("AGENT_MEMORY_RECALL_TOPK", raising=False)
    monkeypatch.delenv("AGENT_MEMORY_RECALL_THRESHOLD", raising=False)
    monkeypatch.delenv("MEMORY_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("OPENVIKING_WORKSPACE_ROOT", raising=False)

    factory = MemoryFactory(workspace_root=tmp_path)

    manager = factory.for_user("alice")

    assert isinstance(manager, FileSystemMemoryManager)
    assert manager.ensure_session(app_session_id="sess-1") == "sess-1"


def test_filesystem_memory_defaults_under_project_runtime_root(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = tmp_path / ".onto-runtime"

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_dir / "system_config.json")
    monkeypatch.delenv("MEMORY_MODE", raising=False)
    monkeypatch.delenv("MEMORY_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("OPENVIKING_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))

    factory = MemoryFactory()

    manager = factory.for_user("alice")

    assert isinstance(manager, FileSystemMemoryManager)
    assert manager.workspace_dir == runtime_root / "memory" / "alice"


def test_factory_disables_memory_when_openviking_config_is_missing(tmp_path, caplog, monkeypatch):
    _MissingConfigClient.attempts = 0
    monkeypatch.setenv("MEMORY_MODE", "openviking")

    factory = MemoryFactory(
        workspace_root=tmp_path,
        client_cls=_MissingConfigClient,
    )

    with caplog.at_level(logging.WARNING):
        assert factory.for_user("alice") is None
        assert factory.for_user("bob") is None

    assert _MissingConfigClient.attempts == 1
    messages = [record.getMessage() for record in caplog.records]
    disabled_logs = [message for message in messages if "OpenViking memory disabled" in message]
    assert len(disabled_logs) == 1
    assert "configuration file not found" in disabled_logs[0]
