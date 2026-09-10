import json
from pathlib import Path

import pytest
from backend_synthetic_credentials import ENCRYPTION_KEY, synthetic_secret
from workspace.workspaces import create_workspace

SECRET_TEST_KEY = ENCRYPTION_KEY


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
        "speedscope": {"retention_days": 30},
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
            "speedscope": {"retention_days": 7},
            "langfuse": {
                "public_key": "pk-test",
                "secret_key": synthetic_secret("workspace_observability_config-credential-05"),
                "base_url": "https://langfuse.example.net",
                "debug": True,
            },
        },
    )

    raw_path = tmp_path / ".onto-runtime" / "workspaces" / "demo" / "observability_config.json"
    raw_text = raw_path.read_text(encoding="utf-8")
    raw = json.loads(raw_text)

    assert synthetic_secret("workspace_observability_config-credential-05") not in raw_text
    assert raw["langfuse"]["secret_key"].startswith("enc:v1:")
    assert get_workspace_observability_config("demo")["langfuse"]["secret_key"] == synthetic_secret(
        "workspace_observability_config-credential-05"
    )
    assert get_workspace_observability_config("demo")["speedscope"] == {"retention_days": 7}
    assert get_public_workspace_observability_config("demo")["langfuse"] == {
        "public_key": "pk-test",
        "base_url": "https://langfuse.example.net",
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
                "secret_key": synthetic_secret("workspace_observability_config-credential-01"),
            },
        },
    )
    raw_path = tmp_path / ".onto-runtime" / "workspaces" / "alpha" / "observability_config.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    beta_path = tmp_path / ".onto-runtime" / "workspaces" / "beta" / "observability_config.json"
    beta_path.write_text(json.dumps(raw), encoding="utf-8")

    assert get_workspace_observability_config("alpha")["langfuse"]["secret_key"] == synthetic_secret(
        "workspace_observability_config-credential-01"
    )
    with pytest.raises(Exception, match="需要先完成迁移或人工修复"):
        get_workspace_observability_config("beta")


def test_workspace_observability_patch_preserves_existing_secret_when_omitted(tmp_path, monkeypatch):
    _create_workspace(tmp_path, monkeypatch)

    from integration.observability.workspace_config import (
        get_workspace_observability_config,
        update_workspace_observability_config,
    )

    update_workspace_observability_config(
        "demo",
        {
            "langfuse": {
                "public_key": "pk-old",
                "secret_key": synthetic_secret("workspace_observability_config-credential-04"),
            }
        },
    )
    update_workspace_observability_config(
        "demo",
        {"langfuse": {"public_key": "pk-new"}},
    )

    config = get_workspace_observability_config("demo")
    assert config["langfuse"]["public_key"] == "pk-new"
    assert config["langfuse"]["secret_key"] == synthetic_secret("workspace_observability_config-credential-04")


def test_workspace_observability_rejects_unknown_and_invalid_values(tmp_path, monkeypatch):
    _create_workspace(tmp_path, monkeypatch)

    from integration.observability.workspace_config import update_workspace_observability_config

    with pytest.raises(ValueError, match="Unknown observability config key"):
        update_workspace_observability_config("demo", {"future": True})

    with pytest.raises(ValueError, match="mode must be local or langfuse"):
        update_workspace_observability_config("demo", {"mode": "otlp"})

    with pytest.raises(ValueError, match="local.retention_days must be greater than or equal to 1"):
        update_workspace_observability_config("demo", {"local": {"retention_days": 0}})

    with pytest.raises(ValueError, match="speedscope.retention_days must be greater than or equal to 1"):
        update_workspace_observability_config("demo", {"speedscope": {"retention_days": 0}})


def test_legacy_workspace_observability_config_reads_with_default_speedscope(
    tmp_path,
    monkeypatch,
):
    _create_workspace(tmp_path, monkeypatch)
    raw_path = tmp_path / ".onto-runtime" / "workspaces" / "demo" / "observability_config.json"
    raw_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "mode": "local",
                "local": {"retention_days": 14},
                "langfuse": {
                    "public_key": "",
                    "secret_key": "",
                    "base_url": "",
                    "debug": False,
                },
            }
        ),
        encoding="utf-8",
    )

    from integration.observability.workspace_config import get_workspace_observability_config

    assert get_workspace_observability_config("demo")["speedscope"] == {"retention_days": 30}
    assert "speedscope" not in json.loads(raw_path.read_text(encoding="utf-8"))


def test_workspace_observability_read_reports_migration_required_without_writing(
    tmp_path,
    monkeypatch,
):
    _create_workspace(tmp_path, monkeypatch)
    raw_path = tmp_path / ".onto-runtime" / "workspaces" / "demo" / "observability_config.json"
    raw_path.write_text('{"observability":{"enabled":true}}', encoding="utf-8")
    before = raw_path.read_bytes()

    from integration.observability.workspace_config import (
        WorkspaceObservabilityMigrationRequiredError,
        get_workspace_observability_config,
    )

    with pytest.raises(
        WorkspaceObservabilityMigrationRequiredError,
        match="需要先完成迁移或人工修复",
    ):
        get_workspace_observability_config("demo")

    assert raw_path.read_bytes() == before


def test_workspace_observability_read_rejects_plaintext_secret_without_rewriting(
    tmp_path,
    monkeypatch,
):
    _create_workspace(tmp_path, monkeypatch)
    raw_path = tmp_path / ".onto-runtime" / "workspaces" / "demo" / "observability_config.json"
    raw_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "mode": "langfuse",
                "local": {"retention_days": 30},
                "langfuse": {
                    "public_key": "pk",
                    "secret_key": synthetic_secret("workspace_observability_config-credential-03"),
                    "base_url": "https://langfuse.example.net",
                    "debug": False,
                },
            }
        ),
        encoding="utf-8",
    )
    before = raw_path.read_bytes()

    from integration.observability.workspace_config import (
        WorkspaceObservabilityMigrationRequiredError,
        get_workspace_observability_config,
    )

    with pytest.raises(WorkspaceObservabilityMigrationRequiredError):
        get_workspace_observability_config("demo")

    assert raw_path.read_bytes() == before


def test_workspace_observability_read_with_missing_key_does_not_generate_new_key(
    tmp_path,
    monkeypatch,
):
    _create_workspace(tmp_path, monkeypatch)
    from integration.observability.workspace_config import (
        WorkspaceObservabilityMigrationRequiredError,
        get_workspace_observability_config,
        update_workspace_observability_config,
    )

    update_workspace_observability_config(
        "demo",
        {"langfuse": {"secret_key": synthetic_secret("workspace_observability_config-credential-02")}},
    )
    runtime_root = tmp_path / ".onto-runtime"
    key_path = runtime_root / "config" / "secret.key"
    assert not key_path.exists()
    monkeypatch.delenv("SECRET_ENCRYPTION_KEY")
    before = {path.relative_to(runtime_root): path.read_bytes() for path in runtime_root.rglob("*") if path.is_file()}

    with pytest.raises(WorkspaceObservabilityMigrationRequiredError):
        get_workspace_observability_config("demo")

    after = {path.relative_to(runtime_root): path.read_bytes() for path in runtime_root.rglob("*") if path.is_file()}
    assert after == before
    assert not key_path.exists()


def test_workspace_observability_atomic_replace_failure_preserves_previous_file(
    tmp_path,
    monkeypatch,
):
    _create_workspace(tmp_path, monkeypatch)
    from integration.observability import workspace_config as workspace_config_module

    workspace_config_module.update_workspace_observability_config(
        "demo",
        {"enabled": False, "local": {"retention_days": 5}},
    )
    raw_path = tmp_path / ".onto-runtime" / "workspaces" / "demo" / "observability_config.json"
    before = raw_path.read_bytes()
    original_replace = workspace_config_module.os.replace

    def fail_target_replace(source, target):
        if Path(target) == raw_path:
            raise OSError("simulated")
        return original_replace(source, target)

    monkeypatch.setattr(workspace_config_module.os, "replace", fail_target_replace)

    with pytest.raises(OSError, match="simulated"):
        workspace_config_module.update_workspace_observability_config(
            "demo",
            {"enabled": True, "local": {"retention_days": 15}},
        )

    assert raw_path.read_bytes() == before
    assert not list(raw_path.parent.glob("observability_config.json.*.tmp"))
