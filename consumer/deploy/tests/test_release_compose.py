import json
import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
from deploy_synthetic_credentials import AUTH_ADMIN_TOKEN, BOOTSTRAP_ADMIN_PASSWORD, MCP_TOKEN_SECRET

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.release.yml"
DEPLOY_ROOT = COMPOSE_PATH.parent
BACKEND_ROOT = DEPLOY_ROOT.parent / "backend"
REPOSITORY_ROOT = DEPLOY_ROOT.parents[1]
SOURCE_COMPOSE_PATH = DEPLOY_ROOT.parent / "docker-compose.yml"
STORAGE_VOLUME_COMPOSE_PATH = DEPLOY_ROOT / "docker-compose.storage-volume.yml"


def _test_bash_executable():
    if os.name == "nt":
        git_path = shutil.which("git")
        if git_path:
            git_bash = Path(git_path).parent.parent / "bin" / "bash.exe"
            if git_bash.is_file():
                return str(git_bash.resolve())
    bash_path = shutil.which("bash")
    if bash_path is None:
        pytest.skip("Bash is required", allow_module_level=True)
    return str(Path(bash_path).resolve())


TEST_BASH = _test_bash_executable()


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


def test_release_compose_requires_an_explicit_independent_solver_image():
    compose_text = (DEPLOY_ROOT / "docker-compose.solver.release.yml").read_text(encoding="utf-8")

    assert "image: ${ORTOOLS_SOLVER_IMAGE:?set ORTOOLS_SOLVER_IMAGE}" in compose_text
    assert "SOLVER_SERVICE_TOKEN_SECRET" in compose_text
    solver_build = Path(__file__).resolve().parents[3] / "solvers" / "ortools" / "build_image.sh"
    assert solver_build.is_file()
    build_text = solver_build.read_text(encoding="utf-8")
    assert "solvers/ortools/Dockerfile" in build_text
    assert "solver-ortools-service" in build_text


def _compose_service_block(compose_text: str, service_name: str) -> str:
    """Return one service block without requiring PyYAML in deployment tests."""

    marker = f"  {service_name}:\n"
    start = compose_text.index(marker)
    remainder = compose_text[start + len(marker) :]
    next_service = re.search(r"^  [a-zA-Z0-9][a-zA-Z0-9_-]*:\n", remainder, re.MULTILINE)
    end = start + len(marker) + (next_service.start() if next_service else len(remainder))
    return compose_text[start:end]


def test_release_compose_exposes_one_solver_service_only_on_the_private_network():
    compose_text = (DEPLOY_ROOT / "docker-compose.solver.release.yml").read_text(encoding="utf-8")
    solver = _compose_service_block(compose_text, "ortools-solver")

    assert 'profiles: ["solver-ortools"]' in solver
    assert 'expose:\n      - "8080"' in solver
    assert "ports:" not in solver
    assert "read_only: true" in solver
    assert "/tmp:rw,noexec,nosuid,size=16m" in solver
    service_names = set(
        re.findall(
            r"^  ([a-zA-Z0-9][a-zA-Z0-9_-]*):$",
            _compose_service_block(compose_text, "ortools-solver") + _compose_service_block(compose_text, "backend"),
            re.MULTILINE,
        )
    )
    assert service_names == {"ortools-solver", "backend"}
    assert "TLS_CERTIFICATE" not in compose_text
    assert "TLS_KEY" not in compose_text


def test_release_compose_explicitly_allows_private_solver_http():
    compose_text = (DEPLOY_ROOT / "docker-compose.solver.release.yml").read_text(encoding="utf-8")
    backend = _compose_service_block(compose_text, "backend")

    assert "SOLVER_SERVICE_ALLOWED_HOSTS=ortools-solver" in backend
    assert "SOLVER_SERVICE_ALLOWED_PORTS=8080" in backend
    assert "SOLVER_SERVICE_ALLOW_HTTP=true" in backend
    assert "SOLVER_SERVICE_ALLOW_LOCALHOST=false" in backend
    assert "SOLVER_CONNECTOR_" not in backend


def test_solver_release_overlay_has_no_nginx_or_tls_sidecar():
    overlay = (DEPLOY_ROOT / "docker-compose.solver.release.yml").read_text(encoding="utf-8")

    assert "nginx" not in overlay.lower()
    assert "sidecar" not in overlay.lower()
    assert "TLS_CERTIFICATE" not in overlay
    assert "TLS_KEY" not in overlay


def test_base_release_compose_is_usable_without_solver_secrets():
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")
    assert "ortools-solver" not in compose_text
    assert "SOLVER_CONNECTOR_SERVICE_TOKEN_FILE" not in compose_text
    assert "ORTOOLS_CONNECTOR_SERVICE_TOKEN_SECRET" not in compose_text
    assert "SOLVER_CONNECTOR_TLS_CA_FILE" not in compose_text


def test_solver_release_overlay_has_no_certificate_secrets_and_is_explicitly_optional():
    overlay = (DEPLOY_ROOT / "docker-compose.solver.release.yml").read_text(encoding="utf-8")
    assert "SOLVER_CONNECTOR_TLS_CA_FILE" not in overlay
    assert "solver_connector_tls_ca_certificate" not in overlay
    assert "ORTOOLS_CONNECTOR_TLS" not in overlay
    assert "--profile solver-ortools" in overlay
    assert "SOLVER_CONNECTOR_" not in overlay
    assert "ORTOOLS_CONNECTOR_" not in overlay


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


def test_bind_mount_storage_remains_the_default_for_source_and_release_compose():
    source_compose = SOURCE_COMPOSE_PATH.read_text(encoding="utf-8")
    release_compose = COMPOSE_PATH.read_text(encoding="utf-8")

    for compose_text in (source_compose, release_compose):
        assert "./.onto-runtime:/app/.onto-runtime" in compose_text
        assert "./.onto-auth:/data" in compose_text
        assert "consumer-runtime-data:/app/.onto-runtime" not in compose_text
        assert "consumer-auth-data:/data" not in compose_text


def test_storage_volume_overlay_declares_stable_names_and_mount_permissions():
    overlay = STORAGE_VOLUME_COMPOSE_PATH.read_text(encoding="utf-8")

    backend = _compose_service_block(overlay, "backend")
    mcp = _compose_service_block(overlay, "mcp")
    jiuwenbox = _compose_service_block(overlay, "jiuwenbox")
    auth_server = _compose_service_block(overlay, "auth-server")

    assert "consumer-runtime-data:/app/.onto-runtime" in backend
    assert "consumer-runtime-data:/app/.onto-runtime" in mcp
    assert "consumer-runtime-data:/app/.onto-runtime:ro" in jiuwenbox
    assert "consumer-auth-data:/data" in auth_server
    assert "consumer-runtime-data:/app/.onto-runtime:ro" not in backend
    assert "consumer-runtime-data:/app/.onto-runtime:ro" not in mcp
    assert "consumer-auth-data:/data:ro" not in auth_server
    assert "consumer-runtime-data:\n    name: consumer-runtime-data" in overlay
    assert "consumer-auth-data:\n    name: consumer-auth-data" in overlay


def _mount_for_target(compose_config, service_name, target):
    mounts = [
        mount for mount in compose_config["services"][service_name].get("volumes", []) if mount.get("target") == target
    ]
    assert len(mounts) == 1
    return mounts[0]


def test_storage_volume_overlay_renders_real_compose_mount_contracts():
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is not available")
    compose_version = subprocess.run(
        [docker, "compose", "version"],
        text=True,
        capture_output=True,
        check=False,
    )
    if compose_version.returncode != 0:
        pytest.skip("Docker Compose plugin is not available")

    consumer_root = DEPLOY_ROOT.parent
    for base_compose in (SOURCE_COMPOSE_PATH, COMPOSE_PATH):
        bind_result = subprocess.run(
            [
                docker,
                "compose",
                "-f",
                str(base_compose),
                "--project-directory",
                str(consumer_root),
                "config",
                "--no-env-resolution",
                "--no-interpolate",
                "--format",
                "json",
            ],
            cwd=consumer_root,
            text=True,
            capture_output=True,
            check=False,
        )
        assert bind_result.returncode == 0, bind_result.stderr
        bind_config = json.loads(bind_result.stdout)
        assert _mount_for_target(bind_config, "backend", "/app/.onto-runtime")["type"] == "bind"

        volume_result = subprocess.run(
            [
                docker,
                "compose",
                "-f",
                str(base_compose),
                "-f",
                str(STORAGE_VOLUME_COMPOSE_PATH),
                "--project-directory",
                str(consumer_root),
                "config",
                "--no-env-resolution",
                "--no-interpolate",
                "--format",
                "json",
            ],
            cwd=consumer_root,
            text=True,
            capture_output=True,
            check=False,
        )
        assert volume_result.returncode == 0, volume_result.stderr
        volume_config = json.loads(volume_result.stdout)
        backend = _mount_for_target(volume_config, "backend", "/app/.onto-runtime")
        mcp = _mount_for_target(volume_config, "mcp", "/app/.onto-runtime")
        jiuwenbox = _mount_for_target(volume_config, "jiuwenbox", "/app/.onto-runtime")
        auth = _mount_for_target(volume_config, "auth-server", "/data")

        assert (backend["type"], backend["source"], backend.get("read_only", False)) == (
            "volume",
            "consumer-runtime-data",
            False,
        )
        assert (mcp["type"], mcp["source"], mcp.get("read_only", False)) == (
            "volume",
            "consumer-runtime-data",
            False,
        )
        assert (
            jiuwenbox["type"],
            jiuwenbox["source"],
            jiuwenbox.get("read_only", False),
        ) == ("volume", "consumer-runtime-data", True)
        assert (auth["type"], auth["source"], auth.get("read_only", False)) == (
            "volume",
            "consumer-auth-data",
            False,
        )
        assert volume_config["volumes"]["consumer-runtime-data"]["name"] == "consumer-runtime-data"
        assert volume_config["volumes"]["consumer-auth-data"]["name"] == "consumer-auth-data"


def _prepare_storage_mode_bundle(tmp_path):
    bundle_root = tmp_path / "consumer"
    deploy_root = bundle_root / "deploy"
    backend_root = bundle_root / "backend"
    fake_bin = tmp_path / "bin"
    deploy_root.mkdir(parents=True)
    backend_root.mkdir()
    fake_bin.mkdir()
    shutil.copyfile(DEPLOY_ROOT / "common.sh", deploy_root / "common.sh")
    shutil.copyfile(COMPOSE_PATH, deploy_root / "docker-compose.release.yml")
    if STORAGE_VOLUME_COMPOSE_PATH.exists():
        shutil.copyfile(
            STORAGE_VOLUME_COMPOSE_PATH,
            deploy_root / "docker-compose.storage-volume.yml",
        )
    (backend_root / ".env").write_text("USER_MAX_COUNT=10\n", encoding="utf-8")

    docker_args = tmp_path / "docker-args.txt"
    docker_calls = tmp_path / "docker-calls.txt"
    event_log = tmp_path / "deployment-events.txt"
    state_io_helper = fake_bin / "state-io"
    state_io_helper.write_text(
        """#!/bin/sh
operation="$1"
shift
printf '%s\n' "$operation" >> "$DEPLOY_TEST_EVENT_LOG"
if [ "${STATE_IO_FAIL_OPERATION:-}" = "$operation" ]; then
    exit 73
fi
case "$operation" in
    fsync-file) test -f "$1" ;;
    replace) mv "$1" "$2" ;;
    fsync-dir) test -d "$1" ;;
    *) exit 64 ;;
esac
""",
        encoding="utf-8",
    )
    state_io_helper.chmod(0o755)
    fake_flock = fake_bin / "flock"
    fake_flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_flock.chmod(0o755)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/sh
printf 'called\\n' >> "$DOCKER_CALLS_FILE"
printf 'docker\\n' >> "$DEPLOY_TEST_EVENT_LOG"
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then
    exit 0
fi
printf '%s\\n' "$@" > "$DOCKER_ARGS_FILE"
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    env = os.environ.copy()
    env.pop("STORAGE_MODE", None)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["DOCKER_ARGS_FILE"] = str(docker_args)
    env["DOCKER_CALLS_FILE"] = str(docker_calls)
    env["DEPLOY_TEST_EVENT_LOG"] = str(event_log)
    env["DEPLOY_STATE_IO_HELPER"] = state_io_helper.as_posix()
    return bundle_root, deploy_root, env, docker_args, docker_calls


def _run_storage_mode_compose(tmp_path, storage_mode=None, *, load_state=False):
    bundle_root, deploy_root, env, docker_args, docker_calls = _prepare_storage_mode_bundle(tmp_path)
    if storage_mode is not None:
        env["STORAGE_MODE"] = storage_mode
    load_command = "load_state; " if load_state else ""
    result = subprocess.run(
        [
            TEST_BASH,
            "-c",
            (
                f'source "{(deploy_root / "common.sh").as_posix()}"; '
                f"{load_command}compose_up backend frontend auth sandbox"
            ),
        ],
        cwd=bundle_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, deploy_root, docker_args, docker_calls, env


def _compose_file_names(args):
    return [args[index + 1].replace("\\", "/").rsplit("/", 1)[-1] for index, value in enumerate(args) if value == "-f"]


def test_release_compose_uses_one_base_file_in_default_bind_mode(tmp_path):
    result, deploy_root, docker_args, _, _ = _run_storage_mode_compose(tmp_path)

    assert result.returncode == 0, result.stderr
    args = docker_args.read_text(encoding="utf-8").splitlines()
    assert _compose_file_names(args) == ["docker-compose.release.yml"]


def test_release_compose_adds_volume_overlay_only_for_explicit_volume_mode(tmp_path):
    result, deploy_root, docker_args, _, _ = _run_storage_mode_compose(tmp_path, "volume")

    assert result.returncode == 0, result.stderr
    args = docker_args.read_text(encoding="utf-8").splitlines()
    assert _compose_file_names(args) == [
        "docker-compose.release.yml",
        "docker-compose.storage-volume.yml",
    ]


def test_invalid_storage_mode_fails_before_any_docker_invocation(tmp_path):
    result, _, _, docker_calls, _ = _run_storage_mode_compose(tmp_path, "automatic")

    assert result.returncode != 0
    assert "STORAGE_MODE must be 'bind' or 'volume'" in result.stderr
    assert not docker_calls.exists()


def test_invalid_persisted_storage_mode_fails_before_any_docker_invocation(tmp_path):
    bundle_root, deploy_root, env, _, docker_calls = _prepare_storage_mode_bundle(tmp_path)
    (deploy_root / "state").mkdir()
    (deploy_root / "state" / "current.env").write_text(
        "CURRENT_VERSION=v1\nCURRENT_STORAGE_MODE=invalid\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            TEST_BASH,
            "-c",
            f'source "{(deploy_root / "common.sh").as_posix()}"; docker info',
        ],
        cwd=bundle_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "STORAGE_MODE must be 'bind' or 'volume'" in result.stderr
    assert not docker_calls.exists()


def test_deployment_lock_has_one_production_flock_backend():
    common_text = (DEPLOY_ROOT / "common.sh").read_text(encoding="utf-8")

    assert "flock -n" in common_text
    assert "acquire_directory_deployment_lock" not in common_text
    assert "release_directory_deployment_lock" not in common_text
    assert "DEPLOY_LOCK_BACKEND" not in common_text
    assert ".consumer-deploy.lock.d" not in common_text


def test_missing_flock_fails_before_docker_with_clear_error(tmp_path):
    bundle_root, deploy_root, env, _, docker_calls = _prepare_storage_mode_bundle(tmp_path)
    bash_executable = shutil.which(TEST_BASH) or TEST_BASH
    fake_bin = tmp_path / "bin"
    (fake_bin / "flock").unlink()
    if os.name != "nt":
        fake_dirname = fake_bin / "dirname"
        fake_dirname.write_text(
            "#!/bin/sh\n"
            'path="$1"\n'
            'case "$path" in\n'
            "  */*) printf '%s\\n' \"${path%/*}\" ;;\n"
            "  *) printf '.\\n' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_dirname.chmod(0o755)
        env["PATH"] = str(fake_bin)

    result = subprocess.run(
        [
            bash_executable,
            "-c",
            f'source "{(deploy_root / "common.sh").as_posix()}"; docker info',
        ],
        cwd=bundle_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "util-linux flock is required" in result.stderr
    assert not docker_calls.exists()
    assert not (deploy_root / "state").exists()
    assert not (deploy_root / "backups").exists()


@pytest.mark.parametrize("script_name", ["deploy.sh", "restart.sh", "rollback.sh"])
@pytest.mark.parametrize("failure_mode", ["missing", "contended"])
def test_lifecycle_lock_failure_has_no_prelock_worktree_writes(
    tmp_path,
    script_name,
    failure_mode,
):
    bundle_root = tmp_path / f"{script_name}-{failure_mode}"
    deploy_root = bundle_root / "deploy"
    fake_bin = bundle_root / "fake-bin"
    deploy_root.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copyfile(DEPLOY_ROOT / "common.sh", deploy_root / "common.sh")
    shutil.copyfile(DEPLOY_ROOT / script_name, deploy_root / script_name)
    marker = deploy_root / "must-remain-non-executable.sh"
    marker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    marker.chmod(0o644)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    if failure_mode == "contended":
        fake_flock = fake_bin / "flock"
        fake_flock.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_flock.chmod(0o755)
        command_prefix = ""
        expected_error = "deployment transaction is already running"
    else:
        command_prefix = (
            'command() { if [ "$1" = -v ] && [ "${2:-}" = flock ]; then return 1; fi; builtin command "$@"; }; '
        )
        expected_error = "util-linux flock is required"

    result = subprocess.run(
        [
            TEST_BASH,
            "-c",
            command_prefix + f'source "{(deploy_root / script_name).as_posix()}"',
        ],
        cwd=bundle_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert marker.stat().st_mode & 0o111 == 0
    assert not (deploy_root / "state").exists()
    assert not (deploy_root / "backups").exists()


@pytest.mark.parametrize("script_name", ["deploy.sh", "restart.sh", "rollback.sh"])
def test_lifecycle_chmod_runs_only_after_common_lock_acquisition(script_name):
    script_text = (DEPLOY_ROOT / script_name).read_text(encoding="utf-8")

    assert script_text.index('source "${SCRIPT_DIR}/common.sh"') < script_text.index('chmod +x "${SCRIPT_DIR}/"*.sh')


def _linux_bash_and_deploy_root():
    if os.name != "nt":
        probe = subprocess.run(
            [TEST_BASH, "-lc", "command -v flock && command -v python3"],
            text=True,
            capture_output=True,
            check=False,
        )
        if probe.returncode != 0:
            pytest.skip("Linux flock and Python 3 are required")
        return [TEST_BASH], str(DEPLOY_ROOT)

    wsl = shutil.which("wsl.exe")
    if wsl is None:
        pytest.skip("WSL is not available for real Linux flock validation")
    probe = subprocess.run(
        [wsl, "-e", "bash", "-lc", "command -v flock && command -v python3"],
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("WSL flock and Python 3 are required")
    converted = subprocess.run(
        [wsl, "-e", "wslpath", "-a", str(DEPLOY_ROOT)],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    return [wsl, "-e", "bash"], converted


def test_real_linux_inherited_lock_rejects_forged_descriptors_before_work():
    bash_command, source_deploy_root = _linux_bash_and_deploy_root()
    script = (
        '\nset -euo pipefail\nSOURCE_DEPLOY_ROOT=__SOURCE_DEPLOY_ROOT__\nWORK_ROOT="$(mktemp -d)"\n'
        'cleanup() {\n    case "${WORK_ROOT}" in\n        /tmp/tmp.*) rm -rf -- "${WORK_ROOT}" ;;\n    '
        'esac\n}\ntrap cleanup EXIT\n\nwait_for_file() {\n    local path="$1"\n    local attempt=0\n   '
        ' while [ ! -f "${path}" ] && [ "${attempt}" -lt 200 ]; do\n        sleep 0.05\n        '
        'attempt=$((attempt + 1))\n    done\n    [ -f "${path}" ]\n}\n\nprepare_common_root() {\n    '
        'local root="$1"\n    mkdir -p "${root}/deploy" "${root}/backend" "${root}/fake-bin"\n    cp '
        '"${SOURCE_DEPLOY_ROOT}/common.sh" "${root}/deploy/common.sh"\n    cp "${SOURCE_DEPLOY_ROOT}/'
        'docker-compose.release.yml" "${root}/deploy/docker-compose.release.yml"\n    cp '
        '"${SOURCE_DEPLOY_ROOT}/docker-compose.storage-volume.yml" "${root}/deploy/'
        "docker-compose.storage-volume.yml\"\n    printf 'USER_MAX_COUNT=10\\n' > \"${root}/backend/"
        '.env"\n    cat > "${root}/fake-bin/docker" <<\'SH\'\n#!/bin/sh\n: > "${DOCKER_MARKER}"\nexit '
        '0\nSH\n    chmod +x "${root}/fake-bin/docker"\n}\n\ninvoke_common_with_forged_lock() {\n    '
        'local root="$1"\n    local mode="$2"\n    (\n        cd "${root}"\n        export '
        'PATH="${root}/fake-bin:${PATH}"\n        export DOCKER_MARKER="${root}/docker.called"\n       '
        " export DEPLOY_TRANSACTION_LOCK_BACKEND=flock\n        export "
        'DEPLOY_TRANSACTION_LOCK_TOKEN=synthetic-forged-token\n        case "${mode}" in\n            '
        "stdout)\n                export DEPLOY_TRANSACTION_LOCK_FD=1\n                ;;\n            "
        'other-inode)\n                exec 9> "${root}/other.lock"\n                export '
        "DEPLOY_TRANSACTION_LOCK_FD=9\n                ;;\n            same-inode)\n                "
        'exec 9> "${root}/.consumer-deploy.lock"\n                export DEPLOY_TRANSACTION_LOCK_FD=9\n'
        "                ;;\n        esac\n        source deploy/common.sh\n        docker info\n    ) "
        '>"${root}/attack.out" 2>"${root}/attack.err"\n}\n\nassert_common_rejected() {\n    local '
        'root="$1"\n    local mode="$2"\n    if invoke_common_with_forged_lock "${root}" "${mode}"; '
        "then\n        printf 'forged common lock unexpectedly accepted: %s\\n' \"${mode}\" >&2\n      "
        '  return 1\n    fi\n    [ ! -f "${root}/docker.called" ]\n    [ ! -d "${root}/deploy/state" '
        ']\n    [ ! -d "${root}/deploy/backups" ]\n}\n\nfor mode in stdout other-inode; do\n    '
        'root="${WORK_ROOT}/common-${mode}"\n    prepare_common_root "${root}"\n    '
        'assert_common_rejected "${root}" "${mode}"\ndone\n\nCOMMON_HELD_ROOT="${WORK_ROOT}/'
        'common-same-inode"\nprepare_common_root "${COMMON_HELD_ROOT}"\n(\n    exec 8> '
        '"${COMMON_HELD_ROOT}/.consumer-deploy.lock"\n    flock -n 8\n    : > "${COMMON_HELD_ROOT}/'
        'holder.ready"\n    while [ ! -f "${COMMON_HELD_ROOT}/holder.release" ]; do sleep 0.05; done\n'
        ') &\nCOMMON_HOLDER_PID=$!\nwait_for_file "${COMMON_HELD_ROOT}/holder.ready"\n'
        'assert_common_rejected "${COMMON_HELD_ROOT}" same-inode\n: > "${COMMON_HELD_ROOT}/'
        'holder.release"\nwait "${COMMON_HOLDER_PID}"\n\nCOMMON_VALID_ROOT="${WORK_ROOT}/'
        'common-valid"\nprepare_common_root "${COMMON_VALID_ROOT}"\n(\n    cd "${COMMON_VALID_ROOT}"\n '
        "   source deploy/common.sh\n    bash -c 'source deploy/common.sh; test "
        '"${DEPLOY_TRANSACTION_LOCK_HELD}" = true\'\n)\n\nprepare_upgrade_root() {\n    local '
        'root="$1"\n    local release_root="${root}/release"\n    local install_root="${root}/'
        'install"\n    mkdir -p "${release_root}/deploy" "${install_root}/deploy/state" '
        '"${install_root}/backend"\n    cp "${SOURCE_DEPLOY_ROOT}/upgrade.sh" "${release_root}/deploy/'
        'upgrade.sh"\n    cp "${SOURCE_DEPLOY_ROOT}/common.sh" "${release_root}/deploy/common.sh"\n    '
        'cp "${SOURCE_DEPLOY_ROOT}/docker-compose.release.yml" "${release_root}/deploy/'
        'docker-compose.release.yml"\n    cp "${SOURCE_DEPLOY_ROOT}/docker-compose.storage-volume.yml" '
        "\"${release_root}/deploy/docker-compose.storage-volume.yml\"\n    printf 'v2\\n' > "
        '"${release_root}/VERSION"\n    printf \'v1\\n\' > "${install_root}/VERSION"\n    printf '
        "'CURRENT_VERSION=v1\\nCURRENT_STORAGE_MODE=bind\\n' > \"${install_root}/deploy/state/"
        'current.env"\n    printf \'USER_MAX_COUNT=10\\n\' > "${install_root}/backend/.env"\n    cat > '
        '"${release_root}/deploy/deploy.sh" <<\'SH\'\n#!/bin/bash\nset -euo pipefail\nSCRIPT_DIR="$(cd '
        '"$(dirname "${BASH_SOURCE[0]}")" && pwd)"\nsource "${SCRIPT_DIR}/common.sh"\n: > '
        '"${UPGRADE_SUCCESS_MARKER}"\nSH\n    chmod +x "${release_root}/deploy/"*.sh\n}\n\n'
        'invoke_upgrade_with_lock() {\n    local root="$1"\n    local mode="$2"\n    local '
        'release_root="${root}/release"\n    local install_root="${root}/install"\n    (\n        '
        'export UPGRADE_SUCCESS_MARKER="${root}/upgrade.completed"\n        export '
        "DEPLOY_TRANSACTION_LOCK_BACKEND=flock\n        export "
        "DEPLOY_TRANSACTION_LOCK_TOKEN=synthetic-forged-token\n        export "
        'DEPLOY_TRANSACTION_LOCK_HELD=true\n        case "${mode}" in\n            stdout)\n           '
        "     export DEPLOY_TRANSACTION_LOCK_FD=1\n                ;;\n            other-inode)\n      "
        '          exec 9> "${root}/other.lock"\n                export DEPLOY_TRANSACTION_LOCK_FD=9\n '
        '               ;;\n            same-inode)\n                exec 9> "${install_root}/'
        '.consumer-deploy.lock"\n                export DEPLOY_TRANSACTION_LOCK_FD=9\n                '
        ';;\n            valid)\n                exec 8> "${install_root}/.consumer-deploy.lock"\n     '
        "           flock -n 8\n                export DEPLOY_TRANSACTION_LOCK_FD=8\n                "
        ';;\n        esac\n        "${release_root}/deploy/upgrade.sh" "${install_root}"\n    ) '
        '>"${root}/upgrade.out" 2>"${root}/upgrade.err"\n}\n\nassert_upgrade_rejected() {\n    local '
        'root="$1"\n    local mode="$2"\n    if invoke_upgrade_with_lock "${root}" "${mode}"; then\n   '
        "     printf 'forged upgrade lock unexpectedly accepted: %s\\n' \"${mode}\" >&2\n        "
        'return 1\n    fi\n    [ ! -f "${root}/upgrade.completed" ]\n    [ ! -d "${root}/install/'
        'deploy/backups" ]\n    [ "$(cat "${root}/install/VERSION")" = v1 ]\n}\n\nfor mode in stdout '
        'other-inode; do\n    root="${WORK_ROOT}/upgrade-${mode}"\n    prepare_upgrade_root "${root}"\n'
        '    assert_upgrade_rejected "${root}" "${mode}"\ndone\n\nUPGRADE_HELD_ROOT="${WORK_ROOT}/'
        'upgrade-same-inode"\nprepare_upgrade_root "${UPGRADE_HELD_ROOT}"\n(\n    exec 7> '
        '"${UPGRADE_HELD_ROOT}/install/.consumer-deploy.lock"\n    flock -n 7\n    : > '
        '"${UPGRADE_HELD_ROOT}/holder.ready"\n    while [ ! -f "${UPGRADE_HELD_ROOT}/holder.release" '
        ']; do sleep 0.05; done\n) &\nUPGRADE_HOLDER_PID=$!\nwait_for_file "${UPGRADE_HELD_ROOT}/'
        'holder.ready"\nassert_upgrade_rejected "${UPGRADE_HELD_ROOT}" same-inode\n: > '
        '"${UPGRADE_HELD_ROOT}/holder.release"\nwait "${UPGRADE_HOLDER_PID}"\n\n'
        'UPGRADE_VALID_ROOT="${WORK_ROOT}/upgrade-valid"\nprepare_upgrade_root '
        '"${UPGRADE_VALID_ROOT}"\ninvoke_upgrade_with_lock "${UPGRADE_VALID_ROOT}" valid\n[ -f '
        '"${UPGRADE_VALID_ROOT}/upgrade.completed" ]\n\nprintf \'real Linux inherited lock validation: '
        "ok\\n'\n"
    )
    script = script.replace("__SOURCE_DEPLOY_ROOT__", shlex.quote(source_deploy_root))
    result = subprocess.run(
        bash_command,
        input=script.encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=60,
    )
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")

    assert result.returncode == 0, stdout + stderr
    assert "real Linux inherited lock validation: ok" in stdout


def test_real_linux_flock_serializes_lifecycle_and_upgrade_overlay():
    bash_command, source_deploy_root = _linux_bash_and_deploy_root()
    script = r"""
set -euo pipefail
SOURCE_DEPLOY_ROOT=__SOURCE_DEPLOY_ROOT__
WORK_ROOT="$(mktemp -d)"
cleanup() {
    case "${WORK_ROOT}" in
        /tmp/tmp.*) rm -rf -- "${WORK_ROOT}" ;;
    esac
}
trap cleanup EXIT

wait_for_file() {
    local path="$1"
    local attempt=0
    while [ ! -f "${path}" ] && [ "${attempt}" -lt 200 ]; do
        sleep 0.05
        attempt=$((attempt + 1))
    done
    [ -f "${path}" ]
}

prepare_bundle() {
    local root="$1"
    mkdir -p "${root}/deploy" "${root}/backend" "${root}/fake-bin"
    cp "${SOURCE_DEPLOY_ROOT}/common.sh" "${root}/deploy/common.sh"
    cp "${SOURCE_DEPLOY_ROOT}/docker-compose.release.yml" "${root}/deploy/docker-compose.release.yml"
    cp "${SOURCE_DEPLOY_ROOT}/docker-compose.storage-volume.yml" "${root}/deploy/docker-compose.storage-volume.yml"
    printf 'USER_MAX_COUNT=10\n' > "${root}/backend/.env"
    cat > "${root}/fake-bin/docker" <<'SH'
#!/bin/sh
printf '%s:%s\n' "${STORAGE_MODE:-bind}" "$*" >> "${DOCKER_LOG}"
exit 0
SH
    chmod +x "${root}/fake-bin/docker"
}

LIFECYCLE_ROOT="${WORK_ROOT}/lifecycle"
prepare_bundle "${LIFECYCLE_ROOT}"
export PATH="${LIFECYCLE_ROOT}/fake-bin:${PATH}"
export DOCKER_LOG="${LIFECYCLE_ROOT}/docker.log"
FIRST_READY="${LIFECYCLE_ROOT}/first.ready"
FIRST_RELEASE="${LIFECYCLE_ROOT}/first.release"

(
    cd "${LIFECYCLE_ROOT}"
    export STORAGE_MODE=volume
    source deploy/common.sh
    bash -c 'source deploy/common.sh; test "${DEPLOY_TRANSACTION_LOCK_HELD}" = true'
    : > "${FIRST_READY}"
    while [ ! -f "${FIRST_RELEASE}" ]; do sleep 0.05; done
    compose_up backend frontend auth sandbox
    write_state v2 '' '' backend frontend '' '' auth '' sandbox ''
) >"${LIFECYCLE_ROOT}/first.out" 2>"${LIFECYCLE_ROOT}/first.err" &
FIRST_PID=$!
wait_for_file "${FIRST_READY}"
LOCK_INODE_BEFORE="$(stat -c %i "${LIFECYCLE_ROOT}/.consumer-deploy.lock")"

# Replacing deploy files must not replace the stable bundle-root lock inode.
cp "${SOURCE_DEPLOY_ROOT}/common.sh" "${LIFECYCLE_ROOT}/deploy/common.sh"
set +e
(
    cd "${LIFECYCLE_ROOT}"
    export STORAGE_MODE=bind
    source deploy/common.sh
    docker info
) >"${LIFECYCLE_ROOT}/second.out" 2>"${LIFECYCLE_ROOT}/second.err"
SECOND_STATUS=$?
set -e
[ "${SECOND_STATUS}" -ne 0 ]
grep -q 'deployment transaction is already running' "${LIFECYCLE_ROOT}/second.err"
[ ! -f "${DOCKER_LOG}" ]
[ "$(stat -c %i "${LIFECYCLE_ROOT}/.consumer-deploy.lock")" = "${LOCK_INODE_BEFORE}" ]

: > "${FIRST_RELEASE}"
wait "${FIRST_PID}"
grep -q '^volume:' "${DOCKER_LOG}"
! grep -q '^bind:' "${DOCKER_LOG}"

# Normal process exit releases the kernel lock while retaining the stable inode.
(
    cd "${LIFECYCLE_ROOT}"
    unset STORAGE_MODE
    source deploy/common.sh
    test "${DEPLOY_TRANSACTION_LOCK_HELD}" = true
)
[ "$(stat -c %i "${LIFECYCLE_ROOT}/.consumer-deploy.lock")" = "${LOCK_INODE_BEFORE}" ]

# SIGKILL leaves no stale ownership because flock is released by the kernel.
CRASH_ROOT="${WORK_ROOT}/crash"
prepare_bundle "${CRASH_ROOT}"
mkfifo "${CRASH_ROOT}/hold.fifo"
(
    cd "${CRASH_ROOT}"
    source deploy/common.sh
    : > "${CRASH_ROOT}/ready"
    read -r _ < "${CRASH_ROOT}/hold.fifo"
) &
CRASH_PID=$!
wait_for_file "${CRASH_ROOT}/ready"
kill -9 "${CRASH_PID}"
set +e
wait "${CRASH_PID}" 2>/dev/null
set -e
(
    cd "${CRASH_ROOT}"
    source deploy/common.sh
    test "${DEPLOY_TRANSACTION_LOCK_HELD}" = true
)

# Hold the same root lock through a completed upgrade overlay.
RELEASE_ROOT="${WORK_ROOT}/release"
INSTALL_ROOT="${WORK_ROOT}/install"
mkdir -p "${RELEASE_ROOT}/deploy" "${INSTALL_ROOT}/deploy/state" "${WORK_ROOT}/upgrade-bin"
cp "${SOURCE_DEPLOY_ROOT}/common.sh" "${RELEASE_ROOT}/deploy/common.sh"
cp "${SOURCE_DEPLOY_ROOT}/upgrade.sh" "${RELEASE_ROOT}/deploy/upgrade.sh"
cp "${SOURCE_DEPLOY_ROOT}/common.sh" "${INSTALL_ROOT}/deploy/common.sh"
printf 'v2\n' > "${RELEASE_ROOT}/VERSION"
printf 'v1\n' > "${INSTALL_ROOT}/VERSION"
printf 'CURRENT_VERSION=v1\nCURRENT_STORAGE_MODE=bind\n' > "${INSTALL_ROOT}/deploy/state/current.env"
printf 'release-lock-must-not-overlay\n' > "${RELEASE_ROOT}/.consumer-deploy.lock"
cat > "${RELEASE_ROOT}/deploy/deploy.sh" <<'SH'
#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"
test "${DEPLOY_TRANSACTION_LOCK_HELD}" = true
: > "${UPGRADE_CHILD_REENTRY}"
SH
chmod +x "${RELEASE_ROOT}/deploy/"*.sh

REAL_TAR="$(command -v tar)"
cat > "${WORK_ROOT}/upgrade-bin/tar" <<'SH'
#!/bin/sh
is_install_extract=false
previous=""
for argument in "$@"; do
    if [ "${previous}" = "-C" ] && [ "${argument}" = "${UPGRADE_INSTALL_ROOT}" ]; then
        is_install_extract=true
    fi
    previous="${argument}"
done
if [ "${is_install_extract}" = true ]; then
    "${REAL_TAR}" "$@"
    : > "${UPGRADE_OVERLAY_READY}"
    while [ ! -f "${UPGRADE_OVERLAY_RELEASE}" ]; do sleep 0.05; done
    exit 0
fi
exec "${REAL_TAR}" "$@"
SH
chmod +x "${WORK_ROOT}/upgrade-bin/tar"
export REAL_TAR UPGRADE_INSTALL_ROOT="${INSTALL_ROOT}"
export UPGRADE_OVERLAY_READY="${WORK_ROOT}/upgrade-overlay.ready"
export UPGRADE_OVERLAY_RELEASE="${WORK_ROOT}/upgrade-overlay.release"
export UPGRADE_CHILD_REENTRY="${WORK_ROOT}/upgrade-child-reentry"
export PATH="${WORK_ROOT}/upgrade-bin:${LIFECYCLE_ROOT}/fake-bin:${PATH}"
export DOCKER_LOG="${WORK_ROOT}/upgrade-docker.log"

"${RELEASE_ROOT}/deploy/upgrade.sh" "${INSTALL_ROOT}" \
    >"${WORK_ROOT}/upgrade.out" 2>"${WORK_ROOT}/upgrade.err" &
UPGRADE_PID=$!
wait_for_file "${UPGRADE_OVERLAY_READY}"
UPGRADE_LOCK_INODE="$(stat -c %i "${INSTALL_ROOT}/.consumer-deploy.lock")"
set +e
(
    cd "${INSTALL_ROOT}"
    source deploy/common.sh
    docker info
) >"${WORK_ROOT}/upgrade-competitor.out" 2>"${WORK_ROOT}/upgrade-competitor.err"
UPGRADE_COMPETITOR_STATUS=$?
set -e
[ "${UPGRADE_COMPETITOR_STATUS}" -ne 0 ]
grep -q 'deployment transaction is already running' "${WORK_ROOT}/upgrade-competitor.err"
[ ! -f "${DOCKER_LOG}" ]
[ "$(stat -c %i "${INSTALL_ROOT}/.consumer-deploy.lock")" = "${UPGRADE_LOCK_INODE}" ]
: > "${UPGRADE_OVERLAY_RELEASE}"
wait "${UPGRADE_PID}"
[ -f "${UPGRADE_CHILD_REENTRY}" ]
[ "$(stat -c %i "${INSTALL_ROOT}/.consumer-deploy.lock")" = "${UPGRADE_LOCK_INODE}" ]
(
    cd "${INSTALL_ROOT}"
    source deploy/common.sh
    test "${DEPLOY_TRANSACTION_LOCK_HELD}" = true
)

printf 'real Linux flock lifecycle and upgrade overlay: ok\n'
"""
    script = script.replace("__SOURCE_DEPLOY_ROOT__", shlex.quote(source_deploy_root))
    result = subprocess.run(
        bash_command,
        input=script.encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=60,
    )
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")

    assert result.returncode == 0, stdout + stderr
    assert "real Linux flock lifecycle and upgrade overlay: ok" in stdout


def test_pending_storage_mode_is_fsynced_and_directory_synced_before_docker(tmp_path):
    result, _, _, docker_calls, env = _run_storage_mode_compose(tmp_path, "volume")

    assert result.returncode == 0, result.stderr
    events = Path(env["DEPLOY_TEST_EVENT_LOG"]).read_text(encoding="utf-8").splitlines()
    assert events[:4] == ["fsync-file", "replace", "fsync-dir", "docker"]
    assert docker_calls.exists()


@pytest.mark.parametrize(
    ("failure_operation", "expected_events"),
    [
        ("fsync-file", ["fsync-file"]),
        ("replace", ["fsync-file", "replace"]),
        ("fsync-dir", ["fsync-file", "replace", "fsync-dir"]),
    ],
)
def test_storage_state_durability_failure_prevents_docker(tmp_path, failure_operation, expected_events):
    bundle_root, deploy_root, env, _, docker_calls = _prepare_storage_mode_bundle(tmp_path)
    env["STORAGE_MODE"] = "volume"
    env["STATE_IO_FAIL_OPERATION"] = failure_operation

    result = subprocess.run(
        [
            TEST_BASH,
            "-c",
            f'source "{(deploy_root / "common.sh").as_posix()}"; compose_up backend frontend auth sandbox',
        ],
        cwd=bundle_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "durably persist" in result.stderr
    assert not docker_calls.exists()
    assert Path(env["DEPLOY_TEST_EVENT_LOG"]).read_text(encoding="utf-8").splitlines() == expected_events
    assert list((deploy_root / "state").glob("*.tmp.*")) == []


def test_legacy_install_state_is_bind_and_cannot_silently_switch_to_volume(tmp_path):
    bundle_root, deploy_root, env, _, docker_calls = _prepare_storage_mode_bundle(tmp_path)
    (deploy_root / "state").mkdir()
    (deploy_root / "state" / "current.env").write_text(
        "CURRENT_VERSION=v1\n",
        encoding="utf-8",
    )
    env["STORAGE_MODE"] = "volume"

    result = subprocess.run(
        [
            TEST_BASH,
            "-c",
            f'source "{(deploy_root / "common.sh").as_posix()}"; load_state; compose_up backend frontend auth sandbox',
        ],
        cwd=bundle_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "does not match installed storage mode 'bind'" in result.stderr
    assert not docker_calls.exists()


def test_pending_volume_mode_survives_failed_deploy_retry_without_environment(
    tmp_path,
):
    bundle_root, deploy_root, env, docker_args, _ = _prepare_storage_mode_bundle(tmp_path)
    first_env = env.copy()
    first_env["STORAGE_MODE"] = "volume"

    first_start = subprocess.run(
        [
            TEST_BASH,
            "-c",
            f'source "{(deploy_root / "common.sh").as_posix()}"; compose_up backend frontend auth sandbox',
        ],
        cwd=bundle_root,
        env=first_env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert first_start.returncode == 0, first_start.stderr
    assert _compose_file_names(docker_args.read_text(encoding="utf-8").splitlines()) == [
        "docker-compose.release.yml",
        "docker-compose.storage-volume.yml",
    ]
    assert not (deploy_root / "state" / "current.env").exists()
    storage_mode_state = deploy_root / "state" / "storage-mode.env"
    assert storage_mode_state.read_text(encoding="utf-8") == "STORAGE_MODE_STATUS=pending\nSTORAGE_MODE=volume\n"

    docker_args.unlink()
    retry = subprocess.run(
        [
            TEST_BASH,
            "-c",
            f'source "{(deploy_root / "common.sh").as_posix()}"; compose_up backend frontend auth sandbox',
        ],
        cwd=bundle_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert retry.returncode == 0, retry.stderr
    assert _compose_file_names(docker_args.read_text(encoding="utf-8").splitlines()) == [
        "docker-compose.release.yml",
        "docker-compose.storage-volume.yml",
    ]


def test_storage_mode_is_persisted_and_prevents_silent_mode_switch(tmp_path):
    bundle_root, deploy_root, env, docker_args, docker_calls = _prepare_storage_mode_bundle(tmp_path)
    persist = subprocess.run(
        [
            TEST_BASH,
            "-c",
            (
                f'source "{(deploy_root / "common.sh").as_posix()}"; '
                "STORAGE_MODE=volume; export STORAGE_MODE; "
                "compose_up backend frontend auth sandbox; "
                "write_state v2 v1 '' backend frontend old-backend old-frontend "
                "auth old-auth sandbox old-sandbox"
            ),
        ],
        cwd=bundle_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert persist.returncode == 0, persist.stderr
    state_text = (deploy_root / "state" / "current.env").read_text(encoding="utf-8")
    assert "CURRENT_STORAGE_MODE=volume\n" in state_text
    assert (deploy_root / "state" / "storage-mode.env").read_text(
        encoding="utf-8"
    ) == "STORAGE_MODE_STATUS=installed\nSTORAGE_MODE=volume\n"

    use_persisted = subprocess.run(
        [
            TEST_BASH,
            "-c",
            f'source "{(deploy_root / "common.sh").as_posix()}"; load_state; compose_up backend frontend auth sandbox',
        ],
        cwd=bundle_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert use_persisted.returncode == 0, use_persisted.stderr
    args = docker_args.read_text(encoding="utf-8").splitlines()
    assert _compose_file_names(args)[-1] == "docker-compose.storage-volume.yml"

    docker_calls.unlink()
    switched_env = env.copy()
    switched_env["STORAGE_MODE"] = "bind"
    reject_switch = subprocess.run(
        [
            TEST_BASH,
            "-c",
            f'source "{(deploy_root / "common.sh").as_posix()}"; load_state; compose_up backend frontend auth sandbox',
        ],
        cwd=bundle_root,
        env=switched_env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert reject_switch.returncode != 0
    assert "does not match installed storage mode 'volume'" in reject_switch.stderr
    assert not docker_calls.exists()


def test_upgrade_acquires_stable_root_lock_before_state_read_and_overlay():
    upgrade_text = (DEPLOY_ROOT / "upgrade.sh").read_text(encoding="utf-8")

    lock_index = upgrade_text.index("\nacquire_upgrade_transaction_lock\n")
    state_read_index = upgrade_text.index('source "${STATE_FILE}"')
    overlay_index = upgrade_text.index('log "Overlaying new release files"')
    assert lock_index < state_read_index < overlay_index
    assert 'UPGRADE_LOCK_FILE="${INSTALL_DIR}/.consumer-deploy.lock"' in upgrade_text
    assert "flock -n" in upgrade_text
    assert "export DEPLOY_TRANSACTION_LOCK_FD" in upgrade_text
    assert "--exclude='./.consumer-deploy.lock'" in upgrade_text


def test_release_compose_shares_deterministic_artifact_staging_with_jiuwenbox():
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")
    mount = "onto-deterministic-artifacts:/run/onto-deterministic-artifacts"

    assert compose_text.count(mount) == 2
    assert "DETERMINISTIC_ARTIFACT_STAGING_ROOT=/run/onto-deterministic-artifacts" in compose_text
    assert "volumes:\n  onto-deterministic-artifacts:" in compose_text


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
    assert "http://127.0.0.1:${MCP_HOST_PORT}/health" in preflight_text
    assert "http://127.0.0.1:${MCP_SERVER_PORT:-7488}/health" not in preflight_text
    assert 'PROTECTED_ITEMS=".env backend/.env ' in upgrade_text


def test_deploy_refuses_fresh_install_when_existing_runtime_has_no_state(tmp_path):
    deploy_root = tmp_path / "deploy"
    bundle_root = tmp_path
    deploy_root.mkdir()
    (bundle_root / ".onto-runtime" / "data" / "chats").mkdir(parents=True)
    (bundle_root / ".onto-runtime" / "data" / "chats" / "existing.json").write_text("{}", encoding="utf-8")
    (bundle_root / ".onto-auth").mkdir()
    (bundle_root / "backend").mkdir()
    (bundle_root / "backend" / ".env").write_text("LLM_MAX_TOKENS=4096\n", encoding="utf-8")
    shutil.copyfile(DEPLOY_ROOT / "common.sh", deploy_root / "common.sh")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "flock").chmod(0o755)
    test_env = os.environ.copy()
    test_env["PATH"] = f"{fake_bin}{os.pathsep}{test_env.get('PATH', '')}"

    result = subprocess.run(
        [
            TEST_BASH,
            "-c",
            f'source "{(deploy_root / "common.sh").as_posix()}"; load_state; assert_no_existing_install_for_install',
        ],
        cwd=bundle_root,
        env=test_env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "existing installation" in result.stderr.lower()


def test_deploy_allows_empty_persistent_directories_for_first_install(tmp_path):
    deploy_root = tmp_path / "deploy"
    deploy_root.mkdir()
    (tmp_path / ".onto-runtime" / "config").mkdir(parents=True)
    (tmp_path / ".onto-auth").mkdir()
    shutil.copyfile(DEPLOY_ROOT / "common.sh", deploy_root / "common.sh")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "flock").chmod(0o755)

    result = subprocess.run(
        [
            TEST_BASH,
            "-c",
            f'source "{(deploy_root / "common.sh").as_posix()}"; load_state; assert_no_existing_install_for_install',
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_deploy_allows_backend_env_only_for_first_install(tmp_path):
    deploy_root = tmp_path / "deploy"
    deploy_root.mkdir()
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / ".env").write_text("LLM_MAX_TOKENS=4096\n", encoding="utf-8")
    shutil.copyfile(DEPLOY_ROOT / "common.sh", deploy_root / "common.sh")

    result = subprocess.run(
        [
            TEST_BASH,
            "-c",
            f'source "{(deploy_root / "common.sh").as_posix()}"; load_state; assert_no_existing_install_for_install',
        ],
        cwd=tmp_path,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_release_dispatcher_routes_staged_package_to_existing_install_upgrade():
    deploy_script = (REPOSITORY_ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")

    assert 'INSTALL_DIR="${ONTO_AGENT_INSTALL_DIR:-/opt/onto-agent}"' in deploy_script
    assert '"${BUNDLE_ROOT}" != "${INSTALL_DIR}"' in deploy_script
    assert '"${INSTALL_DIR}/deploy/state/current.env"' in deploy_script
    assert 'UPGRADE_ARGS=("${INSTALL_DIR}")' in deploy_script
    assert 'exec bash "${SCRIPT_DIR}/upgrade.sh" "${UPGRADE_ARGS[@]}"' in deploy_script
    assert deploy_script.index("Existing installation detected") < deploy_script.index(
        'ensure_release_modeling_service_token "${BUNDLE_ROOT}"'
    )


def test_staged_release_entrypoint_executes_upgrade_against_existing_install(tmp_path):
    staging = tmp_path / "release"
    target = tmp_path / "install"
    (staging / "deploy").mkdir(parents=True)
    (target / "deploy" / "state").mkdir(parents=True)
    (target / "backend").mkdir()
    (target / "VERSION").write_text("old\n", encoding="utf-8")
    (target / "backend" / ".env").write_text("LLM_MAX_TOKENS=4096\n", encoding="utf-8")
    (staging / "deploy" / "manifest.json").write_text("{}\n", encoding="utf-8")
    marker = tmp_path / "upgrade-called"
    (staging / "deploy" / "upgrade.sh").write_text(
        f"#!/bin/sh\nprintf '%s' \"$1\" > {shlex.quote(str(marker))}\n",
        encoding="utf-8",
    )
    (staging / "deploy" / "upgrade.sh").chmod(0o755)
    shutil.copyfile(REPOSITORY_ROOT / "deploy" / "deploy.sh", staging / "deploy" / "deploy.sh")

    result = subprocess.run(
        [TEST_BASH, str(staging / "deploy" / "deploy.sh"), "--consumer-only"],
        env={**os.environ, "ONTO_AGENT_INSTALL_DIR": str(target)},
        cwd=staging,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text(encoding="utf-8") == str(target)


def test_staged_consumer_package_entrypoint_routes_to_existing_install_upgrade(tmp_path):
    staging = tmp_path / "release"
    target = tmp_path / "install"
    (staging / "deploy").mkdir(parents=True)
    (target / "deploy" / "state").mkdir(parents=True)
    (target / "backend").mkdir()
    (target / "VERSION").write_text("old\n", encoding="utf-8")
    (target / "backend" / ".env").write_text("LLM_MAX_TOKENS=4096\n", encoding="utf-8")
    (staging / "deploy" / "manifest.json").write_text("{}\n", encoding="utf-8")
    marker = tmp_path / "consumer-upgrade-called"
    (staging / "deploy" / "upgrade.sh").write_text(
        f"#!/bin/sh\nprintf '%s' \"$1\" > {shlex.quote(str(marker))}\n",
        encoding="utf-8",
    )
    (staging / "deploy" / "upgrade.sh").chmod(0o755)
    shutil.copyfile(DEPLOY_ROOT / "deploy.sh", staging / "deploy" / "deploy.sh")

    result = subprocess.run(
        [TEST_BASH, str(staging / "deploy" / "deploy.sh")],
        env={**os.environ, "ONTO_AGENT_INSTALL_DIR": str(target)},
        cwd=staging,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text(encoding="utf-8") == str(target)


def test_upgrade_forwards_deploy_scope_to_installed_entrypoint(tmp_path):
    upgrade_text = (DEPLOY_ROOT / "upgrade.sh").read_text(encoding="utf-8")

    assert 'DEPLOY_ARGS+=("$1")' in upgrade_text
    assert '"${INSTALL_DIR}/deploy/deploy.sh" "${DEPLOY_ARGS[@]}"' in upgrade_text
    assert "--consumer-only|--modeling-only" in upgrade_text


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
        "BACKEND_PORT=5100\nFRONTEND_PORT=6600\nMCP_SERVER_PORT=9000\nAUTH_USER_MAX_COUNT=0\nSANDBOX_ENABLED=false\n",
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
            TEST_BASH,
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
    assert filtered_env.read_text(encoding="utf-8") == "BACKEND_PORT=5100\nFRONTEND_PORT=6600\nMCP_SERVER_PORT=9000\n"


def _run_mcp_secret_setup(tmp_path, env_content):
    bundle_root = tmp_path / "consumer"
    deploy_root = bundle_root / "deploy"
    backend_root = bundle_root / "backend"
    deploy_root.mkdir(parents=True)
    backend_root.mkdir()
    shutil.copyfile(DEPLOY_ROOT / "common.sh", deploy_root / "common.sh")
    env_file = backend_root / ".env"
    env_file.write_text(env_content, encoding="utf-8")

    result = subprocess.run(
        [
            TEST_BASH,
            "-c",
            'source "$1"; ensure_mcp_token_secret',
            TEST_BASH,
            str(deploy_root / "common.sh"),
        ],
        cwd=bundle_root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, env_file


def test_release_preflight_generates_missing_mcp_token_secret(tmp_path):
    result, env_file = _run_mcp_secret_setup(
        tmp_path,
        "OTHER_SETTING=enabled\n",
    )

    assert result.returncode == 0, result.stderr
    assert "generated and saved" in result.stdout
    secret_lines = env_file.read_text(encoding="utf-8").splitlines()
    assert any(re.fullmatch(r"MCP_TOKEN_SECRET=[0-9a-f]{64}", line) for line in secret_lines)
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"


def test_release_preflight_generates_empty_mcp_token_secret(tmp_path):
    result, env_file = _run_mcp_secret_setup(
        tmp_path,
        "MCP_TOKEN_SECRET=\nOTHER_SETTING=enabled\n",
    )

    assert result.returncode == 0, result.stderr
    assert "generated and saved" in result.stdout
    secret_lines = env_file.read_text(encoding="utf-8").splitlines()
    assert any(re.fullmatch(r"MCP_TOKEN_SECRET=[0-9a-f]{64}", line) for line in secret_lines)
    assert env_file.read_text(encoding="utf-8").count("MCP_TOKEN_SECRET=") == 1


def test_release_preflight_preserves_existing_mcp_token_secret(tmp_path):
    configured_value = "synthetic-existing-mcp-secret-value"
    result, env_file = _run_mcp_secret_setup(
        tmp_path,
        f"MCP_TOKEN_SECRET={configured_value}\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert env_file.read_text(encoding="utf-8") == f"MCP_TOKEN_SECRET={configured_value}\n"


def test_release_preflight_preserves_exported_mcp_token_secret(tmp_path):
    configured_value = "synthetic-exported-mcp-secret-value"
    result, env_file = _run_mcp_secret_setup(
        tmp_path,
        f"export MCP_TOKEN_SECRET={configured_value}\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert env_file.read_text(encoding="utf-8") == f"export MCP_TOKEN_SECRET={configured_value}\n"


def test_release_preflight_rejects_weak_existing_mcp_token_secret(tmp_path):
    result, env_file = _run_mcp_secret_setup(
        tmp_path,
        "MCP_TOKEN_SECRET=synthetic-weak-value\n",
    )

    assert result.returncode != 0
    assert "at least 32 characters" in result.stderr
    assert env_file.read_text(encoding="utf-8") == "MCP_TOKEN_SECRET=synthetic-weak-value\n"


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
        f"AUTH_SERVER_ADMIN_TOKEN={AUTH_ADMIN_TOKEN}\n"
        f"AUTH_BOOTSTRAP_ADMIN_PASSWORD={BOOTSTRAP_ADMIN_PASSWORD}\n"
        "MCP_SERVER_PORT=7488\n"
        f"MCP_TOKEN_SECRET={MCP_TOKEN_SECRET}\n"
        "USER_MAX_COUNT=10\n"
        "AUTH_USER_MAX_COUNT=10\n",
        encoding="utf-8",
    )
    (bundle_root / ".env").write_text(
        "BACKEND_PORT=5100\nFRONTEND_PORT=6600\nMCP_SERVER_PORT=9000\nAUTH_USER_MAX_COUNT=0\nSANDBOX_ENABLED=false\n",
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
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$CURL_ARGS_FILE"\n',
        encoding="utf-8",
    )
    fake_ss = fake_bin / "ss"
    fake_ss.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$SS_ARGS_FILE"\n',
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
        [TEST_BASH, str(deploy_root / "preflight.sh"), "--pre-migration"],
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
    assert filtered_env.read_text(encoding="utf-8") == "BACKEND_PORT=5100\nFRONTEND_PORT=6600\nMCP_SERVER_PORT=9000\n"
    report = json.loads((deploy_root / "state" / "preflight-config.json").read_text())
    assert report["limits"] == {
        "user_max_count": 0,
        "auth_user_max_count": 0,
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
        f"AUTH_SERVER_ADMIN_TOKEN={AUTH_ADMIN_TOKEN}\n"
        f"AUTH_BOOTSTRAP_ADMIN_PASSWORD={BOOTSTRAP_ADMIN_PASSWORD}\n"
        "MCP_SERVER_PORT=7488\n"
        f"MCP_TOKEN_SECRET={MCP_TOKEN_SECRET}\n"
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
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$CURL_ARGS_FILE"\n',
        encoding="utf-8",
    )
    fake_ss = fake_bin / "ss"
    fake_ss.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$SS_ARGS_FILE"\n',
        encoding="utf-8",
    )
    for executable in (fake_docker, fake_docker_compose, fake_curl, fake_ss):
        executable.chmod(0o755)

    curl_args_file = tmp_path / "curl-args.txt"
    ss_args_file = tmp_path / "ss-args.txt"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(
        [TEST_BASH, str(deploy_root / "preflight.sh"), "--pre-migration"],
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
    dry_run = next(
        i
        for i, line in enumerate(deploy_lines)
        if 'migrate.sh" "${CURRENT_VERSION:-}" "${TARGET_VERSION}" --dry-run' in line
    )
    stop_writes = next(i for i, line in enumerate(deploy_lines) if 'compose_stop "${CURRENT_BACKEND_IMAGE}"' in line)
    apply = next(i for i, line in enumerate(deploy_lines) if "--confirm-writes-stopped" in line)
    remove_stopped = next(i for i, line in enumerate(deploy_lines) if 'compose_down "${CURRENT_BACKEND_IMAGE}"' in line)
    strict_preflight = next(
        i for i, line in enumerate(deploy_lines) if "Validating canonical configuration after migration" in line
    )

    assert pre_migration_preflight < dry_run < stop_writes < apply < strict_preflight < remove_stopped
    assert "python -m migrations.runner" in migrate_text
    assert "ontology_data_sources_direct_v2" not in migrate_text
    assert '--env-file "${BUNDLE_ROOT}/backend/.env"' in migrate_text
    assert '"${RUNTIME_STORAGE_SOURCE}:/release/.onto-runtime:${DATA_ACCESS}"' in migrate_text
    assert '"${AUTH_STORAGE_SOURCE}:/release/.onto-auth:${DATA_ACCESS}"' in migrate_text


def test_backup_captures_legacy_backend_container_storage_before_container_removal():
    backup_text = (DEPLOY_ROOT / "backup.sh").read_text(encoding="utf-8")

    assert "--print-legacy-export-paths" in backup_text
    assert 'docker cp "${container_name}:${chat_source}/."' in backup_text
    assert 'docker cp "${container_name}:${execution_source}/."' in backup_text
    assert "legacy-backend-data.tar.gz" in backup_text
    assert 'find "${staging_root}/chats" -mindepth 1 -print -quit | grep -q .' in backup_text
    assert 'find "${staging_root}/executions" -mindepth 1 -print -quit | grep -q .' in backup_text


def test_data_source_compatibility_migration_uses_central_startup_runner_not_runtime_store():
    app_text = (BACKEND_ROOT / "app.py").read_text(encoding="utf-8")
    runner_text = (BACKEND_ROOT / "migrations" / "runner.py").read_text(encoding="utf-8")
    deploy_text = (DEPLOY_ROOT / "deploy.sh").read_text(encoding="utf-8")
    migrate_text = (DEPLOY_ROOT / "migrate.sh").read_text(encoding="utf-8")
    store_text = (BACKEND_ROOT / "agent" / "tools" / "business_data" / "ontology_sources_store.py").read_text(
        encoding="utf-8"
    )

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
    backup_index = rollback_text.index('backup.sh" --confirm-writes-stopped "rollback-${PREVIOUS_VERSION}"')
    down_index = rollback_text.index('compose_down "${FAILED_BACKEND_IMAGE}"')
    assert stop_index < backup_index < down_index
    assert 'restore_archive_if_present "${LAST_BACKUP_DIR}/onto-runtime.tar.gz"' not in rollback_text
    assert 'restore_archive_if_present "${LAST_BACKUP_DIR}/onto-auth.tar.gz"' not in rollback_text
    assert 'rm -rf "${BUNDLE_ROOT}/.onto-runtime"' not in rollback_text
    assert 'rm -rf "${BUNDLE_ROOT}/.onto-auth"' not in rollback_text
    assert not (BACKEND_ROOT / "integration" / "observability" / "migration.py").exists()


def test_system_config_migrations_are_ordered_only_in_shared_runner():
    app_text = (BACKEND_ROOT / "app.py").read_text(encoding="utf-8")
    runner_text = (BACKEND_ROOT / "migrations" / "runner.py").read_text(encoding="utf-8")
    forbidden_values = (
        "system-config-model-profiles-v1",
        "system-config-model-profile-output-budget-env-v1",
        "system-config-model-profile-output-budget-v1",
        "workspace-model-profile-output-budget-v1",
        "system-config-secret-encryption-v1",
        "system_config_model_profiles_v1",
        "system_config_model_profile_output_budget_env_v1",
        "system_config_model_profile_output_budget_v1",
        "workspace_model_profile_output_budget_v1",
        "system_config_secret_encryption_v1",
    )

    assert app_text.count("run_startup_migrations()") == 1
    assert (
        runner_text.index("system-config-model-profile-output-budget-env-v1")
        < runner_text.index("system-config-model-profiles-v1")
        < runner_text.index("system-config-model-profile-output-budget-v1")
        < runner_text.index("workspace-model-profile-output-budget-v1")
        < runner_text.index("system-config-secret-encryption-v1")
    )
    for script_name in ("deploy.sh", "migrate.sh", "preflight.sh", "backup.sh"):
        script_text = (DEPLOY_ROOT / script_name).read_text(encoding="utf-8")
        assert all(value not in script_text for value in forbidden_values)
