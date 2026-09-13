"""Graph mapping resolver for ontology packages."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

import yaml
from ontology.ontologies import get_ontology_version_dir
from ontology.ontology_catalog import (
    find_exact_object_data_mapping,
    resolve_object_reference,
)

from .models import GraphEntityMapping
from .scope import sanitize_return_properties, validate_graph_identifier


def _resolve_ontology_version_dir(
    workspace_root: str,
    ontology_id: str,
    ontology_version: str = "",
) -> Optional[Path]:
    if not workspace_root or not ontology_id:
        return None
    ontology_root = (Path(workspace_root) / ontology_id).resolve()
    if not ontology_root.is_dir():
        return None
    try:
        version_dir = get_ontology_version_dir(
            ontology_root,
            str(ontology_version or "").strip() or None,
            create=False,
        )
    except ValueError:
        version_dir = None
    return version_dir or ontology_root


class GraphMappingResolver:
    """Resolve an ontology entity to a graph label/path mapping."""

    def __init__(
        self,
        *,
        workspace_root: str,
        ontology_id: str,
        ontology_version: str = "",
        provider: str,
        schema_labels: Iterable[str] | None = None,
    ) -> None:
        self.workspace_root = str(workspace_root or "").strip()
        self.ontology_id = str(ontology_id or "").strip()
        self.ontology_version = str(ontology_version or "").strip()
        self.provider = str(provider or "").strip().lower()
        self.schema_labels = {str(label).strip() for label in (schema_labels or []) if str(label).strip()}

    def resolve(self, entity: str) -> Optional[GraphEntityMapping]:
        entity_key = str(entity or "").strip()
        if not entity_key:
            return None

        version_dir = _resolve_ontology_version_dir(
            self.workspace_root,
            self.ontology_id,
            self.ontology_version,
        )
        if version_dir is None:
            return self._resolve_from_schema(entity_key)

        for resolver in (
            self._resolve_from_object_data_mappings,
            self._resolve_from_object_frontmatter,
        ):
            mapping = resolver(version_dir, entity_key)
            if mapping is not None:
                return mapping

        return self._resolve_from_schema(entity_key)

    def _resolve_from_object_data_mappings(
        self,
        version_dir: Path,
        entity: str,
    ) -> Optional[GraphEntityMapping]:
        raw = find_exact_object_data_mapping(version_dir, entity)
        if not isinstance(raw, dict):
            return None

        provider_mapping = _select_provider_mapping(raw, self.provider)

        if provider_mapping is None:
            return None
        return _mapping_from_payload(
            entity=entity,
            raw=provider_mapping,
            source="mappings/object-data-mappings.json",
            fallback_title=str(raw.get("title") or ""),
            allow_graph_extensions=False,
        )

    @staticmethod
    def _resolve_from_object_frontmatter(
        version_dir: Path,
        entity: str,
    ) -> Optional[GraphEntityMapping]:
        resolution, error = resolve_object_reference(version_dir, entity)
        if error or resolution is None:
            return None
        frontmatter = _load_object_frontmatter(resolution.path)
        if not frontmatter:
            return None
        raw_graph = frontmatter.get("graph")
        if isinstance(raw_graph, dict):
            raw: Dict[str, Any] = dict(raw_graph)
        else:
            raw = {}
        if frontmatter.get("graph_label"):
            raw.setdefault("label", frontmatter.get("graph_label"))
            raw.setdefault("kind", "node")
        if frontmatter.get("graph_properties"):
            raw.setdefault("property_map", frontmatter.get("graph_properties"))
        if not raw.get("label") and not raw.get("path_pattern"):
            return None
        return _mapping_from_payload(
            entity=entity,
            raw=raw,
            source="objects/{}".format(resolution.path.name),
            fallback_title=str(frontmatter.get("title") or ""),
            allow_graph_extensions=True,
        )

    def _resolve_from_schema(self, entity: str) -> Optional[GraphEntityMapping]:
        if entity not in self.schema_labels:
            return None
        return GraphEntityMapping(
            entity=entity,
            kind="node",
            label=entity,
            source="graph_schema_exact_match",
        )


def _mapping_from_payload(
    *,
    entity: str,
    raw: Mapping[str, Any],
    source: str,
    fallback_title: str = "",
    allow_graph_extensions: bool = True,
) -> GraphEntityMapping:
    raw = _merge_graph_extension(raw) if allow_graph_extensions else dict(raw)
    kind = (
        str(raw.get("kind") or raw.get("resource_type") or "node").strip().lower() if allow_graph_extensions else "node"
    )
    if kind in {"vertex", "label"}:
        kind = "node"
    if kind in {"relationship", "relation"}:
        kind = "edge"
    if kind not in {"node", "edge", "path"}:
        kind = "node"

    label = str((raw.get("label") if allow_graph_extensions else "") or raw.get("resource") or "").strip()
    edge_type = str(raw.get("edge_type") or raw.get("type") or "").strip() if kind == "edge" else ""
    if kind == "edge" and not edge_type:
        edge_type = label
    if kind in {"node", "edge"}:
        validate_graph_identifier(label or edge_type, "graph label")

    property_map = _normalize_property_map(raw.get("property_map") or raw.get("fields"))
    return GraphEntityMapping(
        entity=entity,
        kind=kind,
        source=source,
        label=label,
        edge_type=edge_type,
        title=str(raw.get("title") or fallback_title or ""),
        id_property=_normalize_id_property(raw.get("id_property") or raw.get("primary_key"), property_map),
        property_map=property_map,
        return_properties=sanitize_return_properties(
            (raw.get("return_properties") if allow_graph_extensions else None)
            or _return_properties_from_fields(raw.get("fields"))
        ),
        default_filters=_normalize_filter_dict(raw.get("default_filters")) if allow_graph_extensions else {},
        path_pattern=(
            _normalize_path_pattern(raw.get("path_pattern")) if allow_graph_extensions and kind == "path" else None
        ),
        return_vars=[
            str(item).strip() for item in (raw.get("return_vars") or []) if allow_graph_extensions and str(item).strip()
        ],
    )


def _select_provider_mapping(raw: Mapping[str, Any], provider: str) -> Mapping[str, Any] | None:
    providers = raw.get("providers")
    if isinstance(providers, dict):
        provider_raw = providers.get(provider)
        if isinstance(provider_raw, Mapping):
            merged = dict(raw)
            merged.pop("providers", None)
            merged.update(dict(provider_raw))
            return merged
        return None

    raw_provider = str(raw.get("provider") or raw.get("provider_type") or "").strip().lower()
    if raw_provider and raw_provider != provider:
        return None
    if _looks_like_object_data_mapping(raw):
        return raw
    return None


def _looks_like_object_data_mapping(raw: Mapping[str, Any]) -> bool:
    mapping_keys = (
        "provider_alias",
        "resource",
        "fields",
        "primary_key",
        "policy",
    )
    return any(key in raw for key in mapping_keys)


def _merge_graph_extension(raw: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(raw)
    graph = raw.get("graph")
    if isinstance(graph, Mapping):
        for key, value in graph.items():
            merged.setdefault(str(key), value)
    return merged


def _normalize_property_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        source_key = str(key or "").strip()
        if isinstance(value, Mapping):
            target_key = str(value.get("source") or "").strip()
        else:
            target_key = str(value or "").strip()
        if not source_key or not target_key:
            continue
        validate_graph_identifier(target_key, "graph property")
        normalized[source_key] = target_key
    return normalized


def _normalize_id_property(raw: Any, property_map: Mapping[str, str]) -> str:
    if raw in (None, ""):
        return ""
    if isinstance(raw, list):
        for item in raw:
            value = str(item or "").strip()
            if value:
                return str(property_map.get(value) or value).strip()
        return ""
    value = str(raw or "").strip()
    return str(property_map.get(value) or value).strip()


def _return_properties_from_fields(raw: Any) -> list[str]:
    if not isinstance(raw, Mapping):
        return []
    properties: list[str] = []
    seen: set[str] = set()
    for value in raw.values():
        if isinstance(value, Mapping):
            source = str(value.get("source") or "").strip()
        else:
            source = str(value or "").strip()
        if not source or source in seen:
            continue
        seen.add(source)
        properties.append(source)
    return properties


def _normalize_filter_dict(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    return {str(key).strip(): value for key, value in raw.items() if str(key).strip()}


def _normalize_path_pattern(raw: Any) -> Optional[list[dict[str, Any]]]:
    if not isinstance(raw, list):
        return None
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        segment = dict(item)
        if segment.get("label"):
            validate_graph_identifier(segment.get("label"), "path label")
        if segment.get("type"):
            validate_graph_identifier(segment.get("type"), "path relationship type")
        if isinstance(segment.get("return_properties"), list):
            segment["return_properties"] = sanitize_return_properties(segment.get("return_properties"))
        normalized.append(segment)
    return normalized or None


def _load_object_frontmatter(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end_index = text.find("\n---", 3)
    if end_index < 0:
        return {}
    try:
        payload = yaml.safe_load(text[3:end_index]) or {}
    except yaml.YAMLError:
        return {}
    return payload if isinstance(payload, dict) else {}
