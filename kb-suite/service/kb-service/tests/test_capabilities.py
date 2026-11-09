from __future__ import annotations

import asyncio
import json
from pathlib import Path

from kb_service.api.management import capabilities

SCHEMA = Path(__file__).resolve().parents[3] / "contracts" / "knowledge-provider" / "v1" / "capabilities.schema.json"


def test_capabilities_follow_the_contract():
    caps = asyncio.run(capabilities(caller=None))
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert caps["contract"] == schema["properties"]["contract"]["const"]
    assert caps["role"] == "base" and caps["provider"]["type"] == "kb-service"
    allowed = set(schema["properties"]["features"]["items"]["enum"])
    assert set(caps["features"]) <= allowed
    # kb-service implements the DA tools itself, so kb-gateway forwards those calls unchanged
    assert "tools.da" in caps["features"] and "config.meta" in caps["features"]
