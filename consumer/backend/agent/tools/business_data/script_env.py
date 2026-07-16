"""Environment bridge for ontology operation scripts."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional
from shared_capabilities.business_data import EffectiveConnection


SCRIPT_BUSINESS_DATA_CONNECTION_ENV = "ONTO_SCRIPT_BUSINESS_DATA_CONNECTION_JSON"


def serialize_script_business_data_connection(
    *,
    connection: EffectiveConnection,
) -> str:
    return json.dumps(connection.to_dict(), ensure_ascii=False)


def load_script_business_data_connection() -> Optional[Dict[str, Any]]:
    """Load either the legacy payload or the versioned envelope.

    Kept only for compatibility with existing in-process callers while generated
    ontology scripts migrate to ``shared_capabilities.ontology_script_sdk``.
    """
    raw_payload = os.getenv(SCRIPT_BUSINESS_DATA_CONNECTION_ENV)
    if not raw_payload:
        return None
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if "config" in payload:
        config = payload.get("config")
        return {
            "id": str(payload.get("id") or "").strip(),
            "name": str(payload.get("name") or "").strip(),
            "provider": str(payload.get("provider") or "").strip().lower(),
            "config": dict(config) if isinstance(config, dict) else {},
        }
    try:
        connection = EffectiveConnection.from_dict(payload)
    except Exception:
        return None
    config: Dict[str, Any] = {
        "endpoint": connection.endpoint,
        "api_url": connection.endpoint,
        **connection.options,
    }
    if connection.auth.type == "bearer":
        config["token"] = connection.auth.token
        config["api_key"] = connection.auth.token
    elif connection.auth.type == "mindsdb_password":
        config["username"] = connection.auth.username
        config["password"] = connection.auth.password
    return {
        "id": "",
        "name": "",
        "provider": connection.provider,
        "config": config,
    }
