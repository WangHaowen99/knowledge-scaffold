"""Independent-process restart and durable Turn-allocation experiments.

The parent process starts a real child process against real ``ExecutionStore``
and ``ChatStore`` directories, explicitly fsyncs the resulting persistence
tree, sends SIGKILL, and verifies the state from another fresh process.  No
fake store, fake manager, or in-memory persistence substitute is used.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
from pathlib import Path
import signal
import tempfile
import time
from typing import Any
from uuid import uuid4

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
from persistence.chat_store import ChatStore
from persistence.execution_store import ExecutionStore

_CHILD_TIMEOUT_SECONDS = 10.0
_PROCESS_CONTEXT = multiprocessing.get_context("spawn")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".{}.{}.tmp".format(path.name, uuid4().hex))
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError("JSON root must be an object: {}".format(path))
    return value


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for path in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    descriptor = os.open(str(root), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stores(round_root: Path) -> tuple[ExecutionStore, ChatStore]:
    return (
        ExecutionStore(str(round_root / "executions")),
        ChatStore(str(round_root / "chats")),
    )


def _session_id(kind: str, round_index: int) -> str:
    return "session_restart_{}_{:04d}".format(kind, round_index)


def _seed_running(round_root: Path, marker: Path, round_index: int) -> None:
    execution_store, chat_store = _stores(round_root)
    session_id = _session_id("running", round_index)
    chat_store.create_session(session_id=session_id, title="Synthetic restart run")
    turn, created = execution_store.create_turn_for_run(
        session_id=session_id,
        user_request="Start a synthetic long-running request",
        idempotency_key_hash="running-seed-key-{:04d}".format(round_index),
        request_fingerprint="running-seed-fingerprint-{:04d}".format(round_index),
    )
    if not created:
        raise RuntimeError("running seed admission was not created")
    chat_store.upsert_message(
        session_id,
        "user",
        "Start a synthetic long-running request",
        turn_id=int(turn["turn_id"]),
        metadata={"run_id": turn["run_id"], "synthetic": True},
    )
    if not chat_store.set_active_run(session_id, str(turn["run_id"])):
        raise RuntimeError("running seed chat projection failed")
    _fsync_tree(round_root)
    _atomic_write_json(
        marker,
        {
            "kind": "running",
            "session_id": session_id,
            "run_id": turn["run_id"],
            "turn_id": turn["turn_id"],
            "status": turn["status"],
        },
    )
    while True:
        signal.pause()


def _verify_running(round_root: Path, marker: Path, round_index: int) -> dict[str, Any]:
    expected = _read_json(marker)
    execution_store, chat_store = _stores(round_root)
    session_id = _session_id("running", round_index)
    if expected.get("session_id") != session_id:
        raise RuntimeError("running marker session mismatch")
    history = execution_store.get_session_history(session_id)
    if history is None or len(history.get("turns", [])) != 1:
        raise RuntimeError("running history was not recovered exactly once")
    interrupted = history["turns"][0]
    running_recovery_failed = any(
        (
            interrupted.get("run_id") != expected.get("run_id"),
            interrupted.get("status") != "failed",
            interrupted.get("error_code") != "SERVICE_RESTARTED",
            (interrupted.get("final_output") or {}).get("error_code") != "SERVICE_RESTARTED",
        )
    )
    if running_recovery_failed:
        raise RuntimeError("running Turn was not marked SERVICE_RESTARTED")
    if chat_store.get_session(session_id) is None:
        raise RuntimeError("running ChatStore session did not survive")

    next_turn, created = execution_store.create_turn_for_run(
        session_id=session_id,
        user_request="Submit a new synthetic request after restart",
        idempotency_key_hash="running-retry-key-{:04d}".format(round_index),
        request_fingerprint="running-retry-fingerprint-{:04d}".format(round_index),
    )
    if not created or int(next_turn["turn_id"]) != int(interrupted["turn_id"]) + 1:
        raise RuntimeError("new request after running-loss was not admitted")
    if next_turn["run_id"] == interrupted["run_id"]:
        raise RuntimeError("running-loss reconstructed the old Run")
    chat_store.upsert_message(
        session_id,
        "user",
        "Submit a new synthetic request after restart",
        turn_id=int(next_turn["turn_id"]),
        metadata={"run_id": next_turn["run_id"], "synthetic": True},
    )
    chat_store.set_active_run(session_id, str(next_turn["run_id"]))
    execution_store.complete_turn(
        session_id,
        int(next_turn["turn_id"]),
        final_output={"text": "Synthetic restart verification completed"},
        summary=None,
    )
    chat_store.clear_active_run(session_id)
    _fsync_tree(round_root)

    persisted = ExecutionStore(str(round_root / "executions")).get_session_history(session_id)
    if persisted is None:
        raise RuntimeError("running verification history disappeared")
    old_run_occurrences = sum(turn.get("run_id") == interrupted["run_id"] for turn in persisted.get("turns", []))
    if old_run_occurrences != 1 or persisted["turns"][-1]["status"] != "success":
        raise RuntimeError("running-loss verification produced invalid durable history")
    return {
        "interrupted": True,
        "new_turn_created": True,
        "old_run_occurrences": old_run_occurrences,
        "old_run_id_reused": False,
        "http_layer_exercised": False,
        "admission_primitive": "ExecutionStore.create_turn_for_run",
    }


def _interaction(round_index: int) -> dict[str, Any]:
    return {
        "id": "interaction_restart_{:04d}".format(round_index),
        "type": "single_choice",
        "prompt": "Choose a synthetic continuation option",
        "options": [
            {"id": "option_a", "label": "Option A", "value": "value_a"},
            {"id": "option_b", "label": "Option B", "value": "value_b"},
        ],
    }


def _seed_awaiting(round_root: Path, marker: Path, round_index: int) -> None:
    execution_store, chat_store = _stores(round_root)
    session_id = _session_id("awaiting", round_index)
    chat_store.create_session(session_id=session_id, title="Synthetic interaction")
    turn, created = execution_store.create_turn_for_run(
        session_id=session_id,
        user_request="Start a synthetic interaction request",
        idempotency_key_hash="awaiting-seed-key-{:04d}".format(round_index),
        request_fingerprint="awaiting-seed-fingerprint-{:04d}".format(round_index),
    )
    if not created:
        raise RuntimeError("awaiting seed admission was not created")
    interaction = _interaction(round_index)
    chat_store.upsert_message(
        session_id,
        "user",
        "Start a synthetic interaction request",
        turn_id=int(turn["turn_id"]),
        metadata={"run_id": turn["run_id"], "synthetic": True},
    )
    execution_store.await_turn(
        session_id,
        int(turn["turn_id"]),
        final_output={"type": "user_interaction", "interaction": interaction},
    )
    chat_store.upsert_message(
        session_id,
        "assistant",
        str(interaction["prompt"]),
        turn_id=int(turn["turn_id"]),
        metadata={
            "run_id": turn["run_id"],
            "status": "awaiting_input",
            "interaction": interaction,
            "synthetic": True,
        },
    )
    chat_store.clear_active_run(session_id)
    _fsync_tree(round_root)
    _atomic_write_json(
        marker,
        {
            "kind": "awaiting",
            "session_id": session_id,
            "run_id": turn["run_id"],
            "turn_id": turn["turn_id"],
            "interaction": interaction,
        },
    )
    while True:
        signal.pause()


def _verify_awaiting(round_root: Path, marker: Path, round_index: int) -> dict[str, Any]:
    expected = _read_json(marker)
    execution_store, chat_store = _stores(round_root)
    session_id = _session_id("awaiting", round_index)
    interaction = expected.get("interaction")
    original = execution_store.get_turn(session_id, int(expected["turn_id"]))
    if not isinstance(interaction, dict) or original is None:
        raise RuntimeError("awaiting checkpoint did not survive SIGKILL")
    if any(
        (
            original.get("status") != "awaiting_input",
            original.get("pending_interaction") != interaction,
            original.get("interaction_consumed_at"),
        )
    ):
        raise RuntimeError("awaiting checkpoint did not survive SIGKILL")
    if chat_store.get_session(session_id) is None:
        raise RuntimeError("awaiting ChatStore session did not survive")
    restored_chat = chat_store.get_session(session_id)
    if restored_chat is None or not any(
        isinstance(message.metadata.get("interaction"), dict)
        and message.metadata["interaction"].get("id") == interaction["id"]
        for message in restored_chat.messages
    ):
        raise RuntimeError("awaiting interaction card did not survive")

    response = {
        "interaction_id": interaction["id"],
        "type": interaction["type"],
        "option_id": "option_a",
        "value": "value_a",
    }
    idempotency_key = "awaiting-answer-key-{:04d}".format(round_index)
    request_fingerprint = "awaiting-answer-fingerprint-{:04d}".format(round_index)
    answer_turn, created = execution_store.consume_interaction_and_create_turn(
        session_id=session_id,
        interaction_id=str(interaction["id"]),
        user_request="Choose synthetic option A",
        interaction_response=response,
        idempotency_key_hash=idempotency_key,
        request_fingerprint=request_fingerprint,
    )
    retry_turn, retry_created = execution_store.consume_interaction_and_create_turn(
        session_id=session_id,
        interaction_id=str(interaction["id"]),
        user_request="Choose synthetic option A",
        interaction_response=response,
        idempotency_key_hash=idempotency_key,
        request_fingerprint=request_fingerprint,
    )
    cross_key_turn, cross_key_created = execution_store.consume_interaction_and_create_turn(
        session_id=session_id,
        interaction_id=str(interaction["id"]),
        user_request="Choose synthetic option A",
        interaction_response=response,
        idempotency_key_hash="awaiting-answer-retry-key-{:04d}".format(round_index),
        request_fingerprint=request_fingerprint,
    )
    answer_allocation_failed = any(
        (
            not created,
            retry_created,
            retry_turn["run_id"] != answer_turn["run_id"],
            cross_key_created,
            cross_key_turn["run_id"] != answer_turn["run_id"],
            int(answer_turn["turn_id"]) != int(original["turn_id"]) + 1,
            answer_turn["run_id"] == original["run_id"],
        )
    )
    if answer_allocation_failed:
        raise RuntimeError("awaiting answer did not create exactly one new Run/Turn")
    chat_store.upsert_message(
        session_id,
        "user",
        "Choose synthetic option A",
        turn_id=int(answer_turn["turn_id"]),
        metadata={
            "run_id": answer_turn["run_id"],
            "interaction_response": response,
            "synthetic": True,
        },
    )
    chat_store.set_active_run(session_id, str(answer_turn["run_id"]))
    execution_store.complete_turn(
        session_id,
        int(answer_turn["turn_id"]),
        final_output={"text": "Synthetic interaction answer completed"},
        summary=None,
    )
    chat_store.upsert_message(
        session_id,
        "assistant",
        "Synthetic interaction answer completed",
        turn_id=int(answer_turn["turn_id"]),
        metadata={"run_id": answer_turn["run_id"], "synthetic": True},
    )
    chat_store.clear_active_run(session_id)
    _fsync_tree(round_root)

    persisted = ExecutionStore(str(round_root / "executions")).get_session_history(session_id)
    if persisted is None or len(persisted.get("turns", [])) != 2:
        raise RuntimeError("awaiting answer durable history is incomplete")
    persisted_original, persisted_answer = persisted["turns"]
    persisted_linkage_failed = any(
        (
            persisted_original.get("status") != "awaiting_input",
            persisted_original.get("run_id") != original["run_id"],
            persisted_original.get("interaction_consumed_by_turn_id") != persisted_answer.get("turn_id"),
            persisted_original.get("interaction_response") != response,
            persisted_answer.get("status") != "success",
        )
    )
    if persisted_linkage_failed:
        raise RuntimeError("awaiting answer changed the original Run or lost linkage")
    return {
        "checkpoint_survived": True,
        "new_run_created": True,
        "new_turn_created": True,
        "exact_retry_reused_run": True,
        "cross_key_retry_reused_run": True,
        "hitl_card_restored": True,
        "original_run_remained_awaiting": True,
        "http_layer_exercised": False,
        "admission_primitive": "ExecutionStore.consume_interaction_and_create_turn",
    }


def _run_seed_child(
    mode: str,
    round_root: Path,
    marker: Path,
    round_index: int,
) -> None:
    if mode == "seed-running":
        _seed_running(round_root, marker, round_index)
        return
    if mode == "seed-awaiting":
        _seed_awaiting(round_root, marker, round_index)
        return
    raise ValueError("unsupported seed child mode")


def _run_verifier_child(
    mode: str,
    round_root: Path,
    marker: Path,
    round_index: int,
    sender: Any,
) -> None:
    try:
        if mode == "verify-running":
            result = _verify_running(round_root, marker, round_index)
        elif mode == "verify-awaiting":
            result = _verify_awaiting(round_root, marker, round_index)
        else:
            raise ValueError("unsupported verifier child mode")
        sender.send({"passed": True, **result})
    finally:
        sender.close()


def _wait_until_ready(process: multiprocessing.Process, marker: Path) -> None:
    deadline = time.monotonic() + _CHILD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if marker.exists():
            _read_json(marker)
            return
        if process.exitcode is not None:
            process.join(timeout=1.0)
            raise RuntimeError("seed child exited before ready: code={}".format(process.exitcode))
        time.sleep(0.01)
    raise TimeoutError("seed child did not persist readiness marker")


def _kill_seed_child(mode: str, round_root: Path, round_index: int) -> Path:
    if os.name != "posix" or not hasattr(signal, "SIGKILL"):
        raise RuntimeError("restart experiment requires POSIX SIGKILL")
    marker = round_root / "ready.json"
    process = _PROCESS_CONTEXT.Process(
        target=_run_seed_child,
        args=(mode, round_root, marker, round_index),
    )
    process.start()
    ready = False
    try:
        _wait_until_ready(process, marker)
        ready = True
    finally:
        if not ready and process.is_alive():
            process.kill()
            process.join(timeout=5.0)
    process.kill()
    process.join(timeout=5.0)
    if process.is_alive():
        raise TimeoutError("seed child did not stop after SIGKILL")
    expected_code = -int(signal.SIGKILL)
    if process.exitcode != expected_code:
        raise RuntimeError("seed child was not killed by SIGKILL: code={}".format(process.exitcode))
    return marker


def _run_verifier(
    mode: str,
    round_root: Path,
    marker: Path,
    round_index: int,
) -> dict[str, Any]:
    receiver, sender = _PROCESS_CONTEXT.Pipe(duplex=False)
    process = _PROCESS_CONTEXT.Process(
        target=_run_verifier_child,
        args=(mode, round_root, marker, round_index, sender),
    )
    process.start()
    sender.close()
    process.join(timeout=_CHILD_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(timeout=5.0)
        receiver.close()
        raise TimeoutError("verifier child timed out")
    result = None
    if receiver.poll():
        try:
            result = receiver.recv()
        except EOFError:
            result = None
    receiver.close()
    if process.exitcode != 0:
        raise RuntimeError("verifier child failed: code={}".format(process.exitcode))
    if result is None:
        raise RuntimeError("verifier child produced no result")
    if not isinstance(result, dict) or not result.get("passed"):
        raise RuntimeError("verifier child reported failure")
    return result


def _run_restart_rounds(
    root: Path,
    *,
    kind: str,
    rounds: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    seed_mode = "seed-{}".format(kind)
    verify_mode = "verify-{}".format(kind)
    verified = 0
    sigkills = 0
    idempotent_retries = 0
    cross_key_retries = 0
    hitl_cards_restored = 0
    new_runs_created = 0
    original_runs_reopened = 0
    old_run_rebuilds = 0
    for round_index in range(rounds):
        round_root = root / kind / "round_{:04d}".format(round_index)
        round_root.mkdir(parents=True, exist_ok=False)
        marker = _kill_seed_child(seed_mode, round_root, round_index)
        sigkills += 1
        result = _run_verifier(verify_mode, round_root, marker, round_index)
        verified += int(bool(result.get("passed")))
        idempotent_retries += int(bool(result.get("exact_retry_reused_run")))
        cross_key_retries += int(bool(result.get("cross_key_retry_reused_run")))
        hitl_cards_restored += int(bool(result.get("hitl_card_restored")))
        new_runs_created += int(bool(result.get("new_run_created") or result.get("new_turn_created")))
        original_runs_reopened += int(result.get("original_run_remained_awaiting") is False)
        old_run_rebuilds += max(
            0,
            int(result.get("old_run_occurrences", 1)) - 1,
        )
    return {
        "rounds": rounds,
        "sigkill_confirmed": sigkills,
        "verified": verified,
        "idempotent_retries": idempotent_retries,
        "cross_key_retries": cross_key_retries,
        "hitl_cards_restored": hitl_cards_restored,
        "new_runs_created": new_runs_created,
        "original_runs_reopened": original_runs_reopened,
        "old_run_rebuilds": old_run_rebuilds,
        "http_layer_exercised": False,
        "admission_primitive": (
            "ExecutionStore.consume_interaction_and_create_turn"
            if kind == "awaiting"
            else "ExecutionStore.create_turn_for_run"
        ),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


def _run_turn_allocations(
    root: Path,
    *,
    allocations: int,
    session_count: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    storage_dir = root / "turn_allocations" / "executions"
    store = ExecutionStore(str(storage_dir))
    counts = [0 for _ in range(session_count)]
    run_ids: set[str] = set()
    duplicate_run_ids = 0
    for index in range(allocations):
        session_index = index % session_count
        session_id = "session_turn_allocation_{:04d}".format(session_index)
        turn, created = store.create_turn_for_run(
            session_id=session_id,
            user_request="Synthetic allocation {:05d}".format(index),
            idempotency_key_hash="allocation-key-{:05d}".format(index),
            request_fingerprint="allocation-fingerprint-{:05d}".format(index),
        )
        if not created:
            raise RuntimeError("Turn allocation was unexpectedly deduplicated")
        counts[session_index] += 1
        if int(turn["turn_id"]) != counts[session_index]:
            raise RuntimeError("Turn allocation regressed before disk verification")
        run_id = str(turn["run_id"])
        duplicate_run_ids += run_id in run_ids
        run_ids.add(run_id)

    duplicate_turn_ids = 0
    regressions = 0
    sessions_starting_at_one = 0
    json_files = 0
    for session_index, expected_count in enumerate(counts):
        session_id = "session_turn_allocation_{:04d}".format(session_index)
        path = storage_dir / session_id / "execution.json"
        history = _read_json(path)
        json_files += 1
        ids = [int(turn["turn_id"]) for turn in history.get("turns", [])]
        duplicate_turn_ids += len(ids) - len(set(ids))
        regressions += sum(right <= left for left, right in zip(ids, ids[1:]))
        sessions_starting_at_one += bool(ids and ids[0] == 1)
        if ids != list(range(1, expected_count + 1)):
            raise RuntimeError("on-disk Turn sequence is not strictly monotonic")

    fresh_store = ExecutionStore(str(storage_dir))
    for session_index in range(session_count):
        session_id = "session_turn_allocation_{:04d}".format(session_index)
        history = fresh_store.get_session_history(session_id)
        ids = [int(turn["turn_id"]) for turn in (history or {}).get("turns", [])]
        if ids != list(range(1, counts[session_index] + 1)):
            raise RuntimeError("fresh ExecutionStore changed Turn ordering")

    temporary_files = list(storage_dir.rglob(".execution.json.*.tmp"))
    allocation_invariants_failed = any(
        (
            len(run_ids) != allocations,
            duplicate_run_ids,
            duplicate_turn_ids,
            regressions,
            sessions_starting_at_one != session_count,
            temporary_files,
        )
    )
    if allocation_invariants_failed:
        raise RuntimeError("Turn allocation invariants failed")
    return {
        "allocations": allocations,
        "sessions": session_count,
        "minimum_turns_per_session": min(counts),
        "maximum_turns_per_session": max(counts),
        "sessions_starting_at_one": sessions_starting_at_one,
        "duplicate_turn_ids": duplicate_turn_ids,
        "turn_id_regressions": regressions,
        "duplicate_run_ids": duplicate_run_ids,
        "json_files_parsed": json_files,
        "fresh_store_sessions": session_count,
        "temporary_files": len(temporary_files),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


def _validate_persistence_tree(root: Path) -> dict[str, int]:
    json_documents = 0
    jsonl_records = 0
    parse_errors = 0
    temporary_files = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith(".tmp") or ".tmp." in path.name:
            temporary_files += 1
        try:
            if path.suffix == ".json":
                _read_json(path)
                json_documents += 1
            elif path.suffix == ".jsonl":
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            json.loads(line)
                            jsonl_records += 1
        except (json.JSONDecodeError, OSError, UnicodeError):
            parse_errors += 1
    if parse_errors or temporary_files:
        raise RuntimeError("persistence tree contains invalid or temporary files")
    return {
        "json_documents": json_documents,
        "jsonl_records": jsonl_records,
        "parse_errors": parse_errors,
        "temporary_files": temporary_files,
    }


def run_experiments(
    storage_root: Path,
    *,
    running_rounds: int = 20,
    awaiting_rounds: int = 20,
    turn_allocations: int = 10_000,
    allocation_sessions: int = 200,
) -> dict[str, Any]:
    values = (
        running_rounds,
        awaiting_rounds,
        turn_allocations,
        allocation_sessions,
    )
    if min(values) <= 0 or allocation_sessions > turn_allocations:
        raise ValueError("experiment counts must be positive and sessions <= allocations")
    storage_root.mkdir(parents=True, exist_ok=True)
    experiment_root = storage_root / "native_async_restart_experiment"
    experiment_root.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    running = _run_restart_rounds(
        experiment_root / "restart",
        kind="running",
        rounds=running_rounds,
    )
    awaiting = _run_restart_rounds(
        experiment_root / "restart",
        kind="awaiting",
        rounds=awaiting_rounds,
    )
    allocations = _run_turn_allocations(
        experiment_root,
        allocations=turn_allocations,
        session_count=allocation_sessions,
    )
    persistence = _validate_persistence_tree(experiment_root)
    passed = (
        running["verified"] == running_rounds
        and running["sigkill_confirmed"] == running_rounds
        and awaiting["verified"] == awaiting_rounds
        and awaiting["sigkill_confirmed"] == awaiting_rounds
        and awaiting["idempotent_retries"] == awaiting_rounds
        and awaiting["cross_key_retries"] == awaiting_rounds
        and awaiting["hitl_cards_restored"] == awaiting_rounds
        and allocations["allocations"] == turn_allocations
        and allocations["duplicate_turn_ids"] == 0
        and allocations["turn_id_regressions"] == 0
        and allocations["duplicate_run_ids"] == 0
        and persistence["parse_errors"] == 0
        and persistence["temporary_files"] == 0
    )
    return {
        "passed": passed,
        "running_loss": running,
        "awaiting_survival": awaiting,
        "turn_allocations": allocations,
        "persistence": persistence,
        "http_layer_exercised": False,
        "runtime_manager_exercised": False,
        "real_processes": True,
        "real_sigkill": True,
        "lingering_children": 0,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--running-rounds", type=_positive_int, default=20)
    parser.add_argument("--awaiting-rounds", type=_positive_int, default=20)
    parser.add_argument("--turn-allocations", type=_positive_int, default=10_000)
    parser.add_argument("--allocation-sessions", type=_positive_int, default=200)
    parser.add_argument("--storage-root", type=Path)
    args = parser.parse_args()
    if args.storage_root is not None:
        result = run_experiments(
            args.storage_root,
            running_rounds=args.running_rounds,
            awaiting_rounds=args.awaiting_rounds,
            turn_allocations=args.turn_allocations,
            allocation_sessions=args.allocation_sessions,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="native-async-restart-") as temporary:
            result = run_experiments(
                Path(temporary),
                running_rounds=args.running_rounds,
                awaiting_rounds=args.awaiting_rounds,
                turn_allocations=args.turn_allocations,
                allocation_sessions=args.allocation_sessions,
            )
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
