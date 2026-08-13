import pytest


SECRET_TEST_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
TEST_BOOTSTRAP_ADMIN_PASSWORD = "TestBootstrap#2026"


@pytest.fixture(autouse=True)
def isolate_system_config_file(tmp_path, monkeypatch):
    """Keep tests independent from ignored local .onto-runtime config files."""
    import config.system_config as system_config_module

    config_path = tmp_path / "config" / "system_config.json"
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_path.parent)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    monkeypatch.setenv(system_config_module.SYSTEM_CONFIG_PATH_ENV, str(config_path))
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    monkeypatch.setenv("AUTH_BOOTSTRAP_ADMIN_PASSWORD", TEST_BOOTSTRAP_ADMIN_PASSWORD)

    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
