"""Vendor-neutral observability for agent runtime events.

Runtime code should enter through trace facades and ``RuntimeObserver``.
Provider-specific code belongs under ``exporters/``. See README.md in this
package for the responsibility map.
"""

from .bus import ObservabilityBus
from .context import ObservabilityContext, bind_observability_context, get_observability_context
from .events import RuntimeEvent, RuntimeEventType, RuntimeRequestLabel, RuntimeStatus
from .observer import NOOP_OBSERVER, NoopObserver, RuntimeObserver
from .provider import get_observer, reset_observer, shutdown_observer
from .traces import AgentRuntimeTrace, LLMRuntimeTrace, RunRuntimeTrace

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
    "reset_observer",
    "shutdown_observer",
]
