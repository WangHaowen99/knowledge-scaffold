"""Crash recovery for deterministic terminal and artifact commits."""

from typing import Callable, Optional

from persistence.chat_store import ChatStore
from persistence.execution_store import ExecutionStore
from utils.logging import get_logger


logger = get_logger("persistence.deterministic_terminal")


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
    # Do not call into RunStore while holding the per-session ExecutionStore
    # lock. Terminal run transitions hold RunStore's lock while committing the
    # execution journal, so the opposite order would deadlock. Snapshot first,
    # classify active runs without either ExecutionStore transaction lock, then
    # revalidate the exact transaction before changing it.
    snapshot = execution_store.get_pending_deterministic_artifacts(session_id)
    snapshot_by_run_id = {}
    inactive_run_ids = set()
    for transaction in snapshot:
        turn_id = transaction.get("turn_id")
        run_id = str(transaction.get("run_id") or "").strip()
        output_refs = transaction.get("output_refs")
        if (
            not isinstance(turn_id, int)
            or not run_id
            or not isinstance(output_refs, list)
            or any(
                not isinstance(output_ref, str)
                or len(output_ref) != 32
                or any(
                    character not in "0123456789abcdef"
                    for character in output_ref
                )
                for output_ref in output_refs
            )
        ):
            raise RuntimeError("deterministic artifact recovery record is invalid")
        snapshot_by_run_id[run_id] = transaction
        if run_is_active is None or not run_is_active(run_id):
            inactive_run_ids.add(run_id)

    recovered = False
    with execution_store.deterministic_terminal_transaction(session_id):
        for transaction in execution_store.get_pending_deterministic_artifacts(
            session_id
        ):
            turn_id = transaction.get("turn_id")
            run_id = str(transaction.get("run_id") or "").strip()
            output_refs = transaction.get("output_refs")
            if (
                not isinstance(turn_id, int)
                or not run_id
                or not isinstance(output_refs, list)
                or any(
                    not isinstance(output_ref, str)
                    or len(output_ref) != 32
                    or any(
                        character not in "0123456789abcdef"
                        for character in output_ref
                    )
                    for output_ref in output_refs
                )
            ):
                raise RuntimeError("deterministic artifact recovery record is invalid")
            if run_id not in inactive_run_ids:
                continue
            # A writer may have reserved more refs after the snapshot. Leave a
            # new or changed transaction for the next recovery pass rather than
            # deleting artifacts that can still belong to a live run.
            if snapshot_by_run_id.get(run_id) != transaction:
                continue

            turn = execution_store.get_turn(session_id, turn_id)
            if (
                isinstance(turn, dict)
                and str(turn.get("run_id") or "") == run_id
                and str(turn.get("status") or "") == "success"
            ):
                execution_store.commit_deterministic_artifacts(
                    session_id,
                    turn_id,
                    run_id,
                )
                recovered = True
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
