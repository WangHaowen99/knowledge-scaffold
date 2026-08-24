from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

from .errors import AgentRuntimeError

logger = logging.getLogger("modeling_agent.core_client")


class CoreGateway(Protocol):
    async def model_config(
        self,
        *,
        project_id: str,
        run_id: str,
        token: str,
    ) -> dict[str, object]: ...

    async def call(
        self,
        *,
        project_id: str,
        run_id: str,
        token: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...

    def run_credential(self, run_id: str) -> str: ...

    def restore_run_credential(self, run_id: str, credential: str) -> None: ...


class HttpCoreGateway:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 120.0,
        service_token: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._client = httpx.AsyncClient(timeout=timeout_seconds, trust_env=False)
        self._run_credentials: dict[str, str] = {}

    def run_credential(self, run_id: str) -> str:
        return self._run_credentials.get(run_id, "")

    def restore_run_credential(self, run_id: str, credential: str) -> None:
        if credential:
            self._run_credentials[run_id] = credential

    async def model_config(
        self,
        *,
        project_id: str,
        run_id: str,
        token: str,
    ) -> dict[str, object]:
        headers = {
            "X-Modeling-Project": project_id,
            "X-Modeling-Run": run_id,
            "X-Modeling-Service-Token": self._service_token,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        credential = self._run_credentials.get(run_id)
        if credential:
            headers["X-Modeling-Agent-Credential"] = credential
        try:
            response = await self._client.get(
                f"{self._base_url}/internal/agent/llm-config",
                headers=headers,
            )
        except httpx.HTTPError as error:
            raise AgentRuntimeError(
                "CORE_UNAVAILABLE",
                "建模模型配置暂时不可用",
                status=503,
            ) from error
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.is_error:
            code = str(payload.get("code") or "LLM_CONFIG_UNAVAILABLE")
            message = str(payload.get("error") or "建模模型配置不可用")
            raise AgentRuntimeError(code, message, status=response.status_code)
        llm = payload.get("llm")
        if not isinstance(llm, dict) or not str(llm.get("baseUrl") or "") or not str(llm.get("model") or ""):
            raise AgentRuntimeError(
                "LLM_CONFIG_INVALID",
                "建模模型配置格式异常",
                status=502,
            )
        return llm

    async def call(
        self,
        *,
        project_id: str,
        run_id: str,
        token: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            headers = {
                "X-Modeling-Project": project_id,
                "X-Modeling-Run": run_id,
            }
            if token:
                headers["Authorization"] = f"Bearer {token}"
            credential = self._run_credentials.get(run_id)
            if credential:
                headers["X-Modeling-Agent-Credential"] = credential
            response = await self._client.post(
                f"{self._base_url}/internal/agent/tools/{tool}",
                json={"arguments": arguments},
                headers=headers,
                timeout=1800.0 if tool in {"run_evaluation", "read_documents_fully"} else self._client.timeout,
            )
        except httpx.LocalProtocolError as error:
            logger.warning(
                "Core request was rejected locally (tool=%s, error=%s)",
                tool,
                type(error).__name__,
            )
            raise AgentRuntimeError(
                "CORE_REQUEST_INVALID",
                "建模核心请求格式无效，Agent 已停止继续写入",
                status=500,
                details={"next": ["保留当前进度并检查服务配置"]},
            ) from error
        except httpx.HTTPError as error:
            logger.warning(
                "Core request failed before a valid response (tool=%s, error=%s)",
                tool,
                type(error).__name__,
            )
            raise AgentRuntimeError(
                "CORE_UNAVAILABLE",
                "建模核心暂时不可用，Agent 已停止继续写入",
                status=503,
                details={"next": ["稍后从当前检查点继续"]},
            ) from error

        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return {
                "error": "CORE_PROTOCOL_INVALID",
                "reason": "建模核心返回格式异常",
                "next": ["保留当前进度，刷新后从最近检查点重试"],
                "display": {"text": "建模核心返回格式异常，当前步骤未继续"},
            }
        issued = payload.get("runCredential")
        if tool == "claim_candidate_workspace" and isinstance(issued, str) and issued:
            self._run_credentials[run_id] = issued
        if response.is_error:
            code = str(payload.get("code") or payload.get("error") or "CORE_TOOL_FAILED")
            reason = str(payload.get("reason") or payload.get("error") or "工具执行失败")
            return {
                **payload,
                "error": code,
                "reason": reason,
                "next": payload.get("next") or [],
                "display": payload.get("display") or {"text": reason},
            }
        return payload

    async def aclose(self) -> None:
        self._run_credentials.clear()
        await self._client.aclose()
