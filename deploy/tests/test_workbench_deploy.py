import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from deploy_synthetic_credentials import (
    AUTH_ADMIN_TOKEN,
    BOOTSTRAP_ADMIN_SECRET,
    MODELING_SERVICE_TOKEN,
)
from test_knowledge_hub_deploy import _run as _run_script

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_ROOT = REPO_ROOT / "deploy"
ONTO_MODELING_DEPLOY = REPO_ROOT / "modeling" / "onto-modeling" / "deploy" / "deploy.sh"
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


@pytest.fixture
def evaluation_runtime(tmp_path: Path, monkeypatch) -> None:
    """Synthetic config and a no-op evaluation deployer for existing stack tests."""
    config = tmp_path / "evaluation.config"
    config.write_text("# Synthetic configuration; no credentials.\n", encoding="utf-8")
    consumer_config = tmp_path / "consumer" / "backend" / ".env"
    consumer_config.parent.mkdir(parents=True)
    consumer_config.write_text(
        "AUTH_SERVER_URL=http://auth-server:5056\n"
        "AUTH_SERVER_ISSUER=https://auth.example.com\n"
        "AUTH_SERVER_AUDIENCE=knowledge-scaffold\n"
        "AUTH_SERVER_ADMIN_TOKEN=synthetic-admin-token-00000000000000000000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EVAL_ENV_FILE", str(config))
    _write_executable(tmp_path / "eval-service" / "deploy" / "deploy.sh", "exit 0")
    _write_executable(tmp_path / "knowledge-hub" / "deploy" / "deploy.sh", "exit 0")
    _write_executable(tmp_path / "knowledge-hub" / "deploy.sh", "exit 0")
    _write_executable(tmp_path / "onto-modeling" / "deploy.sh", "exit 0")
    (tmp_path / "modeling" / "onto-modeling").mkdir(parents=True, exist_ok=True)
    _write_executable(tmp_path / "bin" / "docker", _AUTH_DOCKER_STUB)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ['PATH'])


def test_root_deploy_dispatches_source_checkout_to_full_stack(tmp_path: Path, evaluation_runtime) -> None:
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
        env={**os.environ, "DEPLOY_ARGS_FILE": str(args_file), "CONSUMER_NETWORK": "synthetic-network"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert args_file.read_text(encoding="utf-8").splitlines() == ["up"]


def test_root_deploy_rejects_the_removed_modeling_only_option(tmp_path: Path) -> None:
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

    assert result.returncode != 0
    assert "Unknown argument: --modeling-only" in result.stderr
    assert not args_file.exists()


def test_root_release_deploys_consumer_and_skips_leftover_workbench(tmp_path: Path, evaluation_runtime) -> None:
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    shutil.copy2(DEPLOY_ROOT / "deploy.sh", deploy_dir / "deploy.sh")
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    (backend_dir / ".env.example").write_text(
        (tmp_path / "consumer" / "backend" / ".env").read_text(encoding="utf-8")
        + "MODELING_SERVICE_TOKEN=\n",
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
    # 旧发布包升级后可能残留 deploy/modeling，部署入口不得再调用它
    _write_executable(
        deploy_dir / "modeling" / "deploy.sh",
        """
[[ "${MODELING_SERVICE_TOKEN:-}" =~ ^[0-9a-f]{64}$ ]]
printf "workbench:%s\\n" "${MODELING_SERVICE_TOKEN}" >> "$DEPLOY_CALLS_FILE"
""".strip(),
    )

    result = subprocess.run(
        [str(deploy_dir / "deploy.sh")],
        env={**os.environ, "DEPLOY_CALLS_FILE": str(calls_file), "CONSUMER_NETWORK": "synthetic-network"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = calls_file.read_text(encoding="utf-8").splitlines()
    assert [call.split(":", 1)[0] for call in calls] == ["consumer"]
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


def test_workbench_agent_is_built_only_by_the_modeling_release() -> None:
    modeling_build = (MODELING_ROOT / "build.sh").read_text(encoding="utf-8")
    full_build = (DEPLOY_ROOT / "build.sh").read_text(encoding="utf-8")

    assert '"${SCRIPT_DIR}/modeling-agent"' in modeling_build
    assert "mw-modeling-agent:${VERSION}" in modeling_build
    assert "mw-modeling-agent-${VERSION}.tar" in modeling_build
    assert '"modeling_agent_image": "mw-modeling-agent:${VERSION}"' in modeling_build
    assert '"modeling_agent_image_tar": "images/mw-modeling-agent-${VERSION}.tar"' in modeling_build
    assert '"${REPO_ROOT}/modeling/build.sh"' not in full_build
    assert '"modeling_image"' not in full_build
    assert '"modeling_agent_image"' not in full_build


def test_modeling_agent_pip_download_is_resilient_and_configurable() -> None:
    dockerfile = (MODELING_ROOT / "modeling-agent" / "Dockerfile").read_text(encoding="utf-8")
    compose = (DEPLOY_ROOT / "docker-compose.modeling.yml").read_text(encoding="utf-8")
    build = (MODELING_ROOT / "build.sh").read_text(encoding="utf-8")
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
    for variable in (
        "MODELING_AGENT_PIP_INDEX_URL",
        "MODELING_AGENT_PIP_TIMEOUT",
        "MODELING_AGENT_PIP_RETRIES",
    ):
        assert variable in modeling_deploy


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
        packaged_deploy_files = {
            member.name for member in archive.getmembers()
            if member.isfile() and member.name.startswith("modeling/deploy/")
        }
        legacy_deploy_files = {
            f"modeling/deploy/{path.relative_to(MODELING_ROOT / 'deploy')}"
            for path in (MODELING_ROOT / "deploy").rglob("*")
            if path.is_file() and path.relative_to(MODELING_ROOT / "deploy").parts[0] != "cce"
        }
        assert packaged_deploy_files == legacy_deploy_files | {
            "modeling/deploy/bin/docker-compose", "modeling/deploy/manifest.json"
        }
        assert "modeling/images/mw-modeling-v-test.tar" in names
        assert "modeling/images/mw-modeling-agent-v-test.tar" in names
        assert "modeling/.env.example" in names
        assert "modeling/deploy/install.sh" in names
        assert "modeling/deploy/bin/docker-compose" in names
        assert not any(name.startswith("modeling/deploy/cce/") for name in names)
        manifest_file = archive.extractfile("modeling/deploy/manifest.json")
        assert manifest_file is not None
        manifest = json.load(manifest_file)
    assert manifest["schema_version"] == "2"
    assert manifest["bash_min_version"] == "3.2"
    assert manifest["docker_min_version"] == "20.10"
    assert manifest["modeling_image"] == "mw-modeling:v-test"
    assert manifest["modeling_agent_image"] == "mw-modeling-agent:v-test"
    assert manifest["modeling_agent_image_tar"] == "images/mw-modeling-agent-v-test.tar"


def test_source_deploy_only_starts_consumer() -> None:
    script = (DEPLOY_ROOT / "deploy-stack.sh").read_text(encoding="utf-8")

    assert "ensure_mcp_token_secret" in script
    assert "ensure_modeling_service_token" in script
    assert "ensure_jiuwenbox_image" in script
    assert 'build_jiuwenbox.sh" "$image"' in script
    for container in (
        "onto-agent-auth-server-${TAG}",
        "onto-agent-jiuwenbox-${TAG}",
        "onto-agent-mcp-${TAG}",
    ):
        assert f'wait_for_container "{container}"' in script
    for removed in ("docker-compose.modeling.yml", "modeling_compose", "mw-modeling", "--modeling-only"):
        assert removed not in script


def test_modeling_source_deploy_provisions_its_own_mcp_secret() -> None:
    script = (MODELING_ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")

    assert 'ensure_modeling_mcp_token_secret "${repo_root}/consumer/backend/.env"' in script


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


def test_source_deploy_generates_a_distinct_evaluation_service_token(tmp_path: Path) -> None:
    consumer_root = tmp_path / "consumer"
    backend_root = consumer_root / "backend"
    backend_root.mkdir(parents=True)
    env_file = backend_root / ".env"
    modeling = "m" * 64
    env_file.write_text(f"MODELING_SERVICE_TOKEN={modeling}\nEVALUATION_SERVICE_TOKEN=\n", encoding="utf-8")
    inherited = {key: value for key, value in os.environ.items() if key != "EVALUATION_SERVICE_TOKEN"}

    def ensure(extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return _run_script(
            [
                "-c",
                'source "$DEPLOY_STACK" >/dev/null; CONSUMER_DIR="$TEST_CONSUMER_DIR"; ensure_evaluation_service_token',
            ],
            {
                **inherited,
                "DEPLOY_STACK": str(DEPLOY_ROOT / "deploy-stack.sh"),
                "TEST_CONSUMER_DIR": str(consumer_root),
                **extra,
            },
        )

    result = ensure({})
    assert result.returncode == 0, result.stdout + result.stderr
    match = re.search(r"^EVALUATION_SERVICE_TOKEN=([0-9a-f]{64})$", env_file.read_text(), re.MULTILINE)
    assert match is not None and match.group(1) != modeling
    assert match.group(1) not in result.stdout + result.stderr
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert f"MODELING_SERVICE_TOKEN={modeling}" in env_file.read_text()
    # 重跑沿用已有值；与 Modeling 凭据相同的值被拒绝
    assert ensure({}).returncode == 0
    assert f"EVALUATION_SERVICE_TOKEN={match.group(1)}" in env_file.read_text()
    same = ensure({"EVALUATION_SERVICE_TOKEN": modeling})
    assert same.returncode == 2
    assert "不能与 MODELING_SERVICE_TOKEN 相同" in same.stderr


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
    assert (backend_root / ".env").is_file()


def test_source_deploy_starts_consumer_without_llm_config(
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
                "consumer_compose() { :; }; "
                "ensure_jiuwenbox_image() { :; }; "
                "wait_for_container() { :; }; wait_http() { :; }; "
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


def test_root_deploy_source_writes_onto_modeling_stack_override(tmp_path: Path, evaluation_runtime) -> None:
    """根源码部署为 onto-modeling 生成整栈 override（共享 auth + 网络 + 评测地址）。"""
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    shutil.copy2(DEPLOY_ROOT / "deploy.sh", deploy_dir / "deploy.sh")
    onto_dir = tmp_path / "modeling" / "onto-modeling"
    onto_dir.mkdir(parents=True, exist_ok=True)
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
    # 评测已移出 onto-modeling（ADR-0020），不再接线 Modeling Workbench 地址
    assert "ONTO_WEB_MODELING_URL" not in content
    # 必须是运行中 consumer 容器的实际网络，不是按命名规律猜的名字
    assert "name: onto-agent_default" in content
    assert "consumer_onto-agent-network" not in content
    assert 'profiles: ["disabled"]' in content  # 自带 dev auth 停用，共享账号中心
    # 知识包 Hub 是独立 compose 项目，不再出现在 onto-modeling 的 override 里
    assert "knowledge-hub:" not in content
    assert "docker-compose.stack.yml" in calls_file.read_text(encoding="utf-8")
    assert "--remove-orphans" in calls_file.read_text(encoding="utf-8")
    # consumer 没有 jiuwenbox socket 卷时不挂载，脚本运行验证改在 onto-modeling 容器内
    assert "jiuwenbox-sock" not in content
    assert "未探测到 consumer 的 jiuwenbox socket 卷" in result.stderr


def test_root_deploy_source_mounts_consumer_jiuwenbox_socket(tmp_path: Path, evaluation_runtime) -> None:
    """整栈部署默认把 consumer 的 jiuwenbox socket 只读挂给脚本验证网关，不挂进跑 agent 的 onto-modeling。"""
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    shutil.copy2(DEPLOY_ROOT / "deploy.sh", deploy_dir / "deploy.sh")
    onto_dir = tmp_path / "modeling" / "onto-modeling"
    onto_dir.mkdir(parents=True, exist_ok=True)
    _write_executable(deploy_dir / "deploy-stack.sh", "exit 0")
    _write_executable(deploy_dir / "docker", _DOCKER_STUB_WITH_JIUWENBOX_SOCKET)

    # 安全审查：argv 只有复制到 tmp_path 的仓库自带 deploy.sh，以序列传入、不经 shell 解析；
    # 它调用的 docker 与 deploy-stack.sh 都是本测试写入 tmp_path 的替身。
    result = subprocess.run(  # NOSONAR(S4721)
        [str(deploy_dir / "deploy.sh")],
        env={
            **os.environ,
            "DOCKER_CALLS_FILE": str(tmp_path / "docker-calls.txt"),
            "PATH": f"{deploy_dir}:{os.environ['PATH']}",
        },
        text=True,
        capture_output=True,
        check=False,
        shell=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    content = (onto_dir / "docker-compose.stack.yml").read_text(encoding="utf-8")
    assert (
        "      - consumer-net\n  sandbox-gateway:\n    volumes:\n      - jiuwenbox-sock:/run/jiuwenbox:ro\n"
    ) in content
    onto_modeling_block = content.split("  onto-modeling:\n", 1)[1].split("  sandbox-gateway:\n", 1)[0]
    assert "jiuwenbox" not in onto_modeling_block
    assert (
        "volumes:\n  jiuwenbox-sock:\n    external: true\n    name: onto-agent_onto-jiuwenbox-sock\n"
    ) in content


def test_onto_modeling_deploy_mounts_jiuwenbox_socket_only_when_volume_exists(
    tmp_path: Path,
) -> None:
    """发布包 deploy.sh：.env 记着的 socket 卷真实存在才生成挂载覆盖文件。"""
    bundle = tmp_path / "onto-modeling"
    bundle.mkdir()
    shutil.copy2(ONTO_MODELING_DEPLOY, bundle / "deploy.sh")
    _write_executable(bundle / "preflight.sh", "exit 0")
    (bundle / "dependency_probe.py").write_text("# Synthetic dependency probe.\n", encoding="utf-8")
    (bundle / "images.env").write_text("ONTO_WEB_IMAGE=onto-modeling:synthetic\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    _write_executable(bin_dir / "docker", _ONTO_DEPLOY_DOCKER_STUB)
    calls = tmp_path / "docker-calls.txt"
    env = {
        **os.environ,
        "DOCKER_CALLS_FILE": str(calls),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }

    def deploy(existing_volume: str) -> subprocess.CompletedProcess:
        (bundle / ".env").write_text(
            "ONTO_WEB_PORT=5100\nONTO_MODELING_JIUWENBOX_SOCKET_VOLUME=onto-agent_onto-jiuwenbox-sock\n",
            encoding="utf-8",
        )
        calls.unlink(missing_ok=True)
        # 安全审查：argv 是解析出的 bash 路径加复制到 tmp_path 的发布包 deploy.sh，以序列传入、
        # 不经 shell 解析；它调用的 docker 是本测试写入的替身。
        return subprocess.run(  # NOSONAR(S4721)
            [_bash_executable(), str(bundle / "deploy.sh")],
            env={**env, "STUB_EXISTING_VOLUME": existing_volume},
            text=True,
            capture_output=True,
            check=False,
            shell=False,
            timeout=60,
        )

    result = deploy("onto-agent_onto-jiuwenbox-sock")
    assert result.returncode == 0, result.stdout + result.stderr
    override = (bundle / "docker-compose.jiuwenbox.yml").read_text(encoding="utf-8")
    assert "  sandbox-gateway:\n    volumes:\n      - jiuwenbox-sock:/run/jiuwenbox:ro" in override
    assert "onto-modeling:" not in override
    assert "    name: onto-agent_onto-jiuwenbox-sock" in override
    assert "docker-compose.jiuwenbox.yml" in calls.read_text(encoding="utf-8")

    # 卷已不存在（consumer 被卸载）：不挂载，清掉旧覆盖文件，验证在容器内执行
    result = deploy("some-other-volume")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (bundle / "docker-compose.jiuwenbox.yml").exists()
    assert "docker-compose.jiuwenbox.yml" not in calls.read_text(encoding="utf-8")
    assert "未挂载 jiuwenbox 沙箱" in result.stdout


def test_onto_modeling_deploy_generates_the_evaluation_notification_token_once(tmp_path: Path) -> None:
    """发布包 deploy.sh：缺少评测完成通知的入站凭据时生成，已有值原样保留，值不输出。"""
    bundle = tmp_path / "onto-modeling"
    bundle.mkdir()
    shutil.copy2(ONTO_MODELING_DEPLOY, bundle / "deploy.sh")
    _write_executable(bundle / "preflight.sh", "exit 0")
    (bundle / "dependency_probe.py").write_text("# Synthetic dependency probe.\n", encoding="utf-8")
    (bundle / "images.env").write_text("ONTO_WEB_IMAGE=onto-modeling:synthetic\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    _write_executable(bin_dir / "docker", _ONTO_DEPLOY_DOCKER_STUB)
    env = {
        **{key: value for key, value in os.environ.items() if key != "ONTO_EVALUATION_SERVICE_TOKEN"},
        "DOCKER_CALLS_FILE": str(tmp_path / "docker-calls.txt"),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_EXISTING_VOLUME": "",
    }
    hub_token = secrets.token_hex(32)
    # 末行没有换行符：追加的凭据不能拼到上一行
    (bundle / ".env").write_text(f"ONTO_WEB_PORT=5100\nONTO_HUB_SERVICE_TOKEN={hub_token}", encoding="utf-8")

    def deploy() -> subprocess.CompletedProcess:
        # 安全审查：argv 是解析出的 bash 路径加复制到 tmp_path 的发布包 deploy.sh，以序列传入、
        # 不经 shell 解析；它调用的 docker 是本测试写入的替身。
        return subprocess.run(  # NOSONAR(S4721)
            [_bash_executable(), str(bundle / "deploy.sh")],
            env=env, text=True, capture_output=True, check=False, shell=False, timeout=60,
        )

    result = deploy()
    assert result.returncode == 0, result.stdout + result.stderr
    content = (bundle / ".env").read_text(encoding="utf-8")
    assert f"ONTO_HUB_SERVICE_TOKEN={hub_token}\n" in content
    tokens = re.findall(r"^ONTO_EVALUATION_SERVICE_TOKEN=([0-9a-f]{64})$", content, re.MULTILINE)
    assert len(tokens) == 1 and tokens[0] != hub_token
    assert tokens[0] not in result.stdout + result.stderr
    assert (bundle / ".env").stat().st_mode & 0o777 == 0o600

    result = deploy()
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.findall(
        r"^ONTO_EVALUATION_SERVICE_TOKEN=([0-9a-f]{64})$", (bundle / ".env").read_text(encoding="utf-8"), re.MULTILINE
    ) == tokens


def _compose_service_block(compose_text: str, service: str) -> str:
    """compose 文本里某个服务的配置段（到下一个服务或顶层键为止）。"""
    block = compose_text.split(f"\n  {service}:\n", 1)[1]
    return re.split(r"\n  [a-z-]+:\n|\n[a-z]", block, maxsplit=1)[0]


@pytest.mark.parametrize(
    "compose_path",
    [
        MODELING_ROOT / "onto-modeling" / "docker-compose.yml",
        MODELING_ROOT / "onto-modeling" / "deploy" / "docker-compose.release.yml",
    ],
)
def test_onto_modeling_compose_keeps_jiuwenbox_socket_out_of_the_agent_container(compose_path: Path) -> None:
    """建模 agent 与 onto-modeling 后端同容器、能执行任意命令：socket 只给无特权的脚本验证网关。"""
    content = compose_path.read_text(encoding="utf-8")
    onto_modeling = _compose_service_block(content, "onto-modeling")
    gateway = _compose_service_block(content, "sandbox-gateway")

    assert "/run/jiuwenbox" not in onto_modeling
    assert "ONTO_WEB_SANDBOX_GATEWAY_URL=http://sandbox-gateway:8110" in onto_modeling
    assert 'command: ["python", "-m", "app.sandbox_gateway"]' in gateway
    assert 'user: "65534:65534"' in gateway and "read_only: true" in gateway
    assert "cap_drop:\n      - ALL" in gateway and "no-new-privileges:true" in gateway
    # 网关不读 .env、不挂数据卷、不发布端口：容器里没有密钥和项目数据，宿主上也访问不到
    assert "env_file" not in gateway and "/data" not in gateway and "ports:" not in gateway
    assert "8110/health" in gateway


@pytest.mark.parametrize(
    "compose_path",
    [
        MODELING_ROOT / "onto-modeling" / "docker-compose.yml",
        MODELING_ROOT / "onto-modeling" / "deploy" / "docker-compose.release.yml",
    ],
)
def test_onto_modeling_compose_serves_ops_on_business_port_by_default(compose_path: Path) -> None:
    """没有反代的单端口部署：运维中心默认经业务端口访问，.env 可关；运维监听端口不发布到宿主。"""
    onto_modeling = _compose_service_block(compose_path.read_text(encoding="utf-8"), "onto-modeling")
    assert "- ONTO_WEB_OPS_ON_MAIN=${ONTO_WEB_OPS_ON_MAIN:-true}" in onto_modeling
    assert ":8101" not in onto_modeling


def test_redeploy_app_replaces_gateway_and_keeps_socket_override(tmp_path: Path) -> None:
    """快速重部署要连同两个网关一起替换，并沿用 deploy.sh 生成的 socket 挂载，否则挂载随容器重建丢失。"""
    deploy_dir = tmp_path / "deploy-dir"
    deploy_dir.mkdir()
    shutil.copy2(
        MODELING_ROOT / "onto-modeling" / "deploy" / "docker-compose.release.yml",
        deploy_dir / "docker-compose.release.yml",
    )
    (deploy_dir / ".env").write_text("ONTO_WEB_PORT=5100\n", encoding="utf-8")
    (deploy_dir / "images.env").write_text(
        "ONTO_WEB_IMAGE=onto-modeling:v1\nRELEASE_VERSION=v1\n", encoding="utf-8"
    )
    (deploy_dir / "docker-compose.jiuwenbox.yml").write_text("services: {}\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    _write_executable(bin_dir / "docker", """\
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then exit 0; fi
printf "%s\\n" "$@" >> "$DOCKER_CALLS_FILE"\
""")
    calls = tmp_path / "docker-calls.txt"

    # 安全审查：argv 是解析出的 bash 路径、仓库自带的 redeploy-app.sh 与字面量参数，以序列传入、
    # 不经 shell 解析；它调用的 docker 是本测试写入的替身。
    result = subprocess.run(  # NOSONAR(S4721)
        [_bash_executable(), str(MODELING_ROOT / "onto-modeling" / "deploy" / "redeploy-app.sh"), "v2", "--no-build"],
        env={
            **os.environ,
            "ONTO_MODELING_DEPLOY_DIR": str(deploy_dir),
            "DOCKER_CALLS_FILE": str(calls),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
        },
        text=True,
        capture_output=True,
        check=False,
        shell=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    arguments = calls.read_text(encoding="utf-8").split("\n")
    assert "docker-compose.jiuwenbox.yml" in arguments
    assert arguments[-4:-1] == ["onto-modeling", "sandbox-gateway", "connector-gateway"]
    images = (deploy_dir / "images.env").read_text(encoding="utf-8")
    assert "CONNECTOR_GATEWAY_IMAGE=onto-modeling-connector-gateway:v2" in images
    assert "REA_SIDECAR_IMAGE" not in images
    assert "ONTO_WEB_IMAGE=onto-modeling:v2" in (deploy_dir / "images.env").read_text(encoding="utf-8")


# consumer 后端在 /run/jiuwenbox 挂着 socket 卷（其余同 _DOCKER_STUB_ON_DEFAULT_NETWORK）
_AUTH_DOCKER_STUB = """\
if [ "$1" = "ps" ]; then printf 'onto-agent-auth-server-dev\\n'; exit 0; fi
if [ "$1" = "exec" ] && [ "${3:-}" = "-i" ]; then
    read -r token || :
    [ "$token" = 'synthetic-admin-token-00000000000000000000' ]
    exit $?
fi
if [ "$1" = "exec" ]; then
    case "$*" in *audience*) printf 'knowledge-scaffold\\n' ;; *) printf 'https://auth.example.com\\n' ;; esac
    exit 0
fi
exit 0
"""


_DOCKER_STUB_WITH_JIUWENBOX_SOCKET = """\
if [ "$1" = "inspect" ] && [ "$2" = "--type" ] && [ "$3" = "container" ]; then exit 0; fi
if [ "$1" = "inspect" ] && [ "$2" = "--format" ]; then printf 'onto-agent_onto-jiuwenbox-sock\\n'; exit 0; fi
if [ "$1" = "container" ] && [ "$2" = "inspect" ]; then printf 'onto-agent_default\\n'; exit 0; fi
if [ "$1" = "ps" ]; then printf 'onto-agent-backend\\nonto-agent-auth-server-dev\\n'; exit 0; fi
if [ "$1" = "network" ] && [ "$2" = "inspect" ]; then [ "$3" = "onto-agent_default" ]; exit $?; fi
if [ "$1" = "exec" ]; then
    if [ "${3:-}" = "-i" ]; then
        read -r token || :
        [ "$token" = 'synthetic-admin-token-00000000000000000000' ]
    else
        case "$*" in *audience*) printf 'knowledge-scaffold\\n' ;; *) printf 'https://auth.example.com\\n' ;; esac
    fi
    exit $?
fi
printf "%s\\n" "$@" >> "$DOCKER_CALLS_FILE"\
"""

# onto-modeling 发布包 deploy.sh 用到的 docker 子命令：只有 STUB_EXISTING_VOLUME 这个卷存在
_ONTO_DEPLOY_DOCKER_STUB = """\
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi
if [ "$1" = "container" ] && [ "$2" = "inspect" ] && [ "$3" = "onto-modeling-auth-server" ]; then exit 1; fi
if [ "$1" = "container" ] && [ "$2" = "inspect" ]; then exit 1; fi
if [ "$1" = "network" ] && [ "$2" = "inspect" ]; then
    if [ "${4:-}" = "--format" ]; then printf 'true\\n'; fi
    exit 0
fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then exit 0; fi
if [ "$1" = "volume" ] && [ "$2" = "inspect" ]; then [ "$3" = "${STUB_EXISTING_VOLUME}" ]; exit $?; fi
if [ "$1" = "ps" ]; then exit 0; fi
printf "%s\\n" "$@" >> "$DOCKER_CALLS_FILE"\
"""


# compose 项目名为 onto-agent 时网络叫 onto-agent_default，不含 "onto-agent-network"
_DOCKER_STUB_ON_DEFAULT_NETWORK = """\
if [ "$1" = "inspect" ] && [ "$2" = "--type" ] && [ "$3" = "container" ]; then exit 0; fi
if [ "$1" = "container" ] && [ "$2" = "inspect" ]; then printf 'onto-agent_default\\n'; exit 0; fi
if [ "$1" = "ps" ]; then printf 'onto-agent-backend\\nonto-agent-auth-server-dev\\n'; exit 0; fi
if [ "$1" = "network" ] && [ "$2" = "ls" ]; then printf 'bridge\\nonto-agent_default\\n'; exit 0; fi
if [ "$1" = "network" ] && [ "$2" = "inspect" ]; then [ "$3" = "onto-agent_default" ]; exit $?; fi
if [ "$1" = "exec" ]; then
    if [ "${3:-}" = "-i" ]; then
        read -r token || :
        [ "$token" = 'synthetic-admin-token-00000000000000000000' ]
    else
        case "$*" in *audience*) printf 'knowledge-scaffold\\n' ;; *) printf 'https://auth.example.com\\n' ;; esac
    fi
    exit $?
fi
printf "%s\\n" "$@" >> "$DOCKER_CALLS_FILE"\
"""

# 探测不到任何 consumer 痕迹：必须报错退出，而不是猜一个名字继续
_DOCKER_STUB_NO_CONSUMER = """\
if [ "$1" = "ps" ]; then printf 'onto-agent-auth-server-dev\\n'; exit 0; fi
if [ "$1" = "exec" ]; then
    if [ "${3:-}" = "-i" ]; then
        read -r token || :
        [ "$token" = 'synthetic-admin-token-00000000000000000000' ]
    else
        case "$*" in *audience*) printf 'knowledge-scaffold\\n' ;; *) printf 'https://auth.example.com\\n' ;; esac
    fi
    exit $?
fi
if [ "$1" = "inspect" ] || [ "$1" = "container" ] || [ "$1" = "network" ]; then exit 1; fi
printf "%s\\n" "$@" >> "$DOCKER_CALLS_FILE"\
"""


def test_root_deploy_fails_loudly_when_consumer_network_undetectable(tmp_path: Path, evaluation_runtime) -> None:
    """探测不到 consumer 网络时直接报错。

    回归：旧实现只匹配 'onto-agent-network$'，匹配不到就回退到写死的
    consumer_onto-agent-network。compose 把它当 external 网络，于是部署以
    "network ... declared as external, but could not be found" 失败。
    """
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    shutil.copy2(DEPLOY_ROOT / "deploy.sh", deploy_dir / "deploy.sh")
    (tmp_path / "modeling" / "onto-modeling").mkdir(parents=True, exist_ok=True)
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


def test_interactive_installer_does_not_ask_for_workbench() -> None:
    """Modeling Workbench 不再由 deploy/ 部署，安装器不询问它的发布包和端口。"""
    assert re.search(r"(?<!ONTO_)MODELING_PACKAGE", _INTERACTIVE_INSTALLER) is None
    assert "请输入 Modeling" not in _INTERACTIVE_INSTALLER
    assert "modeling/deploy/install.sh" not in _INTERACTIVE_INSTALLER


def test_interactive_installer_deploys_onto_modeling() -> None:
    """交互式安装器要能装出 Onto Modeling，而不是只装 Consumer + Modeling。

    回归：该脚本此前完全不涉及 onto-modeling，走这条路的用户永远装不到它。
    """
    # 首次部署与升级两条路径都要真正调用部署函数
    assert _INTERACTIVE_INSTALLER.count("deploy_onto_modeling") >= 3
    assert 'validate_package "${ONTO_MODELING_PACKAGE}" onto-modeling' in _INTERACTIVE_INSTALLER
    assert 'tar -xzf "${ONTO_MODELING_PACKAGE}" -C "${INSTALL_ROOT}"' in _INTERACTIVE_INSTALLER
    # Onto Modeling 是必需服务，发布包提示不能为空，也不再提供跳过分支。
    assert "ask '请输入 Onto Modeling 发布包路径：'" in _INTERACTIVE_INSTALLER
    assert '[ -z "${ONTO_MODELING_PACKAGE}" ] || ' not in _INTERACTIVE_INSTALLER

    # 接线：宿主端口 + 共享账号中心的网络 + 脚本验证用的沙箱 socket 卷；评测已移出（ADR-0020），不写 Modeling 地址
    for key in (
        "ONTO_WEB_PORT",
        "ONTO_MODELING_EXTERNAL_AUTH_NETWORK",
        "ONTO_MODELING_JIUWENBOX_SOCKET_VOLUME",
    ):
        assert f'write_env_value "${{env_file}}" {key}' in _INTERACTIVE_INSTALLER
    assert "ONTO_WEB_MODELING_URL" not in _INTERACTIVE_INSTALLER




# 安装器升级路径共用的 docker stub：mw-modeling* 归属 STUB_MODELING_OWNER，Consumer
# 容器归属 STUB_CONSUMER_DIR 且在 onto-agent_default 网络上；设置 STUB_ONTO_MODELING_DIR /
# STUB_KNOWLEDGE_HUB_DIR 时模拟已有 onto-modeling / knowledge-hub 容器，设置
# STUB_LEGACY_MODELING_PORT 时模拟旧 Modeling 的宿主端口。
# STUB_RUNNING=1 时容器在 docker stop 之前处于运行状态；与真实 docker 一样，docker port
# 只对运行中的容器有结果，端口绑定配置停止后仍可读。 docker run 记录到
# $CALLS_FILE.docker-run，不混入步骤记录。STUB_CONSUMER_RUNNING 单独指定 onto-agent-backend
# 是否在运行（接入已运行 Consumer 的路径），未设置时同样按 STUB_RUNNING 判断。
_UPGRADE_DOCKER_STUB = """\
stopped_file="${CALLS_FILE}.stopped"
is_stopped() { [ -z "${STUB_RUNNING:-}" ] || grep -qx "$1" "${stopped_file}" 2>/dev/null; }
case "$1" in
    container) ;;
    image) [ "${4:-}" != --format ] || printf 'sha256:%064d\\n' 0; exit 0 ;;
    exec)
        [ "$2 $3" != "-i onto-agent-backend" ] || printf '%s\\n' "${STUB_EVALUATION_ROUTE_STATUS:-401}"
        exit 0 ;;
    ps)
        case "$*" in *"publish=${STUB_BUSY_PORT:-none}"*) printf 'synthetic-other-service\\n' ;; esac
        exit 0 ;;
    port)
        is_stopped "$2" && exit 0
        [ "$2 $3" != "mw-modeling 5100/tcp" ] || [ -z "${STUB_LEGACY_MODELING_PORT:-}" ] \\
            || printf '0.0.0.0:%s\\n' "${STUB_LEGACY_MODELING_PORT}"
        exit 0 ;;
    stop) printf '%s\\n' "$2" >> "${stopped_file}"; exit 0 ;;
    run) printf '%s\\n' "$*" >> "${CALLS_FILE}.docker-run"; exit 0 ;;
    *) exit 0 ;;
esac
[ "$2" = inspect ] || exit 1
name="$3"
format="${5:-}"
case "${name}" in
    mw-modeling) owner="${STUB_MODELING_OWNER}" ;;
    mw-modeling-agent) owner="${STUB_MODELING_OWNER}" ;;
    onto-agent-*) owner="${STUB_CONSUMER_DIR}" ;;
    onto-modeling) owner="${STUB_ONTO_MODELING_DIR:-}"; [ -n "${owner}" ] || exit 1 ;;
    knowledge-hub) owner="${STUB_KNOWLEDGE_HUB_DIR:-}"; [ -n "${owner}" ] || exit 1 ;;
    *) exit 1 ;;
esac
case "${format}" in
    *PortBindings*)
        case "${name} ${format}" in
            'mw-modeling '*'"5100/tcp"'*)
                [ -z "${STUB_LEGACY_MODELING_PORT:-}" ] \\
                    || printf '%s\\n' "${STUB_LEGACY_MODELING_PORT}" ;;
        esac ;;
    *working_dir*) printf '%s\\n' "${owner}" ;;
    *Mounts*) printf 'mw-modeling_%s-data\\n' "${name#mw-}" ;;
    *Config.Image*) printf '%s:previous\\n' "${name}" ;;
    *State.Running*)
        if [ "${name}" = onto-agent-backend ] && [ -n "${STUB_CONSUMER_RUNNING:-}" ]; then
            printf '%s\\n' "${STUB_CONSUMER_RUNNING}"
        elif is_stopped "${name}"; then printf 'false\\n'
        else printf 'true\\n'; fi ;;
    *Networks*) printf 'onto-agent_default\\n' ;;
esac\
"""


def _write_release_package(path: Path, top: str, files: dict[str, str]) -> Path:
    """合成发布包：files 中 .sh 结尾和 opsctl 写成可执行脚本，其余原样写入。"""
    stage = path.parent / f"{path.name}.stage"
    for relative, body in files.items():
        target = stage / top / relative
        if relative.endswith((".sh", "/opsctl")):
            _write_executable(target, body)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
    with tarfile.open(path, "w:gz") as archive:
        archive.add(stage / top, arcname=top)
    return path


def _prepare_separated_upgrade(tmp_path: Path, *, modeling_port: str) -> dict:
    root = tmp_path / "stack"
    consumer = root / "onto-agent"
    modeling = root / "modeling"
    (consumer / "backend").mkdir(parents=True)
    (consumer / "deploy" / "state").mkdir(parents=True)
    (consumer / "VERSION").write_text("v-previous\n", encoding="utf-8")
    (consumer / "deploy" / "state" / "current.env").write_text(
        "CURRENT_VERSION=v-previous\n", encoding="utf-8"
    )
    (consumer / "backend" / ".env").write_text(
        f"MODELING_SERVICE_TOKEN={MODELING_SERVICE_TOKEN}\n", encoding="utf-8"
    )
    modeling.mkdir(parents=True)
    (modeling / "VERSION").write_text("v-previous\n", encoding="utf-8")
    (modeling / ".env").write_text(
        f"MODELING_SERVICE_TOKEN={MODELING_SERVICE_TOKEN}\nMODELING_PORT={modeling_port}\n",
        encoding="utf-8",
    )

    calls = tmp_path / "calls.txt"

    def record(label: str) -> str:
        return f'printf "{label}\\n" >> "$CALLS_FILE"'

    # 已安装的 Modeling Workbench 不归安装脚本管理，正常流程不应调用它
    _write_executable(modeling / "deploy" / "install.sh", record("stale-modeling-install"))

    packages = tmp_path / "packages"
    packages.mkdir()
    # Consumer 包同时满足非交互、交互式首次安装与 legacy 迁移三种入口
    consumer_package = _write_release_package(
        packages / "onto-agent-v-test.tar.gz",
        "onto-agent",
        {
            "VERSION": "v-test\n",
            "backend/.env.example": "BACKEND_PORT=5055\n",
            "deploy/manifest.json": "{}\n",
            "deploy/opsctl": 'printf "preflight PASS\\n"',
            # 与独立 Consumer 发布包一致：upgrade.sh 只接受安装目录，多余参数直接失败
            "deploy/upgrade.sh": '[ "$#" -le 1 ] || exit 1; ' + record("consumer-upgrade"),
            "deploy/install.sh": (
                'if [ "${1:-}" = --preflight-only ]; then '
                + record("consumer-preflight")
                + "; else "
                + record("consumer-install")
                + "; fi"
            ),
        },
    )
    modeling_package = _write_release_package(
        packages / "modeling-v-test.tar.gz",
        "modeling",
        {
            "VERSION": "v-test\n",
            ".env.example": "MODELING_PORT=8100\n",
            "deploy/install.sh": record("modeling-install"),
        },
    )
    onto_package = _write_release_package(
        packages / "onto-modeling-v-test.tar.gz",
        "onto-modeling",
        {
            ".env.example": "ONTO_WEB_PORT=5100\n",
            "preflight.sh": record("onto-preflight"),
            # 与真实 deploy.sh 一样先切到自身目录，调用方不必 cd
            "deploy.sh": record("onto-deploy") + '\ncd "$(dirname "$0")"\ncp .env "$ONTO_ENV_SNAPSHOT"',
        },
    )
    # Hub 部署替身：凭据按 注入 > .env > 生成 的顺序落到自身 .env，并记录收到的网络与凭据
    hub_package = _write_release_package(
        packages / "knowledge-hub-v-test.tar.gz",
        "knowledge-hub",
        {
            "docker-compose.knowledge-hub.yml": "name: knowledge-hub\n",
            "deploy.sh": record("hub-deploy")
            + '\ncd "$(dirname "$0")"'
            + '\nprintf "CONSUMER_NETWORK=%s\\nRECEIVED_TOKEN=%s\\n" "${CONSUMER_NETWORK:-}"'
            + ' "${HUB_SERVICE_TOKEN:-}" > "$HUB_ENV_SNAPSHOT"'
            + '\ntoken="${HUB_SERVICE_TOKEN:-$(sed -n "s/^HUB_SERVICE_TOKEN=//p" .env 2>/dev/null || true)}"'
            + '\nprintf "HUB_SERVICE_TOKEN=%s\\n" "${token:-$STUB_HUB_TOKEN}" > .env'
            + '\nprintf "HUB_EVALUATION_SERVICE_TOKEN=%s\\n" "$STUB_HUB_EVALUATION_TOKEN" >> .env',
        },
    )

    evaluation_package = _write_release_package(
        packages / "evaluation-v-test.tar.gz", "eval-service",
        {"deploy/deploy.sh": record("evaluation-deploy"),
         "deploy/docker-compose.eval-service.yml": "name: knowledge-evaluation\n",
         "evaluation-image.tar": "Synthetic image archive\n", "IMAGE": "knowledge-evaluation:v-test\n"},
    )

    bin_dir = tmp_path / "bin"
    _write_executable(bin_dir / "docker", _UPGRADE_DOCKER_STUB)
    _write_executable(bin_dir / "curl", 'printf "%s\\n" "$@" >> "$CURL_CALLS_FILE"')
    _write_executable(bin_dir / "uname", 'printf "Linux\\n"')
    for command in ("flock", "sha256sum"):
        if shutil.which(command) is None:
            _write_executable(bin_dir / command, "exit 0")

    env = {
        **{key: value for key, value in os.environ.items() if key not in ("ONTO_WEB_PORT", "MODELING_PORT")},
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "INSTALL_ROOT": str(root),
        "CALLS_FILE": str(calls),
        "CURL_CALLS_FILE": str(tmp_path / "curl-calls.txt"),
        "ONTO_ENV_SNAPSHOT": str(tmp_path / "onto-modeling.env"),
        "HUB_ENV_SNAPSHOT": str(tmp_path / "knowledge-hub.snapshot"),
        "STUB_HUB_TOKEN": secrets.token_hex(32),
        "STUB_HUB_EVALUATION_TOKEN": secrets.token_hex(32),
        "STUB_CONSUMER_DIR": str(consumer),
        "STUB_MODELING_OWNER": str(modeling),
    }
    return {
        "root": root,
        "env": env,
        "calls": calls,
        "packages": (consumer_package, modeling_package, onto_package),
        "hub_package": hub_package,
        "evaluation_package": evaluation_package,
    }


def _run_separated_upgrade(setup: dict, *, with_onto: bool = True, with_hub: bool = True, **env: str):
    consumer_package, _, onto_package = setup["packages"]
    args = [
        _bash_executable(),
        str(DEPLOY_ROOT / "install-consumer-modeling.sh"),
        "upgrade",
        "--consumer-package",
        str(consumer_package),
    ]
    if with_onto:
        args += ["--onto-modeling-package", str(onto_package.parent / "onto-modeling-*.tar.gz")]
    if with_hub:
        args += ["--knowledge-hub-package", str(setup["hub_package"])]
    args += ["--evaluation-package", str(setup["evaluation_package"])]
    return subprocess.run(
        args, env={**setup["env"], **env}, text=True, capture_output=True, check=False
    )


def _calls(setup: dict) -> list[str]:
    calls = setup["calls"]
    return calls.read_text(encoding="utf-8").split() if calls.exists() else []


def test_installer_upgrade_deploys_onto_modeling_after_consumer(
    tmp_path: Path,
) -> None:
    """回归：非交互 upgrade 只升 Consumer，onto-modeling 永远不会被拉起。"""
    setup = _prepare_separated_upgrade(tmp_path, modeling_port="8100")

    result = _run_separated_upgrade(setup)

    assert result.returncode == 0, result.stdout + result.stderr
    # 预检在任何变更之前，onto-modeling 部署排在 Consumer、Hub 之后
    assert _calls(setup) == ['onto-preflight', 'consumer-upgrade', 'hub-deploy', 'onto-deploy', 'evaluation-deploy']
    onto_env = (tmp_path / "onto-modeling.env").read_text(encoding="utf-8")
    assert "ONTO_WEB_PORT=5100" in onto_env
    # 共享 Consumer 账号中心：网络取运行中容器的实际网络，不按命名规律猜
    assert "ONTO_MODELING_EXTERNAL_AUTH_NETWORK=onto-agent_default" in onto_env
    assert "ONTO_WEB_MODELING_URL" not in onto_env
    # 沙箱 socket 卷同样取 consumer 容器的实际挂载（桩按容器名回卷名）
    assert "ONTO_MODELING_JIUWENBOX_SOCKET_VOLUME=mw-modeling_onto-agent-backend-data" in onto_env
    assert (setup["root"] / "onto-modeling" / ".env").stat().st_mode & 0o777 == 0o600
    # 等到健康检查通过才算部署完成
    curl_calls = (tmp_path / "curl-calls.txt").read_text(encoding="utf-8")
    assert "http://127.0.0.1:5100/api/health" in curl_calls
    assert "Onto Modeling deployment finished" in result.stdout














def _drive_tty(
    script: Path, answers: list[tuple[str, str]], env: dict[str, str], timeout: float = 60
) -> tuple[int, str]:
    """用伪终端驱动从 /dev/tty 读输入的交互脚本：等到下一条提示出现再作答。

    stdin 接 /dev/null，脚本只能从控制终端读到答案；提示没有按顺序出现时超时失败。
    """
    import pty
    import select
    import signal
    import time

    bash = _bash_executable()
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
            os.execve(bash, [bash, str(script)], env)
        finally:
            os._exit(127)
    output = b""
    pending = list(answers)
    answered = 0
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"交互脚本超时，未出现的提示：{pending[:1]}\n"
                    + output.decode("utf-8", "replace")
                )
            ready, _, _ = select.select([fd], [], [], min(remaining, 0.5))
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:  # Linux 上子进程退出后读主端报 EIO
                    chunk = b""
                if not chunk:
                    break
                output += chunk
            text = output.decode("utf-8", "replace")
            if pending and (position := text.find(pending[0][0], answered)) >= 0:
                prompt, reply = pending.pop(0)
                answered = position + len(prompt)
                os.write(fd, f"{reply}\n".encode())
    except BaseException:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        raise
    finally:
        os.close(fd)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), output.decode("utf-8", "replace")


def _interactive_upgrade_answers(
    setup: dict, *extra: tuple[str, str], hub: str | None = None, evaluation: str | None = None
) -> list[tuple[str, str]]:
    consumer_package, _, onto_package = setup["packages"]
    return [
        ("请选择模式", "2"),
        ("请输入已分离部署的安装根目录", str(setup["root"])),
        ("请输入 Consumer 发布包路径", str(consumer_package)),
        ("请输入 Onto Modeling 发布包路径", str(onto_package.parent / "onto-modeling-*.tar.gz")),
        ("请输入知识包 Hub 发布包路径", str(setup["hub_package"]) if hub is None else hub),
        ("请输入独立评测服务发布包", str(setup["evaluation_package"]) if evaluation is None else evaluation),
        *extra,
    ]










def _prepare_legacy_upgrade(tmp_path: Path) -> dict:
    """旧合并部署：Consumer 与 Modeling 同在 legacy/onto-agent，mw-modeling 发布在 5100。"""
    setup = _prepare_separated_upgrade(tmp_path, modeling_port="8100")
    legacy = tmp_path / "legacy" / "onto-agent"
    (legacy / "backend").mkdir(parents=True)
    (legacy / "deploy" / "state").mkdir(parents=True)
    (legacy / "deploy" / "modeling").mkdir()
    (legacy / "VERSION").write_text("v-previous\n", encoding="utf-8")
    (legacy / "deploy" / "state" / "current.env").write_text(
        "CURRENT_VERSION=v-previous\n", encoding="utf-8"
    )
    (legacy / "backend" / ".env").write_text(
        f"MODELING_SERVICE_TOKEN={MODELING_SERVICE_TOKEN}\n", encoding="utf-8"
    )
    (legacy / "deploy" / "manifest.json").write_text(
        '{\n  "modeling_image": "mw-modeling:v-previous"\n}\n', encoding="utf-8"
    )
    setup["legacy"] = legacy
    setup["env"] = {
        **setup["env"],
        "STUB_CONSUMER_DIR": str(legacy),
        "STUB_MODELING_OWNER": str(legacy),
        "STUB_LEGACY_MODELING_PORT": "5100",
    }
    return setup


def _legacy_answers(
    setup: dict, onto_answer: str | None, *extra: tuple[str, str],
    hub_answer: str | None = None, evaluation: str | None = None
) -> list:
    consumer_package, _, _ = setup["packages"]
    return [
        ("旧的合并部署根目录", str(setup["legacy"])),
        ("Consumer 镜像 tar 包路径", str(consumer_package)),
        ("Onto Modeling 发布包路径", str(setup["packages"][2]) if onto_answer is None else onto_answer),
        ("知识包 Hub 发布包路径", str(setup["hub_package"]) if hub_answer is None else hub_answer),
        ("独立评测服务发布包", str(setup["evaluation_package"]) if evaluation is None else evaluation),
        *extra,
    ]


def test_legacy_migration_deploys_onto_modeling_on_a_free_port(tmp_path: Path) -> None:
    """回归：legacy 迁移脚本完全不涉及 Onto Modeling；Modeling 继续占用旧的 5100。"""
    setup = _prepare_legacy_upgrade(tmp_path)
    onto_package = setup["packages"][2]

    rc, output = _drive_tty(
        DEPLOY_ROOT / "upgrade-consumer-modeling-legacy-interactive.sh",
        _legacy_answers(setup, str(onto_package), ("请输入 Onto Modeling 宿主端口", "5110")),
        setup["env"],
    )

    assert rc == 0, output
    assert "Onto Modeling 宿主端口 5100 已被当前部署的其他服务使用" in output
    assert _calls(setup) == ['onto-preflight', 'consumer-upgrade', 'hub-deploy', 'onto-deploy', 'evaluation-deploy']
    stack_root = setup["legacy"].parent
    assert not (stack_root / "modeling").exists()
    onto_env = (tmp_path / "onto-modeling.env").read_text(encoding="utf-8")
    assert "ONTO_WEB_PORT=5110" in onto_env
    assert "ONTO_MODELING_EXTERNAL_AUTH_NETWORK=onto-agent_default" in onto_env
    assert "ONTO_WEB_MODELING_URL" not in onto_env
    assert "ONTO_MODELING_JIUWENBOX_SOCKET_VOLUME=mw-modeling_onto-agent-backend-data" in onto_env
    assert (stack_root / "onto-modeling" / ".env").stat().st_mode & 0o777 == 0o600
    assert "Onto Modeling deployment finished" in output






def _run_legacy_upgrade(setup: dict, **env: str):
    consumer_package, _, _ = setup["packages"]
    # 不显式指定 INSTALL_ROOT：组件目录默认落在旧合并部署旁
    run_env = {key: value for key, value in setup["env"].items() if key != "INSTALL_ROOT"}
    return subprocess.run(
        [
            _bash_executable(),
            str(DEPLOY_ROOT / "install-consumer-modeling.sh"),
            "--upgrade-from",
            str(setup["legacy"]),
            "--consumer-package",
            str(consumer_package),
            "--onto-modeling-package", str(setup["packages"][2]),
            "--knowledge-hub-package", str(setup["hub_package"]),
            "--evaluation-package", str(setup["evaluation_package"]),
        ],
        env={**run_env, **env},
        text=True,
        capture_output=True,
        check=False,
    )


def _volume_backups(setup: dict) -> str:
    runs = Path(f"{setup['calls']}.docker-run")
    return runs.read_text(encoding="utf-8") if runs.exists() else ""




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
