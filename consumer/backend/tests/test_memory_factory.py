"""Tests for memory factory backend selection."""

from agent.memory.factory import MemoryFactory
from agent.memory.filesystem_manager import FileSystemMemoryManager


def test_factory_returns_filesystem_manager(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_MEMORY_RECALL_TOPK", raising=False)
    monkeypatch.delenv("AGENT_MEMORY_RECALL_THRESHOLD", raising=False)
    monkeypatch.delenv("MEMORY_WORKSPACE_ROOT", raising=False)

    factory = MemoryFactory(workspace_root=tmp_path)

    manager = factory.for_user("alice")

    assert isinstance(manager, FileSystemMemoryManager)


def test_filesystem_memory_defaults_under_project_runtime_root(tmp_path, monkeypatch):
    import config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = tmp_path / ".onto-runtime"

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_dir / "system_config.json")
    monkeypatch.delenv("MEMORY_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))

    factory = MemoryFactory()

    manager = factory.for_user("alice")

    assert isinstance(manager, FileSystemMemoryManager)
    assert manager.workspace_dir == runtime_root / "memory" / "alice"
