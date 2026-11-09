"""DA tool endpoints that answer before touching the database (synthetic input)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb_service import text_structure
from kb_service.api import da_tools


def test_doc_grep_reports_an_invalid_pattern(monkeypatch):
    async def fake_context(request, capability):
        return type("Caller", (), {"tenant_id": "t1"})(), ["kb1"]

    monkeypatch.setattr(da_tools, "_context", fake_context)
    app = FastAPI()
    app.include_router(da_tools.router)
    too_long = "x" * (text_structure.MAX_PATTERN + 1)
    r = TestClient(app).post("/v1/tools/doc_grep", json={"pattern": too_long})
    assert r.status_code == 200 and "查找条件无效" in r.json()["error"]
