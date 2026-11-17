"""Logging policy shared by the API and the worker."""

from __future__ import annotations

import logging

# httpx logs every request at INFO with its full URL, which includes the model service's address
QUIET_LOGGERS = ("httpx", "httpcore")


def quiet_http_clients() -> None:
    """Keep the HTTP client libraries to warnings, so service addresses do not end up in the logs."""

    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
