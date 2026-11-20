"""Wait for the configured account service before serving authenticated pages."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib.parse import urljoin
import urllib.request

logger = logging.getLogger(__name__)


def _ready_status(status: object) -> bool:
    if not isinstance(status, dict):
        return False
    return (status.get("schemaVersion") == 1
            and status.get("service") == "auth"
            and status.get("state") == "ready")


def available() -> bool:
    base = os.environ.get("ONTO_WEB_AUTH_URL", "").rstrip("/")
    if not base:
        return True  # Local development without remote authentication.
    try:
        with urllib.request.urlopen(urljoin(base + "/", "health"), timeout=3) as response:
            if response.status != 200:
                return False
        try:
            with urllib.request.urlopen(urljoin(base + "/", "status"), timeout=3) as response:
                status = json.load(response)
            if _ready_status(status):
                for key, configured in (("issuer", os.environ.get("ONTO_WEB_AUTH_ISSUER")),
                                        ("audience", os.environ.get("ONTO_WEB_AUTH_AUDIENCE"))):
                    if configured and status.get(key) and configured != status[key]:
                        logger.warning("Auth %s differs from Modeling configuration; check authentication settings", key)
                return True
            # Consumer accepts a legacy /health-only Auth even if /status is
            # absent or lacks the new public-status contract.
            return True
        except (OSError, ValueError, TypeError):
            # Legacy Auth provides /health without /status, as Consumer permits.
            return True
    except (OSError, ValueError, TypeError):
        return False


async def wait_for_auth(interval: int = 3) -> None:
    if not os.environ.get("ONTO_WEB_AUTH_URL"):
        return
    if os.environ.get("ONTO_AUTH_STARTUP_WAIT_ENABLED", "true") == "false":
        return
    logger.warning("Waiting for Auth availability before Onto Modeling startup")
    while not await asyncio.to_thread(available):
        await asyncio.sleep(interval)
    logger.warning("Auth is available; continuing Onto Modeling startup")


if __name__ == "__main__":
    asyncio.run(wait_for_auth())
