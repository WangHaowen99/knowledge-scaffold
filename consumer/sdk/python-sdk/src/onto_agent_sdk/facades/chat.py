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
        agent_mode: Optional[AgentMode] = None,
        llm_request_mode: Optional[str] = None,
        model_profile_id: Optional[str] = None,
        arguments: Optional[JsonDict] = None,
        include_trace: Optional[bool] = None,
        selected_ontology_id: Optional[str] = None,
        interaction_response: Optional[JsonDict] = None,
        idempotency_key: Optional[str] = None,
    ) -> EventStream:
        run_arguments: JsonDict = dict(arguments or {})
        if selected_ontology_id is not None:
            run_arguments["selected_ontology_id"] = selected_ontology_id

        admission = self._runs.create(
            session_id=session_id,
            request=message,
            agent_mode=agent_mode,
            llm_request_mode=llm_request_mode,
            model_profile_id=model_profile_id,
            arguments=run_arguments or None,
            include_trace=include_trace,
            interaction_response=interaction_response,
            idempotency_key=idempotency_key,
        )
        links = admission.get("links")
        if not isinstance(links, dict) or not links.get("events"):
            raise ValueError("run admission response is missing links.events")
        return self._runs.events_from_link(str(links["events"]), after_seq=0)

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
