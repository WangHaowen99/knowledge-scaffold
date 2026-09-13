"""Structured read-document registry helpers for ontology-driven execution."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ontology.hash_contract import field_h, field_s, hash_frames

RECENT_PATHS_LIMIT = 64
ACTIVE_OBJECT_DOCS_LIMIT = 24
GROUNDING_SUBJECT_LIMIT = 32


def normalize_read_path(file_path: str) -> str:
    """Normalize project-relative paths so registry keys stay stable."""
    normalized = str(file_path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def normalize_ontology_name(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def build_read_document_record(
    *,
    file_path: str,
    resolved_path: Optional[Path] = None,
    content: str = "",
    source: str,
    turn_id: int,
    iteration: Optional[int] = None,
    ontology_version: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_path = normalize_read_path(file_path)
    doc_type, ontology_id, entity_name, operation_name = _infer_document_identity(normalized_path)
    stat = None
    if resolved_path is not None:
        try:
            stat = resolved_path.stat()
        except OSError:
            stat = None

    return {
        "path": normalized_path,
        "normalized_path": normalized_path,
        "doc_type": doc_type,
        "ontology_id": ontology_id,
        "ontology_version": str(ontology_version or "").strip() or None,
        "entity_name": entity_name,
        "operation_name": operation_name,
        "description": _extract_document_description(content),
        "source": str(source or "ontology_context"),
        "turn_id": int(turn_id or 0),
        "iteration": int(iteration) if iteration is not None else None,
        "content_hash": _content_hash(content),
        "size_bytes": len(content.encode("utf-8")),
        "mtime_ns": int(stat.st_mtime_ns) if stat is not None else None,
    }


def merge_read_documents_registry(
    existing: Optional[Dict[str, Any]],
    records: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    registry = normalize_read_documents_registry(existing)
    by_path: Dict[str, Dict[str, Any]] = dict(registry["by_path"])
    recent_paths: List[str] = list(registry["recent_paths"])
    active_object_docs: List[str] = list(registry["active_object_docs"])

    for raw_record in records or []:
        if not isinstance(raw_record, dict):
            continue
        normalized_path = normalize_read_path(str(raw_record.get("normalized_path") or raw_record.get("path") or ""))
        if not normalized_path:
            continue

        raw_bindings = raw_record.get("_grounding_bindings") or []
        public_record = {key: value for key, value in raw_record.items() if not str(key).startswith("_")}
        previous = dict(by_path.get(normalized_path) or {})
        merged = {**previous, **public_record, "path": normalized_path, "normalized_path": normalized_path}

        previous_source = str(previous.get("source") or "")
        next_source = str(merged.get("source") or "")
        previous_count = int(previous.get("read_count") or 0)
        if previous and previous_source == next_source == "preload":
            merged["read_count"] = max(1, previous_count)
        else:
            merged["read_count"] = max(1, previous_count + 1)

        by_path[normalized_path] = merged
        recent_paths = _move_to_tail(recent_paths, normalized_path, limit=RECENT_PATHS_LIMIT)

        if str(merged.get("doc_type") or "") == "object_doc":
            active_object_docs = _move_to_tail(
                active_object_docs,
                normalized_path,
                limit=ACTIVE_OBJECT_DOCS_LIMIT,
            )

        for raw_binding in raw_bindings if isinstance(raw_bindings, list) else []:
            _merge_grounding_binding(registry, raw_binding, normalized_path=normalized_path)

    return {
        "by_path": by_path,
        "recent_paths": recent_paths,
        "active_object_docs": active_object_docs,
        "grounding_index": registry["grounding_index"],
    }


def normalize_read_documents_registry(value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    raw_by_path = payload.get("by_path") if isinstance(payload.get("by_path"), dict) else {}
    by_path = {
        normalize_read_path(path): dict(record)
        for path, record in raw_by_path.items()
        if normalize_read_path(path)
    }
    return {
        "by_path": by_path,
        "recent_paths": _normalize_path_list(payload.get("recent_paths"), limit=RECENT_PATHS_LIMIT),
        "active_object_docs": _normalize_path_list(
            payload.get("active_object_docs"),
            limit=ACTIVE_OBJECT_DOCS_LIMIT,
        ),
        "grounding_index": _normalize_grounding_index(payload.get("grounding_index")),
    }


def build_grounding_subject_key(
    *,
    workspace_id: str,
    ontology_id: str,
    ontology_epoch: str,
    resource: str,
    resource_key: str,
) -> str:
    digest = hash_frames(
        "grounding_subject/v1",
        [
            field_s("workspace_id", workspace_id),
            field_s("ontology_id", ontology_id),
            field_h("ontology_epoch", ontology_epoch.removeprefix("sha256:")),
            field_s("resource", resource),
            field_s("resource_key", resource_key),
        ],
    )
    return "sha256:" + digest


def get_registered_object_docs(read_documents: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    registry = normalize_read_documents_registry(read_documents)
    object_docs: Dict[str, Dict[str, Any]] = {}
    for record in registry["by_path"].values():
        if str(record.get("doc_type") or "") != "object_doc":
            continue
        entity_name = normalize_ontology_name(str(record.get("entity_name") or ""))
        if not entity_name:
            continue
        object_docs[entity_name] = dict(record)
    return object_docs


def _normalize_path_list(value: Any, *, limit: int) -> List[str]:
    result: List[str] = []
    for item in value or []:
        normalized = normalize_read_path(str(item or ""))
        if not normalized or normalized in result:
            continue
        result.append(normalized)
    if len(result) > limit:
        result = result[-limit:]
    return result


def _normalize_grounding_index(value: Any) -> Dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    raw_by_id = payload.get("by_evidence_id") if isinstance(payload.get("by_evidence_id"), dict) else {}
    by_id = {
        str(evidence_id): dict(binding)
        for evidence_id, binding in raw_by_id.items()
        if str(evidence_id).startswith("ont_ev_") and isinstance(binding, dict)
    }
    raw_by_subject = payload.get("by_subject") if isinstance(payload.get("by_subject"), dict) else {}
    by_subject: Dict[str, List[str]] = {}
    for subject_key, evidence_ids in raw_by_subject.items():
        if not str(subject_key).startswith("sha256:") or not isinstance(evidence_ids, list):
            continue
        values: List[str] = []
        for evidence_id in evidence_ids:
            normalized_id = str(evidence_id)
            if normalized_id in by_id and normalized_id not in values:
                values.append(normalized_id)
        by_subject[str(subject_key)] = values[-GROUNDING_SUBJECT_LIMIT:]
    return {"by_evidence_id": by_id, "by_subject": by_subject}


def _merge_grounding_binding(registry: Dict[str, Any], raw_binding: Any, *, normalized_path: str) -> None:
    if not isinstance(raw_binding, dict):
        return
    evidence_id = str(raw_binding.get("evidence_id") or "")
    subject_key = str(raw_binding.get("subject_key") or "")
    if not evidence_id.startswith("ont_ev_") or not subject_key.startswith("sha256:"):
        return
    binding = {key: value for key, value in raw_binding.items() if key != "evidence_id"}
    binding["normalized_path"] = normalized_path
    grounding = registry["grounding_index"]
    previous = dict(grounding["by_evidence_id"].get(evidence_id) or {})
    binding["read_count"] = max(1, int(previous.get("read_count") or 0) + 1)
    grounding["by_evidence_id"][evidence_id] = binding
    current = [value for value in grounding["by_subject"].get(subject_key, []) if value != evidence_id]
    current.append(evidence_id)
    grounding["by_subject"][subject_key] = current[-GROUNDING_SUBJECT_LIMIT:]


def _move_to_tail(items: List[str], value: str, *, limit: int) -> List[str]:
    next_items = [item for item in items if item != value]
    next_items.append(value)
    if len(next_items) > limit:
        next_items = next_items[-limit:]
    return next_items


def _extract_document_description(content: str) -> Optional[str]:
    in_frontmatter = False
    in_compact_content = False
    compact_key_values: Set[str] = set()
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered in {"content:", "markdown:"}:
            in_compact_content = True
            continue
        if not in_compact_content and lowered.startswith((
            "operation_key:",
            "object_key:",
            "ontology_id:",
            "entity_key:",
            "requested_key:",
            "matched_by:",
            "key:",
        )):
            value = line.split(":", 1)[1].strip().strip("\"'")
            if value:
                compact_key_values.add(value)
            continue
        if line == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            if lowered.startswith(("description:", "desc:", "summary:", "name:", "title:")):
                value = line.split(":", 1)[1].strip().strip("\"'")
                if value:
                    return value[:160]
            continue
        if line.startswith("#"):
            value = line.lstrip("#").strip()
            if value:
                if value in compact_key_values:
                    continue
                return value[:160]
        return line[:160]
    return None


def _infer_document_identity(file_path: str) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    normalized_path = normalize_read_path(file_path)
    parts = [segment for segment in normalized_path.split("/") if segment]
    ontology_id: Optional[str] = None
    entity_name: Optional[str] = None
    operation_name: Optional[str] = None

    if "ontologies" in parts:
        idx = parts.index("ontologies")
        if idx + 1 < len(parts):
            ontology_id = parts[idx + 1]

    path_obj = Path(normalized_path)
    filename = path_obj.name
    suffix = path_obj.suffix.lower()

    if filename == "manifest.yaml":
        return "manifest", ontology_id, None, None
    if filename == "index.md":
        parent = path_obj.parent.name.lower()
        index_types = {
            "objects": "objects_index",
            "operations": "operations_index",
            "rules": "rules_index",
            "metrics": "metrics_index",
            "scripts": "scripts_index",
        }
        return index_types.get(parent, "ontology_index"), ontology_id, None, None
    if suffix == ".md" and "objects" in parts:
        entity_name = path_obj.stem
        return "object_doc", ontology_id, entity_name, None
    if suffix == ".md" and "operations" in parts:
        operation_name = path_obj.stem
        return "operation_doc", ontology_id, None, operation_name
    if filename == "SKILL.md":
        return "skill_doc", None, None, None
    return "other", ontology_id, None, None


def _content_hash(content: str) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()
