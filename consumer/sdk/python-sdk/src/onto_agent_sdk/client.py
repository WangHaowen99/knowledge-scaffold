# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from .facades import ChatFacade
from .protocols import A2ANamespace, AgenticNamespace
from .protocols.a2a import A2AAgentCardApi, A2ATasksApi
from .protocols.agentic import (
    AgenticOntologiesApi,
    AgenticRunsApi,
    AgenticSessionsApi,
    AgenticSkillsApi,
    AgenticWorkspacesApi,
)
from .transport import HttpClient


class OntoAgentClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        workspace_id: str | None = None,
        timeout: float | None = 60.0,
    ) -> None:
        self.http = HttpClient(
            base_url=base_url,
            token=token,
            workspace_id=workspace_id,
            timeout=timeout,
        )

        runs = AgenticRunsApi(self.http)
        sessions = AgenticSessionsApi(self.http)
        skills = AgenticSkillsApi(self.http)
        ontologies = AgenticOntologiesApi(self.http)
        workspaces = AgenticWorkspacesApi(self.http)
        agent_card = A2AAgentCardApi(self.http)
        tasks = A2ATasksApi(self.http)

        self.agentic = AgenticNamespace(
            runs=runs,
            sessions=sessions,
            skills=skills,
            ontologies=ontologies,
            workspaces=workspaces,
        )
        self.a2a = A2ANamespace(
            agent_card=agent_card,
            tasks=tasks,
        )
        self.chat = ChatFacade(runs, sessions)

    def set_token(self, token: str | None) -> None:
        self.http.set_token(token)

    def set_workspace(self, workspace_id: str | None) -> None:
        self.http.set_workspace(workspace_id)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "OntoAgentClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def create_client(
    *,
    base_url: str,
    token: str | None = None,
    workspace_id: str | None = None,
    timeout: float | None = 60.0,
) -> OntoAgentClient:
    return OntoAgentClient(
        base_url=base_url,
        token=token,
        workspace_id=workspace_id,
        timeout=timeout,
    )
