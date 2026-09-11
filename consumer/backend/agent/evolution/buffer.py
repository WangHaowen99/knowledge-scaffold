"""Proposal buffer: append-only jsonl + aggregate.json per (ontology, op).

Single writer source for operation-evolution data. Reflection writes here
(passive), the model-facing `memory` tool writes active-chat routed operation
learning here, near_point_recall reads from here, and the patcher consumes
actionable aggregates from here.

Concurrency: per-(ontology, op) file lock via fcntl on a sentinel file. Safe
across threads in one process; safe across processes on POSIX. Windows users
will degrade to thread-only safety since fcntl is unavailable — explicitly
not supported for now.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, Iterable, List, Optional

try:  # POSIX only — Windows path is unsupported (see module docstring)
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

from utils.logging import get_logger
from agent.evolution.models import (
    AGGREGATE_STATUSES,
    PROPOSAL_KINDS,
    PROPOSAL_SOURCES,
    Aggregate,
    Proposal,
    compute_fingerprint,
)

logger = get_logger("evolution.buffer")

ONTOLOGY_LEVEL_OPERATION_KEY = "__ontology__"

# In-process locks per op-dir, layered above fcntl for thread safety.
_THREAD_LOCKS: Dict[str, RLock] = {}
_THREAD_LOCKS_GUARD = RLock()


def _safe_segment(value: str) -> str:
    """Sanitize a path segment so an ontology id / op key with weird chars
    cannot escape its directory."""
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(value or ""))
    return cleaned.strip("._") or "_unnamed_"


def _normalize_operation_key(operation_key: Optional[str]) -> Optional[str]:
    """Normalize an operation key while preserving an empty value as None."""
    return str(operation_key or "").strip() or None


def _storage_operation_key(operation_key: Optional[str]) -> str:
    """Return the on-disk operation key, including the ontology-level sentinel."""
    return _normalize_operation_key(operation_key) or ONTOLOGY_LEVEL_OPERATION_KEY


def _semantic_operation_key(storage_operation_key: str) -> Optional[str]:
    """Convert a storage key back to its semantic operation key."""
    normalized = str(storage_operation_key or "").strip()
    if normalized == ONTOLOGY_LEVEL_OPERATION_KEY:
        return None
    return normalized or None


def _thread_lock_for(path: Path) -> RLock:
    """Return the process-local lock associated with an operation directory."""
    key = str(path)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _file_lock(op_dir: Path):
    """Acquire the process and, when available, filesystem lock for an op dir."""
    op_dir.mkdir(parents=True, exist_ok=True)
    sentinel = op_dir / ".lock"
    tlock = _thread_lock_for(op_dir)
    tlock.acquire()
    fp = None
    try:
        if fcntl is not None:
            fp = open(sentinel, "a+")
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fp is not None:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            fp.close()
        tlock.release()


class ProposalBuffer:
    """Filesystem-backed proposal store.

    Layout under `root_dir`:

        <ontology_id>/<operation_key-or-ontology-level>/
            proposals.jsonl          # append-only
            aggregate.json           # {fingerprint: Aggregate.to_dict()}
            applied/<fp>.json        # archived once promoted-and-patched
            .lock                    # fcntl sentinel
    """

    def __init__(
        self,
        root_dir: str,
        *,
        change_listener: Optional[Callable[[str, Optional[str], str, Optional[str], str], None]] = None,
    ) -> None:
        """root_dir: usually `<project_root>/operations-evolution`.

        change_listener(event_type, ontology_id, operation_key, fingerprint):
            Optional hook fired AFTER a state change is committed.
            event_type ∈ {"append", "outcome", "vote", "endorse",
                          "reject", "edit", "applied"}.
            Used by the patcher worker to enqueue scans. Listener exceptions
            are swallowed so a flaky consumer never breaks a writer.
        """
        self._root = Path(root_dir).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._listener = change_listener

    # ------------------------------------------------------------------ paths
    def op_dir(
        self,
        ontology_id: str,
        operation_key: Optional[str],
        *,
        workspace_id: Optional[str] = None,
    ) -> Path:
        """Return the workspace-scoped storage directory for one operation."""
        storage_operation_key = _storage_operation_key(operation_key)
        if workspace_id:
            return (
                self._root
                / "workspaces"
                / _safe_segment(workspace_id)
                / _safe_segment(ontology_id)
                / _safe_segment(storage_operation_key)
            )
        return self._root / _safe_segment(ontology_id) / _safe_segment(storage_operation_key)

    def _aggregate_path(
        self,
        ontology_id: str,
        operation_key: Optional[str],
        *,
        workspace_id: Optional[str] = None,
    ) -> Path:
        return self.op_dir(ontology_id, operation_key, workspace_id=workspace_id) / "aggregate.json"

    def _proposals_path(
        self,
        ontology_id: str,
        operation_key: Optional[str],
        *,
        workspace_id: Optional[str] = None,
    ) -> Path:
        return self.op_dir(ontology_id, operation_key, workspace_id=workspace_id) / "proposals.jsonl"

    def applied_dir(
        self,
        ontology_id: str,
        operation_key: Optional[str],
        *,
        workspace_id: Optional[str] = None,
    ) -> Path:
        return self.op_dir(ontology_id, operation_key, workspace_id=workspace_id) / "applied"

    # ------------------------------------------------------------------ I/O
    def _load_aggregates(
        self,
        ontology_id: str,
        operation_key: Optional[str],
        *,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Aggregate]:
        path = self._aggregate_path(ontology_id, operation_key, workspace_id=workspace_id)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception:
            logger.warning("aggregate.json corrupt at %s — resetting", path, exc_info=True)
            return {}
        out: Dict[str, Aggregate] = {}
        for fp, raw in (data or {}).items():
            try:
                out[fp] = Aggregate.from_dict(raw)
            except Exception:
                logger.warning("dropping malformed aggregate fp=%s", fp, exc_info=True)
        return out

    def _write_aggregates(
        self,
        ontology_id: str,
        operation_key: Optional[str],
        aggregates: Dict[str, Aggregate],
        *,
        workspace_id: Optional[str] = None,
    ) -> None:
        path = self._aggregate_path(ontology_id, operation_key, workspace_id=workspace_id)
        payload = {fp: agg.to_dict() for fp, agg in aggregates.items()}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _append_proposal_record(self, p: Proposal) -> None:
        path = self._proposals_path(
            p.ontology_id,
            p.operation_key,
            workspace_id=p.workspace_id,
        )
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(p.to_dict(), ensure_ascii=False))
            fp.write("\n")

    # ----------------------------------------------------------------- events
    def _emit(
        self,
        event: str,
        ontology_id: str,
        operation_key: Optional[str],
        fingerprint: str,
        *,
        workspace_id: Optional[str] = None,
    ) -> None:
        if self._listener is None:
            return
        try:
            self._listener(event, workspace_id, ontology_id, operation_key, fingerprint)
        except Exception:
            logger.warning(
                "buffer change_listener raised event=%s op=%s",
                event,
                operation_key,
                exc_info=True,
            )

    # -------------------------------------------------------------- mutations
    def append(
        self,
        *,
        workspace_id: Optional[str] = None,
        ontology_id: str,
        operation_key: Optional[str],
        kind: str,
        content: str,
        source: str,
        entity_key: Optional[str] = None,
        rationale: Optional[str] = None,
        run_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Aggregate:
        """Add a proposal. Returns the (possibly new) aggregate."""
        kind = str(kind or "").strip().lower()
        source = str(source or "").strip().lower()
        if kind not in PROPOSAL_KINDS:
            raise ValueError("kind must be one of {}".format(sorted(PROPOSAL_KINDS)))
        if source not in PROPOSAL_SOURCES:
            raise ValueError("source must be one of {}".format(sorted(PROPOSAL_SOURCES)))
        content = str(content or "").strip()
        if len(content) < 4:
            raise ValueError("content too short")
        ontology_id = str(ontology_id or "").strip()
        operation_key = _normalize_operation_key(operation_key)
        workspace_id = str(workspace_id or "").strip() or None
        if not ontology_id:
            raise ValueError("ontology_id is required")
        if kind == "schema" and not operation_key and not str(entity_key or "").strip():
            raise ValueError("entity_key is required for object-level schema")

        ts = time.time()
        fingerprint = compute_fingerprint(
            kind=kind,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            operation_key=operation_key,
            entity_key=entity_key,
            content=content,
        )
        proposal = Proposal(
            id=str(uuid.uuid4()),
            fingerprint=fingerprint,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            operation_key=operation_key,
            kind=kind,
            content=content,
            source=source,
            ts=ts,
            entity_key=entity_key,
            rationale=rationale,
            run_id=run_id,
            user_id=user_id,
        )

        op_dir = self.op_dir(ontology_id, operation_key, workspace_id=workspace_id)
        with _file_lock(op_dir):
            self._append_proposal_record(proposal)
            aggs = self._load_aggregates(ontology_id, operation_key, workspace_id=workspace_id)
            existing = aggs.get(fingerprint)
            if existing is None:
                aggs[fingerprint] = Aggregate.seed_from(proposal)
            else:
                # If it was rejected before, a new active_chat resurrects it;
                # passive duplicates of a rejected aggregate stay rejected.
                if existing.status == "rejected" and source == "active_chat":
                    existing.status = "pending"
                existing.merge(proposal)
            self._write_aggregates(ontology_id, operation_key, aggs, workspace_id=workspace_id)
        self._emit("append", ontology_id, operation_key, fingerprint, workspace_id=workspace_id)
        return aggs[fingerprint]

    def record_outcome(
        self,
        fingerprints: Iterable[str],
        *,
        success: bool,
        workspace_id: Optional[str] = None,
        ontology_id: Optional[str] = None,
        operation_key: Optional[str] = None,
    ) -> None:
        """Increment applied_count for each fingerprint that was recalled in
        a successful turn. Failed turns do nothing — we don't punish a
        proposal for an unrelated turn failure; explicit user 👎 is the
        only way to grow rejection_count.

        ontology_id/operation_key are advisory hints to scope the lookup.
        If absent we walk all op_dirs (rare; should be supplied in practice).
        """
        if not success:
            return
        fps = [fp for fp in (fingerprints or []) if fp]
        if not fps:
            return
        targets = self._resolve_targets(fps, workspace_id, ontology_id, operation_key)
        for (ws, onto, op), op_fps in targets.items():
            with _file_lock(self.op_dir(onto, op, workspace_id=ws)):
                aggs = self._load_aggregates(onto, op, workspace_id=ws)
                touched: List[str] = []
                for fp in op_fps:
                    agg = aggs.get(fp)
                    if agg is None or agg.status in {"rejected", "applied"}:
                        continue
                    agg.applied_count += 1
                    agg.last_seen = time.time()
                    touched.append(fp)
                if touched:
                    self._write_aggregates(onto, op, aggs, workspace_id=ws)
            for fp in touched:
                self._emit("outcome", onto, op, fp, workspace_id=ws)

    def record_vote(
        self,
        fingerprints: Iterable[str],
        *,
        vote: str,
        previous_vote: Optional[str] = None,
        workspace_id: Optional[str] = None,
        ontology_id: Optional[str] = None,
        operation_key: Optional[str] = None,
    ) -> None:
        """Apply a user's changed vote to matching proposal aggregates."""
        vote = str(vote or "").strip().lower()
        previous_vote = str(previous_vote or "").strip().lower() or None
        if vote not in {"up", "down", "none"}:
            raise ValueError("vote must be 'up', 'down', or 'none'")
        if previous_vote not in {None, "up", "down"}:
            raise ValueError("previous_vote must be 'up', 'down', or None")
        if vote == previous_vote:
            return
        fps = [fp for fp in (fingerprints or []) if fp]
        if not fps:
            return
        targets = self._resolve_targets(fps, workspace_id, ontology_id, operation_key)
        for (ws, onto, op), op_fps in targets.items():
            with _file_lock(self.op_dir(onto, op, workspace_id=ws)):
                aggs = self._load_aggregates(onto, op, workspace_id=ws)
                touched: List[str] = []
                for fp in op_fps:
                    agg = aggs.get(fp)
                    if agg is None:
                        continue
                    if previous_vote == "up":
                        agg.active_endorsements = max(0, agg.active_endorsements - 1)
                    elif previous_vote == "down":
                        agg.rejection_count = max(0, agg.rejection_count - 1)
                    if vote == "up":
                        agg.active_endorsements += 1
                    elif vote == "down":
                        agg.rejection_count += 1
                    agg.last_seen = time.time()
                    touched.append(fp)
                if touched:
                    self._write_aggregates(onto, op, aggs, workspace_id=ws)
            for fp in touched:
                self._emit("vote", onto, op, fp, workspace_id=ws)

    def endorse(
        self,
        ontology_id: str,
        operation_key: Optional[str],
        fingerprint: str,
        *,
        workspace_id: Optional[str] = None,
    ) -> None:
        """Add one endorsement to a proposal aggregate when it exists."""
        with _file_lock(self.op_dir(ontology_id, operation_key, workspace_id=workspace_id)):
            aggs = self._load_aggregates(ontology_id, operation_key, workspace_id=workspace_id)
            agg = aggs.get(fingerprint)
            if agg is None:
                return
            agg.active_endorsements += 1
            agg.last_seen = time.time()
            self._write_aggregates(ontology_id, operation_key, aggs, workspace_id=workspace_id)
        self._emit("endorse", ontology_id, operation_key, fingerprint, workspace_id=workspace_id)

    def reject(
        self,
        ontology_id: str,
        operation_key: Optional[str],
        fingerprint: str,
        *,
        workspace_id: Optional[str] = None,
    ) -> None:
        """Reject a proposal aggregate and record the rejection count."""
        with _file_lock(self.op_dir(ontology_id, operation_key, workspace_id=workspace_id)):
            aggs = self._load_aggregates(ontology_id, operation_key, workspace_id=workspace_id)
            agg = aggs.get(fingerprint)
            if agg is None:
                return
            agg.status = "rejected"
            agg.rejection_count += 1
            agg.last_seen = time.time()
            self._write_aggregates(ontology_id, operation_key, aggs, workspace_id=workspace_id)
        self._emit("reject", ontology_id, operation_key, fingerprint, workspace_id=workspace_id)

    def edit(
        self,
        ontology_id: str,
        operation_key: Optional[str],
        fingerprint: str,
        new_content: str,
        *,
        workspace_id: Optional[str] = None,
    ) -> Optional[Aggregate]:
        """Replace canonical_content; recompute fingerprint. The old
        aggregate is removed and a new one (preserving counters) takes its
        place, so downstream consumers always key by fingerprint."""
        new_content = str(new_content or "").strip()
        if len(new_content) < 4:
            raise ValueError("content too short")
        with _file_lock(self.op_dir(ontology_id, operation_key, workspace_id=workspace_id)):
            aggs = self._load_aggregates(ontology_id, operation_key, workspace_id=workspace_id)
            old = aggs.get(fingerprint)
            if old is None:
                return None
            new_fp = compute_fingerprint(
                kind=old.kind,
                workspace_id=workspace_id,
                ontology_id=ontology_id,
                operation_key=operation_key,
                entity_key=old.entity_key,
                content=new_content,
            )
            if new_fp == fingerprint:
                old.canonical_content = new_content
                old.last_seen = time.time()
                self._write_aggregates(ontology_id, operation_key, aggs, workspace_id=workspace_id)
                self._emit("edit", ontology_id, operation_key, fingerprint, workspace_id=workspace_id)
                return old
            replacement = Aggregate(
                fingerprint=new_fp,
                workspace_id=workspace_id,
                ontology_id=ontology_id,
                operation_key=operation_key,
                kind=old.kind,
                canonical_content=new_content,
                occurrences=old.occurrences,
                active_endorsements=old.active_endorsements,
                applied_count=old.applied_count,
                rejection_count=old.rejection_count,
                first_seen=old.first_seen,
                last_seen=time.time(),
                status=old.status if old.status != "rejected" else "pending",
                entity_key=old.entity_key,
                sources=dict(old.sources),
                proposal_ids=list(old.proposal_ids),
            )
            aggs.pop(fingerprint, None)
            aggs[new_fp] = replacement
            self._write_aggregates(ontology_id, operation_key, aggs, workspace_id=workspace_id)
        self._emit("edit", ontology_id, operation_key, new_fp, workspace_id=workspace_id)
        return replacement

    def mark_promoted(
        self,
        ontology_id: str,
        operation_key: Optional[str],
        fingerprints: Iterable[str],
        *,
        workspace_id: Optional[str] = None,
    ) -> List[str]:
        """Patcher-side: flip status to `promoted` for fps included in the
        current staged file. Promoted aggregates remain in the buffer so the
        recall path keeps surfacing them, but the patcher's threshold gate
        treats them as already in-flight."""
        fps = [fp for fp in (fingerprints or []) if fp]
        if not fps:
            return []
        touched: List[str] = []
        with _file_lock(self.op_dir(ontology_id, operation_key, workspace_id=workspace_id)):
            aggs = self._load_aggregates(ontology_id, operation_key, workspace_id=workspace_id)
            for fp in fps:
                agg = aggs.get(fp)
                if agg is None or agg.status in {"rejected", "applied"}:
                    continue
                agg.status = "promoted"
                agg.last_seen = time.time()
                touched.append(fp)
            if touched:
                self._write_aggregates(ontology_id, operation_key, aggs, workspace_id=workspace_id)
        # No event emit — promotion is internal to the patcher; we don't want
        # it to re-fire the listener and re-enqueue the same op.
        return touched

    def revert_promoted(
        self,
        ontology_id: str,
        operation_key: Optional[str],
        fingerprints: Iterable[str],
        *,
        workspace_id: Optional[str] = None,
    ) -> List[str]:
        """Patcher-side: undo a promotion. Used when staged write fails or
        admin rejects the staged patch but we want to re-evaluate later."""
        fps = [fp for fp in (fingerprints or []) if fp]
        if not fps:
            return []
        touched: List[str] = []
        with _file_lock(self.op_dir(ontology_id, operation_key, workspace_id=workspace_id)):
            aggs = self._load_aggregates(ontology_id, operation_key, workspace_id=workspace_id)
            for fp in fps:
                agg = aggs.get(fp)
                if agg is None or agg.status != "promoted":
                    continue
                agg.status = "pending"
                agg.last_seen = time.time()
                touched.append(fp)
            if touched:
                self._write_aggregates(ontology_id, operation_key, aggs, workspace_id=workspace_id)
        return touched

    def archive_applied(
        self,
        ontology_id: str,
        operation_key: Optional[str],
        fingerprints: Iterable[str],
        *,
        workspace_id: Optional[str] = None,
    ) -> None:
        """Called by patcher after a successful apply. Marks aggregates as
        `applied` and writes a snapshot to the applied/ dir."""
        fps = list(fingerprints or [])
        if not fps:
            return
        applied_dir = self.applied_dir(ontology_id, operation_key, workspace_id=workspace_id)
        applied_dir.mkdir(parents=True, exist_ok=True)
        with _file_lock(self.op_dir(ontology_id, operation_key, workspace_id=workspace_id)):
            aggs = self._load_aggregates(ontology_id, operation_key, workspace_id=workspace_id)
            touched: List[str] = []
            for fp in fps:
                agg = aggs.get(fp)
                if agg is None:
                    continue
                agg.status = "applied"
                agg.last_seen = time.time()
                snapshot = applied_dir / "{}.json".format(fp)
                snapshot.write_text(
                    json.dumps(agg.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                touched.append(fp)
            if touched:
                self._write_aggregates(ontology_id, operation_key, aggs, workspace_id=workspace_id)
        for fp in touched:
            self._emit("applied", ontology_id, operation_key, fp, workspace_id=workspace_id)

    # ------------------------------------------------------------------ reads
    def list_aggregates(
        self,
        ontology_id: str,
        operation_key: Optional[str],
        *,
        workspace_id: Optional[str] = None,
        statuses: Optional[Iterable[str]] = None,
    ) -> List[Aggregate]:
        """Return aggregates for an operation filtered by their current status."""
        wanted = (
            {s.strip().lower() for s in statuses if s}
            if statuses is not None
            else AGGREGATE_STATUSES
        )
        aggs = self._load_aggregates(ontology_id, operation_key, workspace_id=workspace_id)
        return [a for a in aggs.values() if a.status in wanted]

    def list_proposal_records(
        self,
        ontology_id: str,
        operation_key: Optional[str],
        *,
        workspace_id: Optional[str] = None,
        fingerprint: Optional[str] = None,
        limit: int = 200,
    ) -> List[Proposal]:
        """Return recent durable proposal records for one operation."""
        path = self._proposals_path(ontology_id, operation_key, workspace_id=workspace_id)
        if not path.exists():
            return []
        wanted_fp = str(fingerprint or "").strip() or None
        records: List[Proposal] = []
        try:
            with path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        proposal = Proposal.from_dict(json.loads(raw))
                    except Exception:
                        logger.warning("dropping malformed proposal record at %s", path, exc_info=True)
                        continue
                    if wanted_fp and proposal.fingerprint != wanted_fp:
                        continue
                    records.append(proposal)
        except Exception:
            logger.warning("failed to read proposals.jsonl at %s", path, exc_info=True)
            return []
        records.sort(key=lambda p: p.ts, reverse=True)
        return records[: max(0, int(limit))]

    def recall(
        self,
        ontology_id: str,
        *,
        workspace_id: Optional[str] = None,
        operation_key: Optional[str] = None,
        kind: Optional[str] = None,
        entity_key: Optional[str] = None,
        k: int = 3,
        statuses: Iterable[str] = ("pending", "promoted", "applied"),
    ) -> List[Aggregate]:
        """Read-side for near_point_recall. Returns top-k aggregates filtered
        by kind/entity, ranked by (active_endorsements desc, applied_count
        desc, occurrences desc, last_seen desc).

        Rejected aggregates are excluded by default. `pending` is included
        because that's the only way `applied_count` ever grows — recall must
        cover unproven proposals so the closed loop can validate them.
        """
        if not ontology_id:
            return []
        if operation_key is None:
            wanted_kind = (kind or "").strip().lower() or None
            if wanted_kind == "schema" and not entity_key:
                return []
        wanted_statuses = {s.strip().lower() for s in statuses if s}
        wanted_kind = (kind or "").strip().lower() or None
        aggs = self._load_aggregates(ontology_id, operation_key, workspace_id=workspace_id)
        out: List[Aggregate] = []
        for agg in aggs.values():
            if agg.status not in wanted_statuses:
                continue
            if wanted_kind and agg.kind != wanted_kind:
                continue
            if entity_key is not None and agg.kind == "schema":
                if (agg.entity_key or "") != entity_key:
                    continue
            out.append(agg)
        out.sort(
            key=lambda a: (
                a.active_endorsements,
                a.applied_count,
                a.occurrences,
                a.last_seen,
            ),
            reverse=True,
        )
        return out[: max(0, int(k))]

    def iter_op_dirs(self) -> List[tuple]:
        """Return all persisted workspace, ontology, and operation triples."""
        out: List[tuple] = []
        if not self._root.exists():
            return out

        workspace_root = self._root / "workspaces"
        if workspace_root.exists():
            for ws_dir in workspace_root.iterdir():
                if not ws_dir.is_dir():
                    continue
                for onto_dir in ws_dir.iterdir():
                    if not onto_dir.is_dir():
                        continue
                    for op_d in onto_dir.iterdir():
                        if not op_d.is_dir():
                            continue
                        if (op_d / "aggregate.json").exists():
                            out.append((ws_dir.name, onto_dir.name, _semantic_operation_key(op_d.name)))

        for onto_dir in self._root.iterdir():
            if not onto_dir.is_dir() or onto_dir.name == "workspaces":
                continue
            for op_d in onto_dir.iterdir():
                if not op_d.is_dir():
                    continue
                if (op_d / "aggregate.json").exists():
                    out.append((None, onto_dir.name, _semantic_operation_key(op_d.name)))
        return out

    def set_change_listener(
        self,
        listener: Optional[Callable[[str, Optional[str], str, Optional[str], str], None]],
    ) -> None:
        """Allow late-binding (orchestrator constructs the patcher after the
        buffer singleton already exists)."""
        self._listener = listener

    # --------------------------------------------------------------- helpers
    def _resolve_targets(
        self,
        fingerprints: List[str],
        workspace_id: Optional[str],
        ontology_id: Optional[str],
        operation_key: Optional[str],
    ) -> Dict[tuple, List[str]]:
        """Group fingerprints by (ontology_id, operation_key). When the
        caller provides both, trust them. Otherwise scan op_dirs for the
        owning aggregate — slower path, used as a safety net."""
        if ontology_id and operation_key:
            return {(workspace_id, ontology_id, operation_key): list(fingerprints)}
        targets: Dict[tuple, List[str]] = {}
        unresolved = set(fingerprints)
        for ws, onto, op in self.iter_op_dirs():
            if not unresolved:
                break
            aggs = self._load_aggregates(onto, op, workspace_id=ws)
            hit = [fp for fp in list(unresolved) if fp in aggs]
            if hit:
                targets[(ws, onto, op)] = hit
                unresolved.difference_update(hit)
        return targets
