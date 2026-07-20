"""onto-platform connector for runtime events and policies."""

from integration.platform.config import PlatformConfig
from integration.platform.contracts import normalize_platform_status
from integration.platform.events import build_heartbeat_event, build_ontology_snapshot_event, to_platform_event
from integration.platform.exporter import PlatformExporter
from integration.platform.policy import fetch_policy_snapshot, map_policy_snapshot_to_agent_config
from integration.platform.telemetry import to_platform_telemetry_event

__all__ = [
    "PlatformConfig",
    "PlatformExporter",
    "build_heartbeat_event",
    "build_ontology_snapshot_event",
    "fetch_policy_snapshot",
    "map_policy_snapshot_to_agent_config",
    "normalize_platform_status",
    "to_platform_event",
    "to_platform_telemetry_event",
]
