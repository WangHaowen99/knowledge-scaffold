# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Single-worker Uvicorn server configuration."""

from __future__ import annotations

import os

# Server configuration
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 5055
SERVER_DEBUG = False
SERVER_AUTO_RELOAD = False
SERVER_WORKERS = int(os.getenv("SERVER_WORKERS", "1"))
SERVER_GRACEFUL_SHUTDOWN_SECONDS = int(
    os.getenv("SERVER_GRACEFUL_SHUTDOWN_SECONDS", "45")
)

# CORS origins (if needed)
CORS_ORIGINS = ["*"]

# Request limits
MAX_CONTENT_LENGTH = 64 * 1024 * 1024


def validate_server_settings(
    *,
    workers: int = SERVER_WORKERS,
    graceful_shutdown_seconds: int = SERVER_GRACEFUL_SHUTDOWN_SECONDS,
) -> None:
    """Reject deployment settings that violate the runtime ownership contract."""
    if int(workers) != 1:
        raise RuntimeError("Consumer backend requires exactly one Uvicorn worker")
    if int(graceful_shutdown_seconds) < 45:
        raise RuntimeError("Uvicorn graceful shutdown must be at least 45 seconds")


validate_server_settings()
