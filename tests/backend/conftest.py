import pytest
import sys
from pathlib import Path


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "platform").is_dir() and (candidate / "runtime").is_dir():
            return candidate
    raise RuntimeError("Could not locate repository root")


ROOT = _repo_root()
for path in (
    ROOT / "platform" / "server",
    ROOT / "modeling" / "skills",
):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)


SECRET_TEST_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


@pytest.fixture(autouse=True)
def isolate_system_config_file(tmp_path, monkeypatch):
    """Keep tests independent from ignored local .onto-runtime config files."""
    import agentic.config.system_config as system_config_module

    config_path = tmp_path / "config" / "system_config.json"
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_path.parent)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    monkeypatch.setenv(system_config_module.SYSTEM_CONFIG_PATH_ENV, str(config_path))
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
