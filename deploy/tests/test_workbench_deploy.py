import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_ROOT = REPO_ROOT / "deploy"
MODELING_ROOT = REPO_ROOT / "modeling"


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/bash\nset -euo pipefail\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def test_root_deploy_dispatches_source_checkout_to_full_stack(tmp_path: Path) -> None:
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    shutil.copy2(DEPLOY_ROOT / "deploy.sh", deploy_dir / "deploy.sh")
    args_file = tmp_path / "args.txt"
    _write_executable(
        deploy_dir / "deploy-stack.sh",
        'printf "%s\\n" "$@" > "$DEPLOY_ARGS_FILE"',
    )

    result = subprocess.run(
        [str(deploy_dir / "deploy.sh")],
        env={**os.environ, "DEPLOY_ARGS_FILE": str(args_file)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert args_file.read_text(encoding="utf-8").splitlines() == ["up"]


def test_root_deploy_forwards_modeling_only_in_source_checkout(tmp_path: Path) -> None:
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    shutil.copy2(DEPLOY_ROOT / "deploy.sh", deploy_dir / "deploy.sh")
    args_file = tmp_path / "args.txt"
    _write_executable(
        deploy_dir / "deploy-stack.sh",
        'printf "%s\\n" "$@" > "$DEPLOY_ARGS_FILE"',
    )

    result = subprocess.run(
        [str(deploy_dir / "deploy.sh"), "--modeling-only"],
        env={**os.environ, "DEPLOY_ARGS_FILE": str(args_file)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert args_file.read_text(encoding="utf-8").splitlines() == [
        "up",
        "--modeling-only",
    ]


def test_root_release_dispatches_consumer_before_workbench(tmp_path: Path) -> None:
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    shutil.copy2(DEPLOY_ROOT / "deploy.sh", deploy_dir / "deploy.sh")
    calls_file = tmp_path / "calls.txt"
    _write_executable(
        deploy_dir / "deploy-consumer.sh",
        'printf "consumer\\n" >> "$DEPLOY_CALLS_FILE"',
    )
    _write_executable(
        deploy_dir / "modeling" / "deploy.sh",
        'printf "workbench\\n" >> "$DEPLOY_CALLS_FILE"',
    )

    result = subprocess.run(
        [str(deploy_dir / "deploy.sh")],
        env={**os.environ, "DEPLOY_CALLS_FILE": str(calls_file)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls_file.read_text(encoding="utf-8").splitlines() == [
        "consumer",
        "workbench",
    ]


def test_workbench_agent_is_built_and_described_in_release_manifests() -> None:
    modeling_build = (MODELING_ROOT / "build.sh").read_text(encoding="utf-8")
    full_build = (DEPLOY_ROOT / "build.sh").read_text(encoding="utf-8")

    assert '"${SCRIPT_DIR}/modeling-agent"' in modeling_build
    assert 'mw-modeling-agent:${VERSION}' in modeling_build
    assert 'mw-modeling-agent-${VERSION}.tar' in modeling_build
    for script in (modeling_build, full_build):
        assert '"modeling_agent_image": "mw-modeling-agent:${VERSION}"' in script
        assert (
            '"modeling_agent_image_tar": '
            '"images/mw-modeling-agent-${VERSION}.tar"'
        ) in script


def test_modeling_agent_pip_download_is_resilient_and_configurable() -> None:
    dockerfile = (MODELING_ROOT / "modeling-agent" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    compose = (DEPLOY_ROOT / "docker-compose.modeling.yml").read_text(
        encoding="utf-8"
    )
    build = (MODELING_ROOT / "build.sh").read_text(encoding="utf-8")
    root_deploy = (DEPLOY_ROOT / "deploy-stack.sh").read_text(encoding="utf-8")
    modeling_deploy = (MODELING_ROOT / "deploy" / "deploy.sh").read_text(
        encoding="utf-8"
    )

    assert dockerfile.count(
        "ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"
    ) == 2
    assert dockerfile.count("ARG PIP_DEFAULT_TIMEOUT=120") == 2
    assert dockerfile.count("ARG PIP_RETRIES=10") == 2
    assert dockerfile.count('--index-url "${PIP_INDEX_URL}"') == 2
    assert dockerfile.count('--timeout "${PIP_DEFAULT_TIMEOUT}"') == 2
    assert dockerfile.count('--retries "${PIP_RETRIES}"') == 2

    assert "PIP_INDEX_URL: ${MODELING_AGENT_PIP_INDEX_URL:-" in compose
    assert "PIP_DEFAULT_TIMEOUT: ${MODELING_AGENT_PIP_TIMEOUT:-120}" in compose
    assert "PIP_RETRIES: ${MODELING_AGENT_PIP_RETRIES:-10}" in compose
    for argument, value in (
        ("PIP_INDEX_URL", "AGENT_PIP_INDEX_URL"),
        ("PIP_DEFAULT_TIMEOUT", "AGENT_PIP_TIMEOUT"),
        ("PIP_RETRIES", "AGENT_PIP_RETRIES"),
    ):
        assert f'--build-arg "{argument}=${{{value}}}"' in build
    for script in (root_deploy, modeling_deploy):
        for variable in (
            "MODELING_AGENT_PIP_INDEX_URL",
            "MODELING_AGENT_PIP_TIMEOUT",
            "MODELING_AGENT_PIP_RETRIES",
        ):
            assert variable in script


def test_modeling_release_package_contains_core_and_agent_images(tmp_path: Path) -> None:
    modeling_root = tmp_path / "modeling"
    fake_bin = tmp_path / "bin"
    modeling_root.mkdir()
    fake_bin.mkdir()
    shutil.copy2(MODELING_ROOT / "build.sh", modeling_root / "build.sh")
    shutil.copytree(MODELING_ROOT / "deploy", modeling_root / "deploy")
    (modeling_root / "modeling-agent").mkdir()
    _write_executable(
        fake_bin / "docker",
        """
if [ "${1:-}" = "buildx" ] && [ "${2:-}" = "version" ]; then
  exit 0
fi
if [ "${1:-}" = "save" ]; then
  shift
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "-o" ]; then
      : > "$2"
      exit 0
    fi
    shift
  done
fi
exit 0
""".strip(),
    )

    result = subprocess.run(
        ["bash", str(modeling_root / "build.sh"), "v-test", "amd"],
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    package = modeling_root / "dist" / "modeling-v-test-linux-amd64.tar.gz"
    with tarfile.open(package) as archive:
        names = set(archive.getnames())
        assert "modeling/images/mw-modeling-v-test.tar" in names
        assert "modeling/images/mw-modeling-agent-v-test.tar" in names
        manifest_file = archive.extractfile("modeling/deploy/manifest.json")
        assert manifest_file is not None
        manifest = json.load(manifest_file)
    assert manifest["modeling_image"] == "mw-modeling:v-test"
    assert manifest["modeling_agent_image"] == "mw-modeling-agent:v-test"
    assert manifest["modeling_agent_image_tar"] == (
        "images/mw-modeling-agent-v-test.tar"
    )


def test_source_deploy_checks_agent_and_workbench_entrypoint() -> None:
    script = (DEPLOY_ROOT / "deploy-stack.sh").read_text(encoding="utf-8")

    assert "ensure_mcp_token_secret" in script
    assert "ensure_modeling_service_token" in script
    assert "ensure_jiuwenbox_image" in script
    assert 'build_jiuwenbox.sh" "$image"' in script
    assert "detect_jiuwenbox_socket_volume" in script
    assert 'wait_for_container "mw-modeling-agent"' in script
    assert "/workbench.html#/create" in script
    assert "modeling-agent|modeling-nl2sql" in script
    for container in (
        "onto-agent-auth-server-${TAG}",
        "onto-agent-jiuwenbox-${TAG}",
        "onto-agent-mcp-${TAG}",
        "mw-modeling-agent",
    ):
        assert f'wait_for_container "{container}"' in script


def test_source_deploy_generates_missing_mcp_secret_without_printing_it(
    tmp_path: Path,
) -> None:
    consumer_root = tmp_path / "consumer"
    backend_root = consumer_root / "backend"
    backend_root.mkdir(parents=True)
    env_file = backend_root / ".env"
    env_file.write_text("AUTH_SERVER_URL=http://auth-server:5056\n", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$DEPLOY_STACK" >/dev/null; '
            'CONSUMER_DIR="$TEST_CONSUMER_DIR"; ensure_mcp_token_secret',
        ],
        env={
            **os.environ,
            "DEPLOY_STACK": str(DEPLOY_ROOT / "deploy-stack.sh"),
            "TEST_CONSUMER_DIR": str(consumer_root),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    match = re.search(r"^MCP_TOKEN_SECRET=([0-9a-f]{64})$", env_file.read_text(), re.MULTILINE)
    assert match is not None
    assert match.group(1) not in result.stdout
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_source_deploy_generates_modeling_service_token_without_printing_it(
    tmp_path: Path,
) -> None:
    consumer_root = tmp_path / "consumer"
    backend_root = consumer_root / "backend"
    backend_root.mkdir(parents=True)
    env_file = backend_root / ".env"
    env_file.write_text("MODELING_SERVICE_TOKEN=\n", encoding="utf-8")
    inherited = {key: value for key, value in os.environ.items() if key != "MODELING_SERVICE_TOKEN"}

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$DEPLOY_STACK" >/dev/null; '
            'CONSUMER_DIR="$TEST_CONSUMER_DIR"; ensure_modeling_service_token',
        ],
        env={
            **inherited,
            "DEPLOY_STACK": str(DEPLOY_ROOT / "deploy-stack.sh"),
            "TEST_CONSUMER_DIR": str(consumer_root),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    match = re.search(
        r"^MODELING_SERVICE_TOKEN=([0-9a-f]{64})$",
        env_file.read_text(),
        re.MULTILINE,
    )
    assert match is not None
    assert match.group(1) not in result.stdout
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_source_deploy_only_blocks_missing_auth_config_before_docker(
    tmp_path: Path,
) -> None:
    consumer_root = tmp_path / "consumer"
    backend_root = consumer_root / "backend"
    backend_root.mkdir(parents=True)
    (backend_root / ".env.example").write_text(
        "AUTH_SERVER_URL=http://auth-server:5056\n"
        "AUTH_SERVER_ADMIN_TOKEN=\n"
        "AUTH_BOOTSTRAP_ADMIN_PASSWORD=\n"
        "MODELING_LLM_BASE_URL=\n"
        "MODELING_LLM_MODEL=\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$DEPLOY_STACK" >/dev/null; '
            'CONSUMER_DIR="$TEST_CONSUMER_DIR"; cmd_up',
        ],
        env={
            **os.environ,
            "DEPLOY_STACK": str(DEPLOY_ROOT / "deploy-stack.sh"),
            "TEST_CONSUMER_DIR": str(consumer_root),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "auth-server 配置不完整" in result.stderr
    assert "Workbench Agent 配置不完整" not in result.stderr
    assert (backend_root / ".env").is_file()


def test_source_deploy_allows_admin_to_configure_llm_after_startup(
    tmp_path: Path,
) -> None:
    consumer_root = tmp_path / "consumer"
    backend_root = consumer_root / "backend"
    backend_root.mkdir(parents=True)
    (backend_root / ".env").write_text(
        "AUTH_SERVER_URL=http://auth-server:5056\n"
        "AUTH_SERVER_ADMIN_TOKEN=synthetic-admin-token-1234\n"
        "AUTH_BOOTSTRAP_ADMIN_PASSWORD=synthetic-bootstrap-password\n"
        "MODELING_LLM_BASE_URL=\n"
        "MODELING_LLM_MODEL=\n",
        encoding="utf-8",
    )
    inherited = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MODELING_LLM_") and key != "MODELING_SERVICE_TOKEN"
    }

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$DEPLOY_STACK" >/dev/null; '
            'CONSUMER_DIR="$TEST_CONSUMER_DIR"; '
            'consumer_compose() { :; }; modeling_compose() { :; }; '
            'ensure_jiuwenbox_image() { :; }; '
            'wait_for_container() { :; }; wait_http() { :; }; '
            'detect_consumer_network() { printf "test-network\\n"; }; '
            'detect_jiuwenbox_socket_volume() { printf "test-socket-volume\\n"; }; '
            'cmd_up',
        ],
        env={
            **inherited,
            "DEPLOY_STACK": str(DEPLOY_ROOT / "deploy-stack.sh"),
            "TEST_CONSUMER_DIR": str(consumer_root),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "部署完成" in result.stdout
    assert "Workbench Agent 配置不完整" not in result.stderr
    assert re.search(
        r"^MODELING_SERVICE_TOKEN=[0-9a-f]{64}$",
        (backend_root / ".env").read_text(),
        re.MULTILINE,
    )


def test_root_deploy_script_is_executable() -> None:
    assert os.access(DEPLOY_ROOT / "deploy.sh", os.X_OK)
