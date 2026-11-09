"""uvicorn entry point: ``uvicorn weknora_adapter.main:app``."""

from __future__ import annotations

import logging

from .app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")  # NOSONAR(S4792)

app = create_app()
