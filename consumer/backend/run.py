# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Development entry point for the single-worker FastAPI server."""

# Load .env FIRST, before any module reads os.getenv(...).
# Lookup order: backend/.env → <consumer>/.env
from pathlib import Path

try:
    from dotenv import load_dotenv

    _backend_env = Path(__file__).resolve().parent / ".env"
    _app_env = Path(__file__).resolve().parent.parent / ".env"
    if _backend_env.exists():
        load_dotenv(_backend_env)
    elif _app_env.exists():
        load_dotenv(_app_env)
except ImportError:
    pass

import uvicorn

# Import server configuration
try:
    from server_config import (
        SERVER_HOST,
        SERVER_PORT,
        SERVER_DEBUG,
        SERVER_AUTO_RELOAD,
        SERVER_GRACEFUL_SHUTDOWN_SECONDS,
        SERVER_WORKERS,
        validate_server_settings,
    )
except ImportError:
    # Fallback to defaults if config not found
    SERVER_HOST = "0.0.0.0"
    SERVER_PORT = 5055
    SERVER_DEBUG = True
    SERVER_AUTO_RELOAD = False
    SERVER_WORKERS = 1
    SERVER_GRACEFUL_SHUTDOWN_SECONDS = 45

    def validate_server_settings(*, workers: int, graceful_shutdown_seconds: int) -> None:
        if workers != 1:
            raise RuntimeError("Consumer backend requires exactly one Uvicorn worker")
        if graceful_shutdown_seconds < 45:
            raise RuntimeError("Uvicorn graceful shutdown must be at least 45 seconds")


if __name__ == "__main__":
    validate_server_settings(
        workers=SERVER_WORKERS,
        graceful_shutdown_seconds=SERVER_GRACEFUL_SHUTDOWN_SECONDS,
    )
    uvicorn.run(
        "app:create_app",
        factory=True,
        host=SERVER_HOST,
        port=SERVER_PORT,
        reload=bool(SERVER_AUTO_RELOAD),
        log_level="debug" if SERVER_DEBUG else "info",
        workers=SERVER_WORKERS,
        timeout_graceful_shutdown=SERVER_GRACEFUL_SHUTDOWN_SECONDS,
    )
