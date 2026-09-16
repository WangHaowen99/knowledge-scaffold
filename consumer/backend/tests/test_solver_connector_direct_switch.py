# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def test_legacy_generic_connector_modules_are_removed():
    connector_dir = BACKEND / "solver_coordination" / "connectors"

    assert not (connector_dir / "client.py").exists()
    assert not (connector_dir / "backend.py").exists()
    assert not (connector_dir / "contracts.py").exists()


def test_consumer_production_exposes_only_the_service_specific_connector():
    inspected = (
        BACKEND / "solver_coordination" / "connectors",
        BACKEND / "solver_coordination" / "ports.py",
        BACKEND / "solver_coordination" / "__init__.py",
        BACKEND / "scripts" / "run_controlled_solver_validation.py",
    )
    forbidden = ("GenericSolverClient", "GenericSolverBackend", "solver.job.v1")
    offenders = []

    for entry in inspected:
        paths = entry.rglob("*.py") if entry.is_dir() else (entry,)
        for path in paths:
            source = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in source:
                    offenders.append((path.relative_to(BACKEND).as_posix(), token))

    assert offenders == []
