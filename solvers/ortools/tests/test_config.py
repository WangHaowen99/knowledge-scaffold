# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

import pytest

from solvers.ortools.config import OrToolsSolverConfig


def test_environment_configuration_requires_service_authentication_by_default(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("ORTOOLS_SOLVER_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ORTOOLS_SOLVER_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("ORTOOLS_SOLVER_SERVICE_TOKEN_FILE", raising=False)
    monkeypatch.delenv("ORTOOLS_SOLVER_ALLOW_UNAUTHENTICATED", raising=False)

    with pytest.raises(ValueError, match="SERVICE_TOKEN"):
        OrToolsSolverConfig.from_env()


def test_environment_configuration_allows_explicit_local_tck_mode(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("ORTOOLS_SOLVER_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ORTOOLS_SOLVER_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("ORTOOLS_SOLVER_SERVICE_TOKEN_FILE", raising=False)
    monkeypatch.setenv("ORTOOLS_SOLVER_ALLOW_UNAUTHENTICATED", "1")

    config = OrToolsSolverConfig.from_env()

    assert config.service_token is None
    assert config.database_path == tmp_path / "attempts.sqlite3"


def test_environment_configuration_reads_service_token_from_secret_file(
    monkeypatch, tmp_path
):
    secret_path = tmp_path / "service-token"
    secret_path.write_text("synthetic-mounted-token-value\n", encoding="utf-8")
    monkeypatch.setenv("ORTOOLS_SOLVER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORTOOLS_SOLVER_SERVICE_TOKEN_FILE", str(secret_path))
    monkeypatch.delenv("ORTOOLS_SOLVER_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("ORTOOLS_SOLVER_ALLOW_UNAUTHENTICATED", raising=False)

    config = OrToolsSolverConfig.from_env()

    assert config.service_token == "synthetic-mounted-token-value"


def test_environment_configuration_rejects_two_service_token_sources(
    monkeypatch, tmp_path
):
    secret_path = tmp_path / "service-token"
    secret_path.write_text("synthetic-mounted-token-value", encoding="utf-8")
    monkeypatch.setenv("ORTOOLS_SOLVER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORTOOLS_SOLVER_SERVICE_TOKEN_FILE", str(secret_path))
    monkeypatch.setenv(
        "ORTOOLS_SOLVER_SERVICE_TOKEN", "synthetic-direct-token-value"
    )

    with pytest.raises(ValueError, match="mutually exclusive"):
        OrToolsSolverConfig.from_env()


@pytest.mark.parametrize("content", ("", "short", "value\nwith-another-line"))
def test_environment_configuration_rejects_invalid_secret_file_content(
    monkeypatch, tmp_path, content
):
    secret_path = tmp_path / "service-token"
    secret_path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("ORTOOLS_SOLVER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORTOOLS_SOLVER_SERVICE_TOKEN_FILE", str(secret_path))
    monkeypatch.delenv("ORTOOLS_SOLVER_SERVICE_TOKEN", raising=False)

    with pytest.raises(ValueError, match="service token file"):
        OrToolsSolverConfig.from_env()


def test_environment_configuration_uses_solver_state_default(monkeypatch):
    monkeypatch.setenv("ORTOOLS_SOLVER_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.delenv("ORTOOLS_SOLVER_STATE_DIR", raising=False)

    config = OrToolsSolverConfig.from_env()

    assert config.database_path.parent.as_posix() == "/var/lib/ortools-solver"


def test_configuration_rejects_more_than_one_search_worker(tmp_path):
    with pytest.raises(ValueError, match="max_search_workers"):
        OrToolsSolverConfig(
            database_path=tmp_path / "attempts.sqlite3",
            attempt_ttl_seconds=3600,
            max_payload_bytes=1024,
            max_solve_time_ms=1000,
            max_search_workers=2,
            max_concurrent_jobs=1,
            memory_limit_bytes=1024,
            file_limit_bytes=1024,
            prefer_wait_max_ms=100,
            service_token=None,
            enforce_os_limits=False,
        )
