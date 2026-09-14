# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Crash recovery for deterministic terminal and artifact commits."""

from typing import Callable, Optional

from persistence.chat_store import ChatStore
from persistence.execution_store import ExecutionStore
from utils.logging import get_logger


logger = get_logger("persistence.deterministic_terminal")


def persist_deterministic_terminal(
    execution_store: ExecutionStore,
    chat_store: ChatStore,
    *,
    session_id: str,
    turn_id: int,
    status: str,
    final_output: dict,
    llm_context: list[dict],
    events: list[dict],
    deterministic_checkpoint: Optional[dict] = None,
) -> None:
    """Durably project one deterministic terminal event with recovery intent."""

    with execution_store.deterministic_terminal_transaction(session_id):
        execution_store.prepare_deterministic_terminal(
            session_id,
            turn_id,
            status=status,
            final_output=final_output,
            summary=None,
            llm_context=llm_context,
            events=events,
            deterministic_checkpoint=deterministic_checkpoint,
        )
        if not chat_store.append_events_once(session_id, events):
            raise RuntimeError("deterministic terminal session is missing")
        if not chat_store.sync_event_log(session_id):
            raise RuntimeError("deterministic terminal event could not be synced")
        execution_store.commit_deterministic_terminal(session_id, turn_id)


def _artifact_transaction_fields(transaction: dict) -> tuple[int, str, list[str]]:
    turn_id = transaction.get("turn_id")
    run_id = str(transaction.get("run_id") or "").strip()
    output_refs = transaction.get("output_refs")
    if not isinstance(turn_id, int) or not run_id:
        raise RuntimeError("deterministic artifact recovery record is invalid")
    if not isinstance(output_refs, list):
        raise RuntimeError("deterministic artifact recovery record is invalid")
    for output_ref in output_refs:
        if not isinstance(output_ref, str) or len(output_ref) != 32:
            raise RuntimeError("deterministic artifact recovery record is invalid")
        if any(character not in "0123456789abcdef" for character in output_ref):
            raise RuntimeError("deterministic artifact recovery record is invalid")
    return turn_id, run_id, output_refs


def recover_pending_deterministic_terminal(
    execution_store: ExecutionStore,
    chat_store: ChatStore,
    session_id: str,
) -> bool:
    """Finish a deterministic terminal commit interrupted by process exit."""
    with execution_store.deterministic_terminal_transaction(session_id):
        pending = execution_store.get_pending_deterministic_terminal(session_id)
        if pending is None:
            return False
        turn_id = pending.get("turn_id")
        events = pending.get("events")
        if not isinstance(turn_id, int) or not isinstance(events, list):
            raise RuntimeError("deterministic terminal recovery record is invalid")
        if not chat_store.append_events_once(session_id, events):
            raise RuntimeError("deterministic terminal recovery session is missing")
        if not chat_store.sync_event_log(session_id):
            raise RuntimeError(
                "deterministic terminal recovery could not sync chat events"
            )
        execution_store.commit_deterministic_terminal(session_id, turn_id)
        logger.info(
            "Recovered deterministic terminal commit session=%s turn=%s",
            session_id,
            turn_id,
        )
        return True


def recover_pending_deterministic_artifacts(
    execution_store: ExecutionStore,
    chat_store: ChatStore,
    session_id: str,
    *,
    run_is_active: Optional[Callable[[str], bool]] = None,
) -> bool:
    """Resolve artifacts left between durable file writes and terminal commit."""
    # Classify active runs before taking the per-session transaction lock. The
    # async run manager may finalize the same turn concurrently, so recovery
    # revalidates the exact journal entry after acquiring the store lock.
    snapshot = execution_store.get_pending_deterministic_artifacts(session_id)
    snapshot_by_run_id = {}
    inactive_run_ids = set()
    for transaction in snapshot:
        _, run_id, _ = _artifact_transaction_fields(transaction)
        snapshot_by_run_id[run_id] = transaction
        if run_is_active is None or not run_is_active(run_id):
            inactive_run_ids.add(run_id)

    recovered = False
    with execution_store.deterministic_terminal_transaction(session_id):
        for transaction in execution_store.get_pending_deterministic_artifacts(
            session_id
        ):
            turn_id, run_id, output_refs = _artifact_transaction_fields(transaction)
            if run_id not in inactive_run_ids:
                continue
            # A writer may have reserved more refs after the snapshot. Leave a
            # new or changed transaction for the next recovery pass rather than
            # deleting artifacts that can still belong to a live run.
            if snapshot_by_run_id.get(run_id) != transaction:
                continue

            turn = execution_store.get_turn(session_id, turn_id)
            turn_belongs_to_run = bool(
                isinstance(turn, dict)
                and str(turn.get("run_id") or "") == run_id
            )
            turn_status = (
                str(turn.get("status") or "") if turn_belongs_to_run else ""
            )
            if turn_status == "success":
                execution_store.commit_deterministic_artifacts(
                    session_id,
                    turn_id,
                    run_id,
                )
                recovered = True
                continue
            # An active-set snapshot can predate a newly admitted Run. Never
            # infer abandonment from absence alone while its durable Turn is
            # still resumable or running.
            if turn_status in {"running", "awaiting_input"}:
                continue

            deleted_refs = []
            failed_refs = []
            for output_ref in output_refs:
                try:
                    deleted = chat_store.delete_tool_output(
                        session_id=session_id,
                        run_id=run_id,
                        output_ref=output_ref,
                        missing_ok=True,
                    )
                except Exception:
                    deleted = False
                    logger.warning(
                        "Failed to recover deterministic artifact run=%s ref=%s",
                        run_id,
                        output_ref,
                        exc_info=True,
                    )
                if deleted:
                    deleted_refs.append(output_ref)
                else:
                    failed_refs.append(output_ref)
            if deleted_refs:
                execution_store.release_deterministic_artifacts(
                    session_id,
                    run_id,
                    deleted_refs,
                )
                recovered = True
            if failed_refs:
                raise RuntimeError(
                    "deterministic artifact recovery remains pending"
                )
    return recovered
