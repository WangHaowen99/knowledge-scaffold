from __future__ import annotations

from typing import Optional

from ..protocols.agentic import AgenticRunsApi, AgenticSessionsApi
from ..streaming import EventStream
from ..types import AgentMode, JsonDict


class ChatFacade:
    def __init__(self, runs: AgenticRunsApi, sessions: AgenticSessionsApi) -> None:
        self._runs = runs
        self._sessions = sessions

    def send(
        self,
        *,
        session_id: str,
        message: str,
        run_id: Optional[str] = None,
        context: Optional[JsonDict] = None,
        agent_mode: Optional[AgentMode] = None,
        selected_ontology_id: Optional[str] = None,
    ) -> EventStream:
        run_context: JsonDict = dict(context or {})
        if agent_mode is not None:
            run_context["agent_mode"] = agent_mode
        if selected_ontology_id is not None:
            run_context["selected_ontology_id"] = selected_ontology_id

        return self._runs.stream(
            session_id=session_id,
            request=message,
            run_id=run_id,
            context=run_context,
        )

    def list_sessions(self) -> list[JsonDict]:
        return self._sessions.list()

    def create_session(
        self,
        *,
        title: Optional[str] = None,
        llm_request_mode: Optional[str] = None,
    ) -> JsonDict:
        return self._sessions.create(title=title, llm_request_mode=llm_request_mode)

    def get_session(self, session_id: str) -> JsonDict:
        return self._sessions.get(session_id)

    def get_messages(self, session_id: str, *, limit: Optional[int] = None) -> list[JsonDict]:
        return self._sessions.messages(session_id, limit=limit)
