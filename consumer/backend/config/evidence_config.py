"""Effective ontology evidence feature switches."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping


logger = logging.getLogger("config.evidence")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _csv_value(value: Any) -> frozenset[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        candidates = value
    else:
        candidates = str(value or "").split(",")
    return frozenset(str(item).strip() for item in candidates if str(item).strip())


@dataclass(frozen=True)
class EvidenceConfig:
    enabled: bool = False
    rollout_mode: str = "off"
    workspace_allowlist: frozenset[str] = frozenset()
    search_enabled: bool = True
    tool_exposure_mode: str = "adaptive"
    auto_build_enabled: bool = False
    sweeper_enabled: bool = True
    sweeper_interval_seconds: int = 1800
    structured_context_enabled: bool = True
    auto_ground_business_data: bool = True
    runtime_fact_mode: str = "sync"
    runtime_fact_write_timeout_ms: int = 50
    runtime_fact_batch_size: int = 10
    runtime_fact_flush_interval_ms: int = 100
    semantic_mode: str = "off"
    graph_enabled: bool = True

    @property
    def semantic_enabled(self) -> bool:
        return self.semantic_mode in {"local", "external"}

    @classmethod
    def from_env(cls) -> "EvidenceConfig":
        mode = os.getenv("ONTOLOGY_EVIDENCE_ROLLOUT_MODE", "off").strip().lower()
        if mode not in {"off", "shadow", "allowlist", "all"}:
            mode = "off"
        runtime_mode = os.getenv("ONTOLOGY_EVIDENCE_RUNTIME_FACT_WRITE_MODE", "sync").strip().lower()
        if runtime_mode not in {"sync", "batched", "off"}:
            runtime_mode = "sync"
        exposure_mode = os.getenv("ONTOLOGY_EVIDENCE_TOOL_EXPOSURE_MODE", "adaptive").strip().lower()
        if exposure_mode not in {"adaptive", "always"}:
            exposure_mode = "adaptive"
        semantic_mode = os.getenv("ONTOLOGY_EVIDENCE_SEMANTIC_MODE", "").strip().lower()
        if not semantic_mode:
            semantic_mode = "local" if _bool("ONTOLOGY_EVIDENCE_SEMANTIC_ENABLED", False) else "off"
        if semantic_mode not in {"off", "local", "external"}:
            semantic_mode = "off"
        structured = _bool("ONTOLOGY_EVIDENCE_STRUCTURED_CONTEXT_ENABLED", True)
        search = _bool("ONTOLOGY_EVIDENCE_SEARCH_ENABLED", True) and structured
        allowlist_value = os.getenv("ONTOLOGY_EVIDENCE_ROLLOUT_WORKSPACE_IDS")
        if allowlist_value is None:
            allowlist_value = os.getenv("ONTOLOGY_EVIDENCE_WORKSPACE_ALLOWLIST", "")
        return cls(
            enabled=_bool("ONTOLOGY_EVIDENCE_ENABLED", False),
            rollout_mode=mode,
            workspace_allowlist=_csv_value(allowlist_value),
            search_enabled=search,
            tool_exposure_mode=exposure_mode,
            auto_build_enabled=_bool("ONTOLOGY_EVIDENCE_AUTO_BUILD_ENABLED", False),
            sweeper_enabled=_bool("ONTOLOGY_EVIDENCE_SWEEPER_ENABLED", True),
            sweeper_interval_seconds=max(1, _int("ONTOLOGY_EVIDENCE_SWEEPER_INTERVAL_SECONDS", 1800)),
            structured_context_enabled=structured,
            auto_ground_business_data=_bool("ONTOLOGY_EVIDENCE_AUTO_GROUND_BUSINESS_DATA", True),
            runtime_fact_mode=runtime_mode,
            runtime_fact_write_timeout_ms=max(1, _int("ONTOLOGY_EVIDENCE_RUNTIME_FACT_WRITE_TIMEOUT_MS", 50)),
            runtime_fact_batch_size=max(1, _int("ONTOLOGY_EVIDENCE_RUNTIME_FACT_BATCH_SIZE", 10)),
            runtime_fact_flush_interval_ms=max(1, _int("ONTOLOGY_EVIDENCE_RUNTIME_FACT_FLUSH_INTERVAL_MS", 100)),
            semantic_mode=semantic_mode,
            graph_enabled=_bool("ONTOLOGY_EVIDENCE_GRAPH_ENABLED", True),
        )

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "EvidenceConfig":
        base = cls.from_env()
        mode = str(section.get("rollout_mode", base.rollout_mode)).strip().lower()
        runtime_mode = str(section.get("runtime_fact_write_mode", base.runtime_fact_mode)).strip().lower()
        exposure_mode = str(section.get("tool_exposure_mode", base.tool_exposure_mode)).strip().lower()
        semantic_mode = str(section.get("semantic_mode", base.semantic_mode)).strip().lower()
        structured = bool(section.get("structured_context_enabled", base.structured_context_enabled))
        return cls(
            enabled=bool(section.get("enabled", base.enabled)),
            rollout_mode=mode if mode in {"off", "shadow", "allowlist", "all"} else "off",
            workspace_allowlist=_csv_value(section.get("rollout_workspace_ids", base.workspace_allowlist)),
            search_enabled=bool(section.get("search_enabled", base.search_enabled)) and structured,
            tool_exposure_mode=exposure_mode if exposure_mode in {"adaptive", "always"} else "adaptive",
            auto_build_enabled=bool(section.get("auto_build_enabled", base.auto_build_enabled)),
            sweeper_enabled=bool(section.get("sweeper_enabled", base.sweeper_enabled)),
            sweeper_interval_seconds=max(1, int(section.get("sweeper_interval_seconds", base.sweeper_interval_seconds))),
            structured_context_enabled=structured,
            auto_ground_business_data=bool(section.get("auto_ground_business_data", base.auto_ground_business_data)),
            runtime_fact_mode=runtime_mode if runtime_mode in {"sync", "batched", "off"} else "sync",
            runtime_fact_write_timeout_ms=max(1, int(section.get("runtime_fact_write_timeout_ms", base.runtime_fact_write_timeout_ms))),
            runtime_fact_batch_size=max(1, int(section.get("runtime_fact_batch_size", base.runtime_fact_batch_size))),
            runtime_fact_flush_interval_ms=max(1, int(section.get("runtime_fact_flush_interval_ms", base.runtime_fact_flush_interval_ms))),
            semantic_mode=semantic_mode if semantic_mode in {"off", "local", "external"} else "off",
            graph_enabled=bool(section.get("graph_enabled", base.graph_enabled)),
        )

    @classmethod
    def load(cls) -> "EvidenceConfig":
        """Snapshot the effective saved-config > env > default values."""
        try:
            from config.system_config import load_system_config

            section = load_system_config().get("ontology_evidence") or {}
            return cls.from_mapping(section)
        except Exception:
            logger.warning("Failed to load ontology evidence config; feature is fail-closed", exc_info=True)
            fallback = cls.from_env()
            return cls.from_mapping({**fallback.__dict__, "enabled": False, "rollout_mode": "off"})

    def workspace_mode(self, workspace_id: str) -> str:
        if not self.enabled or self.rollout_mode == "off":
            return "off"
        if self.rollout_mode == "allowlist" and workspace_id not in self.workspace_allowlist:
            return "off"
        return self.rollout_mode

    def exposes_search_tool(self, workspace_id: str) -> bool:
        return (
            self.search_enabled
            and self.structured_context_enabled
            and self.workspace_mode(workspace_id) in {"allowlist", "all"}
        )

    def builds_indexes(self, workspace_id: str) -> bool:
        return self.auto_build_enabled and self.workspace_mode(workspace_id) in {"shadow", "allowlist", "all"}
