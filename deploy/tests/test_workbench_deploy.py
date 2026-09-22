import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from deploy_synthetic_credentials import AUTH_ADMIN_TOKEN, BOOTSTRAP_ADMIN_SECRET

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_ROOT = REPO_ROOT / "deploy"
MODELING_ROOT = REPO_ROOT / "modeling"


def _bash_executable() -> str:
    bash_path = shutil.which("bash")
    if bash_path is None:
        pytest.skip("Bash is required for deployment script tests")
    return str(Path(bash_path).resolve())


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
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    (backend_dir / ".env.example").write_text(
        "MODELING_SERVICE_TOKEN=\n",
        encoding="utf-8",
    )
    calls_file = tmp_path / "calls.txt"
    _write_executable(
        deploy_dir / "deploy-consumer.sh",
        """
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
token="$(sed -n 's/^MODELING_SERVICE_TOKEN=//p' "${script_dir}/../backend/.env")"
[[ "${token}" =~ ^[0-9a-f]{64}$ ]]
printf "consumer:%s\\n" "${token}" >> "$DEPLOY_CALLS_FILE"
""".strip(),
    )
    _write_executable(
        deploy_dir / "modeling" / "deploy.sh",
        """
[[ "${MODELING_SERVICE_TOKEN:-}" =~ ^[0-9a-f]{64}$ ]]
printf "workbench:%s\\n" "${MODELING_SERVICE_TOKEN}" >> "$DEPLOY_CALLS_FILE"
""".strip(),
    )

    result = subprocess.run(
        [str(deploy_dir / "deploy.sh")],
        env={**os.environ, "DEPLOY_CALLS_FILE": str(calls_file)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = calls_file.read_text(encoding="utf-8").splitlines()
    assert [call.split(":", 1)[0] for call in calls] == ["consumer", "workbench"]
    assert calls[0].split(":", 1)[1] == calls[1].split(":", 1)[1]
    assert (backend_dir / ".env").stat().st_mode & 0o777 == 0o600


def test_modeling_deploy_rejects_running_consumer_with_different_token() -> None:
    script = MODELING_ROOT / "deploy" / "deploy.sh"
    result = subprocess.run(
        [
            _bash_executable(),
            "-c",
            (
                'source <(sed \'/^if \\[ "\\$#" -gt 0 \\]; then/,$d\' "$MODELING_DEPLOY"); '
                'MODELING_SERVICE_TOKEN="modeling-token-with-at-least-32-characters"; '
                'consumer_container() { printf "onto-agent-backend\\n"; }; '
                'docker() { printf "MODELING_SERVICE_TOKEN=consumer-token-with-at-least-32-characters\\n"; }; '
                "verify_consumer_service_token"
            ),
        ],
        env={**os.environ, "MODELING_DEPLOY": str(script)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "does not use the configured MODELING_SERVICE_TOKEN" in result.stderr
    assert "modeling-token" not in result.stdout + result.stderr
    assert "consumer-token" not in result.stdout + result.stderr


def test_workbench_agent_is_built_and_described_in_release_manifests() -> None:
    modeling_build = (MODELING_ROOT / "build.sh").read_text(encoding="utf-8")
    full_build = (DEPLOY_ROOT / "build.sh").read_text(encoding="utf-8")

    assert '"${SCRIPT_DIR}/modeling-agent"' in modeling_build
    assert "mw-modeling-agent:${VERSION}" in modeling_build
    assert "mw-modeling-agent-${VERSION}.tar" in modeling_build
    for script in (modeling_build, full_build):
        assert '"modeling_agent_image": "mw-modeling-agent:${VERSION}"' in script
        assert '"modeling_agent_image_tar": "images/mw-modeling-agent-${VERSION}.tar"' in script


def test_modeling_agent_pip_download_is_resilient_and_configurable() -> None:
    dockerfile = (MODELING_ROOT / "modeling-agent" / "Dockerfile").read_text(encoding="utf-8")
    compose = (DEPLOY_ROOT / "docker-compose.modeling.yml").read_text(encoding="utf-8")
    build = (MODELING_ROOT / "build.sh").read_text(encoding="utf-8")
    root_deploy = (DEPLOY_ROOT / "deploy-stack.sh").read_text(encoding="utf-8")
    modeling_deploy = (MODELING_ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")

    assert dockerfile.count("ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple") == 2
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
        [_bash_executable(), str(modeling_root / "build.sh"), "v-test", "amd"],
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
        assert "modeling/.env.example" in names
        assert "modeling/deploy/install.sh" in names
        assert "modeling/deploy/bin/docker-compose" in names
        manifest_file = archive.extractfile("modeling/deploy/manifest.json")
        assert manifest_file is not None
        manifest = json.load(manifest_file)
    assert manifest["schema_version"] == "2"
    assert manifest["bash_min_version"] == "3.2"
    assert manifest["docker_min_version"] == "20.10"
    assert manifest["modeling_image"] == "mw-modeling:v-test"
    assert manifest["modeling_agent_image"] == "mw-modeling-agent:v-test"
    assert manifest["modeling_agent_image_tar"] == "images/mw-modeling-agent-v-test.tar"


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
            _bash_executable(),
            "-c",
            'source "$DEPLOY_STACK" >/dev/null; CONSUMER_DIR="$TEST_CONSUMER_DIR"; ensure_mcp_token_secret',
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
            _bash_executable(),
            "-c",
            'source "$DEPLOY_STACK" >/dev/null; CONSUMER_DIR="$TEST_CONSUMER_DIR"; ensure_modeling_service_token',
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
            _bash_executable(),
            "-c",
            'source "$DEPLOY_STACK" >/dev/null; CONSUMER_DIR="$TEST_CONSUMER_DIR"; cmd_up',
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
        f"AUTH_SERVER_ADMIN_TOKEN={AUTH_ADMIN_TOKEN}\n"
        f"AUTH_BOOTSTRAP_ADMIN_PASSWORD={BOOTSTRAP_ADMIN_SECRET}\n"
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
            _bash_executable(),
            "-c",
            (
                'source "$DEPLOY_STACK" >/dev/null; '
                'CONSUMER_DIR="$TEST_CONSUMER_DIR"; '
                "consumer_compose() { :; }; modeling_compose() { :; }; "
                "ensure_jiuwenbox_image() { :; }; "
                "wait_for_container() { :; }; wait_http() { :; }; "
                'detect_consumer_network() { printf "test-network\\n"; }; '
                'detect_jiuwenbox_socket_volume() { printf "test-socket-volume\\n"; }; '
                "cmd_up"
            ),
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


def test_modeling_release_uses_bundled_compose_and_no_host_python() -> None:
    deploy = (MODELING_ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")

    assert '"${SCRIPT_DIR}/bin/docker-compose"' in deploy
    assert '"${SCRIPT_DIR}/../bin/docker-compose"' in deploy
    assert "docker version --format '{{.Server.APIVersion}}'" in deploy
    assert 'DOCKER_API_VERSION="${daemon_api_version}"' in deploy
    assert "python3 -c" not in deploy
    assert "od -An -N 32 -tx1 /dev/urandom" in deploy


def test_modeling_build_help_uses_named_parameters() -> None:
    build_script = (MODELING_ROOT / "build.sh").read_text(encoding="utf-8")

    assert "--version <version>" in build_script
    assert "--arch <amd|arm>" in build_script


def test_modeling_install_is_minimal_and_persists_custom_ports() -> None:
    install = (MODELING_ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert "checksums.txt" in install
    assert "MODELING_PORT" in install
    assert "MODELING_MCP_PORT" in install
    assert 'exec bash "${SCRIPT_DIR}/deploy.sh"' in install
    assert "modelingctl" not in install
    assert "reports" not in install

def test_source_deploy_generates_eval_mysql_password_without_printing_it(
    tmp_path: Path,
) -> None:
    """评测库开箱即用：密码首次自动生成、0600 落盘 backend/.env、不回显。"""
    consumer_root = tmp_path / "consumer"
    backend_root = consumer_root / "backend"
    backend_root.mkdir(parents=True)
    env_file = backend_root / ".env"
    env_file.write_text("EVAL_MYSQL_ROOT_PASSWORD=\n", encoding="utf-8")
    inherited = {
        key: value for key, value in os.environ.items() if key != "EVAL_MYSQL_ROOT_PASSWORD"
    }

    result = subprocess.run(
        [
            _bash_executable(),
            "-c",
            'source "$DEPLOY_STACK" >/dev/null; CONSUMER_DIR="$TEST_CONSUMER_DIR"; ensure_eval_mysql_password',
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
        r"^EVAL_MYSQL_ROOT_PASSWORD=([0-9a-f]{32})$",
        env_file.read_text(),
        re.MULTILINE,
    )
    assert match is not None
    assert match.group(1) not in result.stdout
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_root_deploy_source_writes_onto_modeling_stack_override(tmp_path: Path) -> None:
    """根源码部署为 onto-modeling 生成整栈 override（共享 auth + 网络 + 评测地址）。"""
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    shutil.copy2(DEPLOY_ROOT / "deploy.sh", deploy_dir / "deploy.sh")
    onto_dir = tmp_path / "modeling" / "onto-modeling"
    onto_dir.mkdir(parents=True)
    args_file = tmp_path / "args.txt"
    _write_executable(
        deploy_dir / "deploy-stack.sh",
        'printf "%s\\n" "$@" > "$DEPLOY_ARGS_FILE"',
    )
    # stub docker：模拟真实部署——consumer 容器在 onto-agent_default 上
    docker_stub = deploy_dir / "docker"
    _write_executable(docker_stub, _DOCKER_STUB_ON_DEFAULT_NETWORK)
    calls_file = tmp_path / "docker-calls.txt"

    result = subprocess.run(
        [str(deploy_dir / "deploy.sh")],
        env={
            **os.environ,
            "DEPLOY_ARGS_FILE": str(args_file),
            "DOCKER_CALLS_FILE": str(calls_file),
            "PATH": f"{deploy_dir}:{os.environ['PATH']}",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    override = onto_dir / "docker-compose.stack.yml"
    assert override.is_file()
    content = override.read_text(encoding="utf-8")
    assert "ONTO_WEB_MODELING_URL=http://mw-modeling:5100" in content
    # 必须是运行中 consumer 容器的实际网络，不是按命名规律猜的名字
    assert "name: onto-agent_default" in content
    assert "consumer_onto-agent-network" not in content
    assert 'profiles: ["disabled"]' in content  # 自带 dev auth 停用，共享账号中心
    assert "docker-compose.stack.yml" in calls_file.read_text(encoding="utf-8")


# compose 项目名为 onto-agent 时网络叫 onto-agent_default，不含 "onto-agent-network"
_DOCKER_STUB_ON_DEFAULT_NETWORK = """\
if [ "$1" = "inspect" ] && [ "$2" = "--type" ] && [ "$3" = "container" ]; then exit 0; fi
if [ "$1" = "container" ] && [ "$2" = "inspect" ]; then printf 'onto-agent_default\\n'; exit 0; fi
if [ "$1" = "ps" ]; then printf 'onto-agent-backend\\n'; exit 0; fi
if [ "$1" = "network" ] && [ "$2" = "ls" ]; then printf 'bridge\\nonto-agent_default\\n'; exit 0; fi
if [ "$1" = "network" ] && [ "$2" = "inspect" ]; then [ "$3" = "onto-agent_default" ]; exit $?; fi
printf "%s\\n" "$@" >> "$DOCKER_CALLS_FILE"\
"""

# 探测不到任何 consumer 痕迹：必须报错退出，而不是猜一个名字继续
_DOCKER_STUB_NO_CONSUMER = """\
if [ "$1" = "inspect" ] || [ "$1" = "container" ] || [ "$1" = "network" ]; then exit 1; fi
if [ "$1" = "ps" ]; then exit 0; fi
printf "%s\\n" "$@" >> "$DOCKER_CALLS_FILE"\
"""


def test_root_deploy_fails_loudly_when_consumer_network_undetectable(tmp_path: Path) -> None:
    """探测不到 consumer 网络时直接报错。

    回归：旧实现只匹配 'onto-agent-network$'，匹配不到就回退到写死的
    consumer_onto-agent-network。compose 把它当 external 网络，于是部署以
    "network ... declared as external, but could not be found" 失败。
    """
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    shutil.copy2(DEPLOY_ROOT / "deploy.sh", deploy_dir / "deploy.sh")
    (tmp_path / "modeling" / "onto-modeling").mkdir(parents=True)
    _write_executable(deploy_dir / "deploy-stack.sh", 'printf "%s\\n" "$@" > "$DEPLOY_ARGS_FILE"')
    _write_executable(deploy_dir / "docker", _DOCKER_STUB_NO_CONSUMER)

    result = subprocess.run(
        [str(deploy_dir / "deploy.sh")],
        env={
            **os.environ,
            "DEPLOY_ARGS_FILE": str(tmp_path / "args.txt"),
            "DOCKER_CALLS_FILE": str(tmp_path / "docker-calls.txt"),
            "PATH": f"{deploy_dir}:{os.environ['PATH']}",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "无法探测 consumer Docker 网络" in result.stderr
    assert "consumer_onto-agent-network" not in result.stdout + result.stderr


_INTERACTIVE_INSTALLER = (DEPLOY_ROOT / "install-consumer-modeling-interactive.sh").read_text(
    encoding="utf-8"
)


def test_interactive_installer_default_ports_do_not_collide() -> None:
    """交互式安装器的默认端口里 Modeling 与 Onto Modeling 不能撞车。

    回归：默认端口分支原本给 Modeling 写 5100，而 5100 已归 Onto Modeling，
    结果是装完 5100 上仍是 Modeling、Onto Modeling 起不来。
    """
    modeling = re.search(
        r"^\s*MODELING_PORT=(\d+); MODELING_MCP_PORT=", _INTERACTIVE_INSTALLER, re.MULTILINE
    )
    onto = re.search(r"^\s*ONTO_WEB_PORT=(\d+)$", _INTERACTIVE_INSTALLER, re.MULTILINE)
    assert modeling is not None and onto is not None
    assert modeling.group(1) != onto.group(1)


def test_interactive_installer_deploys_onto_modeling() -> None:
    """交互式安装器要能装出 Onto Modeling，而不是只装 Consumer + Modeling。

    回归：该脚本此前完全不涉及 onto-modeling，走这条路的用户永远装不到它。
    """
    # 首次部署与升级两条路径都要真正调用部署函数
    assert _INTERACTIVE_INSTALLER.count("deploy_onto_modeling") >= 3
    assert 'validate_package "${ONTO_MODELING_PACKAGE}" onto-modeling' in _INTERACTIVE_INSTALLER
    assert 'tar -xzf "${ONTO_MODELING_PACKAGE}" -C "${INSTALL_ROOT}"' in _INTERACTIVE_INSTALLER
    # 缺包时跳过，不能硬失败——存量发布没有这个包
    assert _INTERACTIVE_INSTALLER.count('[ -z "${ONTO_MODELING_PACKAGE}" ] || ') >= 2

    # 接线：宿主端口 + 共享账号中心的网络 + 评测用的 Modeling 地址
    for key in (
        "ONTO_WEB_PORT",
        "ONTO_MODELING_EXTERNAL_AUTH_NETWORK",
        "ONTO_WEB_MODELING_URL",
    ):
        assert f'write_env_value "${{env_file}}" {key}' in _INTERACTIVE_INSTALLER


def test_interactive_installer_never_guesses_consumer_network() -> None:
    """网络名必须现场探测；猜名字会让 compose 报 external network not found。"""
    assert "docker container inspect onto-agent-backend" in _INTERACTIVE_INSTALLER
    assert "无法探测 Consumer 容器网络" in _INTERACTIVE_INSTALLER
    assert "consumer_onto-agent-network" not in _INTERACTIVE_INSTALLER


def _host_port_default(compose_text: str, var: str) -> str:
    """取 compose ports 里 "${VAR:-默认}:容器端口" 的宿主侧默认值。"""
    match = re.search(rf'- "\$\{{{var}:-(\d+)\}}:\d+"', compose_text)
    assert match is not None, f"未找到 {var} 的宿主端口映射"
    return match.group(1)


def test_modeling_healthcheck_port_matches_published_host_port() -> None:
    """健康检查端口必须跟 compose 发布的宿主端口同源。

    回归：端口互换只改了 compose 默认值（5100→8100），deploy.sh 仍轮询 5100，
    未显式设置 MODELING_PORT 的新部署必然卡在「Workbench page did not become reachable」。
    """
    release_compose = (MODELING_ROOT / "deploy" / "docker-compose.release.yml").read_text(
        encoding="utf-8"
    )
    source_compose = (DEPLOY_ROOT / "docker-compose.modeling.yml").read_text(encoding="utf-8")
    deploy_script = (MODELING_ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")

    release_port = _host_port_default(release_compose, "MODELING_PORT")
    assert _host_port_default(source_compose, "MODELING_PORT") == release_port

    checked_ports = set(re.findall(r"127\.0\.0\.1:\$\{MODELING_PORT:-(\d+)\}", deploy_script))
    assert checked_ports == {release_port}

    # 发布包模板会被 install.sh 拷成 .env，显式值优先于上面所有默认值，
    # 漏改这里会让独立发布包继续落在旧端口上。
    build_script = (MODELING_ROOT / "build.sh").read_text(encoding="utf-8")
    seeded = re.search(r"^MODELING_PORT=(\d+)$", build_script, re.MULTILINE)
    assert seeded is not None, "modeling/build.sh 未生成 MODELING_PORT 模板项"
    assert seeded.group(1) == release_port

    # 交互式安装器的「默认端口」分支是又一处独立的宿主端口来源
    chosen = re.search(
        r"^\s*MODELING_PORT=(\d+); MODELING_MCP_PORT=", _INTERACTIVE_INSTALLER, re.MULTILINE
    )
    assert chosen is not None, "交互式安装器未设置默认 MODELING_PORT"
    assert chosen.group(1) == release_port


def test_modeling_and_onto_modeling_do_not_share_host_port() -> None:
    """两个栈由同一次根部署先后拉起，宿主端口默认值不能撞车。"""
    onto_root = MODELING_ROOT / "onto-modeling"
    modeling_port = _host_port_default(
        (MODELING_ROOT / "deploy" / "docker-compose.release.yml").read_text(encoding="utf-8"),
        "MODELING_PORT",
    )
    for compose_path in (
        onto_root / "deploy" / "docker-compose.release.yml",
        onto_root / "docker-compose.yml",
    ):
        onto_port = _host_port_default(compose_path.read_text(encoding="utf-8"), "ONTO_WEB_PORT")
        assert onto_port != modeling_port, f"{compose_path} 与 modeling 栈抢占同一宿主端口"
