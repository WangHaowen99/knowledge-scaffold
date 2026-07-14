import json

import pytest

from workspace.workspaces import create_workspace


SECRET_TEST_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def _create_workspace(tmp_path, monkeypatch, workspace_id="demo"):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    create_workspace("Demo Workspace", workspace_id=workspace_id, set_as_default=True)


def test_workspace_observability_defaults_to_disabled_local(tmp_path, monkeypatch):
    _create_workspace(tmp_path, monkeypatch)

    from integration.observability.workspace_config import get_workspace_observability_config

    assert get_workspace_observability_config("demo") == {
        "enabled": False,
        "mode": "local",
        "local": {"retention_days": 30},
        "langfuse": {
            "public_key": "",
            "secret_key": "",
            "base_url": "",
            "debug": False,
        },
    }


def test_workspace_observability_update_encrypts_and_masks_langfuse_secret(tmp_path, monkeypatch):
    _create_workspace(tmp_path, monkeypatch)

    from integration.observability.workspace_config import (
        get_public_workspace_observability_config,
        get_workspace_observability_config,
        update_workspace_observability_config,
    )

    update_workspace_observability_config(
        "demo",
        {
            "enabled": True,
            "mode": "langfuse",
            "local": {"retention_days": 14},
            "langfuse": {
                "public_key": "pk-test",
                "secret_key": "sk-test",
                "base_url": "https://langfuse.example",
                "debug": True,
            },
        },
    )

    raw_path = tmp_path / ".onto-runtime" / "workspaces" / "demo" / "observability_config.json"
    raw_text = raw_path.read_text(encoding="utf-8")
    raw = json.loads(raw_text)

    assert "sk-test" not in raw_text
    assert raw["langfuse"]["secret_key"].startswith("enc:v1:")
    assert get_workspace_observability_config("demo")["langfuse"]["secret_key"] == "sk-test"
    assert get_public_workspace_observability_config("demo")["langfuse"] == {
        "public_key": "pk-test",
        "base_url": "https://langfuse.example",
        "debug": True,
        "has_secret_key": True,
    }


def test_workspace_observability_encrypted_secret_is_bound_to_workspace_context(tmp_path, monkeypatch):
    _create_workspace(tmp_path, monkeypatch, workspace_id="alpha")
    create_workspace("Beta Workspace", workspace_id="beta")

    from integration.observability.workspace_config import (
        get_workspace_observability_config,
        update_workspace_observability_config,
    )

    update_workspace_observability_config(
        "alpha",
        {
            "langfuse": {
                "secret_key": "alpha-secret",
            },
        },
    )
    raw_path = tmp_path / ".onto-runtime" / "workspaces" / "alpha" / "observability_config.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    beta_path = tmp_path / ".onto-runtime" / "workspaces" / "beta" / "observability_config.json"
    beta_path.write_text(json.dumps(raw), encoding="utf-8")

    assert get_workspace_observability_config("alpha")["langfuse"]["secret_key"] == "alpha-secret"
    with pytest.raises(Exception, match="secret 解密失败"):
        get_workspace_observability_config("beta")


def test_workspace_observability_patch_preserves_existing_secret_when_omitted(tmp_path, monkeypatch):
    _create_workspace(tmp_path, monkeypatch)

    from integration.observability.workspace_config import (
        get_workspace_observability_config,
        update_workspace_observability_config,
    )

    update_workspace_observability_config(
        "demo",
        {"langfuse": {"public_key": "pk-old", "secret_key": "sk-old"}},
    )
    update_workspace_observability_config(
        "demo",
        {"langfuse": {"public_key": "pk-new"}},
    )

    config = get_workspace_observability_config("demo")
    assert config["langfuse"]["public_key"] == "pk-new"
    assert config["langfuse"]["secret_key"] == "sk-old"


def test_workspace_observability_rejects_unknown_and_invalid_values(tmp_path, monkeypatch):
    _create_workspace(tmp_path, monkeypatch)

    from integration.observability.workspace_config import update_workspace_observability_config

    with pytest.raises(ValueError, match="Unknown observability config key"):
        update_workspace_observability_config("demo", {"future": True})

    with pytest.raises(ValueError, match="mode must be local or langfuse"):
        update_workspace_observability_config("demo", {"mode": "otlp"})

    with pytest.raises(ValueError, match="local.retention_days must be greater than or equal to 1"):
        update_workspace_observability_config("demo", {"local": {"retention_days": 0}})
