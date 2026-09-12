"""Build-contract tests for the Onto-owned JiuwenBox image."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
JIUWENSWARM_COMMIT = "d7c2ab2f299cd2c73201afb41ebb8b9c00cc7015"


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_jiuwenbox_build_is_pinned_to_commit_and_accepts_release_arch():
    script = _read("deploy/build_jiuwenbox.sh")
    release_script = _read("deploy/build_release.sh")
    contract = json.loads(_read("backend/integration/sandbox/runtime-contract.json"))

    assert contract["upstream"]["commit"] == JIUWENSWARM_COMMIT
    assert 'RUNTIME_CONTRACT="${PROJECT_ROOT}/backend/integration/sandbox/runtime-contract.json"' in script
    assert 'JIUWENSWARM_COMMIT="$(python3 -c' in script
    assert 'fetch --depth=1 origin "${JIUWENSWARM_COMMIT}"' in script
    assert 'if [ "${ACTUAL_COMMIT}" != "${JIUWENSWARM_COMMIT}" ]' in script
    assert 'Usage: $(basename "$0") <image-tag> [amd|arm]' in script
    assert 'BUILD_CMD="docker buildx build --platform ${PLATFORM} --load"' in script
    assert '"onto-agent-jiuwenbox:${VERSION}" "${ARCH}"' in release_script
    assert script.index('JIUWENSWARM_COMMIT="$(python3 -c') < script.index('COMMIT_SHORT="${JIUWENSWARM_COMMIT:0:12}"')


def test_jiuwenbox_overlay_contains_shared_capabilities_and_runtime_dependencies():
    script = _read("deploy/build_jiuwenbox.sh")
    dockerfile = _read("deploy/jiuwenbox/Dockerfile")
    requirements = _read("backend/integration/sandbox/runtime-requirements.txt")
    contract = json.loads(_read("backend/integration/sandbox/runtime-contract.json"))

    assert "requests==" in requirements
    assert "neo4j==" in requirements
    assert "pytz==" in requirements
    assert contract["allowed_external_import_roots"] == []
    assert set(contract["allowed_runtime_imports"]["shared_capabilities.ontology_script_sdk"]) == {
        "DeterministicProviderCallError",
        "DeterministicProviderClient",
        "write_artifact",
    }
    assert "COPY runtime/ /app/sandbox-runtime/python/" in dockerfile
    assert "COPY runtime-contract.json /app/sandbox-runtime/runtime-contract.json" in dockerfile
    assert "backend/shared_capabilities" in script
    assert "-name '__pycache__'" in script
    assert "-name '*.pyc' -delete" in script
    assert 'SANDBOX_RUNTIME_REVISION="$(' in script
    assert "--target runtime-payload" in script
    assert 'PAYLOAD_IMAGE_ID="$(docker image inspect' in script
    assert "--target runtime-attested" in script
    assert "sandbox_runtime_revision(sys.argv[1], sys.argv[2])" in script
    assert '--build-arg "SANDBOX_RUNTIME_REVISION=${SANDBOX_RUNTIME_REVISION}"' in script
    assert 'io.onto.sandbox-runtime.revision="${SANDBOX_RUNTIME_REVISION}"' in dockerfile
    assert "> /app/sandbox-runtime/python/.runtime-revision" in dockerfile
    assert "COPY runtime-entrypoint.sh" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/onto-jiuwenbox-entrypoint"]' in dockerfile
    assert "FROM ${RUNTIME_PAYLOAD_IMAGE} AS runtime-attested" in dockerfile
    assert "import neo4j; import requests" in dockerfile
    assert "backend/onto_sandbox_runtime" in script
    assert "sdk/sandbox-python-sdk" not in script
    assert "from shared_capabilities.business_data import ProviderResult" in dockerfile
    assert "from shared_capabilities.ontology_script_sdk import DataProviderClient" in dockerfile
    assert "DataProviderClient, DeterministicProviderClient" in dockerfile
    assert "import onto_sandbox_runtime.bounded_runner" in dockerfile


def test_jiuwenbox_overlay_preserves_default_server_startup_command():
    dockerfile = _read("deploy/jiuwenbox/Dockerfile")
    entrypoint = 'ENTRYPOINT ["/usr/local/bin/onto-jiuwenbox-entrypoint"]'
    command = 'CMD ["python3", "-m", "jiuwenbox.server.launcher"]'

    assert entrypoint in dockerfile
    assert command in dockerfile
    assert dockerfile.index(entrypoint) < dockerfile.index(command)


def test_backend_and_compose_do_not_package_or_mount_sandbox_runtime():
    backend_dockerfile = _read("backend/Dockerfile")
    compose_files = [
        _read("docker-compose.yml"),
        _read("backend/docker-compose.yml"),
        _read("deploy/docker-compose.release.yml"),
    ]

    assert "COPY sdk/sandbox-python-sdk" not in backend_dockerfile
    assert "python agent/direct_execution/build_revision.py" in backend_dockerfile
    assert "--output /app/.app-runtime-revision" in backend_dockerfile
    build_revision_index = backend_dockerfile.index("python agent/direct_execution/build_revision.py")
    assert backend_dockerfile.index("COPY consumer/backend/ ./") < build_revision_index
    assert backend_dockerfile.index("COPY contracts/solver_services/") < build_revision_index
    assert all("onto-sandbox-runtime" not in compose for compose in compose_files)
    assert all("/app/sandbox-runtime:ro" not in compose for compose in compose_files)
    assert all("/app/config" not in compose for compose in compose_files)
    removed_backend_env = {
        "JIUWENBOX_URL",
        "SANDBOX_PYTHON_BIN",
        "SANDBOX_RUNTIME_ROOT",
        "SANDBOX_DATA_PROXY_BASE_URL",
    }
    assert all(env_name not in compose for compose in compose_files for env_name in removed_backend_env)


def test_sandbox_settings_come_from_backend_env_file_in_supported_compose_files():
    compose_files = [
        _read("docker-compose.yml"),
        _read("deploy/docker-compose.release.yml"),
    ]

    for compose in compose_files:
        assert "env_file:\n      - ./backend/.env" in compose
        assert "SANDBOX_EXECUTION_MODE" not in compose
        assert "SANDBOX_ENABLED" not in compose
        assert "SANDBOX_FALLBACK_LOCAL" not in compose
        assert "SANDBOX_SECRET_ALLOW_PLAIN_HTTP" not in compose


def test_supported_compose_files_share_deterministic_artifact_staging_with_jiuwenbox():
    compose_files = [
        _read("docker-compose.yml"),
        _read("deploy/docker-compose.release.yml"),
    ]
    mount = "onto-deterministic-artifacts:/run/onto-deterministic-artifacts"

    for compose in compose_files:
        assert compose.count(mount) == 2
        assert "DETERMINISTIC_ARTIFACT_STAGING_ROOT=/run/onto-deterministic-artifacts" in compose
        assert "volumes:\n  onto-deterministic-artifacts:" in compose
