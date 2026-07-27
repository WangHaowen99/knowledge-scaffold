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
            for value in (
                self.workspace_property,
                self.workspace_value,
                self.ontology_property,
                self.ontology_value,
            )
        ):
            raise ValueError("GraphScope requires workspace and ontology scope")

    def is_enabled(self) -> bool:
        return bool(
            self.workspace_property
            and self.workspace_value
            and self.ontology_property
            and self.ontology_value
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
