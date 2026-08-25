from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import requests

from integration.observability.events import RuntimeEvent, RuntimeEventType
from utils.logging import get_logger
from workspace.workspaces import list_workspaces
from integration.platform.config import PlatformConfig
from integration.platform.events import build_heartbeat_event, build_ontology_snapshot_event
from integration.platform.telemetry import to_platform_telemetry_event


PlatformPostTransport = Callable[[str, dict[str, Any], dict[str, str], float], Any]
logger = get_logger("platform.exporter")


class PlatformExporter:
    """Export runtime observability summaries to onto-platform."""

    def __init__(
        self,
        config: PlatformConfig | Mapping[str, Any] | None = None,
        *,
        transport: PlatformPostTransport | None = None,
        start_heartbeat: bool = False,
    ) -> None:
        if isinstance(config, PlatformConfig):
            self.config = config
        else:
            self.config = PlatformConfig.from_env()
        self._transport = transport or _default_post
        self._buffer: list[dict[str, Any]] = []
        self._run_context: dict[str, dict[str, Any]] = {}
        self._heartbeat_lock = threading.Lock()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        if start_heartbeat:
            self.start_heartbeat()

    @property
    def available(self) -> bool:
        return self.config.available

    def export(self, event: RuntimeEvent) -> None:
        if not self.available:
            return
        self._track_context(event)
        envelope = to_platform_telemetry_event(
            event,
            self.config,
            run_context=self._run_context.get(event.run_id),
        )
        if envelope is None:
            return
        self._buffer.append(envelope)
        if len(self._buffer) >= self.config.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self.available or not self._buffer:
            return
        events = list(self._buffer)
        self._buffer.clear()
        try:
            self._transport(
                _join_url(self.config.base_url, "/api/v1/events/ingest"),
                {"events": events},
                _environment_headers(self.config, accept_json=False),
                self.config.timeout_seconds,
            )
        except Exception:
            self._buffer = events + self._buffer
            logger.warning("Failed to export onto-platform event batch", exc_info=True)

    def close(self) -> None:
        self.stop_heartbeat()
        self.flush()

    def export_ontology_snapshot(
        self,
        *,
        workspace_id: str | None = None,
        ontology_root: Path | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        if not self.available:
            return
        try:
            self._transport(
                _join_url(self.config.base_url, "/api/v1/events/ingest"),
                {
                    "events": [
                        build_ontology_snapshot_event(
                            self.config,
                            workspace_id=workspace_id,
                            ontology_root=ontology_root,
                            occurred_at=occurred_at,
                        )
                    ]
                },
                _environment_headers(self.config, accept_json=False),
                self.config.timeout_seconds,
            )
        except Exception:
            logger.warning("Failed to export onto-platform ontology snapshot", exc_info=True)

    def export_ontology_snapshots(self, *, occurred_at: datetime | None = None) -> None:
        if not self.available:
            return
        try:
            workspace_ids = [str(workspace["id"]) for workspace in list_workspaces()]
        except Exception:
            logger.warning("Failed to list workspaces for onto-platform snapshots", exc_info=True)
            return
        for workspace_id in workspace_ids:
            self.export_ontology_snapshot(workspace_id=workspace_id, occurred_at=occurred_at)

    def start_heartbeat(self) -> None:
        if not self.available:
            return
        with self._heartbeat_lock:
            if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
                return
            self._heartbeat_stop.clear()
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                name="onto-platform-heartbeat",
                daemon=True,
            )
            self._heartbeat_thread.start()

    def stop_heartbeat(self, timeout: float = 1.0) -> None:
        with self._heartbeat_lock:
            thread = self._heartbeat_thread
            self._heartbeat_stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.is_set():
            self._send_heartbeat()
            self._heartbeat_stop.wait(self.config.heartbeat_interval_seconds)

    def _send_heartbeat(self) -> None:
        try:
            self._transport(
                _join_url(self.config.base_url, "/api/v1/events/ingest"),
                {"events": [build_heartbeat_event(self.config)]},
                _environment_headers(self.config, accept_json=False),
                self.config.timeout_seconds,
            )
        except Exception:
            logger.warning("Failed to send onto-platform heartbeat", exc_info=True)

    def _track_context(self, event: RuntimeEvent) -> None:
        payload = dict(event.payload or {})
        context = self._run_context.setdefault(event.run_id, {})
        if event.type == RuntimeEventType.RUN_STARTED:
            for key in ("workspace_id", "user_id"):
                if payload.get(key) is not None:
                    context[key] = payload[key]
            if event.trace_id:
                context["trace_id"] = event.trace_id
            elif not context.get("trace_id") and payload.get("external_trace_id"):
                context["trace_id"] = payload["external_trace_id"]
            context["tool_call_count"] = 0
            context["model_call_count"] = 0
            return
        if event.type == RuntimeEventType.TOOL_FINISHED:
            context["tool_call_count"] = int(context.get("tool_call_count") or 0) + 1
        elif event.type == RuntimeEventType.LLM_FINISHED:
            context["model_call_count"] = int(context.get("model_call_count") or 0) + 1


def _environment_headers(config: PlatformConfig, *, accept_json: bool) -> dict[str, str]:
    headers = {
        "X-Environment-Id": config.environment_id,
        "X-Environment-Api-Key": config.api_key,
    }
    if accept_json:
        headers["Accept"] = "application/json"
    else:
        headers["Content-Type"] = "application/json"
    return headers


def _runtime_headers(config: PlatformConfig, *, accept_json: bool) -> dict[str, str]:
    return _environment_headers(config, accept_json=accept_json)


def _join_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def _default_post(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float):
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json() if response.content else {}
