import json
import re
from pathlib import Path

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.release.yml"
DEPLOY_ROOT = COMPOSE_PATH.parent
BACKEND_ROOT = DEPLOY_ROOT.parent / "backend"
REPOSITORY_ROOT = DEPLOY_ROOT.parents[1]


def _manifest_template(build_script: Path) -> dict[str, str]:
    script_text = build_script.read_text(encoding="utf-8")
    match = re.search(
        r'cat > "\$\{BUNDLE_DIR\}/deploy/manifest\.json" <<EOF\n(\{.*?\})\nEOF',
        script_text,
        flags=re.DOTALL,
    )
    assert match is not None, f"manifest template not found in {build_script}"
    return json.loads(match.group(1).replace("${VERSION}", "v-test"))


def test_release_compose_has_no_legacy_config_mount_or_environment():
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "./backend/config:" not in compose_text
    assert "LEGACY_SYSTEM_CONFIG_PATH" not in compose_text


def test_release_compose_pins_fail_closed_sandbox_settings():
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "env_file:\n      - ./backend/.env" in compose_text
    assert "- SANDBOX_ENABLED=true" in compose_text
    assert "- SANDBOX_FALLBACK_LOCAL=false" in compose_text
    assert "- SANDBOX_SECRET_ALLOW_PLAIN_HTTP=false" in compose_text


def test_release_compose_pins_persistent_storage_dirs_inside_runtime_volume():
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "./.onto-runtime:/app/.onto-runtime" in compose_text
    assert "CHAT_STORAGE_DIR=/app/.onto-runtime/data/chats" in compose_text
    assert "EXECUTION_STORAGE_DIR=/app/.onto-runtime/data/executions" in compose_text


def test_release_scripts_have_no_legacy_config_directory_plumbing():
    for script_name in (
        "backup.sh",
        "build_release.sh",
        "common.sh",
        "rollback.sh",
        "upgrade.sh",
    ):
        script_text = (DEPLOY_ROOT / script_name).read_text(encoding="utf-8")
        assert "backend/config" not in script_text
        assert "backend-config" not in script_text


def test_full_release_manifest_preserves_all_consumer_fields():
    consumer_manifest = _manifest_template(DEPLOY_ROOT / "build_release.sh")
    full_manifest = _manifest_template(REPOSITORY_ROOT / "deploy" / "build.sh")

    assert consumer_manifest.keys() <= full_manifest.keys()


def test_deploy_runs_shared_migration_runner_before_strict_preflight():
    deploy_text = (DEPLOY_ROOT / "deploy.sh").read_text(encoding="utf-8")
    migrate_text = (DEPLOY_ROOT / "migrate.sh").read_text(encoding="utf-8")
    deploy_lines = deploy_text.splitlines()

    pre_migration_preflight = next(i for i, line in enumerate(deploy_lines) if 'preflight.sh" --pre-migration' in line)
    dry_run = next(i for i, line in enumerate(deploy_lines) if 'migrate.sh" "${CURRENT_VERSION:-}" "${TARGET_VERSION}" --dry-run' in line)
    stop_writes = next(i for i, line in enumerate(deploy_lines) if 'compose_stop "${CURRENT_BACKEND_IMAGE}"' in line)
    apply = next(i for i, line in enumerate(deploy_lines) if '--confirm-writes-stopped' in line)
    remove_stopped = next(i for i, line in enumerate(deploy_lines) if 'compose_down "${CURRENT_BACKEND_IMAGE}"' in line)
    strict_preflight = next(i for i, line in enumerate(deploy_lines) if 'Validating canonical configuration after migration' in line)

    assert pre_migration_preflight < dry_run < stop_writes < apply < strict_preflight < remove_stopped
    assert "python -m migrations.runner" in migrate_text
    assert "ontology_data_sources_direct_v2" not in migrate_text
    assert '--env-file "${BUNDLE_ROOT}/backend/.env"' in migrate_text
    assert '${BUNDLE_ROOT}/.onto-runtime:/app/.onto-runtime' in migrate_text


def test_backup_captures_legacy_backend_container_storage_before_container_removal():
    backup_text = (DEPLOY_ROOT / "backup.sh").read_text(encoding="utf-8")

    assert '--print-legacy-export-paths' in backup_text
    assert 'docker cp "${container_name}:${chat_source}/."' in backup_text
    assert 'docker cp "${container_name}:${execution_source}/."' in backup_text
    assert 'legacy-backend-data.tar.gz' in backup_text
    assert 'find "${staging_root}/chats" -mindepth 1 -print -quit | grep -q .' in backup_text
    assert 'find "${staging_root}/executions" -mindepth 1 -print -quit | grep -q .' in backup_text


def test_data_source_compatibility_migration_uses_central_startup_runner_not_runtime_store():
    app_text = (BACKEND_ROOT / "app.py").read_text(encoding="utf-8")
    runner_text = (BACKEND_ROOT / "migrations" / "runner.py").read_text(encoding="utf-8")
    deploy_text = (DEPLOY_ROOT / "deploy.sh").read_text(encoding="utf-8")
    migrate_text = (DEPLOY_ROOT / "migrate.sh").read_text(encoding="utf-8")
    store_text = (
        BACKEND_ROOT / "agent" / "tools" / "business_data" / "ontology_sources_store.py"
    ).read_text(encoding="utf-8")

    assert "run_startup_migrations()" in app_text
    assert "migrate_legacy_ontology_data_sources" not in app_text
    assert "migrate_legacy_ontology_data_sources" not in store_text
    assert "MODE_CONNECTION_REF" not in store_text
    assert "ontology_data_sources_direct_v2" in runner_text
    assert "ontology_data_sources_direct_v2" not in deploy_text
    assert "ontology_data_sources_direct_v2" not in migrate_text


def test_observability_compatibility_migration_has_no_entrypoint_or_deploy_coupling():
    app_text = (BACKEND_ROOT / "app.py").read_text(encoding="utf-8")
    runner_text = (BACKEND_ROOT / "migrations" / "runner.py").read_text(encoding="utf-8")
    deploy_text = (DEPLOY_ROOT / "deploy.sh").read_text(encoding="utf-8")
    migrate_text = (DEPLOY_ROOT / "migrate.sh").read_text(encoding="utf-8")

    assert app_text.count("run_startup_migrations()") == 1
    assert "observability_workspace_config_v1" not in app_text
    assert "migrate_global_observability_to_workspaces" not in app_text
    assert "observability_workspace_config_v1" in runner_text
    assert "MigrationFailurePolicy.NON_BLOCKING" in runner_text
    assert "observability-workspace-config-v1" not in deploy_text
    assert "observability-workspace-config-v1" not in migrate_text
    assert "observability_workspace_config_v1" not in deploy_text
    assert "observability_workspace_config_v1" not in migrate_text


def test_rollback_is_code_only_and_does_not_restore_runtime_snapshots():
    rollback_text = (DEPLOY_ROOT / "rollback.sh").read_text(encoding="utf-8")

    assert "Code-only rollback keeps current runtime/auth data in place" in rollback_text
    assert 'compose_stop "${FAILED_BACKEND_IMAGE}"' in rollback_text
    assert 'compose_down "${FAILED_BACKEND_IMAGE}"' in rollback_text
    stop_index = rollback_text.index('compose_stop "${FAILED_BACKEND_IMAGE}"')
    backup_index = rollback_text.index('backup.sh" "rollback-${PREVIOUS_VERSION}"')
    down_index = rollback_text.index('compose_down "${FAILED_BACKEND_IMAGE}"')
    assert stop_index < backup_index < down_index
    assert 'restore_archive_if_present "${LAST_BACKUP_DIR}/onto-runtime.tar.gz"' not in rollback_text
    assert 'restore_archive_if_present "${LAST_BACKUP_DIR}/onto-auth.tar.gz"' not in rollback_text
    assert 'rm -rf "${BUNDLE_ROOT}/.onto-runtime"' not in rollback_text
    assert 'rm -rf "${BUNDLE_ROOT}/.onto-auth"' not in rollback_text
    assert not (
        BACKEND_ROOT / "integration" / "observability" / "migration.py"
    ).exists()


def test_system_config_migrations_are_ordered_only_in_shared_runner():
    app_text = (BACKEND_ROOT / "app.py").read_text(encoding="utf-8")
    runner_text = (BACKEND_ROOT / "migrations" / "runner.py").read_text(encoding="utf-8")
    forbidden_values = (
        "system-config-model-profiles-v1",
        "system-config-secret-encryption-v1",
        "system_config_model_profiles_v1",
        "system_config_secret_encryption_v1",
    )

    assert app_text.count("run_startup_migrations()") == 1
    assert runner_text.index("system-config-model-profiles-v1") < runner_text.index(
        "system-config-secret-encryption-v1"
    )
    for script_name in ("deploy.sh", "migrate.sh", "preflight.sh", "backup.sh"):
        script_text = (DEPLOY_ROOT / script_name).read_text(encoding="utf-8")
        assert all(value not in script_text for value in forbidden_values)
