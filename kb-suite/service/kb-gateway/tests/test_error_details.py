"""A backend's error details reach the caller through the gateway (synthetic data only)."""

from __future__ import annotations

import httpx
from conftest import auth


def _answer(provider, path: str, response: httpx.Response) -> None:
    real = provider.handle

    def handle(request: httpx.Request) -> httpx.Response:
        return response if request.url.path == path else real(request)

    provider.handle = handle


def test_duplicate_upload_keeps_the_existing_document_fields(client, providers):
    _answer(providers["kbs"], "/v1/kbs/kb1", httpx.Response(409, json={"error": {
        "code": "duplicate", "message": "同样的内容已经上传过", "existingDocId": "d1", "existingStatus": "completed"}}))
    response = client.get("/v1/kbs/kb1", headers=auth())
    assert response.status_code == 409
    assert response.json()["error"] == {"code": "duplicate", "message": "同样的内容已经上传过", "existingDocId": "d1",
                                        "existingStatus": "completed", "backend": "kbs"}


def test_only_scalar_details_pass_and_backend_is_the_gateways(client, providers):
    _answer(providers["kbs"], "/v1/kbs/kb1", httpx.Response(400, json={"error": {
        "code": "bad_request", "message": "不行", "backend": "spoofed", "nested": {"a": 1}, "items": [1], "hint": "改一下"}}))
    error = client.get("/v1/kbs/kb1", headers=auth()).json()["error"]
    assert error == {"code": "bad_request", "message": "不行", "hint": "改一下", "backend": "kbs"}
