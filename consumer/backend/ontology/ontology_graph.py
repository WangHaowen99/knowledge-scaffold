"""Build display-ready ontology graph snapshots from ontology files."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from ontology.ontologies import OntologyManifest
from ontology.ontology_catalog import parse_markdown_table, read_markdown_frontmatter, read_optional_text

_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def build_ontology_graph(
    *,
    ontology: OntologyManifest,
    ontology_dir: Path,
    version: str = "",
) -> Dict[str, Any]:
    """Return nodes and edges for the active ontology version directory."""

    root = ontology_dir.resolve()
    operation_catalog = _catalog_by_key(
        read_optional_text(root / "operations" / "index.md"),
        key_fields=("名称", "operation", "operation_key", "逻辑", "name"),
    )
    object_catalog = _catalog_by_key(
        read_optional_text(root / "objects" / "index.md"),
        key_fields=("中文名称", "object", "object_key", "实体", "name"),
    )

    nodes_by_id: Dict[str, Dict[str, Any]] = {}
    key_index: Dict[Tuple[str, str], str] = {}

    for node in _load_existing_nodes(root):
        _add_node(nodes_by_id, key_index, node)

    _add_markdown_nodes(
        nodes_by_id,
        key_index,
        directory=root / "operations",
        node_type="operation",
        source_prefix="operations",
        catalog=operation_catalog,
    )
    _add_markdown_nodes(
        nodes_by_id,
        key_index,
        directory=root / "objects",
        node_type="object",
        source_prefix="objects",
        catalog=object_catalog,
    )

    edges_by_id: Dict[str, Dict[str, Any]] = {}
    for edge in _load_existing_edges(root, nodes_by_id, key_index):
        _add_edge(edges_by_id, edge)

    operation_keys = _keys_for_type(nodes_by_id, "operation")
    object_keys = _keys_for_type(nodes_by_id, "object")
    operation_ids = {key: key_index[("operation", key)] for key in operation_keys}
    object_ids = {key: key_index[("object", key)] for key in object_keys}

    _add_generated_operation_edges(
        edges_by_id=edges_by_id,
        operations_dir=root / "operations",
        operation_ids=operation_ids,
        object_ids=object_ids,
    )
    _add_generated_object_edges(
        edges_by_id=edges_by_id,
        objects_dir=root / "objects",
        object_ids=object_ids,
    )

    nodes = sorted(nodes_by_id.values(), key=lambda item: (_type_order(item.get("type")), item.get("label") or ""))
    edges = sorted(
        edges_by_id.values(),
        key=lambda item: (item.get("source") or "", item.get("target") or "", item.get("type") or ""),
    )
    return {
        "ontology_id": ontology.id,
        "name": ontology.name,
        "description": ontology.description,
        "version": version or ontology.active_version or ontology.version,
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "operation_count": sum(1 for node in nodes if node.get("type") == "operation"),
            "object_count": sum(1 for node in nodes if node.get("type") == "object"),
        },
    }


def _catalog_by_key(markdown: str, *, key_fields: Iterable[str]) -> Dict[str, Dict[str, str]]:
    rows = parse_markdown_table(markdown)
    result: Dict[str, Dict[str, str]] = {}
    for row in rows:
        key = ""
        for field in key_fields:
            key = str(row.get(field) or "").strip()
            if key:
                break
        if key:
            result[key] = row
    return result


def _add_markdown_nodes(
    nodes_by_id: Dict[str, Dict[str, Any]],
    key_index: Dict[Tuple[str, str], str],
    *,
    directory: Path,
    node_type: str,
    source_prefix: str,
    catalog: Dict[str, Dict[str, str]],
) -> None:
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.md"), key=lambda item: item.stem):
        if path.name.lower() == "index.md":
            continue
        frontmatter = read_markdown_frontmatter(path)
        key = str(frontmatter.get("key") or path.stem).strip()
        row = catalog.get(key, {})
        _add_node(
            nodes_by_id,
            key_index,
            {
                "id": f"{node_type}:{key}",
                "type": node_type,
                "key": key,
                "label": str(frontmatter.get("title") or key).strip(),
                "description": _description_from_catalog(row),
                "source": f"{source_prefix}/{path.name}",
            },
        )


def _description_from_catalog(row: Dict[str, str]) -> str:
    for field in ("业务描述", "业务定义", "描述", "description", "说明"):
        text = str(row.get(field) or "").strip()
        if text:
            return text
    return ""


def _load_existing_nodes(root: Path) -> List[Dict[str, Any]]:
    raw = _read_json_list(root / "graph" / "nodes.json")
    nodes: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if _is_manifest_node(item):
            continue
        key = str(item.get("key") or item.get("id") or item.get("label") or "").strip()
        node_type = _normalize_node_type(item.get("type"), item.get("source"))
        if not key or not node_type:
            continue
        if ":" in key and key.split(":", 1)[0] in {"ontology", "operation", "object"}:
            key = key.split(":", 1)[1]
        nodes.append(
            {
                "id": f"{node_type}:{key}",
                "type": node_type,
                "key": key,
                "label": str(item.get("label") or item.get("name") or key).strip() or key,
                "description": str(item.get("description") or "").strip(),
                "source": _safe_source(item.get("source")),
            }
        )
    return nodes


def _normalize_node_type(value: Any, source: Any = None) -> str:
    raw = str(value or "").strip().lower()
    source_text = str(source or "").replace("\\", "/").lower()
    if raw in {"operation", "logic"} or source_text.startswith("operations/"):
        return "operation"
    if raw in {"object", "entity"} or source_text.startswith("objects/"):
        return "object"
    if raw == "ontology":
        return "ontology"
    return ""


def _load_existing_edges(
    root: Path,
    nodes_by_id: Dict[str, Dict[str, Any]],
    key_index: Dict[Tuple[str, str], str],
) -> List[Dict[str, Any]]:
    raw = _read_json_list(root / "graph" / "edges.json")
    edges: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        source = _resolve_node_id(item.get("source"), nodes_by_id, key_index)
        target = _resolve_node_id(item.get("target"), nodes_by_id, key_index)
        if not source or not target or source == target:
            continue
        edges.append(
            {
                "source": source,
                "target": target,
                "type": str(item.get("type") or "related").strip() or "related",
                "label": str(item.get("label") or "").strip(),
                "confidence": str(item.get("confidence") or "explicit").strip() or "explicit",
            }
        )
    return edges


def _read_json_list(path: Path) -> List[Any]:
    if not path.is_file():
        return []
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _safe_source(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/").strip("/")
    if not text or text.startswith("/") or ".." in Path(text).parts:
        return ""
    return text


def _is_manifest_node(item: Dict[str, Any]) -> bool:
    source = _safe_source(item.get("source")).lower()
    if source == "manifest.yaml":
        return True
    for key in ("id", "key", "label", "name"):
        if str(item.get(key) or "").strip().lower() == "manifest.yaml":
            return True
    return False


def _add_node(
    nodes_by_id: Dict[str, Dict[str, Any]],
    key_index: Dict[Tuple[str, str], str],
    node: Dict[str, Any],
) -> None:
    node_type = _normalize_node_type(node.get("type"), node.get("source"))
    key = str(node.get("key") or "").strip()
    node_id = str(node.get("id") or "").strip()
    if not node_type or not key:
        return
    if not node_id:
        node_id = f"{node_type}:{key}"
    normalized = {
        "id": node_id,
        "type": node_type,
        "key": key,
        "label": str(node.get("label") or key).strip() or key,
        "description": str(node.get("description") or "").strip(),
        "source": _safe_source(node.get("source")),
    }
    existing = nodes_by_id.get(node_id)
    if existing:
        nodes_by_id[node_id] = {
            **existing,
            **{key_name: value for key_name, value in normalized.items() if value},
        }
    else:
        nodes_by_id[node_id] = normalized
    key_index[(node_type, key)] = node_id


def _keys_for_type(nodes_by_id: Dict[str, Dict[str, Any]], node_type: str) -> List[str]:
    return sorted(
        [
            str(node.get("key") or "")
            for node in nodes_by_id.values()
            if node.get("type") == node_type and node.get("key")
        ],
        key=len,
        reverse=True,
    )


def _resolve_node_id(
    value: Any,
    nodes_by_id: Dict[str, Dict[str, Any]],
    key_index: Dict[Tuple[str, str], str],
) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text in nodes_by_id:
        return text
    if ":" in text:
        node_type, key = text.split(":", 1)
        resolved = key_index.get((_normalize_node_type(node_type), key))
        if resolved:
            return resolved
    for node_type in ("operation", "object", "ontology"):
        resolved = key_index.get((node_type, text))
        if resolved:
            return resolved
    return ""


def _add_generated_operation_edges(
    *,
    edges_by_id: Dict[str, Dict[str, Any]],
    operations_dir: Path,
    operation_ids: Dict[str, str],
    object_ids: Dict[str, str],
) -> None:
    if not operations_dir.is_dir():
        return
    for path in sorted(operations_dir.glob("*.md"), key=lambda item: item.stem):
        if path.name.lower() == "index.md":
            continue
        source_id = operation_ids.get(_resource_key(path))
        if not source_id:
            continue
        content = _read_text(path)
        explicit_links = {link.strip() for link in _WIKI_LINK_RE.findall(content) if link.strip()}
        for linked_key in sorted(explicit_links):
            if linked_key in operation_ids and operation_ids[linked_key] != source_id:
                _add_edge(
                    edges_by_id,
                    {
                        "source": source_id,
                        "target": operation_ids[linked_key],
                        "type": "calls",
                        "label": "calls",
                        "confidence": "explicit",
                    },
                )
            if linked_key in object_ids:
                _add_edge(
                    edges_by_id,
                    {
                        "source": source_id,
                        "target": object_ids[linked_key],
                        "type": "uses_object",
                        "label": "uses",
                        "confidence": "explicit",
                    },
                )
        for object_key, target_id in object_ids.items():
            if object_key in explicit_links:
                continue
            if _contains_key(content, object_key):
                _add_edge(
                    edges_by_id,
                    {
                        "source": source_id,
                        "target": target_id,
                        "type": "mentions_object",
                        "label": "mentions",
                        "confidence": "inferred",
                    },
                )


def _add_generated_object_edges(
    *,
    edges_by_id: Dict[str, Dict[str, Any]],
    objects_dir: Path,
    object_ids: Dict[str, str],
) -> None:
    if not objects_dir.is_dir():
        return
    for path in sorted(objects_dir.glob("*.md"), key=lambda item: item.stem):
        if path.name.lower() == "index.md":
            continue
        source_id = object_ids.get(_resource_key(path))
        if not source_id:
            continue
        content = _read_text(path)
        explicit_links = {link.strip() for link in _WIKI_LINK_RE.findall(content) if link.strip()}
        for object_key, target_id in object_ids.items():
            if target_id == source_id:
                continue
            if object_key in explicit_links or _contains_key(content, object_key):
                _add_edge(
                    edges_by_id,
                    {
                        "source": source_id,
                        "target": target_id,
                        "type": "object_reference",
                        "label": "references",
                        "confidence": "explicit" if object_key in explicit_links else "inferred",
                    },
                )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _resource_key(path: Path) -> str:
    return str(read_markdown_frontmatter(path).get("key") or path.stem).strip()


def _contains_key(content: str, key: str) -> bool:
    key = str(key or "").strip()
    if len(key) < 3:
        return False
    if not content:
        return False
    if re.fullmatch(r"[A-Za-z0-9_ -]+", key):
        pattern = r"(?<![A-Za-z0-9_]){}(?![A-Za-z0-9_])".format(re.escape(key))
        return re.search(pattern, content) is not None
    return key in content


def _add_edge(edges_by_id: Dict[str, Dict[str, Any]], edge: Dict[str, Any]) -> None:
    source = str(edge.get("source") or "").strip()
    target = str(edge.get("target") or "").strip()
    edge_type = str(edge.get("type") or "related").strip() or "related"
    if not source or not target or source == target:
        return
    edge_id = f"{source}:{edge_type}:{target}"
    current = edges_by_id.get(edge_id)
    normalized = {
        "source": source,
        "target": target,
        "type": edge_type,
        "label": str(edge.get("label") or "").strip(),
        "confidence": str(edge.get("confidence") or "inferred").strip() or "inferred",
    }
    if current and current.get("confidence") == "explicit":
        return
    edges_by_id[edge_id] = normalized


def _type_order(value: Any) -> int:
    return {"ontology": 0, "operation": 1, "object": 2}.get(str(value or ""), 9)
