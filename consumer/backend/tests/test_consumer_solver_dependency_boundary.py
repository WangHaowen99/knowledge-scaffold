import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND.parents[1]


def test_consumer_production_has_no_concrete_solver_dependency_or_worker():
    forbidden_files = {
        "solver_coordination/backends/ortools_cp_sat.py",
        "solver_coordination/backends/local_worker.py",
        "solver_coordination/backends/local_process.py",
        "solver_coordination/backends/windows_job.py",
    }
    assert not any((BACKEND / relative).exists() for relative in forbidden_files)
    requirements = (BACKEND / "requirements.txt").read_text(encoding="utf-8")
    assert "ortools" not in requirements.casefold()


def test_runtime_assembly_uses_consumer_connector_and_http_transport():
    source = (BACKEND / "integration" / "solver_coordination_runtime.py").read_text(encoding="utf-8")
    assert "LocalProcessSolverBackend" not in source
    assert "solver_coordination.backends" not in source
    assert "OrToolsConnector" in source
    assert "HttpSolverTransport" in source
    assert "GenericSolverClient" not in source
    assert "GenericSolverBackend" not in source


def test_all_consumer_production_python_avoids_concrete_solver_and_worker_imports():
    forbidden = (
        "import ortools",
        "from ortools",
        "from connectors.ortools",
        "from solvers.ortools",
        "import solvers.ortools",
        "solver_coordination.backends",
        "LocalProcessSolverBackend",
    )
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in source:
                offenders.append((path.relative_to(BACKEND).as_posix(), token))
    assert offenders == []


def test_backend_image_packages_standalone_solver_contract_from_root_context():
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    compose = (BACKEND.parent / "docker-compose.yml").read_text(encoding="utf-8")
    release_build = (BACKEND.parent / "deploy" / "build_release.sh").read_text(encoding="utf-8")

    assert "COPY consumer/backend/requirements.txt ./requirements.txt" in dockerfile
    assert "COPY consumer/backend/ ./" in dockerfile
    assert "COPY contracts/solver_services/ /app/contracts/solver_services/" in dockerfile
    assert "COPY contracts/solver/" not in dockerfile
    assert "COPY requirements.txt" not in dockerfile
    assert "COPY . ." not in dockerfile
    assert "COPY connectors" not in dockerfile
    assert "context: .." in compose
    assert "dockerfile: consumer/backend/Dockerfile" in compose
    assert '"${REPOSITORY_ROOT}"' in release_build
    assert '-f "${PROJECT_ROOT}/backend/Dockerfile"' in release_build


def test_root_dockerignore_keeps_contracts_but_excludes_local_secrets():
    dockerignore = (BACKEND.parents[1] / ".dockerignore").read_text(encoding="utf-8")

    assert "contracts/solver_services" not in dockerignore
    assert "**/.env" in dockerignore
    assert "**/.onto-runtime/" in dockerignore


def test_independent_solver_image_copies_only_solver_and_contract_sources():
    ortools = (REPOSITORY_ROOT / "solvers" / "ortools" / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY solvers/ortools/" in ortools
    assert "COPY contracts/solver_services/" in ortools
    assert "COPY contracts /" not in ortools
    assert "COPY consumer" not in ortools
    assert "COPY connectors" not in ortools


def test_direct_switch_has_no_standalone_connector_or_tls_sidecar_sources():
    connector_root = REPOSITORY_ROOT / "connectors"
    assert not any(connector_root.rglob("*.py"))
    assert not any(connector_root.rglob("Dockerfile*"))

    tls_proxy_root = REPOSITORY_ROOT / "solvers" / "ortools" / "tls-proxy"
    assert not any(tls_proxy_root.rglob("Dockerfile*"))
    assert not (tls_proxy_root / "nginx.conf").exists()


def test_ortools_connector_imports_when_started_from_backend_directory():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from solver_coordination.connectors.ortools import API_VERSION; print(API_VERSION)",
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ortools.solver.v1"
