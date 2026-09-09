"""Ontology context tool for loading ontology specs by semantic keys."""

from __future__ import annotations
import copy
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
from typing import Any, Callable, Dict, Optional, Set

from agent.tools.base import ToolCaller, ToolContext, ToolOutcome
from agent.evolution import build_near_point_block
from utils.logging import get_logger
from ontology.capabilities import (
    BUSINESS_CHECKPOINT_TEMPLATE_KEY,
    is_business_checkpoint_available,
)
from ontology.ontology_catalog import (
    build_root_payload,
    find_asset_file,
    find_script_entry,
    find_markdown_file,
    format_markdown_payload_compact,
    format_root_payload_compact,
    get_ontology_manifest,
    get_object_value_bound_fields,
    load_script_catalog,
    read_required_text,
    read_required_asset_text,
    resolve_object_reference,
)
from ontology.package_browser import PackageBrowserError, read_package_text
from ontology.resource_parser import parse_resource_cached
from ontology.resource_types import ResourceSource
from ontology.evidence.manager import (
    EvidenceIndexManager,
    EvidenceIndexManagerError,
    active_pointer_is_serviceable,
    read_active_pointer_snapshot,
)
from config.evidence_config import EvidenceConfig
from workspace.workspaces import get_workspace_paths
from utils.read_docs import (
    build_grounding_subject_key,
    build_read_document_record,
    normalize_read_documents_registry,
)
from ontology.operation_execution import (
    is_protected_handler_descriptor,
    is_protected_operation_descriptor,
)

logger = get_logger("tools.ontology_context")

_TEXT_RESOURCES = ("root", "operation", "object", "rule", "metric", "script")


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
            "Load ontology root catalogs, operation, object/entity, rule, metric, script, and CheckPoint template specs. "
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
                    "enum": ["root", "operation", "object", "rule", "metric", "script", "asset"],
                    "description": (
                        "The ontology resource to load: root returns ontology metadata and catalogs; "
                        "operation returns operation content by key; object returns object/entity definition by key; "
                        "rule returns rule content by key; metric returns metric content by key; "
                        "script returns registered script description by key or bound operation key; "
"asset is accepted only "
                        "when response_format is structured. "
                        "asset returns only business-checkpoint-template.md "
                        "when CheckPoint is enabled."
                    ),
                },
                "key": {
                    "type": "string",
                    "description": (
                        "Resource key when loading operation/object/rule/metric/script/asset. "
                        "For resource='object', the object key, title, canonical_key, or declared alias is accepted. "
                        "Leave empty for `root`."
                    ),
                },
                "section": {
                    "type": "string",
                    "maxLength": 256,
                    "description": "Optional exact section anchor for structured detail loading.",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 20000,
                    "default": 8000,
                },
                "response_format": {
                    "type": "string",
                    "enum": ["text", "structured"],
                    "default": "text",
                },
            },
            "required": ["ontology_id", "resource"],
        },
    },
}


def get_ontology_context_schema(*, structured_enabled: bool) -> Dict[str, Any]:
    """Return the request-visible schema without mutating the execution schema."""
    schema = copy.deepcopy(ONTOLOGY_CONTEXT_TOOL_SCHEMA)
    if structured_enabled:
        return schema
    properties = schema["function"]["parameters"]["properties"]
    properties["resource"]["enum"] = list(_TEXT_RESOURCES)
    properties["resource"]["description"] = properties["resource"]["description"].split(
        "; asset is accepted only",
        1,
    )[0] + "."
    for field in ("section", "max_chars", "response_format"):
        properties.pop(field, None)
    return schema


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
    if ontology is None:
        return _OntologyContextResult(content="Error: ontology manifest is unavailable.")

    normalized_resource = str(resource or "").strip()
    ontology_dir = Path(ontology.ontology_dir).resolve()

    if normalized_resource == "root":
        return _OntologyContextResult(
            content=_build_root_payload(
                ontology=ontology,
                ontology_dir=ontology_dir,
                workspace_id=workspace_id,
            ),
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
            path_protection=lambda path: is_protected_operation_descriptor(
                ontology_dir,
                path,
            ),
        )
    if normalized_resource == "object":
        resolution, resolution_error = resolve_object_reference(ontology_dir, key)
        if resolution_error:
            return _OntologyContextResult(content=resolution_error)
        if resolution is None:
            return _OntologyContextResult(content="Error: object reference is unavailable.")
        content, read_error = read_required_text(
            resolution.path,
            "object '{}'".format(key),
        )
        if read_error:
            return _OntologyContextResult(content=read_error)
        rendered = format_markdown_payload_compact(
            key_field="object_key",
            key=resolution.object_key,
            content=content or "",
            requested_key=resolution.requested_key,
            matched_by=resolution.matched_by,
        )
        bound_fields = get_object_value_bound_fields(
            ontology_dir,
            resolution.object_key,
        )
        if bound_fields:
            rendered = "{}\n\nruntime_field_context:\n{}".format(
                rendered.rstrip(),
                "\n".join(
                    "- field_key: {}; value_bound: true".format(field_key)
                    for field_key in bound_fields
                ),
            )
        return _OntologyContextResult(
            content=rendered,
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
        script_entry = find_script_entry(load_script_catalog(ontology_dir), key)
        resolved_key = str(
            (script_entry or {}).get("script_key") or key or ""
        ).strip()
        return _build_markdown_result(
            ontology_id=ontology.id,
            ontology_dir=ontology_dir,
            directory=ontology_dir / "scripts",
            key=resolved_key,
            resource_name="script",
            key_field="script_key",
            path_protection=lambda path: is_protected_handler_descriptor(
                ontology_dir,
                path,
            ),
        )
    if normalized_resource == "asset":
        normalized_key = str(key or "").replace("\\", "/").strip("/")
        if normalized_key != BUSINESS_CHECKPOINT_TEMPLATE_KEY:
            return _OntologyContextResult(
                content=(
                    "Error: ontology_context resource 'asset' only supports '{}'."
                ).format(BUSINESS_CHECKPOINT_TEMPLATE_KEY)
            )
        if not is_business_checkpoint_available(
            ontology.id,
            ontology_version=str(getattr(ontology, "version", "") or "").strip() or None,
            workspace_id=workspace_id,
        ):
            return _OntologyContextResult(
                content="Error: CheckPoint is disabled or its package template is unavailable."
            )
        path, asset_error = find_asset_file(ontology_dir, BUSINESS_CHECKPOINT_TEMPLATE_KEY)
        if asset_error:
            return _OntologyContextResult(content=asset_error)
        if path is None:
            return _OntologyContextResult(content="Error: CheckPoint template is unavailable.")
        content, read_error = read_required_asset_text(
            path,
            "asset '{}'".format(BUSINESS_CHECKPOINT_TEMPLATE_KEY),
        )
        if read_error:
            return _OntologyContextResult(content=read_error)
        return _OntologyContextResult(
            content="asset_key: {}\n\n{}".format(BUSINESS_CHECKPOINT_TEMPLATE_KEY, content or ""),
            resolved_path=path,
            resolved_key=BUSINESS_CHECKPOINT_TEMPLATE_KEY,
        )
    return _OntologyContextResult(
        content=(
            "Error: Unsupported ontology_context resource '{}'. Use root, operation, object, rule, metric, script, or asset."
        ).format(normalized_resource)
    )


def _resolve_script_resource_key(ontology_dir: Path, key: str) -> str:
    entry = find_script_entry(load_script_catalog(ontology_dir), key)
    if not entry:
        return str(key or "").strip()
    return str(entry.get("script_key") or key or "").strip()


def _build_root_payload(
    *,
    ontology: Any,
    ontology_dir: Path,
    workspace_id: Optional[str],
) -> str:
    payload = build_root_payload(ontology=ontology, ontology_dir=ontology_dir)
    if is_business_checkpoint_available(
        ontology.id,
        ontology_version=str(getattr(ontology, "version", "") or "").strip() or None,
        workspace_id=workspace_id,
    ):
        payload["assets_catalog"] = [BUSINESS_CHECKPOINT_TEMPLATE_KEY]
    return "\n".join(format_root_payload_compact(ontology_id=ontology.id, root_payload=payload))


def _build_markdown_result(
    *,
    ontology_id: str,
    ontology_dir: Path,
    directory: Path,
    key: str,
    resource_name: str,
    key_field: str,
    path_protection: Optional[Callable[[Path], bool]] = None,
) -> _OntologyContextResult:
    path, error = find_markdown_file(directory.resolve(), key, resource_name)
    if error:
        return _OntologyContextResult(content=error)
    if path is None:
        return _OntologyContextResult(
            content="Error: {} '{}' could not be resolved.".format(resource_name, key)
        )

    if path_protection is not None:
        try:
            if path_protection(path):
                return _OntologyContextResult(
                    content="Error: deterministic_operation_protected"
                )
        except Exception:
            logger.exception(
                "Failed to classify deterministic %s descriptor protection: %s",
                resource_name,
                path,
            )
            return _OntologyContextResult(
                content="Error: deterministic_operation_protection_unavailable"
            )

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
        response_format = str(args.get("response_format") or "text").strip()
        state_updates: Dict[str, Any] = {}
        if response_format == "structured":
            content, record = _build_structured_context(args=args, context=context)
            if record:
                state_updates["read_document_records"] = [record]
            return ToolOutcome(content=content, state_updates=state_updates)

        resource = str(args.get("resource") or "").strip()
        if resource not in _TEXT_RESOURCES and resource != "asset":
            allowed = json.dumps(list(_TEXT_RESOURCES), ensure_ascii=False)
            return ToolOutcome(
                content=(
                    "Error: Tool 'ontology_context' invalid argument(s): "
                    "arguments.resource must be one of {}."
                ).format(allowed)
            )
        result = _handle_ontology_context_result(
            ontology_id=str(args.get("ontology_id") or ""),
            resource=resource,
            key=str(args.get("key") or ""),
            ontology_version=(
                context.selected_ontology_version
                if str(args.get("ontology_id") or "").strip() == str(context.selected_ontology_id or "").strip()
                else None
            ),
            workspace_id=context.workspace_id,
        )
        content = result.content
        resolved_args = dict(args)
        if result.resolved_key and resource == "object":
            resolved_args["key"] = result.resolved_key
        record = _build_read_record(
            args=resolved_args,
            context=context,
            content=content,
            resolved_path=result.resolved_path,
        )
        if record:
            state_updates["read_document_records"] = [record]
        if result.resolved_path is not None:
            content, recall_records = _append_near_point_recall(
                content=content,
                args=resolved_args,
                context=context,
            )
            if recall_records:
                state_updates["evolution_recall_records"] = recall_records
        return ToolOutcome(content=content, state_updates=state_updates)


def _build_structured_context(
    *,
    args: Dict[str, Any],
    context: ToolContext,
    manager: Optional[EvidenceIndexManager] = None,
) -> tuple[str, Optional[Dict[str, Any]]]:
    workspace_id = str(context.workspace_id or "").strip()
    if not workspace_id:
        return "Error: WORKSPACE_REQUIRED", None
    config = EvidenceConfig.load()
    if config.workspace_mode(workspace_id) == "off" or not config.structured_context_enabled:
        return "Error: EVIDENCE_FEATURE_DISABLED", None

    ontology, error = get_ontology_manifest(
        str(args.get("ontology_id") or ""),
        workspace_id=workspace_id,
    )
    if error or ontology is None:
        return str(error or "Error: ONTOLOGY_NOT_FOUND"), None
    resource = str(args.get("resource") or "").strip()
    key = str(args.get("key") or "").strip()
    section_anchor = str(args.get("section") or "").strip() or None
    max_chars = int(args.get("max_chars") or 8000)
    if max_chars < 1000 or max_chars > 20000:
        return "Error: INVALID_ARGUMENTS", None

    ontology_dir = Path(ontology.ontology_dir).resolve()
    resolved_path, locate_error = _structured_resource_path(
        ontology_dir=ontology_dir,
        resource=resource,
        key=key,
    )
    if locate_error or resolved_path is None:
        return str(locate_error or "Error: RESOURCE_NOT_FOUND"), None
    relative_source_path = resolved_path.relative_to(ontology_dir).as_posix()
    active_manager = manager or EvidenceIndexManager(get_workspace_paths(workspace_id).runtime_dir)
    try:
        pointer = active_manager.active_pointer(ontology.id)
        if not active_pointer_is_serviceable(pointer):
            raise EvidenceIndexManagerError("INDEX_BUILDING", "evidence index is not ready")
    except EvidenceIndexManagerError as exc:
        return "Error: {}".format(exc.code), None
    try:
        payload = read_package_text(
            ontology_dir,
            relative_source_path,
            allowed_extensions=None,
            max_bytes=512_000,
            trusted_root=ontology_dir,
            include_sha256=True,
        )
    except PackageBrowserError as exc:
        return "Error: {}".format(exc.code.upper()), None
    raw_text = str(payload["content"])
    source_sha256 = str(payload["sha256"])
    parsed = parse_resource_cached(
        ResourceSource(
            ontology_id=ontology.id,
            ontology_version=str(ontology.active_version),
            resource=resource,
            source_path=relative_source_path,
            media_type="text/markdown" if resolved_path.suffix.lower() == ".md" else "text/plain",
            source_text=raw_text,
            source_content_sha256=source_sha256,
        ),
        workspace_id=workspace_id,
        mutation_revision=int(pointer.get("mutation_revision") or 0),
    )
    selected_sections = list(parsed.sections)
    if section_anchor:
        selected_sections = [section for section in selected_sections if section.section_anchor == section_anchor]
        if not selected_sections:
            available = [section.section_anchor for section in parsed.sections]
            return json.dumps(
                {"contract": "OntologyContext/v2", "success": False, "result_code": "SECTION_NOT_FOUND", "available_sections": available},
                ensure_ascii=False,
            ), None
    content = "\n\n".join(section.text for section in selected_sections) if selected_sections else parsed.body_text
    truncated = len(content) > max_chars
    content = content[:max_chars]

    try:
        index = active_manager.open_active(ontology.id)
        pointer_after = active_manager.active_pointer(ontology.id)
    except EvidenceIndexManagerError as exc:
        return "Error: {}".format(exc.code), None
    if pointer is None:
        return "Error: INDEX_NOT_READY", None
    if not pointer_after or any(
        pointer.get(field) != pointer_after.get(field)
        for field in ("mutation_revision", "expected_epoch", "active_generation", "status")
    ):
        return "Error: INDEX_STALE", None
    evidence_chunks = index.find_chunks(resource=resource, key=parsed.key, section_anchor=section_anchor)
    evidence_chunks = [chunk for chunk in evidence_chunks if chunk["source_content_hash"] == source_sha256]
    source_evidence_ids = list(dict.fromkeys(chunk["evidence_id"] for chunk in evidence_chunks))

    file_path = _project_relative_path(resolved_path, context.project_root)
    registry = normalize_read_documents_registry(context.read_documents)
    previous = registry["by_path"].get(file_path) or {}
    record = build_read_document_record(
        file_path=file_path,
        resolved_path=resolved_path,
        content=content,
        source="ontology_context",
        turn_id=context.turn_id,
        iteration=context.agent_iteration,
        ontology_version=str(ontology.active_version),
    )
    record.update(
        {
            "mutation_revision": int(pointer.get("mutation_revision") or 0),
            "ontology_epoch": str(pointer["expected_epoch"]),
            "source_content_sha256": "sha256:" + source_sha256,
        }
    )
    bindings = [
        _grounding_binding(
            chunk=chunk,
            context=context,
            pointer=pointer,
            file_path=file_path,
            ontology_version=str(ontology.active_version),
            source_sha256=source_sha256,
        )
        for chunk in evidence_chunks
    ]
    if bindings:
        record["_grounding_bindings"] = bindings
    response = {
        "contract": "OntologyContext/v2",
        "success": True,
        "ontology_id": ontology.id,
        "resource": resource,
        "key": parsed.key,
        "section": section_anchor,
        "content": content,
        "continuation": {"truncated": truncated, "next_offset": max_chars if truncated else None},
        "context_meta": {
            "ontology_version": str(ontology.active_version),
            "mutation_revision": int(pointer.get("mutation_revision") or 0),
            "ontology_epoch": str(pointer["expected_epoch"]),
            "index_generation": str(pointer["active_generation"]),
            "source_path": relative_source_path,
            "content_hash": record["content_hash"],
            "source_content_sha256": "sha256:" + source_sha256,
            "loaded_before": bool(previous),
            "read_count_before": int(previous.get("read_count") or 0),
            "grounding_recorded": bool(bindings),
            "source_evidence_ids": source_evidence_ids,
        },
    }
    return json.dumps(response, ensure_ascii=False), record


def _structured_resource_path(*, ontology_dir: Path, resource: str, key: str) -> tuple[Optional[Path], Optional[str]]:
    if resource == "root":
        path = ontology_dir / "index.md"
        return (path, None) if path.is_file() else (None, "Error: root not found.")
    if resource == "asset":
        return find_asset_file(ontology_dir, key, trusted_root=ontology_dir)
    if resource == "object":
        resolution, error = resolve_object_reference(ontology_dir, key)
        return (resolution.path, None) if resolution is not None else (None, error)
    if resource not in {"operation", "rule", "metric", "script"}:
        return None, "Error: Unsupported structured resource."
    return find_markdown_file(ontology_dir / "{}s".format(resource), key, resource)


def _project_relative_path(path: Path, project_root: Optional[str]) -> str:
    if project_root:
        try:
            return path.resolve().relative_to(Path(project_root).resolve()).as_posix()
        except ValueError:
            pass
    return path.name


def _grounding_binding(
    *,
    chunk: Dict[str, Any],
    context: ToolContext,
    pointer: Dict[str, Any],
    file_path: str,
    ontology_version: str,
    source_sha256: str,
) -> Dict[str, Any]:
    subject_key = build_grounding_subject_key(
        workspace_id=str(context.workspace_id),
        ontology_id=str(pointer["ontology_id"]),
        ontology_epoch=str(pointer["expected_epoch"]),
        resource=str(chunk["resource"]),
        resource_key=str(chunk["resource_key"]),
    )
    return {
        "evidence_id": chunk["evidence_id"],
        "subject_key": subject_key,
        "workspace_id": str(context.workspace_id),
        "normalized_path": file_path,
        "ontology_id": str(pointer["ontology_id"]),
        "ontology_version": ontology_version,
        "ontology_epoch": str(pointer["expected_epoch"]),
        "mutation_revision": int(pointer.get("mutation_revision") or 0),
        "index_generation": str(pointer["active_generation"]),
        "resource": str(chunk["resource"]),
        "resource_key": str(chunk["resource_key"]),
        "section_anchor": str(chunk["section_anchor"]),
        "source_content_sha256": "sha256:" + source_sha256,
        "loaded_turn_id": int(context.turn_id or 0),
        "loaded_iteration": context.agent_iteration,
    }


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
    record = build_read_document_record(
        file_path=file_path,
        resolved_path=resolved_path,
        content=content,
        source="ontology_context",
        turn_id=context.turn_id,
        iteration=context.agent_iteration,
        ontology_version=str(getattr(ontology, "version", "") or "").strip() or None,
    )
    resource = str(args.get("resource") or "").strip()
    if resource == "root":
        _stamp_root_record(record, resolved_path=resolved_path, context=context, ontology_id=ontology.id)
    return record


def _stamp_root_record(
    record: Dict[str, Any],
    *,
    resolved_path: Path,
    context: ToolContext,
    ontology_id: str,
) -> None:
    workspace_id = str(context.workspace_id or "").strip()
    if not workspace_id or EvidenceConfig.load().workspace_mode(workspace_id) == "off":
        return
    try:
        pointer = read_active_pointer_snapshot(get_workspace_paths(workspace_id).runtime_dir, ontology_id)
    except (ValueError, EvidenceIndexManagerError):
        return
    if not active_pointer_is_serviceable(pointer):
        return
    try:
        source_sha256 = hashlib.sha256(resolved_path.read_bytes()).hexdigest()
    except OSError:
        source_sha256 = ""
    record.update(
        {
            "mutation_revision": int(pointer.get("mutation_revision") or 0),
            "ontology_epoch": str(pointer.get("expected_epoch") or ""),
        }
    )
    if source_sha256:
        record["source_content_sha256"] = "sha256:" + source_sha256


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
