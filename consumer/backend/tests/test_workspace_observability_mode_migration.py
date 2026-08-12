import json


def test_mode_migration_removes_legacy_field_and_is_idempotent(tmp_path):
    from migrations.observability_workspace_mode_v2 import (
        MARKER_RELATIVE_PATH,
        execute_migration,
    )

    runtime_root = tmp_path / ".onto-runtime"
    config_path = runtime_root / "workspaces" / "demo" / "observability_config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "mode": "langfuse",
                "local": {"retention_days": 14},
                "speedscope": {"retention_days": 7},
                "langfuse": {
                    "public_key": "pk-test",
                    "secret_key": "enc:v1:unchanged",
                    "base_url": "https://langfuse.example",
                    "debug": False,
                },
            }
        ),
        encoding="utf-8",
    )

    code, report = execute_migration(runtime_root, dry_run=False, writes_stopped=True)

    assert code == 0
    assert report["counts"]["workspace_configs_updated"] == 1
    migrated = json.loads(config_path.read_text(encoding="utf-8"))
    assert "mode" not in migrated
    assert migrated["langfuse"]["secret_key"] == "enc:v1:unchanged"
    assert (runtime_root / MARKER_RELATIVE_PATH).exists()

    second_code, second_report = execute_migration(
        runtime_root,
        dry_run=False,
        writes_stopped=True,
    )
    assert second_code == 0
    assert second_report["status"] == "complete"
