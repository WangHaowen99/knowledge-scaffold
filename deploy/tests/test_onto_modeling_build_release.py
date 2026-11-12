import gzip
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ONTO_MODELING_ROOT = REPO_ROOT / "modeling" / "onto-modeling"
DEFAULT_PIP_INDEX_URL = "https://repo.huaweicloud.com/repository/pypi/simple"


def _bash_executable() -> str:
    bash_path = shutil.which("bash")
    if bash_path is None:
        pytest.skip("Bash is required for deployment script tests")
    return str(Path(bash_path).resolve())


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/bash\nset -euo pipefail\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run_build_release(
    tmp_path: Path, host_pip_python: str, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """在临时仓库副本里跑 build_release.sh，docker / pip3 换成只记录参数的桩。"""
    project_root = tmp_path / "repo" / "modeling" / "onto-modeling"
    shutil.copytree(ONTO_MODELING_ROOT / "deploy", project_root / "deploy")
    shutil.copy2(ONTO_MODELING_ROOT / ".env.example", project_root / ".env.example")
    (project_root / "backend").mkdir()
    shutil.copy2(
        ONTO_MODELING_ROOT / "backend" / "requirements.txt",
        project_root / "backend" / "requirements.txt",
    )
    # drawio 已供应则脚本不走 npm
    drawio_index = project_root / "vendor" / "drawio" / "webapp" / "index.html"
    drawio_index.parent.mkdir(parents=True)
    drawio_index.write_text("<html></html>\n", encoding="utf-8")
    (tmp_path / "repo" / "auth-server").mkdir()
    # REA 旁路容器在 connectors/rea-sidecar 下自带 Dockerfile，脚本 cd 进去构建
    (project_root / "connectors" / "rea-sidecar").mkdir(parents=True)

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    fake_bin = tmp_path / "bin"
    _write_executable(
        fake_bin / "docker",
        """
printf '%s\\n' "$*" >> "${FAKE_LOG_DIR}/docker.log"
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
    _write_executable(
        fake_bin / "pip3",
        """
if [ "${1:-}" = "--version" ]; then
  if [ -n "${FAKE_PIP_PYTHON}" ]; then
    echo "pip 24.0 from /fake/site-packages/pip (python ${FAKE_PIP_PYTHON})"
  else
    echo "pip 24.0 from /fake/site-packages/pip"
  fi
  exit 0
fi
printf '%s\\n' "$*" >> "${FAKE_LOG_DIR}/pip3.log"
""".strip(),
    )

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"ONTO_MODELING_PIP_INDEX_URL", "PIP_INDEX_URL"}
    }
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG_DIR": str(log_dir),
            "FAKE_PIP_PYTHON": host_pip_python,
            **(extra_env or {}),
        }
    )
    # Security review: argv is the resolved bash binary plus the repository's own build
    # script copied into tmp_path, with literal arguments; it is passed as a sequence and
    # never parsed by a shell.
    result = subprocess.run(  # NOSONAR(S4721)
        [_bash_executable(), str(project_root / "deploy" / "build_release.sh"), "v-test", "amd"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        shell=False,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (project_root / "dist" / "onto-modeling-v-test-linux-amd64.tar.gz").is_file()
    _assert_packaged_deploy_scripts(project_root / "dist" / "onto-modeling-v-test-linux-amd64.tar.gz")
    return result, log_dir


def _assert_packaged_deploy_scripts(archive: Path, max_bytes: int = 8 * 1024 * 1024) -> None:
    # This fixture contains empty image stubs. Bound decompression before parsing
    # tar headers, and inspect metadata only; never extract files to disk.
    assert archive.stat().st_size <= max_bytes, "Compressed test package exceeds size limit"
    with gzip.open(archive, "rb") as compressed:
        payload = compressed.read(max_bytes + 1)
    assert len(payload) <= max_bytes, "Expanded test package exceeds size limit"
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as package:  # NOSONAR(S5042): bounded metadata-only read
        members = package.getmembers()
        assert len(members) <= 512, "Test package contains too many members"
        assert all(0 <= member.size <= max_bytes for member in members), "Invalid test member size"
        deploy = package.getmember("onto-modeling/deploy.sh")
        assert deploy.isfile()
        assert deploy.mode & 0o111
        # Modeling 关联随评测一起移出 onto-modeling（ADR-0020），关联脚本不再打包
        assert "onto-modeling/configure-modeling-association.sh" not in package.getnames()


def test_package_metadata_check_rejects_excessive_expansion(tmp_path: Path) -> None:
    archive = tmp_path / "synthetic-package.tar.gz"
    with gzip.open(archive, "wb") as compressed:
        compressed.write(b"0" * 2048)
    with pytest.raises(AssertionError, match="Expanded test package exceeds size limit"):
        _assert_packaged_deploy_scripts(archive, max_bytes=1024)


def _onto_modeling_build_command(log_dir: Path) -> str:
    commands = (log_dir / "docker.log").read_text(encoding="utf-8").splitlines()
    builds = [line for line in commands if "-t onto-modeling:v-test " in line]
    assert len(builds) == 1, commands
    return builds[0]


@pytest.mark.parametrize("host_pip_python", ["3.10", "3.9", ""])
def test_build_release_skips_wheel_vendoring_when_host_python_is_older_than_image(
    tmp_path: Path, host_pip_python: str
) -> None:
    result, log_dir = _run_build_release(tmp_path, host_pip_python)

    assert not (log_dir / "pip3.log").exists()
    assert "skip wheel vendoring" in result.stdout
    assert (
        f"--build-arg PIP_INDEX_URL={DEFAULT_PIP_INDEX_URL}"
        in _onto_modeling_build_command(log_dir)
    )


@pytest.mark.parametrize("host_pip_python", ["3.11", "3.12"])
def test_build_release_vendors_wheels_from_overridable_index(
    tmp_path: Path, host_pip_python: str
) -> None:
    index_url = "https://pypi.example.com/simple"
    _, log_dir = _run_build_release(
        tmp_path, host_pip_python, {"ONTO_MODELING_PIP_INDEX_URL": index_url}
    )

    pip_download = (log_dir / "pip3.log").read_text(encoding="utf-8")
    assert pip_download.startswith("download ")
    assert f"-i {index_url}" in pip_download
    assert "--python-version 3.11" in pip_download
    assert "--abi cp311" in pip_download
    assert f"--build-arg PIP_INDEX_URL={index_url}" in _onto_modeling_build_command(log_dir)


def test_build_release_bundles_the_gateway_and_sidecar_images(tmp_path: Path) -> None:
    """离线包带齐 compose 里的四个镜像，images.env 给出各自的 tag，目标机 deploy.sh 才能全部加载并拉起。"""
    _, log_dir = _run_build_release(tmp_path, "")

    saved = [line for line in (log_dir / "docker.log").read_text(encoding="utf-8").splitlines()
             if line.startswith("save ")]
    for image in ("onto-modeling", "onto-modeling-auth-server", "onto-modeling-connector-gateway",
                  "onto-modeling-rea-sidecar"):
        assert any(line.endswith(f" {image}:v-test") for line in saved), image
    package_path = tmp_path / "repo" / "modeling" / "onto-modeling" / "dist" / "onto-modeling-v-test-linux-amd64.tar.gz"
    with tarfile.open(package_path) as package:  # NOSONAR(S5042) 合成的桩包，只读清单，不展开
        images_env = package.extractfile("onto-modeling/images.env").read().decode()
        names = set(package.getnames())
    assert "CONNECTOR_GATEWAY_IMAGE=onto-modeling-connector-gateway:v-test" in images_env
    assert "REA_SIDECAR_IMAGE=onto-modeling-rea-sidecar:v-test" in images_env
    assert "onto-modeling/images/onto-modeling-rea-sidecar-v-test.tar" in names


def test_build_release_no_longer_bundles_the_knowledge_hub(tmp_path: Path) -> None:
    """Hub 是独立交付包（knowledge-hub/build.sh），onto-modeling 包不再构建或携带其镜像。"""
    _, log_dir = _run_build_release(tmp_path, "")

    assert "knowledge-hub" not in (log_dir / "docker.log").read_text(encoding="utf-8")


def test_onto_modeling_dockerfile_takes_pip_index_from_build_arg() -> None:
    dockerfile = (ONTO_MODELING_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert f"ARG PIP_INDEX_URL={DEFAULT_PIP_INDEX_URL}" in dockerfile
    assert '-i "${PIP_INDEX_URL}"' in dockerfile
    assert "pypi.tuna.tsinghua.edu.cn" not in dockerfile
    # 改源只应失效 pip 层，不能连带 apt 层重跑
    assert dockerfile.index("apt-get install") < dockerfile.index("ARG PIP_INDEX_URL=")
