"""Vendor-neutral observability for agent runtime events.

Runtime code should enter through trace facades and ``RuntimeObserver``.
Provider-specific code belongs under ``exporters/``. See README.md in this
package for the responsibility map.
"""

from integration.observability.bus import ObservabilityBus
from integration.observability.context import ObservabilityContext, bind_observability_context, get_observability_context
from integration.observability.events import RuntimeEvent, RuntimeEventType, RuntimeRequestLabel, RuntimeStatus
from integration.observability.observer import NOOP_OBSERVER, NoopObserver, RuntimeObserver
from integration.observability.provider import (
    get_observer,
    get_observer_for_run,
    reset_observer,
    shutdown_observer,
)
from integration.observability.traces import AgentRuntimeTrace, LLMRuntimeTrace, RunRuntimeTrace

__all__ = [
    "LLMRuntimeTrace",
    "NOOP_OBSERVER",
    "AgentRuntimeTrace",
    "NoopObserver",
    "ObservabilityBus",
    "ObservabilityContext",
    "RuntimeEvent",
    "RuntimeEventType",
    "RuntimeRequestLabel",
    "RuntimeStatus",
    "RuntimeObserver",
    "RunRuntimeTrace",
    "bind_observability_context",
    "get_observability_context",
    "get_observer",
    "get_observer_for_run",
    "reset_observer",
    "shutdown_observer",
]
