"""Build-contract tests for the Onto-owned JiuwenBox image."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
JIUWENSWARM_COMMIT = "d7c2ab2f299cd2c73201afb41ebb8b9c00cc7015"


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_jiuwenbox_build_is_pinned_to_commit_and_accepts_release_arch():
    script = _read("deploy/build_jiuwenbox.sh")
    release_script = _read("deploy/build_release.sh")

    assert f'JIUWENSWARM_COMMIT="{JIUWENSWARM_COMMIT}"' in script
    assert 'fetch --depth=1 origin "${JIUWENSWARM_COMMIT}"' in script
    assert 'if [ "${ACTUAL_COMMIT}" != "${JIUWENSWARM_COMMIT}" ]' in script
    assert 'Usage: $(basename "$0") <image-tag> [amd|arm]' in script
    assert 'BUILD_CMD="docker buildx build --platform ${PLATFORM} --load"' in script
    assert '"onto-agent-jiuwenbox:${VERSION}" "${ARCH}"' in release_script


def test_jiuwenbox_overlay_contains_shared_capabilities_and_runtime_dependencies():
    script = _read("deploy/build_jiuwenbox.sh")
    dockerfile = _read("deploy/jiuwenbox/Dockerfile")
    requirements = _read("deploy/jiuwenbox/requirements.txt")

    assert "requests" in requirements
    assert "COPY runtime/ /app/sandbox-runtime/python/" in dockerfile
    assert 'backend/shared_capabilities' in script
    assert 'sdk/sandbox-python-sdk' not in script
    assert "from shared_capabilities.business_data import ProviderResult" in dockerfile
    assert "from shared_capabilities.ontology_script_sdk import DataProviderClient" in dockerfile


def test_backend_and_compose_do_not_package_or_mount_sandbox_runtime():
    backend_dockerfile = _read("backend/Dockerfile")
    compose_files = [
        _read("docker-compose.yml"),
        _read("backend/docker-compose.yml"),
        _read("deploy/docker-compose.release.yml"),
    ]

    assert "COPY sdk/sandbox-python-sdk" not in backend_dockerfile
    assert all("onto-sandbox-runtime" not in compose for compose in compose_files)
    assert all("/app/sandbox-runtime:ro" not in compose for compose in compose_files)
    removed_backend_env = {
        "JIUWENBOX_URL",
        "SANDBOX_PYTHON_BIN",
        "SANDBOX_RUNTIME_ROOT",
        "SANDBOX_DATA_PROXY_BASE_URL",
    }
    assert all(
        env_name not in compose
        for compose in compose_files
        for env_name in removed_backend_env
    )


def test_sandbox_settings_come_from_backend_env_file_in_supported_compose_files():
    root_compose = _read("docker-compose.yml")
    release_compose = _read("deploy/docker-compose.release.yml")
    backend_only_compose = _read("backend/docker-compose.yml")

    for compose in (root_compose, release_compose):
        assert "env_file:\n      - ./backend/.env" in compose
        assert "- SANDBOX_ENABLED=true" in compose
        assert "- SANDBOX_FALLBACK_LOCAL=false" in compose
        assert "- SANDBOX_SECRET_ALLOW_PLAIN_HTTP=false" in compose

    assert "env_file:\n      - ./.env" in backend_only_compose
    assert "- SANDBOX_ENABLED=false" in backend_only_compose
    assert "- SANDBOX_FALLBACK_LOCAL=true" in backend_only_compose
    assert "- SANDBOX_SECRET_ALLOW_PLAIN_HTTP=false" in backend_only_compose


def test_release_bundle_uses_release_specific_backend_env_template():
    release_script = _read("deploy/build_release.sh")
    release_env_template = _read("backend/.env.release.example")

    assert 'backend/.env.release.example' in release_script
    assert "SANDBOX_ENABLED=true" in release_env_template
    assert "SANDBOX_FALLBACK_LOCAL=false" in release_env_template
    assert "SANDBOX_SECRET_ALLOW_PLAIN_HTTP=false" in release_env_template
