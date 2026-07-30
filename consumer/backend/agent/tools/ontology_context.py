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
    find_script_entry,
    find_markdown_file,
    format_markdown_payload_compact,
    format_root_payload_compact,
    get_ontology_manifest,
    load_script_catalog,
    read_required_text,
    resolve_object_reference,
)
from utils.read_docs import build_read_document_record, normalize_read_documents_registry

logger = get_logger("tools.ontology_context")


@dataclass(frozen=True)
class _OntologyContextResult:
    content: str
    resolved_path: Optional[Path] = None
    resolved_key: str = ""


ONTOLOGY_CONTEXT_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ontology_context",
        "description": (
            "Load ontology root catalogs, operation, object/entity, rule, metric, and script specs. "
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
                    "enum": ["root", "operation", "object", "rule", "metric", "script"],
                    "description": (
                        "The ontology resource to load: root returns ontology metadata and catalogs; "
                        "operation returns operation content by key; object returns object/entity definition by key; "
                        "rule returns rule content by key; metric returns metric content by key; "
                        "script returns registered script description by key or bound operation key."
                    ),
                },
                "key": {
                    "type": "string",
                    "description": (
                        "Resource key when loading operation/object/rule/metric/script. "
                        "For resource='object', the object key, title, canonical_key, or declared alias is accepted. "
                        "Leave empty for `root`."
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
    ontology_version: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> str:
    return _handle_ontology_context_result(
        ontology_id=ontology_id,
        resource=resource,
        key=key,
        ontology_version=ontology_version,
        workspace_id=workspace_id,
    ).content


def _handle_ontology_context_result(
    *,
    ontology_id: str,
    resource: str,
    key: str = "",
    ontology_version: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> _OntologyContextResult:
    manifest_kwargs = {"workspace_id": workspace_id}
    if str(ontology_version or "").strip():
        manifest_kwargs["version"] = ontology_version
    ontology, error = get_ontology_manifest(ontology_id, **manifest_kwargs)
    if error:
        return _OntologyContextResult(content=error)
    assert ontology is not None

    normalized_resource = str(resource or "").strip()
    ontology_dir = Path(ontology.ontology_dir).resolve()

    if normalized_resource == "root":
        return _OntologyContextResult(
            content=_build_root_payload(ontology=ontology, ontology_dir=ontology_dir),
            resolved_path=Path(ontology.manifest_path).resolve(),
        )
    if normalized_resource == "operation":
        return _build_markdown_result(
            ontology_id=ontology.id,
            ontology_dir=ontology_dir,
            directory=ontology_dir / "operations",
            key=key,
            resource_name="operation",
            key_field="operation_key",
        )
    if normalized_resource == "object":
        resolution, resolution_error = resolve_object_reference(ontology_dir, key)
        if resolution_error:
            return _OntologyContextResult(content=resolution_error)
        assert resolution is not None
        content, read_error = read_required_text(
            resolution.path,
            "object '{}'".format(key),
        )
        if read_error:
            return _OntologyContextResult(content=read_error)
        return _OntologyContextResult(
            content=format_markdown_payload_compact(
                key_field="object_key",
                key=resolution.object_key,
                content=content or "",
                requested_key=resolution.requested_key,
                matched_by=resolution.matched_by,
            ),
            resolved_path=resolution.path,
            resolved_key=resolution.object_key,
        )
    if normalized_resource == "rule":
        return _build_markdown_result(
            ontology_id=ontology.id,
            ontology_dir=ontology_dir,
            directory=ontology_dir / "rules",
            key=key,
            resource_name="rule",
            key_field="rule_key",
        )
    if normalized_resource == "metric":
        return _build_markdown_result(
            ontology_id=ontology.id,
            ontology_dir=ontology_dir,
            directory=ontology_dir / "metrics",
            key=key,
            resource_name="metric",
            key_field="metric_key",
        )
    if normalized_resource == "script":
        resolved_key = _resolve_script_resource_key(ontology_dir, key)
        return _build_markdown_result(
            ontology_id=ontology.id,
            ontology_dir=ontology_dir,
            directory=ontology_dir / "scripts",
            key=resolved_key,
            resource_name="script",
            key_field="script_key",
        )
    return _OntologyContextResult(
        content=(
            "Error: Unsupported ontology_context resource '{}'. Use root, operation, object, rule, metric, or script."
        ).format(normalized_resource)
    )


def _resolve_script_resource_key(ontology_dir: Path, key: str) -> str:
    entry = find_script_entry(load_script_catalog(ontology_dir), key)
    if not entry:
        return str(key or "").strip()
    return str(entry.get("script_key") or key or "").strip()


def _build_root_payload(*, ontology: Any, ontology_dir: Path) -> str:
    payload = build_root_payload(ontology=ontology, ontology_dir=ontology_dir)
    return "\n".join(format_root_payload_compact(ontology_id=ontology.id, root_payload=payload))


def _build_markdown_result(
    *,
    ontology_id: str,
    ontology_dir: Path,
    directory: Path,
    key: str,
    resource_name: str,
    key_field: str,
) -> _OntologyContextResult:
    path, error = find_markdown_file(directory.resolve(), key, resource_name)
    if error:
        return _OntologyContextResult(content=error)
    assert path is not None

    content, read_error = read_required_text(path, "{} '{}'".format(resource_name, key))
    if read_error:
        return _OntologyContextResult(content=read_error)

    return _OntologyContextResult(
        content=format_markdown_payload_compact(
            key_field=key_field,
            key=str(key or "").strip(),
            content=content or "",
        ),
        resolved_path=path,
        resolved_key=str(key or "").strip(),
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
        result = _handle_ontology_context_result(
            **args,
            ontology_version=(
                context.selected_ontology_version
                if str(args.get("ontology_id") or "").strip() == str(context.selected_ontology_id or "").strip()
                else None
            ),
            workspace_id=context.workspace_id,
        )
        content = result.content
        state_updates: Dict[str, Any] = {}
        resolved_args = dict(args)
        if result.resolved_key and str(args.get("resource") or "").strip() == "object":
            resolved_args["key"] = result.resolved_key

        record = _build_read_record(
            args=resolved_args,
            context=context,
            content=content,
            resolved_path=result.resolved_path,
        )
        if record:
            state_updates["read_document_records"] = [record]
        recall_records: list[Dict[str, Any]] = []
        if result.resolved_path is not None:
            content, recall_records = _append_near_point_recall(
                content=content,
                args=resolved_args,
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
    resolved_path: Optional[Path],
) -> Optional[Dict[str, Any]]:
    if (
        not context.project_root
        or resolved_path is None
        or str(content or "").startswith("Error:")
    ):
        return None

    manifest_kwargs = {"workspace_id": context.workspace_id}
    if (
        str(args.get("ontology_id") or "").strip() == str(context.selected_ontology_id or "").strip()
        and str(context.selected_ontology_version or "").strip()
    ):
        manifest_kwargs["version"] = context.selected_ontology_version
    ontology, error = get_ontology_manifest(
        str(args.get("ontology_id") or ""),
        **manifest_kwargs,
    )
    if error or ontology is None:
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
        ontology_version=str(getattr(ontology, "version", "") or "").strip() or None,
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
