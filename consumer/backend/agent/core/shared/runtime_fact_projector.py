"""Project tool completion into persisted, grounded Runtime Facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

from config.evidence_config import EvidenceConfig
from ontology.evidence.manager import (
    EvidenceIndexManagerError,
    active_pointer_is_serviceable,
    read_active_pointer_snapshot,
)
from ontology.hash_contract import field_h, field_i, field_n, field_s, hash_frames
from persistence.runtime_facts import RuntimeFactsStore, RuntimeFactsStoreError
from utils.read_docs import build_grounding_subject_key, normalize_read_documents_registry
from workspace.workspaces import get_workspace_paths

if TYPE_CHECKING:
    from agent.tools.base import ToolContext


@dataclass(frozen=True)
class GroundingSelector:
    ontology_id: str
    resource: str
    resource_key: str


@dataclass(frozen=True)
class GroundingLookupSnapshot:
    workspace_id: str
    runtime_dir: Any
    run_id: str
    tool_call_id: str
    tool_name: str
    ontology_id: str
    ontology_epoch: Optional[str]
    mutation_revision: Optional[int]
    index_generation: Optional[str]
    subject: str
    query_digest: str
    provider: Optional[str]
    selectors: tuple[GroundingSelector, ...]
    read_documents: Dict[str, Any]
    runtime_fact_mode: str
    runtime_fact_write_timeout_ms: int
    runtime_fact_batch_size: int
    runtime_fact_flush_interval_ms: int


def capture_business_data_snapshot(
    *,
    context: ToolContext,
    args: Mapping[str, Any],
    tool_call_id: Optional[str],
    provider: Optional[str],
) -> tuple[Optional[GroundingLookupSnapshot], str]:
    """Capture immutable grounding inputs before the Provider is called."""
    workspace_id = str(context.workspace_id or "").strip()
    config = EvidenceConfig.load()
    if not workspace_id or config.workspace_mode(workspace_id) == "off":
        return None, "inactive"
    if config.runtime_fact_mode == "off":
        return None, "disabled"

    ontology_id = str(args.get("ontology_id") or "").strip()
    entity = str(args.get("entity") or "").strip()
    query_digest = _query_digest(ontology_id=ontology_id, entity=entity, query=str(args.get("query") or ""))
    stable_call_id = str(tool_call_id or "").strip() or _fallback_tool_call_id(
        context=context,
        ontology_id=ontology_id,
        entity=entity,
        query_digest=query_digest,
    )
    runtime_dir = get_workspace_paths(workspace_id).runtime_dir
    try:
        pointer = read_active_pointer_snapshot(runtime_dir, ontology_id)
    except (ValueError, EvidenceIndexManagerError):
        pointer = None
    selectors = (
        (GroundingSelector(ontology_id=ontology_id, resource="object", resource_key=entity),)
        if ontology_id and entity
        else ()
    )
    return GroundingLookupSnapshot(
        workspace_id=workspace_id,
        runtime_dir=runtime_dir,
        run_id=str(context.run_id or "run-unknown"),
        tool_call_id=stable_call_id,
        tool_name="business_data_search",
        ontology_id=ontology_id,
        ontology_epoch=str(pointer.get("expected_epoch") or "") or None if pointer else None,
        mutation_revision=int(pointer.get("mutation_revision") or 0) if pointer else None,
        index_generation=str(pointer.get("active_generation") or "") or None if pointer else None,
        subject=entity,
        query_digest=query_digest,
        provider=str(provider or "").strip() or None,
        selectors=selectors,
        read_documents=normalize_read_documents_registry(context.read_documents),
        runtime_fact_mode=config.runtime_fact_mode,
        runtime_fact_write_timeout_ms=config.runtime_fact_write_timeout_ms,
        runtime_fact_batch_size=config.runtime_fact_batch_size,
        runtime_fact_flush_interval_ms=config.runtime_fact_flush_interval_ms,
    ), "enabled"


def project_business_data_result(
    *,
    snapshot: GroundingLookupSnapshot,
    content: str,
    provider_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate Grounding and synchronously persist one idempotent Runtime Fact."""
    parsed = _parse_result(content)
    grounding_status, warnings, derived_from = _resolve_grounding(snapshot)
    provider = str(provider_override or parsed.get("provider") or snapshot.provider or "").strip() or None
    command = {
        "workspace_id": snapshot.workspace_id,
        "run_id": snapshot.run_id,
        "tool_call_id": snapshot.tool_call_id,
        "kind": "provider_query",
        "ontology_id": snapshot.ontology_id,
        "ontology_epoch": snapshot.ontology_epoch,
        "subject": snapshot.subject,
        "status": parsed["status"],
        "summary": parsed["summary"],
        "provider": provider,
        "query_digest": snapshot.query_digest,
        "row_count": parsed.get("row_count"),
        "grounding_status": grounding_status,
        "grounding_warnings": warnings,
        "provider_evidence": _provider_evidence(parsed, provider=provider),
        "derived_from": derived_from,
        "validity_class": "current_fact",
        "read_at": _utc_now(),
    }
    try:
        facts_path = snapshot.runtime_dir / "ontology-evidence" / "runtime-facts.sqlite3"
        if snapshot.runtime_fact_mode == "batched":
            record = _batcher_for(
                facts_path,
                batch_size=snapshot.runtime_fact_batch_size,
                flush_interval_ms=snapshot.runtime_fact_flush_interval_ms,
                write_timeout_ms=snapshot.runtime_fact_write_timeout_ms,
            ).submit(command, timeout_ms=snapshot.runtime_fact_write_timeout_ms)
        else:
            record = RuntimeFactsStore(
                facts_path,
                timeout_ms=snapshot.runtime_fact_write_timeout_ms,
            ).put(command)
    except RuntimeFactPersistencePending as exc:
        return {
            "evidence_capability": "pending",
            "error_code": "RUNTIME_FACT_PERSISTENCE_UNKNOWN",
            "message": str(exc)[:240],
            "runtime_evidence": {
                "evidence_id": None,
                "persistence_status": "pending",
                "grounding_status": grounding_status,
                "grounding_warnings": warnings,
                "derived_from": derived_from,
            },
        }
    except (OSError, sqlite3.Error, RuntimeFactsStoreError, ValueError) as exc:
        return {
            "evidence_capability": "persist_failed",
            "error_code": "RUNTIME_FACT_PERSIST_FAILED",
            "message": str(exc)[:240],
        }
    return {
        "evidence_capability": "enabled",
        "runtime_evidence": {
            "evidence_id": record["evidence_id"],
            "persistence_status": record["persistence_status"],
            "grounding_status": record["grounding_status"],
            "grounding_warnings": record["grounding_warnings"],
            "derived_from": record["provenance"]["derived_from"],
        },
    }


def grounding_snapshot_is_resolved(snapshot: GroundingLookupSnapshot) -> bool:
    """Return whether a captured business-data selector has current static evidence."""
    status, _warnings, evidence_ids = _resolve_grounding(snapshot)
    return status == "grounded" and bool(evidence_ids)


@dataclass
class _PendingRuntimeFact:
    command: Mapping[str, Any]
    completed: threading.Event
    deadline: float
    state: str = "queued"
    record: Optional[dict[str, Any]] = None
    error: Optional[BaseException] = None


class RuntimeFactPersistencePending(RuntimeFactsStoreError):
    """The caller timed out after persistence had already started."""


class _RuntimeFactBatcher:
    """Batch concurrent fact writes while returning only persisted receipts."""

    def __init__(
        self,
        path: Path,
        *,
        batch_size: int,
        flush_interval_ms: int,
        write_timeout_ms: int,
    ) -> None:
        self.path = path.resolve()
        self.batch_size = max(1, min(int(batch_size), 100))
        self.flush_interval_seconds = max(0.001, int(flush_interval_ms) / 1000.0)
        self.write_timeout_ms = max(1, int(write_timeout_ms))
        self._condition = threading.Condition()
        self._queue: list[_PendingRuntimeFact] = []
        self._thread = threading.Thread(
            target=self._run,
            name="runtime-fact-batcher",
            daemon=True,
        )
        self._thread.start()

    def submit(self, command: Mapping[str, Any], *, timeout_ms: int) -> dict[str, Any]:
        timeout_seconds = max(0.001, int(timeout_ms) / 1000.0)
        pending = _PendingRuntimeFact(
            dict(command),
            threading.Event(),
            deadline=time.monotonic() + timeout_seconds,
        )
        with self._condition:
            self._queue.append(pending)
            self._condition.notify_all()
        if not pending.completed.wait(timeout_seconds):
            with self._condition:
                if pending.state == "queued":
                    # The worker must skip canceled work. This makes the
                    # persist_failed receipt deterministic: no late ghost write.
                    pending.state = "canceled"
                    self._condition.notify_all()
                    raise RuntimeFactsStoreError(
                        "Runtime Fact batched write timed out before persistence"
                    )
                if pending.state == "writing":
                    # SQLite work cannot be canceled safely once its transaction
                    # has started. Report an unknown/pending receipt instead of
                    # claiming a deterministic failure.
                    raise RuntimeFactPersistencePending(
                        "Runtime Fact persistence is still in progress after the caller timeout"
                    )
        if pending.error is not None:
            raise RuntimeFactsStoreError(str(pending.error)) from pending.error
        if pending.record is None:
            raise RuntimeFactsStoreError("Runtime Fact batched write returned no persisted record")
        return pending.record

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue:
                    self._condition.wait()
                deadline = time.monotonic() + self.flush_interval_seconds
                while len(self._queue) < self.batch_size:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                batch = self._queue[: self.batch_size]
                del self._queue[: len(batch)]
                active_batch: list[_PendingRuntimeFact] = []
                now = time.monotonic()
                for pending in batch:
                    if pending.state != "queued":
                        continue
                    if pending.deadline <= now:
                        pending.state = "canceled"
                        pending.error = RuntimeFactsStoreError(
                            "Runtime Fact batched write timed out before persistence"
                        )
                        pending.completed.set()
                        continue
                    pending.state = "writing"
                    active_batch.append(pending)
            if not active_batch:
                continue
            try:
                records = RuntimeFactsStore(
                    self.path,
                    timeout_ms=self.write_timeout_ms,
                ).put_many(
                    [pending.command for pending in active_batch]
                )
            except BaseException as exc:
                with self._condition:
                    for pending in active_batch:
                        pending.error = exc
                        pending.state = "completed"
                        pending.completed.set()
                continue
            with self._condition:
                for pending, record in zip(active_batch, records):
                    pending.record = record
                    pending.state = "completed"
                    pending.completed.set()


_BATCHERS_LOCK = threading.Lock()
_BATCHERS: dict[tuple[str, int, int, int], _RuntimeFactBatcher] = {}


def _batcher_for(
    path: Path,
    *,
    batch_size: int,
    flush_interval_ms: int,
    write_timeout_ms: int,
) -> _RuntimeFactBatcher:
    effective_interval_ms = min(
        max(1, int(flush_interval_ms)),
        max(1, int(write_timeout_ms) // 2),
    )
    key = (
        str(path.resolve()),
        int(batch_size),
        int(flush_interval_ms),
        int(write_timeout_ms),
    )
    with _BATCHERS_LOCK:
        batcher = _BATCHERS.get(key)
        if batcher is None:
            batcher = _RuntimeFactBatcher(
                path,
                batch_size=batch_size,
                # A single request must still finish inside the configured
                # write timeout. The caller's timeout remains authoritative.
                flush_interval_ms=effective_interval_ms,
                write_timeout_ms=write_timeout_ms,
            )
            _BATCHERS[key] = batcher
        return batcher


def _resolve_grounding(snapshot: GroundingLookupSnapshot) -> tuple[str, list[str], list[str]]:
    if not snapshot.selectors:
        return "not_applicable", [], []
    if not snapshot.ontology_epoch or snapshot.mutation_revision is None or not snapshot.index_generation:
        return "unverified", ["GROUNDING_CONTEXT_STALE"], []
    try:
        current = read_active_pointer_snapshot(snapshot.runtime_dir, snapshot.ontology_id)
    except (ValueError, EvidenceIndexManagerError):
        current = None
    if not current or not _pointer_matches(snapshot, current):
        return "unverified", ["GROUNDING_CONTEXT_STALE"], []

    registry = snapshot.read_documents
    grounding = registry["grounding_index"]
    derived: list[tuple[int, int, str]] = []
    warnings: list[str] = []
    all_selectors_resolved = True
    for selector in snapshot.selectors:
        subject_key = build_grounding_subject_key(
            workspace_id=snapshot.workspace_id,
            ontology_id=selector.ontology_id,
            ontology_epoch=snapshot.ontology_epoch,
            resource=selector.resource,
            resource_key=selector.resource_key,
        )
        candidates = list(grounding["by_subject"].get(subject_key) or [])[:32]
        if not candidates:
            all_selectors_resolved = False
            _append_unique(warnings, "DETAIL_NOT_LOADED")
            continue
        valid_for_selector = 0
        saw_stale = False
        saw_unavailable = False
        for evidence_id in candidates:
            binding = grounding["by_evidence_id"].get(evidence_id)
            if not isinstance(binding, dict):
                saw_unavailable = True
                continue
            if not _binding_subject_matches(binding, snapshot=snapshot, selector=selector):
                saw_stale = True
                continue
            record = registry["by_path"].get(str(binding.get("normalized_path") or ""))
            if not _record_matches_binding(record, binding):
                saw_unavailable = True
                continue
            valid_for_selector += 1
            derived.append(
                (
                    int(binding.get("loaded_turn_id") or 0),
                    int(binding.get("loaded_iteration") or 0),
                    str(evidence_id),
                )
            )
        if valid_for_selector == 0:
            all_selectors_resolved = False
            if saw_stale:
                _append_unique(warnings, "GROUNDING_CONTEXT_STALE")
            if saw_unavailable:
                _append_unique(warnings, "SOURCE_EVIDENCE_UNAVAILABLE")
            if not saw_stale and not saw_unavailable:
                _append_unique(warnings, "SOURCE_EVIDENCE_UNAVAILABLE")

    evidence_ids: list[str] = []
    for _turn, _iteration, evidence_id in sorted(derived, key=lambda item: (item[0], item[1], item[2])):
        if evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)
    return ("grounded" if all_selectors_resolved else "unverified"), warnings, evidence_ids


def _pointer_matches(snapshot: GroundingLookupSnapshot, pointer: Mapping[str, Any]) -> bool:
    return bool(
        active_pointer_is_serviceable(pointer)
        and int(pointer.get("mutation_revision") or 0) == snapshot.mutation_revision
        and str(pointer.get("expected_epoch") or "") == snapshot.ontology_epoch
        and str(pointer.get("active_generation") or "") == snapshot.index_generation
    )


def _binding_subject_matches(
    binding: Mapping[str, Any],
    *,
    snapshot: GroundingLookupSnapshot,
    selector: GroundingSelector,
) -> bool:
    return bool(
        str(binding.get("workspace_id") or "") == snapshot.workspace_id
        and str(binding.get("ontology_id") or "") == selector.ontology_id
        and str(binding.get("resource") or "") == selector.resource
        and str(binding.get("resource_key") or "") == selector.resource_key
        and str(binding.get("ontology_epoch") or "").removeprefix("sha256:")
        == str(snapshot.ontology_epoch or "").removeprefix("sha256:")
        and int(
            binding.get("mutation_revision")
            if binding.get("mutation_revision") is not None
            else -1
        ) == snapshot.mutation_revision
        and str(binding.get("index_generation") or "") == snapshot.index_generation
    )


def _record_matches_binding(record: Any, binding: Mapping[str, Any]) -> bool:
    if not isinstance(record, dict):
        return False
    return bool(
        int(
            record.get("mutation_revision")
            if record.get("mutation_revision") is not None
            else -1
        )
        == int(
            binding.get("mutation_revision")
            if binding.get("mutation_revision") is not None
            else -2
        )
        and str(record.get("ontology_epoch") or "").removeprefix("sha256:")
        == str(binding.get("ontology_epoch") or "").removeprefix("sha256:")
        and str(record.get("source_content_sha256") or "") == str(binding.get("source_content_sha256") or "")
        and str(binding.get("index_generation") or "")
    )


def _parse_result(content: str) -> Dict[str, Any]:
    text = str(content or "").strip()
    parsed: Dict[str, Any] = {}
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            parsed = value
    except json.JSONDecodeError:
        pass
    success = not text.lower().startswith("error:") and parsed.get("success") is not False
    row_count = parsed.get("result_count", parsed.get("row_count"))
    if row_count is None and isinstance(parsed.get("data"), list):
        row_count = len(parsed["data"])
    if row_count is None and isinstance(parsed.get("records"), list):
        row_count = len(parsed["records"])
    summary = str(parsed.get("summary") or parsed.get("message") or "").strip()
    if not summary:
        summary = "业务数据查询成功。" if success else "业务数据查询失败。"
    return {
        "status": "success" if success else "failed",
        "summary": summary[:1000],
        "row_count": _safe_int(row_count),
        "provider": parsed.get("provider"),
        "provider_evidence_raw": parsed.get("evidence") if isinstance(parsed.get("evidence"), list) else [],
    }


def _provider_evidence(parsed: Mapping[str, Any], *, provider: Optional[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in parsed.get("provider_evidence_raw") or []:
        if not isinstance(item, Mapping):
            continue
        ref = str(item.get("evidence_id") or item.get("provider_evidence_ref") or "").strip()
        if not ref:
            continue
        result.append(
            {
                "provider_evidence_ref": ref,
                "provider": str(item.get("provider") or provider or "").strip() or None,
                "kind": str(item.get("kind") or "provider_query_result"),
            }
        )
    return result


def _query_digest(*, ontology_id: str, entity: str, query: str) -> str:
    return "sha256:" + hash_frames(
        "runtime_query/v1",
        [
            field_s("ontology_id", ontology_id),
            field_s("entity", entity),
            field_s("query", str(query or "").strip()),
        ],
    )


def _fallback_tool_call_id(
    *,
    context: ToolContext,
    ontology_id: str,
    entity: str,
    query_digest: str,
) -> str:
    return "call_auto_" + hash_frames(
        "runtime_tool_call/v1",
        [
            field_s("run_id", str(context.run_id or "")),
            field_n("session_id") if context.session_id is None else field_s("session_id", str(context.session_id)),
            field_i("turn_id", int(context.turn_id or 0)),
            field_n("iteration") if context.agent_iteration is None else field_i("iteration", int(context.agent_iteration)),
            field_s("ontology_id", ontology_id),
            field_s("entity", entity),
            field_h("query_digest", query_digest.removeprefix("sha256:")),
        ],
    )[:32]


def _safe_int(value: Any) -> Optional[int]:
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
