import json
import os
import platform
import re
import shutil
import subprocess
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


def test_release_compose_loads_sandbox_settings_from_backend_env_file():
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "env_file:\n      - ./backend/.env" in compose_text
    for env_name in (
        "SANDBOX_EXECUTION_MODE",
        "SANDBOX_ENABLED",
        "SANDBOX_FALLBACK_LOCAL",
        "SANDBOX_SECRET_ALLOW_PLAIN_HTTP",
    ):
        assert env_name not in compose_text


def test_release_compose_pins_persistent_storage_dirs_inside_runtime_volume():
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "./.onto-runtime:/app/.onto-runtime" in compose_text
    assert "CHAT_STORAGE_DIR=/app/.onto-runtime/data/chats" in compose_text
    assert "EXECUTION_STORAGE_DIR=/app/.onto-runtime/data/executions" in compose_text


def test_release_lifecycle_supports_legacy_docker_compose_and_uses_compatible_env_files():
    common_text = (DEPLOY_ROOT / "common.sh").read_text(encoding="utf-8")
    preflight_text = (DEPLOY_ROOT / "preflight.sh").read_text(encoding="utf-8")
    upgrade_text = (DEPLOY_ROOT / "upgrade.sh").read_text(encoding="utf-8")

    assert "docker compose version" in common_text
    assert "command -v docker-compose" in common_text
    assert "export DOCKER_COMPOSE_CMD='docker compose'" in common_text
    assert "export DOCKER_COMPOSE_CMD='docker-compose'" in common_text
    assert "<1.28" in common_text
    assert 'BACKEND_COMPOSE_ENV_FILE="${BUNDLE_ROOT}/backend/.env"' in common_text
    assert 'LEGACY_COMPOSE_ENV_FILE="${BUNDLE_ROOT}/.env"' in common_text
    assert 'LEGACY_COMPOSE_HOST_ENV_FILE="${STATE_DIR}/legacy-compose-host.env"' in common_text
    assert common_text.count('"${COMPOSE_ENV_ARGS[@]}"') == 3
    assert "compose_probe_output" in preflight_text
    assert '--compose-env-file "${compose_env_file}"' in preflight_text
    assert 'check_port_available "${MCP_HOST_PORT}"' in preflight_text
    assert 'http://127.0.0.1:${MCP_HOST_PORT}/health' in preflight_text
    assert 'http://127.0.0.1:${MCP_SERVER_PORT:-7488}/health' not in preflight_text
    assert 'PROTECTED_ITEMS=".env backend/.env ' in upgrade_text


def test_release_lifecycle_preserves_legacy_root_env_port_overrides(tmp_path):
    deploy_root = tmp_path / "deploy"
    backend_root = tmp_path / "backend"
    fake_bin = tmp_path / "bin"
    deploy_root.mkdir()
    backend_root.mkdir()
    fake_bin.mkdir()
    shutil.copyfile(DEPLOY_ROOT / "common.sh", deploy_root / "common.sh")
    (backend_root / ".env").write_text(
        "AUTH_USER_MAX_COUNT=10\nUSER_MAX_COUNT=10\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "BACKEND_PORT=5100\nFRONTEND_PORT=6600\nMCP_SERVER_PORT=9000\n"
        "AUTH_USER_MAX_COUNT=0\nSANDBOX_ENABLED=false\n",
        encoding="utf-8",
    )
    docker_args = tmp_path / "docker-args.txt"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/sh
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then
    exit 0
fi
printf '%s\n' "$@" > "$DOCKER_ARGS_FILE"
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["DOCKER_ARGS_FILE"] = str(docker_args)

    subprocess.run(
        [
            "bash",
            "-c",
            f'source "{deploy_root / "common.sh"}"; compose_up backend frontend auth sandbox',
        ],
        env=env,
        check=True,
    )

    args = docker_args.read_text(encoding="utf-8").splitlines()
    first_env = args.index("--env-file")
    second_env = args.index("--env-file", first_env + 2)
    assert args[first_env + 1] == str(backend_root / ".env")
    filtered_env = deploy_root / "state" / "legacy-compose-host.env"
    assert args[second_env + 1] == str(filtered_env)
    assert first_env < second_env
    assert filtered_env.read_text(encoding="utf-8") == (
        "BACKEND_PORT=5100\nFRONTEND_PORT=6600\nMCP_SERVER_PORT=9000\n"
    )


def test_preflight_uses_legacy_mcp_host_port_without_root_config_override(
    tmp_path,
):
    bundle_root = tmp_path / "consumer"
    deploy_root = bundle_root / "deploy"
    backend_root = bundle_root / "backend"
    fake_bin = tmp_path / "bin"
    deploy_root.mkdir(parents=True)
    backend_root.mkdir()
    fake_bin.mkdir()

    for filename in (
        "common.sh",
        "preflight.sh",
        "preflight_config.py",
        "jiuwenbox-policy.yaml",
        "docker-compose.release.yml",
    ):
        shutil.copy2(DEPLOY_ROOT / filename, deploy_root / filename)

    (backend_root / ".env").write_text(
        "AUTH_SERVER_URL=http://auth-server:5056\n"
        "AUTH_SERVER_ADMIN_TOKEN=test-admin-token-with-sufficient-length\n"
        "MCP_SERVER_PORT=7488\n"
        "MCP_TOKEN_SECRET=test-mcp-token-secret-with-sufficient-length\n"
        "USER_MAX_COUNT=10\n"
        "AUTH_USER_MAX_COUNT=10\n",
        encoding="utf-8",
    )
    (bundle_root / ".env").write_text(
        "BACKEND_PORT=5100\nFRONTEND_PORT=6600\nMCP_SERVER_PORT=9000\n"
        "AUTH_USER_MAX_COUNT=0\nSANDBOX_ENABLED=false\n",
        encoding="utf-8",
    )

    release_arch = {
        "x86_64": "amd64",
        "aarch64": "arm64",
    }.get(platform.machine(), platform.machine())
    image_tars = {
        "backend_image_tar": "backend.tar",
        "frontend_image_tar": "frontend.tar",
        "jiuwenbox_image_tar": "jiuwenbox.tar",
        "auth_server_image_tar": "auth.tar",
    }
    for filename in image_tars.values():
        (bundle_root / filename).touch()
    (deploy_root / "manifest.json").write_text(
        json.dumps({"release_arch": release_arch, **image_tars}, indent=2),
        encoding="utf-8",
    )

    compose_config = {
        "services": {
            "backend": {
                "environment": {
                    "SANDBOX_ENABLED": "true",
                    "SANDBOX_FALLBACK_LOCAL": "false",
                    "SANDBOX_SECRET_ALLOW_PLAIN_HTTP": "false",
                    "MCP_SERVER_PORT": "7488",
                },
                "ports": [{"target": 5055, "published": "5100"}],
            },
            "mcp": {"ports": [{"target": 7488, "published": "9000"}]},
            "frontend": {"ports": [{"target": 80, "published": "6600"}]},
        }
    }
    compose_config_path = tmp_path / "compose-config.json"
    compose_config_path.write_text(json.dumps(compose_config), encoding="utf-8")

    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/sh
if [ "$1" = "info" ]; then
    exit 0
fi
if [ "$1" = "compose" ]; then
    if [ "$2" = "version" ]; then
        exit 0
    fi
    case " $* " in
        *" config "*) cat "$COMPOSE_CONFIG_JSON"; exit 0 ;;
    esac
fi
if [ "$1" = "inspect" ]; then
    case "$*" in
        *onto-agent-mcp*) printf 'healthy\\n' ;;
    esac
    exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CURL_ARGS_FILE\"\n",
        encoding="utf-8",
    )
    fake_ss = fake_bin / "ss"
    fake_ss.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SS_ARGS_FILE\"\n",
        encoding="utf-8",
    )
    for executable in (fake_docker, fake_curl, fake_ss):
        executable.chmod(0o755)

    curl_args_file = tmp_path / "curl-args.txt"
    ss_args_file = tmp_path / "ss-args.txt"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["COMPOSE_CONFIG_JSON"] = str(compose_config_path)
    env["CURL_ARGS_FILE"] = str(curl_args_file)
    env["SS_ARGS_FILE"] = str(ss_args_file)
    result = subprocess.run(
        ["bash", str(deploy_root / "preflight.sh"), "--pre-migration"],
        cwd=bundle_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    curl_args = curl_args_file.read_text(encoding="utf-8")
    assert "http://127.0.0.1:9000/health" in curl_args
    assert "http://127.0.0.1:7488/health" not in curl_args
    checked_ports = ss_args_file.read_text(encoding="utf-8")
    assert ":5100 " in checked_ports
    assert ":9000 " in checked_ports
    assert ":6600 " in checked_ports

    filtered_env = deploy_root / "state" / "legacy-compose-host.env"
    assert filtered_env.read_text(encoding="utf-8") == (
        "BACKEND_PORT=5100\nFRONTEND_PORT=6600\nMCP_SERVER_PORT=9000\n"
    )
    report = json.loads(
        (deploy_root / "state" / "preflight-config.json").read_text()
    )
    assert report["limits"] == {
        "user_max_count": 10,
        "auth_user_max_count": 10,
    }
    assert report["host_ports"] == {
        "backend": 5100,
        "mcp": 9000,
        "frontend": 6600,
    }


def test_preflight_supports_docker_compose_v1_fallback_for_release_port_resolution(
    tmp_path,
):
    bundle_root = tmp_path / "consumer"
    deploy_root = bundle_root / "deploy"
    backend_root = bundle_root / "backend"
    fake_bin = tmp_path / "bin"
    deploy_root.mkdir(parents=True)
    backend_root.mkdir()
    fake_bin.mkdir()

    for filename in (
        "common.sh",
        "preflight.sh",
        "preflight_config.py",
        "jiuwenbox-policy.yaml",
        "docker-compose.release.yml",
    ):
        shutil.copy2(DEPLOY_ROOT / filename, deploy_root / filename)

    (backend_root / ".env").write_text(
        "AUTH_SERVER_URL=http://auth-server:5056\n"
        "AUTH_SERVER_ADMIN_TOKEN=test-admin-token-with-sufficient-length\n"
        "MCP_SERVER_PORT=7488\n"
        "MCP_TOKEN_SECRET=test-mcp-token-secret-with-sufficient-length\n"
        "USER_MAX_COUNT=10\n"
        "AUTH_USER_MAX_COUNT=10\n",
        encoding="utf-8",
    )
    (bundle_root / ".env").write_text(
        "BACKEND_PORT=5100\nFRONTEND_PORT=6600\nMCP_SERVER_PORT=9000\n",
        encoding="utf-8",
    )

    release_arch = {
        "x86_64": "amd64",
        "aarch64": "arm64",
    }.get(platform.machine(), platform.machine())
    image_tars = {
        "backend_image_tar": "backend.tar",
        "frontend_image_tar": "frontend.tar",
        "jiuwenbox_image_tar": "jiuwenbox.tar",
        "auth_server_image_tar": "auth.tar",
    }
    for filename in image_tars.values():
        (bundle_root / filename).touch()
    (deploy_root / "manifest.json").write_text(
        json.dumps({"release_arch": release_arch, **image_tars}, indent=2),
        encoding="utf-8",
    )

    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/sh
if [ "$1" = "info" ]; then
    exit 0
fi
if [ "$1" = "inspect" ]; then
    case "$*" in
        *onto-agent-mcp*) printf 'healthy\\n' ;;
    esac
    exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_docker_compose = fake_bin / "docker-compose"
    fake_docker_compose.write_text(
        """#!/bin/sh
if [ "$1" = "version" ] && [ "$2" = "--short" ]; then
    printf '1.29.2\\n'
    exit 0
fi
if [ "$1" = "version" ]; then
    printf 'docker-compose version 1.29.2, build test\\n'
    exit 0
fi
args=" $* "
case "$args" in
    *" config --format json "*) exit 1 ;;
    *" config "*) exit 0 ;;
esac
exit 1
""",
        encoding="utf-8",
    )
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CURL_ARGS_FILE\"\n",
        encoding="utf-8",
    )
    fake_ss = fake_bin / "ss"
    fake_ss.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SS_ARGS_FILE\"\n",
        encoding="utf-8",
    )
    for executable in (fake_docker, fake_docker_compose, fake_curl, fake_ss):
        executable.chmod(0o755)

    curl_args_file = tmp_path / "curl-args.txt"
    ss_args_file = tmp_path / "ss-args.txt"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(
        ["bash", str(deploy_root / "preflight.sh"), "--pre-migration"],
        cwd=bundle_root,
        env={
            **env,
            "CURL_ARGS_FILE": str(curl_args_file),
            "SS_ARGS_FILE": str(ss_args_file),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    curl_args = curl_args_file.read_text(encoding="utf-8")
    assert "http://127.0.0.1:9000/health" in curl_args
    checked_ports = ss_args_file.read_text(encoding="utf-8")
    assert ":5100 " in checked_ports
    assert ":9000 " in checked_ports
    assert ":6600 " in checked_ports

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
