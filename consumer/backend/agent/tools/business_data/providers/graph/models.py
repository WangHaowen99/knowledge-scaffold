"""Internal models for GraphSearchProvider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class GraphScope:
    workspace_property: str
    workspace_value: str
    ontology_property: str
    ontology_value: str

    def __post_init__(self) -> None:
        if not all(
            str(value or "").strip()
            for value in (self.workspace_property, self.workspace_value)
        ):
            raise ValueError("GraphScope requires workspace scope")
        has_ontology_property = bool(str(self.ontology_property or "").strip())
        has_ontology_value = bool(str(self.ontology_value or "").strip())
        if has_ontology_property != has_ontology_value:
            raise ValueError(
                "GraphScope ontology property and value must be configured together",
            )

    def is_enabled(self) -> bool:
        """Return whether Workspace isolation is enabled; Ontology isolation is optional."""
        return bool(
            str(self.workspace_property or "").strip()
            and str(self.workspace_value or "").strip()
        )


@dataclass(frozen=True)
class GraphEntityMapping:
    entity: str
    kind: str
    source: str
    label: str = ""
    edge_type: str = ""
    title: str = ""
    id_property: str = ""
    property_map: Dict[str, str] = field(default_factory=dict)
    return_properties: List[str] = field(default_factory=list)
    default_filters: Dict[str, Any] = field(default_factory=dict)
    path_pattern: Optional[List[Dict[str, Any]]] = None
    return_vars: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class GraphBackendResult:
    records: List[Dict[str, Any]] = field(default_factory=list)
    total: Optional[int] = None
    truncated: bool = False
    warnings: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphAgentResult:
    tool_name: str
    tool_args: Dict[str, Any]
    backend_result: GraphBackendResult
    executed_steps: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    final_message: str = ""


@dataclass(frozen=True)
class GraphSearchRuntimeContext:
    provider: str
    workspace_id: str
    ontology_id: str
    workspace_root: str
    entity: str
    query: str
    timeout_seconds: int
    max_result_rows: int
    scope: GraphScope
    mapping: Optional[GraphEntityMapping]
    provider_config: Dict[str, Any] = field(default_factory=dict)
    business_context: Dict[str, Any] = field(default_factory=dict)
    schema_context: Dict[str, Any] = field(default_factory=dict)
    ontology_version: str = ""
