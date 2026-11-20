"""Persistent cleanup for Modeling projects left by failed Onto provisioning."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .. import config
from . import modeling_mcp

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (0, 30, 120, 600, 3600)
_TASKS_LOCK = threading.RLock()


def _path() -> Path:
    return Path(config.DATA_DIR) / "modeling-cleanup-tasks.json"


def _read() -> list[dict]:
    path = _path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def _write(tasks: list[dict]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def enqueue(meta: dict, reason: str, operation: str = "provision_rollback") -> dict:
    """Persist an idempotent cleanup task without storing a user token."""
    project_id = str(meta.get("modeling_project_id") or "").strip()
    onto_id = str(meta.get("id") or "").strip()
    if not project_id or not onto_id:
        return {"queued": False}
    with _TASKS_LOCK:
        tasks = _read()
        now = time.time()
        external_id = f"onto:{onto_id}"
        operation = "project_delete" if operation == "project_delete" else "provision_rollback"
        for task in tasks:
            if (task.get("modeling_project_id") == project_id
                    and task.get("external_id") == external_id
                    and task.get("operation") == operation):
                task["last_error"] = reason[:500]
                task["status"] = "retrying"
                task["next_retry_at"] = min(float(task.get("next_retry_at", now)), now)
                _write(tasks)
                return {"queued": True, "id": task["id"]}
        task = {
            "id": f"cleanup-{int(now * 1000)}-{project_id}",
            "onto_project_id": onto_id,
            "modeling_project_id": project_id,
            "external_id": external_id,
            "operation": operation,
            "status": "pending",
            "attempts": 0,
            "next_retry_at": now,
            "last_error": reason[:500],
            "created_at": now,
            "updated_at": now,
        }
        tasks.append(task)
        _write(tasks)
        return {"queued": True, "id": task["id"]}


def list_tasks() -> list[dict]:
    with _TASKS_LOCK:
        return _read()


def retry_task(task_id: str) -> bool:
    with _TASKS_LOCK:
        tasks = _read()
        found = False
        now = time.time()
        for task in tasks:
            if task.get("id") == task_id:
                task["status"] = "retrying"
                task["next_retry_at"] = now
                task["updated_at"] = now
                found = True
                break
        if found:
            _write(tasks)
        return found


def retry_due(limit: int = 5) -> int:
    if config.MODELING_CORE_RETIRED:
        # Keep historical tasks for an explicit legacy rollback or data audit.
        return 0
    with _TASKS_LOCK:
        tasks = _read()
        now = time.time()
        changed = False
        processed = 0
        for task in tasks:
            if processed >= limit or task.get("status") in ("succeeded", "dead_letter"):
                continue
            if float(task.get("next_retry_at", 0)) > now:
                continue
            processed += 1
            task["status"] = "processing"
            task["updated_at"] = now
            changed = True
            meta = {
                "id": task["onto_project_id"],
                "modeling_project_id": task["modeling_project_id"],
            }
            try:
                modeling_mcp.delete_project(
                    meta,
                    cleanup=True,
                    operation=str(task.get("operation") or "provision_rollback"),
                )
                task["status"] = "succeeded"
                task["last_error"] = ""
            except modeling_mcp.ModelingMcpError as exc:
                if exc.status == 404:
                    task["status"] = "succeeded"
                    task["last_error"] = ""
                else:
                    attempts = int(task.get("attempts", 0)) + 1
                    task["attempts"] = attempts
                    task["last_error"] = str(exc)[:500]
                    if attempts >= MAX_ATTEMPTS:
                        task["status"] = "dead_letter"
                    else:
                        task["status"] = "retrying"
                        task["next_retry_at"] = now + BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS) - 1)]
            finally:
                task["updated_at"] = time.time()
        if changed:
            _write(tasks)
        return processed


async def scheduler_loop() -> None:
    import asyncio

    interval = max(5, config.MODELING_CLEANUP_INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.to_thread(retry_due)
        except Exception:
            # A failed sweep must not terminate the application lifecycle.
            pass
        await asyncio.sleep(interval)
