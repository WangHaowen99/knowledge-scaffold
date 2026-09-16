# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from ..streaming import EventStream
from ..transport import HttpClient
from ..types import AgentMode, JsonDict, normalize_a2a_sse_event


def _path(value: str) -> str:
    return quote(value, safe="")


class A2AAgentCardApi:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def get(self) -> JsonDict:
        return self._http.get("/.well-known/a2a/agent-card.json")


class A2ATasksApi:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def create(
        self,
        *,
        message: Optional[str] = None,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        context: Optional[JsonDict] = None,
        agent_mode: Optional[AgentMode] = None,
        input_payload: Optional[JsonDict] = None,
    ) -> JsonDict:
        run_context: JsonDict = dict(context or {})
        if agent_mode is not None:
            run_context["agent_mode"] = agent_mode
        payload: JsonDict = {"context": run_context}
        if message is not None:
            payload["message"] = message
        if session_id is not None:
            payload["session_id"] = session_id
        if task_id is not None:
            payload["task_id"] = task_id
        if input_payload is not None:
            payload["input"] = input_payload
        return self._http.post("/a2a/tasks", json=payload)

    def get(self, task_id: str, *, session_id: Optional[str] = None) -> JsonDict:
        return self._http.get(
            "/a2a/tasks/{}".format(_path(task_id)),
            params={"session_id": session_id},
        )

    def cancel(self, task_id: str) -> JsonDict:
        return self._http.delete("/a2a/tasks/{}".format(_path(task_id)))

    def events(self, task_id: str, *, session_id: Optional[str] = None) -> EventStream:
        response = self._http.get(
            "/a2a/tasks/{}/events".format(_path(task_id)),
            params={"session_id": session_id},
            stream=True,
        )
        return EventStream(response, normalize_a2a_sse_event)

    def stream_create(
        self,
        *,
        message: str,
        session_id: str,
        task_id: Optional[str] = None,
        context: Optional[JsonDict] = None,
        agent_mode: Optional[AgentMode] = None,
    ) -> EventStream:
        run_context: JsonDict = dict(context or {})
        if agent_mode is not None:
            run_context["agent_mode"] = agent_mode
        response = self._http.post(
            "/a2a/tasks/stream",
            json={
                "message": message,
                "session_id": session_id,
                "task_id": task_id,
                "context": run_context,
            },
            stream=True,
        )
        return EventStream(response, normalize_a2a_sse_event)


@dataclass(slots=True)
class A2ANamespace:
    agent_card: A2AAgentCardApi
    tasks: A2ATasksApi
