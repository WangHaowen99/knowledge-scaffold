"""Auth server environment configuration tests."""

from __future__ import annotations

import builtins

from auth_server import config as config_module
from auth_server.config import DEFAULT_ISSUER, AuthServerConfig


def test_from_env_loads_project_dotenv(tmp_path, monkeypatch):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "AUTH_SERVER_ISSUER=http://127.0.0.1:5056\n"
        "AUTH_SERVER_ADMIN_TOKEN=dotenv-admin-token\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_DOTENV_PATH", dotenv_path)
    monkeypatch.delenv("AUTH_SERVER_ISSUER", raising=False)
    monkeypatch.delenv("AUTH_SERVER_ADMIN_TOKEN", raising=False)

    config = AuthServerConfig.from_env()

    assert config.issuer == "http://127.0.0.1:5056"
    assert config.admin_token == "dotenv-admin-token"


def test_existing_environment_takes_precedence_over_dotenv(tmp_path, monkeypatch):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "AUTH_SERVER_ISSUER=http://dotenv.example:5056\n", encoding="utf-8"
    )
    monkeypatch.setattr(config_module, "_DOTENV_PATH", dotenv_path)
    monkeypatch.setenv("AUTH_SERVER_ISSUER", "http://external.example:5056")

    config = AuthServerConfig.from_env()

    assert config.issuer == "http://external.example:5056"


def test_missing_dotenv_keeps_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_DOTENV_PATH", tmp_path / "missing.env")
    monkeypatch.delenv("AUTH_SERVER_ISSUER", raising=False)

    config = AuthServerConfig.from_env()

    assert config.issuer == DEFAULT_ISSUER


def test_missing_python_dotenv_is_ignored(tmp_path, monkeypatch):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "AUTH_SERVER_ISSUER=http://dotenv.example:5056\n", encoding="utf-8"
    )
    monkeypatch.setattr(config_module, "_DOTENV_PATH", dotenv_path)
    monkeypatch.delenv("AUTH_SERVER_ISSUER", raising=False)
    original_import = builtins.__import__

    def import_without_dotenv(name, *args, **kwargs):
        if name == "dotenv":
            raise ModuleNotFoundError("No module named 'dotenv'", name="dotenv")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_dotenv)

    config = AuthServerConfig.from_env()

    assert config.issuer == DEFAULT_ISSUER


def test_custom_environment_does_not_load_dotenv(monkeypatch):
    def fail_if_called():
        raise AssertionError(".env must not be loaded for an explicit environment mapping")

    monkeypatch.setattr(config_module, "_load_local_dotenv", fail_if_called)

    config = AuthServerConfig.from_env(
        {"AUTH_SERVER_ISSUER": "http://custom.example:5056"}
    )

    assert config.issuer == "http://custom.example:5056"
