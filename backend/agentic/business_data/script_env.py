"""Environment bridge for ontology operation scripts."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional


SCRIPT_BUSINESS_DATA_CONNECTION_ENV = "ONTO_SCRIPT_BUSINESS_DATA_CONNECTION_JSON"


def serialize_script_business_data_connection(
    *,
    connection_id: str,
    name: str,
    provider: str,
    config: Dict[str, Any],
) -> str:
    return json.dumps(
        {
            "id": str(connection_id or "").strip(),
            "name": str(name or "").strip(),
            "provider": str(provider or "").strip().lower(),
            "config": dict(config or {}),
        },
        ensure_ascii=False,
    )


def load_script_business_data_connection() -> Optional[Dict[str, Any]]:
    raw_payload = os.getenv(SCRIPT_BUSINESS_DATA_CONNECTION_ENV)
    if not raw_payload:
        return None
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    config = payload.get("config")
    if not isinstance(config, dict):
        config = {}
    return {
        "id": str(payload.get("id") or "").strip(),
        "name": str(payload.get("name") or "").strip(),
        "provider": str(payload.get("provider") or "").strip().lower(),
        "config": dict(config),
    }
