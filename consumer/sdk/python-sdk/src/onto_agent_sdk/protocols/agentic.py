from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional
from urllib.parse import quote
from uuid import uuid4

from ..streaming import EventStream
from ..transport import HttpClient
from ..types import AgentMode, JsonDict, normalize_agentic_sse_event


def _path(value: str) -> str:
    return quote(value, safe="")


def _relative_api_path(value: str, *, field: str) -> str:
    path = str(value or "").strip()
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("{} must be a relative API path".format(field))
    return path


class AgenticRunsApi:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def create(
        self,
        *,
        session_id: str,
        request: str,
        agent_mode: Optional[AgentMode] = None,
        llm_request_mode: Optional[str] = None,
        model_profile_id: Optional[str] = None,
        arguments: Optional[JsonDict] = None,
        include_trace: Optional[bool] = None,
        interaction_response: Optional[JsonDict] = None,
        idempotency_key: Optional[str] = None,
    ) -> JsonDict:
        payload: JsonDict = {
            "session_id": session_id,
            "request": request,
        }
        if agent_mode is not None:
            payload["agent_mode"] = agent_mode
        if llm_request_mode is not None:
            payload["llm_request_mode"] = llm_request_mode
        if model_profile_id is not None:
            payload["model_profile_id"] = model_profile_id
        if arguments is not None:
            payload["arguments"] = dict(arguments)
        if include_trace is not None:
            payload["include_trace"] = include_trace
        if interaction_response is not None:
            payload["interaction_response"] = interaction_response
        return self._http.post(
            "/agentic/runs",
            json=payload,
            headers={
                "Idempotency-Key": idempotency_key or uuid4().hex,
            },
        )

    def get(self, run_id: str) -> JsonDict:
        return self._http.get("/agentic/runs/{}".format(_path(run_id)))

    def events(self, run_id: str, *, after_seq: int = 0) -> EventStream:
        """Attach or reattach to an admitted run after the last consumed sequence."""
        return self.events_from_link(
            "/agentic/runs/{}/events".format(_path(run_id)),
            after_seq=after_seq,
        )

    def events_from_link(self, events_link: str, *, after_seq: int = 0) -> EventStream:
        """Attach using the canonical events link returned by run admission."""
        path = _relative_api_path(events_link, field="events_link")
        cursor = max(0, int(after_seq or 0))
        response = self._http.get(
            path,
            params={"after_seq": cursor},
            headers={"Last-Event-ID": str(cursor)} if cursor else None,
            stream=True,
        )
        return EventStream(response, normalize_agentic_sse_event)

    def resume(self, run_id: str, *, after_seq: int = 0) -> EventStream:
        """Alias for :meth:`events`, kept as the explicit reconnect entry point."""
        return self.events(run_id, after_seq=after_seq)

    def cancel(self, run_id: str, *, reason: Optional[str] = None) -> JsonDict:
        return self._http.post(
            "/agentic/runs/{}/cancel".format(_path(run_id)),
            json={"reason": reason} if reason is not None else {},
        )


class AgenticSessionsApi:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def list(self) -> list[JsonDict]:
        response = self._http.get("/agentic/sessions")
        return response.get("sessions", [])

    def create(
        self,
        *,
        title: Optional[str] = None,
        llm_request_mode: Optional[str] = None,
    ) -> JsonDict:
        payload: JsonDict = {}
        if title is not None:
            payload["title"] = title
        if llm_request_mode is not None:
            payload["llm_request_mode"] = llm_request_mode
        return self._http.post("/agentic/sessions", json=payload)

    def get(self, session_id: str) -> JsonDict:
        return self._http.get("/agentic/sessions/{}".format(_path(session_id)))

    def messages(self, session_id: str, *, limit: Optional[int] = None) -> list[JsonDict]:
        response = self._http.get(
            "/agentic/sessions/{}/messages".format(_path(session_id)),
            params={"limit": limit},
        )
        return response.get("messages", [])

    def delete(self, session_id: str) -> JsonDict:
        return self._http.delete("/agentic/sessions/{}".format(_path(session_id)))

    def generate_title(self, session_id: str, *, message: str) -> JsonDict:
        return self._http.post(
            "/agentic/sessions/{}/generate-title".format(_path(session_id)),
            json={"message": message},
        )


class AgenticSkillsApi:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def list(self) -> list[JsonDict]:
        response = self._http.get("/agentic/skills")
        return response.get("skills", [])

    def get(self, name: str) -> JsonDict:
        return self._http.get("/agentic/skills/{}".format(_path(name)))


class AgenticOntologiesApi:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def list(self) -> list[JsonDict]:
        response = self._http.get("/agentic/ontologies")
        return response.get("ontologies", [])

    def list_files(self, ontology_id: str, *, folder: str) -> JsonDict:
        return self._http.get(
            "/agentic/ontologies/{}/files".format(_path(ontology_id)),
            params={"folder": folder},
        )

    def get_file(self, ontology_id: str, *, folder: str, name: str) -> JsonDict:
        return self._http.get(
            "/agentic/ontologies/{}/file".format(_path(ontology_id)),
            params={"folder": folder, "name": name},
        )

    def put_file(self, ontology_id: str, *, folder: str, name: str, content: str) -> JsonDict:
        return self._http.put(
            "/agentic/ontologies/{}/file".format(_path(ontology_id)),
            json={
                "folder": folder,
                "name": name,
                "content": content,
            },
        )

    def upload_file(
        self,
        ontology_id: str,
        *,
        folder: str,
        file_path: str | Path,
        filename: Optional[str] = None,
    ) -> JsonDict:
        resolved_path = Path(file_path)
        with resolved_path.open("rb") as handle:
            return self.upload_file_obj(
                ontology_id,
                folder=folder,
                file_obj=handle,
                filename=filename or resolved_path.name,
            )

    def upload_file_obj(
        self,
        ontology_id: str,
        *,
        folder: str,
        file_obj: BinaryIO,
        filename: str,
    ) -> JsonDict:
        return self._http.post(
            "/agentic/ontologies/{}/upload".format(_path(ontology_id)),
            data={"folder": folder},
            files={"file": (filename, file_obj)},
        )

    def import_zip(
        self,
        *,
        file_path: str | Path,
        overwrite: Optional[bool] = None,
    ) -> JsonDict:
        resolved_path = Path(file_path)
        with resolved_path.open("rb") as handle:
            return self.import_zip_obj(
                file_obj=handle,
                filename=resolved_path.name,
                overwrite=overwrite,
            )

    def import_zip_obj(
        self,
        *,
        file_obj: BinaryIO,
        filename: str,
        overwrite: Optional[bool] = None,
    ) -> JsonDict:
        data: JsonDict = {}
        if overwrite is not None:
            data["overwrite"] = "true" if overwrite else "false"
        return self._http.post(
            "/agentic/ontologies/import-zip",
            data=data,
            files={"file": (filename, file_obj)},
        )

    def delete(self, ontology_id: str) -> JsonDict:
        return self._http.delete("/agentic/ontologies/{}".format(_path(ontology_id)))


class AgenticWorkspacesApi:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def list(self) -> JsonDict:
        return self._http.get("/agentic/workspaces")

    def get(self, workspace_id: str) -> JsonDict:
        return self._http.get("/agentic/workspaces/{}".format(_path(workspace_id)))

    def create(
        self,
        *,
        name: str,
        workspace_id: Optional[str] = None,
        description: Optional[str] = None,
        set_as_default: Optional[bool] = None,
    ) -> JsonDict:
        payload: JsonDict = {"name": name}
        if workspace_id is not None:
            payload["id"] = workspace_id
        if description is not None:
            payload["description"] = description
        if set_as_default is not None:
            payload["set_as_default"] = set_as_default
        return self._http.post("/agentic/workspaces", json=payload)

    def update(
        self,
        workspace_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> JsonDict:
        payload: JsonDict = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        return self._http.patch("/agentic/workspaces/{}".format(_path(workspace_id)), json=payload)

    def delete(self, workspace_id: str) -> JsonDict:
        return self._http.delete("/agentic/workspaces/{}".format(_path(workspace_id)))

    def set_default(self, workspace_id: str) -> JsonDict:
        return self._http.post("/agentic/workspaces/{}/default".format(_path(workspace_id)))


@dataclass(slots=True)
class AgenticNamespace:
    runs: AgenticRunsApi
    sessions: AgenticSessionsApi
    skills: AgenticSkillsApi
    ontologies: AgenticOntologiesApi
    workspaces: AgenticWorkspacesApi
