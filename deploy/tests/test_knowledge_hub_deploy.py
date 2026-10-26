"""知识包 Hub 独立部署：compose 解耦、部署脚本、整栈编排与离线打包。

只使用合成凭据和本地命令替身，不调用真实 Docker。
"""

import os
import re
import secrets
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_ROOT = REPO_ROOT / "deploy"
HUB_COMPOSE = DEPLOY_ROOT / "docker-compose.knowledge-hub.yml"
ONTO_COMPOSE_FILES = (
    REPO_ROOT / "modeling" / "onto-modeling" / "docker-compose.yml",
    REPO_ROOT / "modeling" / "onto-modeling" / "deploy" / "docker-compose.release.yml",
)


def _bash_executable() -> str:
    bash_path = shutil.which("bash")
    if bash_path is None:
        pytest.skip("Bash is required for deployment script tests")
    return str(Path(bash_path).resolve())


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/bash\nset -euo pipefail\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # Security review: argv is the resolved bash binary plus a repository script copied
    # into tmp_path; docker and sibling scripts it calls are stubs written by these tests.
    return subprocess.run(  # NOSONAR(S4721)
        [_bash_executable(), *argv],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        shell=False,
        timeout=60,
    )


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# ---------- compose 解耦 ----------


def test_hub_compose_is_an_independent_project_on_the_shared_network() -> None:
    content = HUB_COMPOSE.read_text(encoding="utf-8")

    # 固定项目名锚定数据卷；不再借用 onto-modeling 的项目（onto-web）
    assert re.search(r"^name: knowledge-hub$", content, re.MULTILINE)
    assert "container_name: knowledge-hub" in content
    assert "knowledge-hub-data:/data" in content
    # 网络与凭据缺失时失败关闭，不猜网络名、不发布宿主端口
    assert "name: ${CONSUMER_NETWORK:?" in content
    assert "external: true" in content
    assert "HUB_SERVICE_TOKEN=${HUB_SERVICE_TOKEN:?" in content
    assert "ports:" not in content
    # 源码与离线包共用：镜像由部署脚本构建或加载，compose 不引用源码目录
    assert "build:" not in content
    assert "ONTO_AUTHZ_URL=http://onto-modeling:8100/api/internal/hub/authorize" in content


@pytest.mark.parametrize("compose_path", ONTO_COMPOSE_FILES, ids=lambda path: path.name)
def test_onto_modeling_compose_no_longer_defines_the_hub(compose_path: Path) -> None:
    content = compose_path.read_text(encoding="utf-8")

    assert not re.search(r"^  knowledge-hub:", content, re.MULTILINE)
    assert "knowledge-hub-data" not in content
    assert "KNOWLEDGE_HUB_IMAGE" not in content
    # onto-modeling 仍按服务名访问独立部署的 Hub
    assert "ONTO_HUB_URL=http://knowledge-hub:8200" in content
    assert "ONTO_HUB_SERVICE_TOKEN=${ONTO_HUB_SERVICE_TOKEN:?" in content


def test_onto_modeling_external_auth_override_does_not_reference_the_hub() -> None:
    script = (REPO_ROOT / "modeling" / "onto-modeling" / "deploy" / "deploy.sh").read_text(
        encoding="utf-8"
    )
    override = script.split("cat > docker-compose.external-auth.yml <<EOF2", 1)[1].split("EOF2", 1)[0]

    assert "knowledge-hub" not in override
    # 旧版本随 onto-modeling 部署的 Hub 容器作为孤儿被清理
    assert "up -d --remove-orphans" in script


# ---------- deploy-knowledge-hub.sh ----------

_HUB_DOCKER_STUB = """\
printf '%s\\n' "$*" >> "$DOCKER_CALLS_FILE"
case "$1" in
    network) [ "$2" = inspect ] && [ "$3" = "${STUB_NETWORK:-}" ]; exit $? ;;
    save) : > "$3" ;;
esac
exit 0\
"""


def _hub_source_layout(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    shutil.copy2(DEPLOY_ROOT / "deploy-knowledge-hub.sh", deploy_dir / "deploy-knowledge-hub.sh")
    shutil.copy2(HUB_COMPOSE, deploy_dir / HUB_COMPOSE.name)
    (tmp_path / "knowledge-hub").mkdir()
    _write_executable(tmp_path / "bin" / "docker", _HUB_DOCKER_STUB)
    env = {
        **{key: value for key, value in os.environ.items()
           if key not in ("HUB_SERVICE_TOKEN", "CONSUMER_NETWORK", "KNOWLEDGE_HUB_IMAGE")},
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "DOCKER_CALLS_FILE": str(tmp_path / "docker-calls.txt"),
        "STUB_NETWORK": "shared-net",
    }
    return deploy_dir, env


def test_hub_deploy_builds_from_source_and_generates_a_private_token(tmp_path: Path) -> None:
    deploy_dir, env = _hub_source_layout(tmp_path)
    script = str(deploy_dir / "deploy-knowledge-hub.sh")

    result = _run([script], {**env, "CONSUMER_NETWORK": "shared-net"})

    assert result.returncode == 0, result.stdout + result.stderr
    env_file = deploy_dir / ".env"
    match = re.search(r"^HUB_SERVICE_TOKEN=([0-9a-f]{64})$", env_file.read_text(), re.MULTILINE)
    assert match is not None
    assert match.group(1) not in result.stdout + result.stderr
    assert env_file.stat().st_mode & 0o777 == 0o600
    calls = _lines(tmp_path / "docker-calls.txt")
    assert "build -t knowledge-hub:latest ." in calls
    assert "compose --env-file .env -f docker-compose.knowledge-hub.yml up -d" in calls
    assert calls.index("build -t knowledge-hub:latest .") < calls.index(
        "compose --env-file .env -f docker-compose.knowledge-hub.yml up -d"
    )
    assert any(call.startswith("exec knowledge-hub python") for call in calls)

    # 重跑沿用已生成的凭据
    rerun = _run([script], {**env, "CONSUMER_NETWORK": "shared-net"})
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert f"HUB_SERVICE_TOKEN={match.group(1)}" in env_file.read_text()


def test_hub_deploy_prefers_the_injected_token(tmp_path: Path) -> None:
    deploy_dir, env = _hub_source_layout(tmp_path)
    (deploy_dir / ".env").write_text(f"HUB_SERVICE_TOKEN={secrets.token_hex(32)}\n")
    injected = secrets.token_hex(32)

    result = _run(
        [str(deploy_dir / "deploy-knowledge-hub.sh")],
        {**env, "CONSUMER_NETWORK": "shared-net", "HUB_SERVICE_TOKEN": injected},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _lines(deploy_dir / ".env") == [f"HUB_SERVICE_TOKEN={injected}"]


def test_hub_deploy_rejects_a_short_token(tmp_path: Path) -> None:
    deploy_dir, env = _hub_source_layout(tmp_path)

    result = _run(
        [str(deploy_dir / "deploy-knowledge-hub.sh")],
        {**env, "CONSUMER_NETWORK": "shared-net", "HUB_SERVICE_TOKEN": "too-short"},
    )

    assert result.returncode != 0
    assert "至少需要 32 个字符" in result.stderr
    assert not any(call.startswith(("build", "compose --env-file")) for call in _lines(
        tmp_path / "docker-calls.txt"
    ))


@pytest.mark.parametrize("network", ["", "missing-net"])
def test_hub_deploy_requires_an_existing_shared_network(tmp_path: Path, network: str) -> None:
    deploy_dir, env = _hub_source_layout(tmp_path)

    result = _run([str(deploy_dir / "deploy-knowledge-hub.sh")], {**env, "CONSUMER_NETWORK": network})

    assert result.returncode != 0
    assert ("未指定共享 Docker 网络" if not network else "Docker 网络不存在") in result.stderr
    assert not any(call.startswith(("build", "compose --env-file")) for call in _lines(
        tmp_path / "docker-calls.txt"
    ))


def test_hub_deploy_from_package_loads_only_the_release_image(tmp_path: Path) -> None:
    bundle = tmp_path / "knowledge-hub"
    (bundle / "images").mkdir(parents=True)
    shutil.copy2(DEPLOY_ROOT / "deploy-knowledge-hub.sh", bundle / "deploy.sh")
    shutil.copy2(HUB_COMPOSE, bundle / HUB_COMPOSE.name)
    (bundle / "images.env").write_text(
        "KNOWLEDGE_HUB_IMAGE=knowledge-hub:v-test\nRELEASE_VERSION=v-test\nRELEASE_ARCH=amd64\n"
    )
    for version in ("v-test", "v-old"):
        (bundle / "images" / f"knowledge-hub-{version}.tar").write_bytes(b"")
    _write_executable(tmp_path / "bin" / "docker", _HUB_DOCKER_STUB)
    env = {
        **{key: value for key, value in os.environ.items()
           if key not in ("HUB_SERVICE_TOKEN", "CONSUMER_NETWORK", "KNOWLEDGE_HUB_IMAGE")},
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "DOCKER_CALLS_FILE": str(tmp_path / "docker-calls.txt"),
        "STUB_NETWORK": "shared-net",
    }
    # 离线包场景：网络由 .env 显式配置
    (bundle / ".env").write_text("CONSUMER_NETWORK=shared-net\n")

    result = _run([str(bundle / "deploy.sh")], env)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = _lines(tmp_path / "docker-calls.txt")
    assert "load -i images/knowledge-hub-v-test.tar" in calls
    assert "load -i images/knowledge-hub-v-old.tar" not in calls
    assert not any(call.startswith("build") for call in calls)
    assert "compose --env-file .env --env-file images.env -f docker-compose.knowledge-hub.yml up -d" in calls


# ---------- deploy.sh 整栈编排 ----------

_STACK_DOCKER_STUB = """\
if [ "$1" = "inspect" ] && [ "$2" = "--type" ] && [ "$3" = "container" ]; then exit 0; fi
if [ "$1" = "container" ] && [ "$2" = "inspect" ]; then printf 'onto-agent_default\\n'; exit 0; fi
if [ "$1" = "ps" ]; then printf 'onto-agent-backend\\n'; exit 0; fi
if [ "$1" = "network" ] && [ "$2" = "inspect" ]; then [ "$3" = "onto-agent_default" ]; exit $?; fi
if [ "$1" = "compose" ]; then printf 'onto-compose %s\\n' "$*" >> "$CALLS_FILE"; fi
exit 0\
"""

# Hub 部署替身：模拟真实脚本的凭据优先级（注入 > .env > 生成）并记录收到的网络
_HUB_DEPLOY_STUB = """\
printf 'hub\\n' >> "$CALLS_FILE"
cd "$(dirname "$0")"
printf 'CONSUMER_NETWORK=%s\\nRECEIVED_TOKEN=%s\\n' "${CONSUMER_NETWORK:-}" "${HUB_SERVICE_TOKEN:-}" > "$HUB_SNAPSHOT"
token="${HUB_SERVICE_TOKEN:-$(sed -n 's/^HUB_SERVICE_TOKEN=//p' .env 2>/dev/null || true)}"
printf 'HUB_SERVICE_TOKEN=%s\\n' "${token:-$STUB_HUB_TOKEN}" > .env\
"""


def _stack_env(tmp_path: Path) -> dict[str, str]:
    config = tmp_path / "evaluation.config"
    config.write_text("# Synthetic configuration; no credentials.\n", encoding="utf-8")
    _write_executable(tmp_path / "bin" / "docker", _STACK_DOCKER_STUB)
    _write_executable(
        tmp_path / "eval-service" / "deploy" / "deploy.sh", 'printf "evaluation\\n" >> "$CALLS_FILE"'
    )
    return {
        **{key: value for key, value in os.environ.items()
           if key not in ("HUB_SERVICE_TOKEN", "CONSUMER_NETWORK")},
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "EVAL_ENV_FILE": str(config),
        "CALLS_FILE": str(tmp_path / "calls.txt"),
        "HUB_SNAPSHOT": str(tmp_path / "hub-snapshot.txt"),
        "STUB_HUB_TOKEN": secrets.token_hex(32),
        "ONTO_AGENT_INSTALL_DIR": str(tmp_path / "installation"),
        "MODELING_SERVICE_TOKEN": secrets.token_hex(32),
        "MODELING_MCP_TOKEN_SECRET": secrets.token_hex(32),
    }


def _source_checkout(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    shutil.copy2(DEPLOY_ROOT / "deploy.sh", deploy_dir / "deploy.sh")
    _write_executable(deploy_dir / "deploy-stack.sh", 'printf "stack\\n" >> "$CALLS_FILE"')
    _write_executable(deploy_dir / "deploy-knowledge-hub.sh", _HUB_DEPLOY_STUB)
    (tmp_path / "knowledge-hub").mkdir()
    (tmp_path / "modeling" / "onto-modeling").mkdir(parents=True)
    return deploy_dir, _stack_env(tmp_path)


_ONTO_SOURCE_UP = (
    "onto-compose compose -f docker-compose.yml -f docker-compose.stack.yml up -d --build --remove-orphans"
)


def test_source_full_stack_deploys_hub_before_onto_modeling_and_aligns_the_token(
    tmp_path: Path,
) -> None:
    deploy_dir, env = _source_checkout(tmp_path)

    result = _run([str(deploy_dir / "deploy.sh")], env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _lines(tmp_path / "calls.txt") == ["stack", "hub", _ONTO_SOURCE_UP, "evaluation"]
    # Hub 收到探测到的 consumer 网络；Onto 没有旧凭据时由 Hub 生成，再对齐到 Onto
    assert "CONSUMER_NETWORK=onto-agent_default" in _lines(tmp_path / "hub-snapshot.txt")
    onto_env = tmp_path / "modeling" / "onto-modeling" / ".env"
    assert f"ONTO_HUB_SERVICE_TOKEN={env['STUB_HUB_TOKEN']}" in _lines(onto_env)
    assert onto_env.stat().st_mode & 0o777 == 0o600
    override = (tmp_path / "modeling" / "onto-modeling" / "docker-compose.stack.yml").read_text()
    assert "knowledge-hub" not in override.split("services:", 1)[1]
    assert env["STUB_HUB_TOKEN"] not in result.stdout + result.stderr


def test_source_hub_reuses_the_existing_onto_modeling_token(tmp_path: Path) -> None:
    deploy_dir, env = _source_checkout(tmp_path)
    existing = secrets.token_hex(32)
    onto_env = tmp_path / "modeling" / "onto-modeling" / ".env"
    onto_env.write_text(f"ONTO_WEB_PORT=5100\nONTO_HUB_SERVICE_TOKEN={existing}\n")

    result = _run([str(deploy_dir / "deploy.sh")], env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"RECEIVED_TOKEN={existing}" in _lines(tmp_path / "hub-snapshot.txt")
    assert f"HUB_SERVICE_TOKEN={existing}" in _lines(deploy_dir / ".env")
    assert _lines(onto_env) == ["ONTO_WEB_PORT=5100", f"ONTO_HUB_SERVICE_TOKEN={existing}"]


def test_knowledge_hub_only_touches_nothing_else(tmp_path: Path) -> None:
    deploy_dir, env = _source_checkout(tmp_path)
    env.pop("EVAL_ENV_FILE")

    result = _run([str(deploy_dir / "deploy.sh"), "--knowledge-hub-only"], env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _lines(tmp_path / "calls.txt") == ["hub"]
    assert not (tmp_path / "modeling" / "onto-modeling" / ".env").exists()


def test_onto_modeling_only_does_not_redeploy_the_hub(tmp_path: Path) -> None:
    deploy_dir, env = _source_checkout(tmp_path)
    env.pop("EVAL_ENV_FILE")
    hub_token = secrets.token_hex(32)
    (deploy_dir / ".env").write_text(f"HUB_SERVICE_TOKEN={hub_token}\n")

    result = _run([str(deploy_dir / "deploy.sh"), "--onto-modeling-only"], env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _lines(tmp_path / "calls.txt") == [_ONTO_SOURCE_UP]
    # 仍对齐到已部署 Hub 的凭据
    assert f"ONTO_HUB_SERVICE_TOKEN={hub_token}" in _lines(
        tmp_path / "modeling" / "onto-modeling" / ".env"
    )


def test_only_flags_cannot_be_combined(tmp_path: Path) -> None:
    deploy_dir, env = _source_checkout(tmp_path)

    result = _run([str(deploy_dir / "deploy.sh"), "--knowledge-hub-only", "--onto-modeling-only"], env)

    assert result.returncode != 0
    assert "cannot be combined" in result.stderr
    assert _lines(tmp_path / "calls.txt") == []


def _release_bundle(tmp_path: Path, *, with_hub: bool = True) -> tuple[Path, dict[str, str]]:
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    shutil.copy2(DEPLOY_ROOT / "deploy.sh", deploy_dir / "deploy.sh")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / ".env.example").write_text("# Synthetic configuration\n")
    _write_executable(deploy_dir / "deploy-consumer.sh", 'printf "consumer\\n" >> "$CALLS_FILE"')
    _write_executable(deploy_dir / "modeling" / "deploy.sh", 'printf "modeling\\n" >> "$CALLS_FILE"')
    if with_hub:
        _write_executable(tmp_path / "knowledge-hub" / "deploy.sh", _HUB_DEPLOY_STUB)
    onto_dir = tmp_path / "onto-modeling"
    _write_executable(
        onto_dir / "deploy.sh",
        'printf "onto\\n" >> "$CALLS_FILE"\ncp "$(dirname "$0")/.env" "$ONTO_ENV_SNAPSHOT"',
    )
    (onto_dir / ".env.example").write_text("ONTO_WEB_PORT=5100\n")
    env = _stack_env(tmp_path)
    env["ONTO_ENV_SNAPSHOT"] = str(tmp_path / "onto-env-snapshot.txt")
    return deploy_dir, env


def test_release_deploys_hub_as_its_own_bundle_before_onto_modeling(tmp_path: Path) -> None:
    deploy_dir, env = _release_bundle(tmp_path)

    result = _run([str(deploy_dir / "deploy.sh")], env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _lines(tmp_path / "calls.txt") == ["consumer", "modeling", "hub", "onto", "evaluation"]
    assert "CONSUMER_NETWORK=onto-agent_default" in _lines(tmp_path / "hub-snapshot.txt")
    onto_env = _lines(tmp_path / "onto-env-snapshot.txt")
    # .env.example 先落地，再对齐 Hub 凭据，Onto 原有默认值不丢
    assert "ONTO_WEB_PORT=5100" in onto_env
    assert f"ONTO_HUB_SERVICE_TOKEN={env['STUB_HUB_TOKEN']}" in onto_env


def test_release_knowledge_hub_only_requires_the_hub_bundle(tmp_path: Path) -> None:
    deploy_dir, env = _release_bundle(tmp_path, with_hub=False)

    result = _run([str(deploy_dir / "deploy.sh"), "--knowledge-hub-only"], env)

    assert result.returncode != 0
    assert "Knowledge Hub deploy script not found" in result.stderr
    assert _lines(tmp_path / "calls.txt") == []


def test_release_without_hub_bundle_still_deploys_onto_modeling(tmp_path: Path) -> None:
    deploy_dir, env = _release_bundle(tmp_path, with_hub=False)

    result = _run([str(deploy_dir / "deploy.sh")], env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Knowledge Hub bundle not present; skipping" in result.stdout
    assert _lines(tmp_path / "calls.txt") == ["consumer", "modeling", "onto", "evaluation"]


# ---------- 离线打包 ----------


def test_hub_build_produces_a_standalone_package(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "knowledge-hub").mkdir(parents=True)
    (repo / "deploy").mkdir()
    shutil.copy2(REPO_ROOT / "knowledge-hub" / "build.sh", repo / "knowledge-hub" / "build.sh")
    (repo / "knowledge-hub" / "README.md").write_text("# Synthetic README\n")
    shutil.copy2(DEPLOY_ROOT / "deploy-knowledge-hub.sh", repo / "deploy" / "deploy-knowledge-hub.sh")
    shutil.copy2(HUB_COMPOSE, repo / "deploy" / HUB_COMPOSE.name)
    _write_executable(tmp_path / "bin" / "docker", _HUB_DOCKER_STUB)
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "DOCKER_CALLS_FILE": str(tmp_path / "docker-calls.txt"),
    }

    result = _run([str(repo / "knowledge-hub" / "build.sh"), "v-test", "amd"], env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert any("-t knowledge-hub:v-test ." in call for call in _lines(tmp_path / "docker-calls.txt"))
    archive = repo / "knowledge-hub" / "dist" / "knowledge-hub-v-test-linux-amd64.tar.gz"
    with tarfile.open(archive, "r:gz") as package:  # NOSONAR(S5042): synthetic stub archive, metadata only
        members = {member.name: member for member in package.getmembers()}
        assert members["knowledge-hub/deploy.sh"].mode & 0o111
        assert "knowledge-hub/docker-compose.knowledge-hub.yml" in members
        assert "knowledge-hub/images/knowledge-hub-v-test.tar" in members
        images_env = package.extractfile("knowledge-hub/images.env").read().decode()
    assert "KNOWLEDGE_HUB_IMAGE=knowledge-hub:v-test" in images_env
    assert "RELEASE_VERSION=v-test" in images_env
    assert not (repo / "knowledge-hub" / "dist" / "release-v-test-amd64").exists()


def test_hub_build_rejects_unsafe_version(tmp_path: Path) -> None:
    result = _run([str(REPO_ROOT / "knowledge-hub" / "build.sh"), "../escape"], dict(os.environ))

    assert result.returncode != 0
    assert "Invalid version" in result.stderr


def test_full_release_bundles_the_hub_package_and_lists_it_in_the_manifest() -> None:
    build = (DEPLOY_ROOT / "build.sh").read_text(encoding="utf-8")

    assert 'bash "${REPO_ROOT}/knowledge-hub/build.sh" "${VERSION}" "${ARCH}"' in build
    assert '"knowledge_hub_bundle_dir": "knowledge-hub"' in build
    assert "knowledge-hub/images/*.tar" in build
