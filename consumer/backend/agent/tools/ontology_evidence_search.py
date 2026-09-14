"""Agent tool for deterministic ontology evidence retrieval."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Dict, Optional, Set

from agent.tools.base import ToolCaller, ToolContext, ToolOutcome
from config.evidence_config import EvidenceConfig
from ontology.evidence.manager import (
    EvidenceIndexManager,
    EvidenceIndexManagerError,
    active_pointer_is_serviceable,
    read_active_pointer_snapshot,
)
from ontology.evidence.service import EvidenceSearchError, EvidenceSearchService
from ontology.mutations import read_mutation_state_snapshot
from utils.read_docs import normalize_read_documents_registry
from workspace.workspaces import get_workspace_paths

ONTOLOGY_EVIDENCE_SEARCH_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ontology_evidence_search",
        "description": (
            "Default first discovery tool for ontology-backed factual questions. Search the selected "
            "ontology package for authoritative definitions, rules, operations, metrics, scripts, and "
            "controlled assets before loading full detail. Use the returned "
            "ontology_context next_action to load evidence-backed detail; do not treat snippets as "
            "complete business data."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "ontology_id": {
                    "type": "string",
                    "description": "The currently selected ontology id.",
                },
                "query": {
                    "type": "string",
                    "description": "A natural-language ontology evidence query, up to 512 characters.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                },
            },
            "required": ["ontology_id", "query"],
        },
    },
}


def is_ontology_evidence_search_visible(
    *,
    workspace_id: Optional[str],
    selected_ontology_id: Optional[str],
) -> bool:
    """Cheap schema visibility check that never opens SQLite."""
    workspace = str(workspace_id or "").strip()
    ontology_id = str(selected_ontology_id or "").strip()
    config = EvidenceConfig.load()
    if not workspace or not config.exposes_search_tool(workspace):
        return False
    if config.tool_exposure_mode == "always":
        return True
    if not ontology_id:
        return False
    try:
        pointer = read_active_pointer_snapshot(get_workspace_paths(workspace).runtime_dir, ontology_id)
    except (ValueError, EvidenceIndexManagerError):
        return False
    return bool(active_pointer_is_serviceable(pointer) and pointer.get("epoch_status") == "ready")


@dataclass(frozen=True)
class OntologyEvidenceSearchTool:
    name: str = "ontology_evidence_search"
    schema: Dict[str, Any] = None  # type: ignore[assignment]
    allowed_callers: Set[ToolCaller] = None  # type: ignore[assignment]
    concurrency_safe: bool = True
    max_parallelism: Optional[int] = None
    category: str = "regular"

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", ONTOLOGY_EVIDENCE_SEARCH_TOOL_SCHEMA)
        object.__setattr__(self, "allowed_callers", {"main", "subagent"})

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def execute(
        self,
        *,
        args: Dict[str, Any],
        context: ToolContext,
        tool_call_id: Optional[str] = None,
        event_emitter: Optional[Callable[..., Any]] = None,
    ) -> ToolOutcome:
        del tool_call_id, event_emitter
        workspace_id = str(context.workspace_id or "").strip()
        ontology_id = str(args.get("ontology_id") or "").strip()
        query = str(args.get("query") or "").strip()
        limit = args.get("limit", 5)
        config = EvidenceConfig.load()

        if not workspace_id:
            return ToolOutcome(content=_error("WORKSPACE_REQUIRED", "缺少工作空间上下文。"))
        if not config.exposes_search_tool(workspace_id):
            return ToolOutcome(content=_error("EVIDENCE_FEATURE_DISABLED", "当前工作空间未启用本体证据检索。"))
        selected = str(context.selected_ontology_id or "").strip()
        if selected and ontology_id != selected:
            return ToolOutcome(content=_error("ONTOLOGY_NOT_FOUND", "目标本体不是当前选中的知识包。"))

        runtime_dir = get_workspace_paths(workspace_id).runtime_dir
        mutation_state_before = read_mutation_state_snapshot(
            runtime_dir,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
        )
        try:
            pointer_before = read_active_pointer_snapshot(runtime_dir, ontology_id)
        except EvidenceIndexManagerError as exc:
            return ToolOutcome(content=_manager_error(exc))
        if not pointer_before:
            return ToolOutcome(content=_error("INDEX_BUILDING", "当前知识包的证据索引尚未就绪。", True, 2000))

        mutation_state_after_pointer = read_mutation_state_snapshot(
            runtime_dir,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
        )
        if (runtime_dir / "ontology-mutations.sqlite3").is_file() and not _state_pointer_consistent(
            mutation_state_before,
            pointer_before,
            mutation_state_after_pointer,
        ):
            return ToolOutcome(content=_error("INDEX_STALE", "知识包变更状态与证据索引不一致，请稍后重试。", True, 500))

        pointer_error = _pointer_error(pointer_before)
        if pointer_error:
            return ToolOutcome(content=pointer_error)
        context_error = _validate_root_context(context.read_documents, ontology_id, pointer_before)
        if context_error:
            return ToolOutcome(content=context_error)

        manager = EvidenceIndexManager(runtime_dir)
        try:
            index = manager.open_active(ontology_id)
            response = EvidenceSearchService(
                index,
                workspace_id=workspace_id,
                graph_enabled=config.graph_enabled,
            ).search(
                ontology_id=ontology_id,
                query=query,
                limit=limit,
            )
            pointer_after = manager.active_pointer(ontology_id)
            mutation_state_after_query = read_mutation_state_snapshot(
                runtime_dir,
                workspace_id=workspace_id,
                ontology_id=ontology_id,
            )
        except EvidenceSearchError as exc:
            return ToolOutcome(content=_error(exc.code, str(exc)))
        except EvidenceIndexManagerError as exc:
            return ToolOutcome(content=_manager_error(exc))

        if not _same_pointer(pointer_before, pointer_after):
            return ToolOutcome(
                content=_error("INDEX_STALE", "检索期间知识包版本发生变化，请重新加载本体根上下文。", True, 500)
            )
        if (runtime_dir / "ontology-mutations.sqlite3").is_file() and not _same_mutation_state(
            mutation_state_after_pointer,
            mutation_state_after_query,
        ):
            return ToolOutcome(
                content=_error("INDEX_STALE", "检索期间知识包版本发生变化，请重新加载本体根上下文。", True, 500)
            )
        _mark_loaded_hits(response, context.read_documents)
        return ToolOutcome(content=json.dumps(response, ensure_ascii=False, separators=(",", ":")))


def _validate_root_context(
    read_documents: Optional[Dict[str, Any]],
    ontology_id: str,
    pointer: Dict[str, Any],
) -> Optional[str]:
    registry = normalize_read_documents_registry(read_documents)
    roots = []
    for record in registry["by_path"].values():
        if _is_root_context_record(record, ontology_id):
            roots.append(record)
    if not roots:
        return _error("ROOT_REQUIRED", "请先加载当前知识包的根上下文。")
    expected_epoch = str(pointer.get("expected_epoch") or "")
    expected_revision = int(pointer.get("mutation_revision") or 0)
    if any(
        str(record.get("ontology_epoch") or "").removeprefix("sha256:") == expected_epoch.removeprefix("sha256:")
        and int(record.get("mutation_revision") if record.get("mutation_revision") is not None else -1)
        == expected_revision
        for record in roots
    ):
        return None
    return _error("CONTEXT_STALE", "已加载的知识包根上下文不是当前版本，请重新加载。")


def _is_root_context_record(record: Dict[str, Any], ontology_id: str) -> bool:
    if str(record.get("ontology_id") or "") != ontology_id:
        return False
    if str(record.get("doc_type") or "") not in {"manifest", "ontology_index"}:
        return False
    return str(record.get("source") or "") in {"preload", "ontology_context"}


def _pointer_error(pointer: Dict[str, Any]) -> Optional[str]:
    status = str(pointer.get("status") or "")
    if active_pointer_is_serviceable(pointer):
        return None
    if status in {"epoch_pending", "queued", "building", "building_base", "manual_required"}:
        return _error("INDEX_BUILDING", "当前知识包的证据索引正在构建。", True, 2000)
    if status in {"failed", "epoch_failed", "base_failed"}:
        return _error("INDEX_BUILD_FAILED", "当前知识包的证据索引构建失败。")
    return _error("INDEX_STALE", "当前知识包的证据索引已失效。", True, 1000)


def _same_pointer(before: Dict[str, Any], after: Optional[Dict[str, Any]]) -> bool:
    if not after:
        return False
    fields = ("mutation_revision", "expected_epoch", "active_generation", "status")
    return all(before.get(field) == after.get(field) for field in fields)


def _same_mutation_state(
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
) -> bool:
    if not before or not after:
        return False
    fields = ("workspace_id", "ontology_id", "active_version", "enabled", "mutation_revision")
    return all(before.get(field) == after.get(field) for field in fields)


def _state_pointer_consistent(
    state_before: Optional[Dict[str, Any]],
    pointer: Dict[str, Any],
    state_after: Optional[Dict[str, Any]],
) -> bool:
    if not _same_mutation_state(state_before, state_after) or not state_after:
        return False
    return bool(
        state_after.get("enabled")
        and int(state_after.get("mutation_revision") if state_after.get("mutation_revision") is not None else -1)
        == int(pointer.get("mutation_revision") if pointer.get("mutation_revision") is not None else -2)
        and str(state_after.get("active_version") or "") == str(pointer.get("ontology_version") or "")
    )


def _mark_loaded_hits(response: Dict[str, Any], read_documents: Optional[Dict[str, Any]]) -> None:
    grounding = normalize_read_documents_registry(read_documents)["grounding_index"]["by_evidence_id"]
    expected_epoch = str(response.get("ontology_epoch") or "").removeprefix("sha256:")
    for hit in response.get("hits") or []:
        binding = grounding.get(str(hit.get("evidence_id") or "")) or {}
        binding_epoch = str(binding.get("ontology_epoch") or "").removeprefix("sha256:")
        hit["loaded_before"] = bool(binding and binding_epoch == expected_epoch)


def _manager_error(exc: EvidenceIndexManagerError) -> str:
    retryable = exc.code in {"INDEX_BUILDING", "INDEX_STALE"}
    return _error(exc.code, str(exc), retryable, 1000 if retryable else None)


def _error(code: str, message: str, retryable: bool = False, retry_after_ms: Optional[int] = None) -> str:
    error: Dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": bool(retryable),
    }
    if retry_after_ms is not None:
        error["retry_after_ms"] = int(retry_after_ms)
    return json.dumps(
        {"contract": "OntologyEvidenceSearch/v1", "success": False, "error": error},
        ensure_ascii=False,
        separators=(",", ":"),
    )
