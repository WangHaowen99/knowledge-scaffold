# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def _compose(relative_path: str) -> dict:
    return yaml.safe_load(
        (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
    )


def test_development_compose_has_an_isolated_ortools_solver_service():
    base = _compose("consumer/docker-compose.yml")
    compose = _compose("consumer/docker-compose.solver.yml")

    assert "ortools-solver" not in base["services"]
    assert "secrets" not in base

    solver = compose["services"]["ortools-solver"]
    assert solver["build"] == {
        "context": "..",
        "dockerfile": "solvers/ortools/Dockerfile",
    }
    assert "ports" not in solver
    assert solver["profiles"] == ["solver-ortools"]
    assert solver["read_only"] is True
    assert solver["cap_drop"] == ["ALL"]
    assert solver["security_opt"] == ["no-new-privileges:true"]
    assert solver["deploy"]["resources"]["limits"] == {
        "cpus": "2.0",
        "memory": "1536M",
    }
    assert solver["volumes"] == [
        "ortools-solver-state:/var/lib/ortools-solver"
    ]
    assert "env_file" not in solver
    assert solver["networks"] == ["onto-agent-network"]
    assert "onto-jiuwenbox-sock" not in str(solver)
    assert "onto-runtime" not in str(solver)
    assert "ortools-solver-state" in compose["volumes"]


def test_release_compose_uses_a_separate_solver_image_and_state():
    base = _compose("consumer/deploy/docker-compose.release.yml")
    compose = _compose("consumer/deploy/docker-compose.solver.release.yml")

    assert "ortools-solver" not in base["services"]
    assert "secrets" not in base

    solver = compose["services"]["ortools-solver"]
    assert solver["image"] == "${ORTOOLS_SOLVER_IMAGE:?set ORTOOLS_SOLVER_IMAGE}"
    assert solver["profiles"] == ["solver-ortools"]
    assert solver["read_only"] is True
    assert solver["cap_drop"] == ["ALL"]
    assert solver["security_opt"] == ["no-new-privileges:true"]
    assert solver["deploy"]["resources"]["limits"] == {
        "cpus": "2.0",
        "memory": "1536M",
    }
    assert solver["volumes"] == [
        "ortools-solver-state:/var/lib/ortools-solver"
    ]
    assert "env_file" not in solver
    assert "networks" not in solver
    assert "onto-jiuwenbox-sock" not in str(solver)
    assert "onto-runtime" not in str(solver)
    assert "ortools-solver-state" in compose["volumes"]


def test_solver_overlays_mount_only_the_shared_service_token():
    development = _compose("consumer/docker-compose.solver.yml")
    release = _compose("consumer/deploy/docker-compose.solver.release.yml")
    for compose in (development, release):
        solver = compose["services"]["ortools-solver"]
        backend = compose["services"]["backend"]
        assert set(compose["services"]) == {"ortools-solver", "backend"}
        assert solver["secrets"] == ["ortools_solver_service_token"]
        assert "ortools-solver-state" in str(solver)
        assert "ortools_solver_service_token" in str(backend)
        assert "solver_service_tls_ca_certificate" not in str(backend)
        assert compose["services"]["backend"]["environment"][-4:] == [
            "SOLVER_SERVICE_ALLOWED_HOSTS=ortools-solver",
            "SOLVER_SERVICE_ALLOWED_PORTS=8080",
            "SOLVER_SERVICE_ALLOW_HTTP=true",
            "SOLVER_SERVICE_ALLOW_LOCALHOST=false",
        ]
        assert "SOLVER_CONNECTOR_" not in str(compose)
        assert "ORTOOLS_CONNECTOR_" not in str(compose)
