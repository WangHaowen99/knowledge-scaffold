import pytest


SECRET_TEST_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


@pytest.fixture(autouse=True)
def isolate_system_config_file(tmp_path, monkeypatch):
    """Keep tests independent from ignored local .onto-runtime config files."""
    import agentic.config.system_config as legacy_system_config
    import config.system_config as layered_system_config

    config_path = tmp_path / "config" / "system_config.json"
    for system_config_module in (legacy_system_config, layered_system_config):
        monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_path.parent)
        monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
        monkeypatch.setenv(system_config_module.SYSTEM_CONFIG_PATH_ENV, str(config_path))
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)

    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
