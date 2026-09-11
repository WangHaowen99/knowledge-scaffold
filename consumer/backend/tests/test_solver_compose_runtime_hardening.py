from __future__ import annotations

from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _solver_services(relative_path: str) -> dict:
    payload = yaml.safe_load((REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8"))
    return payload["services"]


def test_development_solver_overlay_uses_one_private_http_solver_service():
    services = _solver_services("consumer/docker-compose.solver.yml")

    assert set(services) == {"ortools-solver", "backend"}
    assert services["ortools-solver"]["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=16m"]
    assert "ports" not in services["ortools-solver"]
    backend = services["backend"]
    assert "SOLVER_SERVICE_ALLOW_HTTP=true" in backend["environment"]
    assert "SOLVER_SERVICE_ALLOWED_HOSTS=ortools-solver" in backend["environment"]
    assert "SOLVER_SERVICE_ALLOWED_PORTS=8080" in backend["environment"]
    assert "SOLVER_SERVICE_TLS_CA_FILE" not in str(backend)
    assert "SOLVER_CONNECTOR_" not in str(services)
    assert "ORTOOLS_CONNECTOR_" not in str(services)


def test_release_solver_overlay_uses_one_private_http_solver_service():
    services = _solver_services("consumer/deploy/docker-compose.solver.release.yml")

    assert set(services) == {"ortools-solver", "backend"}
    assert services["ortools-solver"]["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=16m"]
    assert "ports" not in services["ortools-solver"]
    backend = services["backend"]
    assert "SOLVER_SERVICE_ALLOW_HTTP=true" in backend["environment"]
    assert "SOLVER_SERVICE_ALLOWED_HOSTS=ortools-solver" in backend["environment"]
    assert "SOLVER_SERVICE_ALLOWED_PORTS=8080" in backend["environment"]
    assert "SOLVER_SERVICE_TLS_CA_FILE" not in str(backend)
    assert "SOLVER_CONNECTOR_" not in str(services)
    assert "ORTOOLS_CONNECTOR_" not in str(services)
