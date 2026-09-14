# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Environment-backed configuration for the standalone OR-Tools Solver Service."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path


_DEFAULT_BUILD = "sha256:" + hashlib.sha256(b"ortools-solver-service:1.0.0").hexdigest()


def _positive(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _service_token_from_file(path_text: str) -> str:
    path = Path(path_text)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("service token file cannot be read") from exc
    if len(raw) > 4096 or raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("service token file is invalid")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("service token file is invalid") from exc
    token = text[:-1] if text.endswith("\n") else text
    if token.endswith("\r"):
        token = token[:-1]
    if len(token) < 16 or token != token.strip():
        raise ValueError("service token file is invalid")
    if "\n" in token or "\r" in token:
        raise ValueError("service token file is invalid")
    return token


@dataclass(frozen=True)
class OrToolsSolverConfig:
    database_path: Path
    attempt_ttl_seconds: int
    max_payload_bytes: int
    max_solve_time_ms: int
    max_search_workers: int
    max_concurrent_jobs: int
    memory_limit_bytes: int
    file_limit_bytes: int
    prefer_wait_max_ms: int
    service_token: str | None
    enforce_os_limits: bool
    service_build: str = _DEFAULT_BUILD

    def __post_init__(self) -> None:
        object.__setattr__(self, "database_path", Path(self.database_path))
        for name in (
            "attempt_ttl_seconds",
            "max_payload_bytes",
            "max_solve_time_ms",
            "max_search_workers",
            "max_concurrent_jobs",
            "memory_limit_bytes",
            "file_limit_bytes",
            "prefer_wait_max_ms",
        ):
            _positive(getattr(self, name), name)
        if self.max_search_workers != 1:
            raise ValueError("max_search_workers must be 1")
        if self.service_token is not None and (
            not isinstance(self.service_token, str) or len(self.service_token) < 16
        ):
            raise ValueError("service_token must contain at least 16 characters")
        if not isinstance(self.enforce_os_limits, bool):
            raise ValueError("enforce_os_limits must be boolean")
        if (
            not isinstance(self.service_build, str)
            or not self.service_build.startswith("sha256:")
            or len(self.service_build) != 71
        ):
            raise ValueError("service_build must be a sha256 digest")

    @classmethod
    def from_env(cls) -> "OrToolsSolverConfig":
        root = Path(os.environ.get("ORTOOLS_SOLVER_STATE_DIR", "/var/lib/ortools-solver"))
        service_token = os.environ.get("ORTOOLS_SOLVER_SERVICE_TOKEN")
        service_token_file = os.environ.get("ORTOOLS_SOLVER_SERVICE_TOKEN_FILE")
        if service_token is not None and service_token_file is not None:
            raise ValueError("service token sources are mutually exclusive")
        if service_token_file is not None:
            service_token = _service_token_from_file(service_token_file)
        if service_token is None and os.environ.get("ORTOOLS_SOLVER_ALLOW_UNAUTHENTICATED") != "1":
            raise ValueError("ORTOOLS_SOLVER_SERVICE_TOKEN is required")
        return cls(
            database_path=root / "attempts.sqlite3",
            attempt_ttl_seconds=int(os.environ.get("ORTOOLS_SOLVER_ATTEMPT_TTL_SECONDS", "86400")),
            max_payload_bytes=int(os.environ.get("ORTOOLS_SOLVER_MAX_PAYLOAD_BYTES", "1048576")),
            max_solve_time_ms=int(os.environ.get("ORTOOLS_SOLVER_MAX_SOLVE_TIME_MS", "3600000")),
            max_search_workers=int(os.environ.get("ORTOOLS_SOLVER_MAX_SEARCH_WORKERS", "1")),
            max_concurrent_jobs=int(os.environ.get("ORTOOLS_SOLVER_MAX_CONCURRENT_JOBS", "2")),
            memory_limit_bytes=int(os.environ.get("ORTOOLS_SOLVER_MEMORY_LIMIT_BYTES", str(1024 * 1024 * 1024))),
            file_limit_bytes=int(os.environ.get("ORTOOLS_SOLVER_FILE_LIMIT_BYTES", str(16 * 1024 * 1024))),
            prefer_wait_max_ms=int(os.environ.get("ORTOOLS_SOLVER_PREFER_WAIT_MAX_MS", "5000")),
            service_token=service_token,
            enforce_os_limits=os.environ.get("ORTOOLS_SOLVER_ENFORCE_OS_LIMITS", "1") != "0",
            service_build=os.environ.get("ORTOOLS_SOLVER_BUILD", _DEFAULT_BUILD),
        )


__all__ = ["OrToolsSolverConfig"]
