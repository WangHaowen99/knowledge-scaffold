"""Workspace-scoped observability event routing."""

from __future__ import annotations

import threading
from typing import Any, Iterable

from utils.logging import get_logger
from workspace.workspaces import get_current_workspace_id, list_workspaces, validate_workspace_id
from integration.observability.bus import ObservabilityBus
from integration.observability.events import RuntimeEvent, RuntimeEventType
from integration.observability.exporters.langfuse import create_langfuse_exporter
from integration.observability.exporters.local import LocalPerfExporter
from integration.observability.exporters.speedscope import create_speedscope_exporter
from integration.observability.workspace_config import (
    get_workspace_observability_config,
    get_workspace_observability_log_dir,
)

logger = get_logger("observability.workspace_router")

_TERMINAL_EVENTS = {
    RuntimeEventType.RUN_FINISHED,
    RuntimeEventType.RUN_FAILED,
    RuntimeEventType.RUN_CANCELLED,
}


class _WorkspaceExporterFanout:
    """Keep one queue per workspace while isolating exporter failures."""

    def __init__(self, exporters: Iterable[Any]) -> None:
        self._exporters = [exporter for exporter in exporters if exporter is not None]

    @property
    def available(self) -> bool:
        return bool(self._exporters)

    def export(self, event: RuntimeEvent) -> None:
        for exporter in self._exporters:
            try:
                exporter.export(event)
            except Exception:
                logger.warning("Workspace observability exporter failed", exc_info=True)

    def record_drop(self, event: RuntimeEvent) -> None:
        for exporter in self._exporters:
            record_drop = getattr(exporter, "record_drop", None)
            if not callable(record_drop):
                continue
            try:
                record_drop(event)
            except Exception:
                logger.warning("Workspace observability exporter drop recording failed", exc_info=True)

    def flush(self) -> None:
        for exporter in self._exporters:
            flush = getattr(exporter, "flush", None)
            if not callable(flush):
                continue
            try:
                flush()
            except Exception:
                logger.warning("Workspace observability exporter flush failed", exc_info=True)

    def close(self) -> None:
        for exporter in self._exporters:
            close = getattr(exporter, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception:
                logger.warning("Workspace observability exporter close failed", exc_info=True)


class WorkspaceObservabilityRouter:
    """Route runtime events to the configured exporter for the event workspace."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._observers_by_workspace: dict[str, ObservabilityBus | None] = {}
        self._run_workspaces: dict[str, str] = {}
        self._retiring = False
        self._closed = False

    @property
    def enabled(self) -> bool:
        return True

    @property
    def available(self) -> bool:
        try:
            workspaces = list_workspaces()
        except Exception:
            logger.warning("Failed to list workspaces for observability availability", exc_info=True)
            return False

        for workspace in workspaces:
            workspace_id = str(workspace.get("id") or "").strip()
            if not workspace_id:
                continue
            try:
                if bool(get_workspace_observability_config(workspace_id).get("enabled")):
                    return True
            except Exception:
                logger.warning(
                    "Failed to inspect workspace observability config: workspace_id=%s",
                    workspace_id,
                    exc_info=True,
                )
        return False

    def emit(self, event: RuntimeEvent) -> bool:
        if self._is_closed():
            return False

        workspace_id = self._resolve_workspace_for_event(event)
        if not workspace_id:
            return False

        observer = self._get_observer(workspace_id)
        if observer is None:
            self._cleanup_terminal_run(event)
            return False

        try:
            return bool(observer.emit(event))
        except Exception:
            logger.warning(
                "Workspace observability observer failed: workspace_id=%s run_id=%s event=%s",
                workspace_id,
                event.run_id,
                event.type,
                exc_info=True,
            )
            return False
        finally:
            self._cleanup_terminal_run(event)

    def export(self, event: RuntimeEvent) -> None:
        self.emit(event)

    def retire(self, timeout: float = 1.0) -> None:
        del timeout
        with self._lock:
            self._retiring = True
            should_close = not self._run_workspaces
        if should_close:
            self.close()

    def close(self, timeout: float = 1.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            observers = [
                observer
                for observer in self._observers_by_workspace.values()
                if observer is not None
            ]
            self._observers_by_workspace.clear()
            self._run_workspaces.clear()

        for observer in observers:
            try:
                observer.close(timeout=timeout)
            except Exception:
                logger.warning("Workspace observability observer close failed", exc_info=True)

    def _is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def _resolve_workspace_for_event(self, event: RuntimeEvent) -> str | None:
        payload = dict(event.payload or {})
        explicit_workspace_id = str(payload.get("workspace_id") or "").strip()
        workspace_id: str | None = None

        with self._lock:
            if event.type == RuntimeEventType.RUN_STARTED and self._retiring:
                return None
            if event.type != RuntimeEventType.RUN_STARTED:
                workspace_id = self._run_workspaces.get(event.run_id)

        if not workspace_id:
            if explicit_workspace_id:
                workspace_id = _validate_workspace_id(explicit_workspace_id)
            elif event.type == RuntimeEventType.RUN_STARTED:
                workspace_id = _current_workspace_id()

        if event.type == RuntimeEventType.RUN_STARTED and workspace_id:
            with self._lock:
                self._run_workspaces[event.run_id] = workspace_id

        return workspace_id

    def _get_observer(self, workspace_id: str) -> ObservabilityBus | None:
        with self._lock:
            if workspace_id in self._observers_by_workspace:
                return self._observers_by_workspace[workspace_id]

        exporter = self._build_exporter(workspace_id)
        observer = ObservabilityBus(exporter) if exporter is not None else None
        with self._lock:
            self._observers_by_workspace[workspace_id] = observer
        return observer

    def _build_exporter(self, workspace_id: str) -> object | None:
        try:
            config = get_workspace_observability_config(workspace_id)
        except Exception:
            logger.warning("Failed to load workspace observability config: %s", workspace_id, exc_info=True)
            return None

        if not bool(config.get("enabled")):
            return None

        mode = str(config.get("mode") or "local").strip().lower()
        if mode not in {"local", "langfuse"}:
            logger.warning("Unknown workspace observability mode=%s workspace_id=%s", mode, workspace_id)
            return None

        exporters: list[object] = []
        log_dir = get_workspace_observability_log_dir(workspace_id)
        if mode == "local":
            try:
                exporters.append(LocalPerfExporter(config, log_dir=log_dir))
            except Exception:
                logger.warning(
                    "Failed to initialize local observability exporter: workspace_id=%s",
                    workspace_id,
                    exc_info=True,
                )
        else:
            try:
                exporters.append(create_langfuse_exporter(config))
            except Exception:
                logger.warning(
                    "Failed to initialize Langfuse exporter: workspace_id=%s",
                    workspace_id,
                    exc_info=True,
                )

        try:
            exporters.append(
                create_speedscope_exporter(
                    config,
                    output_dir=log_dir / "speedscope",
                )
            )
        except Exception:
            logger.warning(
                "Failed to initialize Speedscope exporter: workspace_id=%s",
                workspace_id,
                exc_info=True,
            )

        available_exporters = []
        for exporter in exporters:
            if getattr(exporter, "available", True) is False:
                close = getattr(exporter, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        logger.warning("Unavailable workspace exporter close failed", exc_info=True)
                continue
            available_exporters.append(exporter)
        return _WorkspaceExporterFanout(available_exporters) if available_exporters else None

    def _cleanup_terminal_run(self, event: RuntimeEvent) -> None:
        if event.type not in _TERMINAL_EVENTS:
            return
        should_close = False
        with self._lock:
            self._run_workspaces.pop(event.run_id, None)
            should_close = self._retiring and not self._run_workspaces
        if should_close:
            self.close()


def create_workspace_observability_router() -> WorkspaceObservabilityRouter:
    return WorkspaceObservabilityRouter()


def _validate_workspace_id(workspace_id: str) -> str | None:
    try:
        return validate_workspace_id(workspace_id)
    except ValueError:
        logger.warning("Invalid workspace id in observability event: %s", workspace_id)
        return None


def _current_workspace_id() -> str | None:
    try:
        return get_current_workspace_id()
    except Exception:
        return None
