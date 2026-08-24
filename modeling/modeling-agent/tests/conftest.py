from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import pytest
from openjiuwen.core.foundation.llm import init_model
from openjiuwen.core.foundation.llm.model import Model
from openjiuwen.core.foundation.llm.schema.message import AssistantMessage
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall

from modeling_agent.service import AgentService
from modeling_agent.settings import Settings


TEST_PACKAGE_CONTRACT = {
    "validatorVersion": "package-format-test-v1",
    "manifest": {
        "requiredFields": ["kind", "schema_version", "id", "name", "description", "version", "status"],
        "platformManagedFields": ["kind", "schema_version", "id", "version", "status"],
    },
}


@dataclass(frozen=True)
class ToolTurn:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


class ScriptedModel(Model):
    def __init__(
        self,
        script: Sequence[Sequence[ToolTurn]],
        final: str = "本轮已完成，下一步请确认流程范围。",
        tool_content_by_call: dict[int, str] | None = None,
        content_by_call: dict[int, str] | None = None,
        reasoning_by_call: dict[int, str] | None = None,
    ) -> None:
        template = init_model(
            provider="OpenAI",
            model_name="scripted-model",
            api_key="unused",
            api_base="http://127.0.0.1:9/unused",
        )
        super().__init__(
            model_client_config=template.model_client_config,
            model_config=template.model_config,
        )
        self.script = [list(turns) for turns in script]
        self.final = final
        self.tool_content_by_call = tool_content_by_call or {}
        self.content_by_call = content_by_call or {}
        self.reasoning_by_call = reasoning_by_call or {}
        self.calls = 0

    def _next(self) -> AssistantMessage:
        index = self.calls
        self.calls += 1
        turns = self.script[index] if index < len(self.script) else []
        content = self.content_by_call.get(index, self.final)
        reasoning = self.reasoning_by_call.get(index, "")
        if not turns:
            return AssistantMessage(content=content, reasoning_content=reasoning)
        return AssistantMessage(
            content=self.tool_content_by_call.get(index, ""),
            reasoning_content=reasoning,
            tool_calls=[
                ToolCall(
                    id=f"call_{index}_{position}",
                    type="function",
                    name=turn.name,
                    arguments=json.dumps(turn.arguments, ensure_ascii=False),
                )
                for position, turn in enumerate(turns)
            ],
        )

    async def invoke(self, *_args: Any, **_kwargs: Any) -> AssistantMessage:  # type: ignore[override]
        return self._next()

    async def stream(self, *_args: Any, **_kwargs: Any):  # type: ignore[override]
        response = self._next()
        reasoning = str(response.reasoning_content or "")
        for index in range(0, len(reasoning), 4):
            yield AssistantMessageChunk(
                reasoning_content=reasoning[index:index + 4],
                finish_reason="null",
            )
        if response.tool_calls:
            content = str(response.content or "")
            for index in range(0, len(content), 4):
                yield AssistantMessageChunk(content=content[index:index + 4], finish_reason="null")
            yield AssistantMessageChunk(content="", tool_calls=response.tool_calls, finish_reason="tool_calls")
            return
        content = str(response.content or "")
        parts = [content[index:index + 4] for index in range(0, len(content), 4)] or [""]
        for index, part in enumerate(parts):
            yield AssistantMessageChunk(content=part, finish_reason="stop" if index == len(parts) - 1 else "null")


class FakeGateway:
    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[dict[str, Any]] = []
        self.credentials: dict[str, str] = {}

    def run_credential(self, run_id: str) -> str:
        return self.credentials.get(run_id, "")

    def restore_run_credential(self, run_id: str, credential: str) -> None:
        if credential:
            self.credentials[run_id] = credential

    async def call(
        self,
        *,
        project_id: str,
        run_id: str,
        token: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append({
            "projectId": project_id,
            "runId": run_id,
            "token": token,
            "tool": tool,
            "arguments": arguments,
        })
        if tool == "claim_candidate_workspace":
            self.credentials[run_id] = str(
                self.responses.get(tool, {}).get("runCredential")
                or self.credentials.get(run_id)
                or f"arc_{run_id}_secret"
            )
        result = dict(self.responses.get(tool, {"ok": True, "display": {"text": f"{tool} 完成"}}))
        if tool == "claim_candidate_workspace":
            result.setdefault("packageContract", TEST_PACKAGE_CONTRACT)
        return result

    async def aclose(self) -> None:
        return None


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    root = Path(__file__).resolve().parents[1]
    return Settings(
        data_dir=tmp_path / "agent-data",
        core_base_url="http://127.0.0.1:5100",
        service_token="s" * 32,
        llm_base_url="http://127.0.0.1:9/unused",
        llm_api_key="unused",
        llm_model="scripted",
        llm_timeout_seconds=5,
        llm_max_tokens=4096,
        max_iterations=20,
        host="127.0.0.1",
        port=5110,
        skills_dir=root / "skills",
    )


def service(settings: Settings, model: ScriptedModel, gateway: FakeGateway | None = None) -> AgentService:
    return AgentService(
        settings,
        gateway=gateway or FakeGateway(),
        model_factory=lambda: model,
    )
