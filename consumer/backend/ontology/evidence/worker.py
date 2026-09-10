"""Background outbox consumer for ontology evidence index builds."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from config.evidence_config import EvidenceConfig
from ontology.change_events import empty_digest_hints
from ontology.evidence.manager import (
    EvidenceIndexManager,
    active_pointer_is_serviceable,
    mark_active_pointer_state,
    read_active_pointer_snapshot,
    recover_interrupted_builds,
    revoke_active_pointer_snapshot,
)
from ontology.mutations import (
    MutationOutboxDispatcher,
    MutationStore,
    OntologyMutationCoordinator,
    PreparedOntologyMutation,
    read_mutation_state_snapshot,
)
from ontology.ontologies import get_ontology_registry, load_ontology_manifest_data
from persistence.evidence_operations import EvidenceOperationsStore
from workspace.workspaces import get_workspace_paths, list_workspaces, workspace_scope

logger = logging.getLogger("ontology.evidence.worker")


class EvidenceIndexWorker:
    def __init__(self, *, poll_seconds: float = 1.0) -> None:
        self.poll_seconds = max(0.2, float(poll_seconds))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ontology-evidence-index", daemon=True)
        self._last_sweep_at = 0.0
        self._recovery_done = False

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(0.0, timeout))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.warning("Ontology evidence worker sweep failed", exc_info=True)
            self._stop.wait(self.poll_seconds)

    def run_once(self) -> dict[str, int]:
        totals = {
            "claimed": 0,
            "dispatched": 0,
            "retried": 0,
            "dead_letter": 0,
            "build_claimed": 0,
            "build_succeeded": 0,
            "build_failed": 0,
            "build_cancelled": 0,
            "drift_detected": 0,
            "recovery_removed": 0,
        }
        for workspace in list_workspaces():
            workspace_id = str(workspace.get("id") or "").strip()
            if not workspace_id:
                continue
            runtime_root = get_workspace_paths(workspace_id).runtime_dir
            if not self._recovery_done:
                totals["recovery_removed"] += recover_interrupted_builds(runtime_root)["removed"]
            store_path = runtime_root / "ontology-mutations.sqlite3"
            if store_path.is_file():
                dispatcher = MutationOutboxDispatcher(MutationStore(store_path))
                stats = dispatcher.dispatch_once(
                    lambda event, root=runtime_root: process_ontology_changed_event(root, event),
                    limit=8,
                )
                for key in ("claimed", "dispatched", "retried", "dead_letter"):
                    totals[key] += int(stats.get(key) or 0)
            operations_path = runtime_root / "ontology-evidence" / "operations.sqlite3"
            if operations_path.is_file():
                operations = EvidenceOperationsStore(operations_path)
                for job in operations.claim_build_jobs(limit=2):
                    totals["build_claimed"] += 1
                    status = process_manual_build_job(runtime_root, job, operations)
                    totals["build_{}".format(status)] += 1
        config = EvidenceConfig.load()
        self._recovery_done = True
        now = time.monotonic()
        if config.enabled and config.sweeper_enabled and now - self._last_sweep_at >= config.sweeper_interval_seconds:
            totals["drift_detected"] = run_evidence_sweeper_once(config=config)
            self._last_sweep_at = now
        return totals


def process_ontology_changed_event(runtime_root: Path, event: dict[str, Any]) -> dict[str, Any]:
    workspace_id = str(event["workspace_id"])
    ontology_id = str(event["ontology_id"])
    state = read_mutation_state_snapshot(
        runtime_root,
        workspace_id=workspace_id,
        ontology_id=ontology_id,
    )
    if state is None:
        raise RuntimeError("authoritative mutation state is unavailable")
    event_revision = int(event["mutation_revision"])
    current_revision = int(state["mutation_revision"])
    if event_revision < current_revision:
        return {"status": "stale", "mutation_revision": current_revision}
    if event_revision > current_revision:
        raise RuntimeError("mutation state has not reached event revision")

    config = EvidenceConfig.load()
    if not bool(state.get("enabled")) or not state.get("active_version"):
        mark_active_pointer_state(
            runtime_root,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            mutation_revision=current_revision,
            status="disabled",
        )
        return {"status": "inactive", "mutation_revision": current_revision}
    if config.workspace_mode(workspace_id) == "off":
        mark_active_pointer_state(
            runtime_root,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            mutation_revision=current_revision,
            status="disabled",
        )
        return {"status": "disabled", "mutation_revision": current_revision}
    if not config.auto_build_enabled:
        mark_active_pointer_state(
            runtime_root,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            mutation_revision=current_revision,
            status="manual_required",
        )
        return {"status": "manual_required", "mutation_revision": current_revision}

    with workspace_scope(workspace_id):
        registry = get_ontology_registry()
        registry.refresh()
        ontology = registry.get_ontology(ontology_id)
        if ontology is None or not ontology.enabled:
            return {"status": "inactive", "mutation_revision": current_revision}
        ontology_dir = Path(ontology.ontology_dir).resolve()
        package_root = ontology_dir.parent.parent if ontology_dir.parent.name == "versions" else ontology_dir
        manifest = load_ontology_manifest_data(package_root)
        pointer = EvidenceIndexManager(runtime_root).build(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            ontology_version=ontology.active_version,
            ontology_dir=ontology_dir,
            manifest=manifest,
            mutation_revision=current_revision,
            trigger="ontology_changed",
            event_id=str(event["event_id"]),
        )

    latest = read_mutation_state_snapshot(
        runtime_root,
        workspace_id=workspace_id,
        ontology_id=ontology_id,
    )
    if latest is None or int(latest["mutation_revision"]) != current_revision:
        revoke_active_pointer_snapshot(
            runtime_root,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            mutation_revision=int((latest or {}).get("mutation_revision") or current_revision),
            ontology_version=(latest or {}).get("active_version"),
            enabled=bool((latest or {}).get("enabled")),
        )
        return {"status": "superseded", "mutation_revision": current_revision}
    return {
        "status": "ready",
        "mutation_revision": current_revision,
        "ontology_epoch": pointer["expected_epoch"],
        "index_generation": pointer["active_generation"],
    }


def process_manual_build_job(
    runtime_root: Path,
    job: dict[str, Any],
    operations: EvidenceOperationsStore,
) -> str:
    workspace_id = str(job["workspace_id"])
    ontology_id = str(job["ontology_id"])
    job_id = str(job["job_id"])
    config = EvidenceConfig.load()
    if config.workspace_mode(workspace_id) == "off":
        operations.mark_build_failed(
            job_id,
            error_code="EVIDENCE_FEATURE_DISABLED",
            error_summary="当前工作空间未启用本体证据索引。",
        )
        return "failed"
    state = read_mutation_state_snapshot(
        runtime_root,
        workspace_id=workspace_id,
        ontology_id=ontology_id,
    )
    if (
        state is None
        or not bool(state.get("enabled"))
        or int(state.get("mutation_revision") if state.get("mutation_revision") is not None else -1)
        != int(job["mutation_revision"])
    ):
        operations.mark_build_cancelled(job_id)
        return "cancelled"

    try:
        with workspace_scope(workspace_id):
            registry = get_ontology_registry()
            registry.refresh()
            ontology = registry.get_ontology(ontology_id)
            if ontology is None or not ontology.enabled:
                operations.mark_build_cancelled(job_id, reason="目标知识包不存在或已禁用。")
                return "cancelled"
            if str(ontology.active_version) != str(state.get("active_version") or ""):
                operations.mark_build_cancelled(job_id)
                return "cancelled"
            ontology_dir = Path(ontology.ontology_dir).resolve()
            package_root = ontology_dir.parent.parent if ontology_dir.parent.name == "versions" else ontology_dir
            manifest = load_ontology_manifest_data(package_root)
            pointer = EvidenceIndexManager(runtime_root).build(
                workspace_id=workspace_id,
                ontology_id=ontology_id,
                ontology_version=ontology.active_version,
                ontology_dir=ontology_dir,
                manifest=manifest,
                mutation_revision=int(job["mutation_revision"]),
                trigger="manual",
                expected_ontology_epoch=str(job["ontology_epoch"]),
                expected_index_generation=str(job["target_generation"]),
                event_id=str(state.get("last_event_id") or "") or None,
            )
        latest = read_mutation_state_snapshot(
            runtime_root,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
        )
        if latest is None or int(
            latest.get("mutation_revision") if latest.get("mutation_revision") is not None else -1
        ) != int(job["mutation_revision"]):
            revoke_active_pointer_snapshot(
                runtime_root,
                workspace_id=workspace_id,
                ontology_id=ontology_id,
                mutation_revision=int((latest or {}).get("mutation_revision") or job["mutation_revision"]),
                ontology_version=(latest or {}).get("active_version"),
                enabled=bool((latest or {}).get("enabled")),
            )
            operations.mark_build_cancelled(job_id)
            return "cancelled"
        if (
            pointer.get("expected_epoch") != job["ontology_epoch"]
            or pointer.get("active_generation") != job["target_generation"]
        ):
            operations.mark_build_failed(
                job_id,
                error_code="EVIDENCE_INTEGRITY_FAILED",
                error_summary="发布结果与重建目标不一致。",
            )
            return "failed"
    except Exception as exc:
        code = getattr(exc, "code", "INDEX_BUILD_FAILED")
        logger.warning("Manual evidence build failed: job_id=%s code=%s", job_id, code, exc_info=True)
        operations.mark_build_failed(
            job_id,
            error_code=str(code),
            error_summary="证据索引构建失败。",
        )
        return "failed"

    operations.mark_build_succeeded(job_id)
    return "succeeded"


def run_evidence_sweeper_once(*, config: EvidenceConfig | None = None) -> int:
    """Detect out-of-band package drift without publishing from the sweep path."""
    effective = config or EvidenceConfig.load()
    if not effective.enabled or not effective.sweeper_enabled:
        return 0
    detected = 0
    for workspace in list_workspaces():
        workspace_id = str(workspace.get("id") or "").strip()
        if not workspace_id or effective.workspace_mode(workspace_id) not in {"shadow", "allowlist", "all"}:
            continue
        paths = get_workspace_paths(workspace_id)
        with workspace_scope(workspace_id):
            registry = get_ontology_registry()
            registry.refresh()
            ontologies = list(registry.ontologies.values())
        for ontology in ontologies:
            ontology_id = str(ontology.id)
            if not ontology.enabled or not ontology.active_version:
                continue
            store = MutationStore(paths.runtime_dir / "ontology-mutations.sqlite3")
            try:
                state = store.ensure_state(
                    workspace_id=workspace_id,
                    ontology_id=ontology_id,
                    active_version=str(ontology.active_version),
                    enabled=True,
                )
            except Exception:
                logger.warning(
                    "Evidence sweeper could not adopt mutation state: workspace_id=%s ontology_id=%s",
                    workspace_id,
                    ontology_id,
                    exc_info=True,
                )
                continue
            try:
                pointer = read_active_pointer_snapshot(paths.runtime_dir, ontology_id)
            except Exception:
                logger.debug(
                    "Evidence sweeper pointer read failed: workspace_id=%s ontology_id=%s",
                    workspace_id,
                    ontology_id,
                    exc_info=True,
                )
                pointer = None
            logger.debug(
                "Evidence sweeper snapshot workspace_id=%s ontology_id=%s pointer_status=%s pointer_revision=%s state_revision=%s",
                workspace_id,
                ontology_id,
                (pointer or {}).get("status"),
                (pointer or {}).get("mutation_revision"),
                state.get("mutation_revision"),
            )
            needs_reconcile = pointer is None
            if active_pointer_is_serviceable(pointer):
                pointer_revision = int(
                    pointer.get("mutation_revision") if pointer.get("mutation_revision") is not None else -1
                )
                if pointer_revision != int(state["mutation_revision"]):
                    needs_reconcile = True
                else:
                    try:
                        ontology_dir = Path(ontology.ontology_dir).resolve()
                        package_root = (
                            ontology_dir.parent.parent if ontology_dir.parent.name == "versions" else ontology_dir
                        )
                        plan = EvidenceIndexManager(paths.runtime_dir).plan(
                            ontology_id=ontology_id,
                            ontology_version=str(ontology.active_version),
                            ontology_dir=ontology_dir,
                            manifest=load_ontology_manifest_data(package_root),
                        )
                        needs_reconcile = bool(
                            str(pointer.get("ontology_version") or "") != str(ontology.active_version)
                            or str(pointer.get("expected_epoch") or "") != plan["ontology_epoch"]
                            or str(pointer.get("active_generation") or "") != plan["index_generation"]
                        )
                        logger.debug(
                            "Evidence sweeper comparison workspace_id=%s ontology_id=%s revision=%s/%s epoch_match=%s generation_match=%s reconcile=%s",
                            workspace_id,
                            ontology_id,
                            pointer_revision,
                            state["mutation_revision"],
                            str(pointer.get("expected_epoch") or "") == plan["ontology_epoch"],
                            str(pointer.get("active_generation") or "") == plan["index_generation"],
                            needs_reconcile,
                        )
                    except Exception:
                        logger.warning(
                            "Evidence sweeper plan failed: workspace_id=%s ontology_id=%s",
                            workspace_id,
                            ontology_id,
                            exc_info=True,
                        )
                        continue
            elif pointer is not None:
                # A normal revoked/failed pointer already has an authoritative
                # state at the same revision.  A pointer revision ahead of the
                # store means a process died or rollback was uncertain between
                # filesystem commit and the final state/outbox CAS; reconcile
                # that gap instead of leaving the ontology permanently stale.
                pointer_revision = int(
                    pointer.get("mutation_revision") if pointer.get("mutation_revision") is not None else -1
                )
                needs_reconcile = pointer_revision != int(state["mutation_revision"])
                if (
                    not needs_reconcile
                    and effective.auto_build_enabled
                    and str(pointer.get("status") or "") in {"disabled", "manual_required"}
                ):
                    # Configuration changed without an ontology mutation. Emit
                    # a fresh revision so the normal outbox/build path restores
                    # the pointer even when its revision already matches state.
                    needs_reconcile = True
            if not needs_reconcile:
                continue
            before_version = str(state.get("active_version") or "") or None
            after_version = str(ontology.active_version)
            before_enabled = bool(state.get("enabled"))
            if before_version != after_version:
                recovery_mutation_type = "ACTIVE_VERSION_CHANGED"
            elif not before_enabled:
                recovery_mutation_type = "ONTOLOGY_VISIBILITY_CHANGED"
            else:
                recovery_mutation_type = "EXTERNAL_DRIFT_DETECTED"
            try:
                OntologyMutationCoordinator(paths.runtime_dir).commit(
                    PreparedOntologyMutation(
                        workspace_id=workspace_id,
                        ontology_id=ontology_id,
                        mutation_type=recovery_mutation_type,
                        before_version=before_version,
                        after_version=after_version,
                        before_enabled=before_enabled,
                        after_enabled=True,
                        commit=lambda: None,
                        rollback=lambda: None,
                        digest_hints=empty_digest_hints(scope_version=str(ontology.active_version)),
                        trigger="sweeper",
                        base_mutation_revision=int(state["mutation_revision"]),
                    )
                )
                detected += 1
            except Exception:
                logger.warning(
                    "Evidence sweeper reconcile event failed: workspace_id=%s ontology_id=%s",
                    workspace_id,
                    ontology_id,
                    exc_info=True,
                )
    return detected


_WORKER_LOCK = threading.Lock()
_WORKER: EvidenceIndexWorker | None = None


def refresh_evidence_index_worker() -> None:
    """Start or stop the worker according to the latest effective master switch."""
    global _WORKER
    config = EvidenceConfig.load()
    with _WORKER_LOCK:
        if config.enabled and config.rollout_mode != "off":
            if _WORKER is None:
                _WORKER = EvidenceIndexWorker()
                _WORKER.start()
            return
        if _WORKER is not None:
            _WORKER.stop()
            _WORKER = None
