from pathlib import Path

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.release.yml"
DEPLOY_ROOT = COMPOSE_PATH.parent
BACKEND_ROOT = DEPLOY_ROOT.parent / "backend"


def test_release_compose_has_no_legacy_config_mount_or_environment():
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "./backend/config:" not in compose_text
    assert "LEGACY_SYSTEM_CONFIG_PATH" not in compose_text


def test_release_compose_loads_sandbox_settings_from_backend_env_file():
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "env_file:\n      - ./backend/.env" in compose_text
    for env_name in (
        "SANDBOX_ENABLED",
        "SANDBOX_FALLBACK_LOCAL",
        "SANDBOX_SECRET_ALLOW_PLAIN_HTTP",
    ):
        assert env_name not in compose_text


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


def test_deploy_runs_shared_migration_runner_before_strict_preflight():
    deploy_text = (DEPLOY_ROOT / "deploy.sh").read_text(encoding="utf-8")
    migrate_text = (DEPLOY_ROOT / "migrate.sh").read_text(encoding="utf-8")

    pre_migration_preflight = deploy_text.index('preflight.sh" --pre-migration')
    dry_run = deploy_text.index('migrate.sh" "${CURRENT_VERSION:-}" "${TARGET_VERSION}" --dry-run')
    apply = deploy_text.index('migrate.sh" "${CURRENT_VERSION:-}" "${TARGET_VERSION}" --confirm-writes-stopped')
    strict_preflight = deploy_text.index('Validating canonical configuration after migration')

    assert pre_migration_preflight < dry_run < apply < strict_preflight
    assert "python -m migrations.runner" in migrate_text
    assert "ontology_data_sources_direct_v2" not in migrate_text
    assert '--env-file "${BUNDLE_ROOT}/backend/.env"' in migrate_text
    assert '${BUNDLE_ROOT}/.onto-runtime:/app/.onto-runtime' in migrate_text


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
