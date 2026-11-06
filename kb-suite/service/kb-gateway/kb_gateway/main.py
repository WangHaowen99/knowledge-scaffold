"""uvicorn entry point: ``uvicorn kb_gateway.main:app``."""

from __future__ import annotations

import logging

from .api.app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")  # NOSONAR(S4792)

app = create_app()
