"""Versioned connection envelope loading for ontology scripts."""

from __future__ import annotations

import json
import os

from shared_capabilities.business_data import (
    ConnectionEnvelopeError,
    EffectiveConnection,
)

SCRIPT_BUSINESS_DATA_CONNECTION_ENV = "ONTO_SCRIPT_BUSINESS_DATA_CONNECTION_JSON"


def load_effective_connection() -> EffectiveConnection:
    raw = os.getenv(SCRIPT_BUSINESS_DATA_CONNECTION_ENV)
    if not raw:
        raise ConnectionEnvelopeError(
            "ontology business data source is not configured"
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConnectionEnvelopeError("connection envelope is not valid JSON") from exc
    return EffectiveConnection.from_dict(payload)
