"""Ontology context tool for loading ontology specs by semantic keys."""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

from agent.tools.base import ToolCaller, ToolContext, ToolOutcome
from agent.evolution import build_near_point_block
from utils.logging import get_logger
from ontology.ontology_catalog import (
    build_root_payload,
    find_asset_file,
    find_markdown_file,
    format_markdown_payload_compact,
    format_root_payload_compact,
    get_ontology_manifest,
    read_required_asset_text,
    read_required_text,
)
from utils.read_docs import build_read_document_record, normalize_read_documents_registry

logger = get_logger("tools.ontology_context")


ONTOLOGY_CONTEXT_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ontology_context",
        "description": (
            "Load ontology root catalogs, operation markdown, object/entity definitions, and shared asset files. "
            "This tool reads ontology specs only; it does not query business data or execute scripts. "
            "Load each needed ontology resource once and reuse it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ontology_id": {
                    "type": "string",
                    "description": "The ontology id selected from the available ontology list.",
                },
                "resource": {
                    "type": "string",
                    "enum": ["root", "operation", "object", "asset"],
                    "description": (
                        "The ontology resource to load: root returns ontology metadata and catalogs; "
                        "operation returns operation content by key; object returns object/entity definition by key; "
                        "asset returns a shared text/config file by key from assets_catalog."
                    ),
                },
                "key": {
                    "type": "string",
                    "description": (
                        "Operation key when resource is `operation`, or object/entity key when "
                        "resource is `object`, or asset file key when resource is `asset`. Leave empty for `root`."
                    ),
                },
            },
            "required": ["ontology_id", "resource"],
        },
    },
}


def handle_ontology_context(
    *,
    ontology_id: str,
    resource: str,
    key: str = "",
) -> str:
    ontology, error = get_ontology_manifest(ontology_id)
    if error:
        return error
    assert ontology is not None

    normalized_resource = str(resource or "").strip()
    ontology_dir = Path(ontology.ontology_dir).resolve()

    if normalized_resource == "root":
        return _build_root_payload(ontology=ontology, ontology_dir=ontology_dir)
    if normalized_resource == "operation":
        return _build_markdown_payload(
            ontology_id=ontology.id,
            ontology_dir=ontology_dir,
            directory=ontology_dir / "operations",
            key=key,
            resource_name="operation",
            key_field="operation_key",
        )
    if normalized_resource == "object":
        return _build_markdown_payload(
            ontology_id=ontology.id,
            ontology_dir=ontology_dir,
            directory=ontology_dir / "objects",
            key=key,
            resource_name="object",
            key_field="object_key",
        )
    if normalized_resource == "asset":
        return _build_asset_payload(
            ontology_dir=ontology_dir,
            trusted_root=Path(ontology.manifest_path).resolve().parent,
            key=key,
        )
    return "Error: Unsupported ontology_context resource '{}'. Use root, operation, object, or asset.".format(
        normalized_resource
    )


def _build_root_payload(*, ontology: Any, ontology_dir: Path) -> str:
    payload = build_root_payload(ontology=ontology, ontology_dir=ontology_dir)
    return "\n".join(format_root_payload_compact(ontology_id=ontology.id, root_payload=payload))


def _build_markdown_payload(
    *,
    ontology_id: str,
    ontology_dir: Path,
    directory: Path,
    key: str,
    resource_name: str,
    key_field: str,
) -> str:
    path, error = find_markdown_file(directory.resolve(), key, resource_name)
    if error:
        return error
    assert path is not None

    content, read_error = read_required_text(path, "{} '{}'".format(resource_name, key))
    if read_error:
        return read_error

    return format_markdown_payload_compact(
        key_field=key_field,
        key=str(key or "").strip(),
        content=content or "",
    )


def _build_asset_payload(
    *,
    ontology_dir: Path,
    trusted_root: Path,
    key: str,
) -> str:
    path, error = find_asset_file(
        ontology_dir,
        key,
        trusted_root=trusted_root,
    )
    if error:
        return error
    assert path is not None

    content, read_error = read_required_asset_text(path, "asset '{}'".format(key))
    if read_error:
        return read_error

    return format_markdown_payload_compact(
        key_field="asset_key",
        key=str(key or "").strip().replace("\\", "/").strip("/"),
        content=content or "",
    )


@dataclass(frozen=True)
class OntologyContextTool:
    name: str = "ontology_context"
    schema: Dict[str, Any] = None  # type: ignore[assignment]
    allowed_callers: Set[ToolCaller] = None  # type: ignore[assignment]
    concurrency_safe: bool = True
    max_parallelism: Optional[int] = None
    category: str = "regular"

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", ONTOLOGY_CONTEXT_TOOL_SCHEMA)
        object.__setattr__(self, "allowed_callers", {"main", "subagent", "acp"})

    def execute(
        self,
        *,
        args: Dict[str, Any],
        context: ToolContext,
        tool_call_id: Optional[str] = None,
        event_emitter: Optional[Callable[..., Any]] = None,
    ) -> ToolOutcome:
        del tool_call_id, event_emitter
        content = handle_ontology_context(**args)
        state_updates: Dict[str, Any] = {}

        record = _build_read_record(args=args, context=context, content=content)
        if record:
            state_updates["read_document_records"] = [record]
        content, recall_records = _append_near_point_recall(
            content=content,
            args=args,
            context=context,
        )
        if recall_records:
            state_updates["evolution_recall_records"] = recall_records
        return ToolOutcome(content=content, state_updates=state_updates)


def _build_read_record(
    *,
    args: Dict[str, Any],
    context: ToolContext,
    content: str,
) -> Optional[Dict[str, Any]]:
    if not context.project_root or str(content or "").startswith("Error:"):
        return None

    ontology, error = get_ontology_manifest(str(args.get("ontology_id") or ""))
    if error or ontology is None:
        return None

    resource = str(args.get("resource") or "").strip()
    key = str(args.get("key") or "").strip()
    ontology_dir = Path(ontology.ontology_dir).resolve()
    if resource == "root":
        resolved_path = Path(ontology.manifest_path).resolve()
    elif resource == "operation":
        resolved_path = ontology_dir / "operations" / "{}.md".format(key)
    elif resource == "object":
        resolved_path = ontology_dir / "objects" / "{}.md".format(key)
    elif resource == "asset":
        resolved_path, error = find_asset_file(
            ontology_dir,
            key,
            trusted_root=Path(ontology.manifest_path).resolve().parent,
        )
        if error or resolved_path is None:
            return None
    else:
        return None

    try:
        file_path = str(resolved_path.resolve().relative_to(Path(context.project_root).resolve())).replace("\\", "/")
    except ValueError:
        return None
    return build_read_document_record(
        file_path=file_path,
        resolved_path=resolved_path,
        content=content,
        source="ontology_context",
        turn_id=context.turn_id,
        iteration=context.agent_iteration,
        ontology_version=str(getattr(ontology, "active_version", "") or "").strip() or None,
    )


def _append_near_point_recall(
    *,
    content: str,
    args: Dict[str, Any],
    context: ToolContext,
) -> tuple[str, list[Dict[str, Any]]]:
    if str(content or "").startswith("Error:"):
        return content, []

    resource = str(args.get("resource") or "").strip()
    ontology_id = str(args.get("ontology_id") or "").strip()
    key = str(args.get("key") or "").strip()
    operation_key: Optional[str] = None
    entity_key: Optional[str] = None

    if resource == "operation":
        operation_key = key
    elif resource == "object":
        entity_key = key
        operation_key = _infer_recent_operation_key(
            read_documents=context.read_documents,
            ontology_id=ontology_id,
        )
    else:
        return content, []

    if not ontology_id:
        return content, []
    if resource == "operation" and not operation_key:
        return content, []

    block, fingerprints = build_near_point_block(
        workspace_id=context.workspace_id,
        ontology_id=ontology_id,
        operation_key=operation_key,
        entity_key=entity_key,
    )
    if not block or not fingerprints:
        return content, []

    record = {
        "workspace_id": context.workspace_id,
        "ontology_id": ontology_id,
        "operation_key": operation_key,
        "entity_key": entity_key,
        "fingerprints": list(fingerprints),
    }
    return "{}\n\n{}".format(str(content or "").rstrip(), block), [record]


def _infer_recent_operation_key(
    *,
    read_documents: Optional[Dict[str, Any]],
    ontology_id: str,
) -> Optional[str]:
    registry = normalize_read_documents_registry(read_documents)
    for path in reversed(registry["recent_paths"]):
        record = dict(registry["by_path"].get(path) or {})
        if str(record.get("doc_type") or "") != "operation_doc":
            continue
        if str(record.get("ontology_id") or "") != str(ontology_id or ""):
            continue
        operation_key = str(record.get("operation_name") or "").strip()
        if operation_key:
            return operation_key
    return None
