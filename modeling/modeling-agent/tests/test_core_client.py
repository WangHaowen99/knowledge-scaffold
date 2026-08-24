from __future__ import annotations

import httpx
import pytest

from modeling_agent.core_client import HttpCoreGateway


@pytest.mark.asyncio
async def test_claimed_run_credential_replaces_expired_user_token_for_tools() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/claim_candidate_workspace"):
            return httpx.Response(200, json={"runCredential": "arc_project_run_secret"})
        assert request.headers["authorization"] == "Bearer expired-user-token"
        assert request.headers["x-modeling-agent-credential"] == "arc_project_run_secret"
        return httpx.Response(200, json={"ok": True})

    gateway = HttpCoreGateway("http://modeling-core.invalid")
    await gateway._client.aclose()  # noqa: SLF001
    gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    try:
        await gateway.call(
            project_id="project_demo",
            run_id="run_demo",
            token="valid-user-token",
            tool="claim_candidate_workspace",
            arguments={},
        )
        result = await gateway.call(
            project_id="project_demo",
            run_id="run_demo",
            token="expired-user-token",
            tool="write_file",
            arguments={"path": "index.md"},
        )
        assert result["ok"] is True
        assert "x-modeling-agent-credential" not in requests[0].headers
    finally:
        await gateway.aclose()


@pytest.mark.asyncio
async def test_persisted_run_credential_can_be_restored_before_user_reauthenticates() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"runCredential": "arc_refreshed"})

    gateway = HttpCoreGateway("http://modeling-core.invalid")
    await gateway._client.aclose()  # noqa: SLF001
    gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    gateway.restore_run_credential("run_demo", "arc_persisted")
    try:
        await gateway.call(
            project_id="project_demo",
            run_id="run_demo",
            token="",
            tool="claim_candidate_workspace",
            arguments={},
        )
        assert requests[0].headers["x-modeling-agent-credential"] == "arc_persisted"
        assert "authorization" not in requests[0].headers
        assert gateway.run_credential("run_demo") == "arc_refreshed"
    finally:
        await gateway.aclose()


@pytest.mark.asyncio
async def test_non_object_success_is_reported_as_core_protocol_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "fusion-plan-1"}])

    gateway = HttpCoreGateway("http://modeling-core.invalid")
    await gateway._client.aclose()  # noqa: SLF001
    gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    try:
        result = await gateway.call(
            project_id="project_demo",
            run_id="run_demo",
            token="token_demo",
            tool="list_package_fusion_plans",
            arguments={},
        )
        assert result["error"] == "CORE_PROTOCOL_INVALID"
        assert result["reason"] == "建模核心返回格式异常"
        assert "空" not in result["display"]["text"]
    finally:
        await gateway.aclose()
