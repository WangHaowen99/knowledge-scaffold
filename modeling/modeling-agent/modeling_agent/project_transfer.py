from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .errors import AgentRuntimeError
from .store import RuntimeStore, now_iso


TRANSFER_FORMAT = "modeling-agent-project-transfer"
TRANSFER_VERSION = 1
SOURCE_PROJECT_ID = "pj_source"
ACTIVE_EXECUTIONS = {"queued", "running", "waiting", "suspended"}

PROJECT_TABLES = (
    "agent_runs",
    "agent_interactions",
    "agent_events",
    "external_project_events",
    "steering_messages",
    "project_agent_state",
    "project_agent_state_transitions",
    "agent_tool_receipts",
    "agent_validation_repair_state",
)


def export_project_transfer(
    store: RuntimeStore,
    checkpoint_db: Path,
    project_id: str,
) -> dict[str, Any]:
    with store._lock:  # noqa: SLF001 - migration must share the store transaction lock
        database = store._db  # noqa: SLF001
        state = database.execute(
            "SELECT state FROM project_agent_state WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        execution = str(state["state"]).partition(":")[2] if state else "idle"
        if execution in {"queued", "running"}:
            raise AgentRuntimeError(
                "PROJECT_TRANSFER_BUSY",
                "建模助手仍在运行，请等待当前任务结束或暂停后再导出",
                status=409,
            )
        runs = _rows(database, "SELECT * FROM agent_runs WHERE project_id = ?", (project_id,))
        run_ids = [str(row["id"]) for row in runs]
        tables: dict[str, list[dict[str, Any]]] = {
            "agent_runs": runs,
            "agent_interactions": _rows(
                database,
                "SELECT * FROM agent_interactions WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ),
            "agent_events": _rows(
                database,
                "SELECT * FROM agent_events WHERE project_id = ? ORDER BY seq",
                (project_id,),
            ),
            "external_project_events": _rows(
                database,
                "SELECT * FROM external_project_events WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ),
            "steering_messages": _rows(
                database,
                "SELECT * FROM steering_messages WHERE project_id = ? ORDER BY id",
                (project_id,),
            ),
            "project_agent_state": _rows(
                database,
                "SELECT * FROM project_agent_state WHERE project_id = ?",
                (project_id,),
            ),
            "project_agent_state_transitions": _rows(
                database,
                "SELECT * FROM project_agent_state_transitions WHERE project_id = ? ORDER BY id",
                (project_id,),
            ),
            "agent_tool_receipts": _rows_for_runs(database, "agent_tool_receipts", run_ids),
            "agent_validation_repair_state": _rows_for_runs(
                database,
                "agent_validation_repair_state",
                run_ids,
            ),
        }
        sessions = [str(row["session_id"]) for row in runs if str(row["session_id"])]

    for row in tables["agent_runs"]:
        row["token_fingerprint"] = ""
        row["run_credential"] = ""
    normalized_tables = {
        table: [_normalize_project_value(row, project_id) for row in rows]
        for table, rows in tables.items()
    }
    checkpoints = [
        {
            "key": str(_normalize_project_value(row["key"], project_id)),
            "value": str(row["value"]),
        }
        for row in _checkpoint_rows(checkpoint_db, sessions)
    ]
    return {
        "format": TRANSFER_FORMAT,
        "version": TRANSFER_VERSION,
        "sourceProjectId": SOURCE_PROJECT_ID,
        "tables": normalized_tables,
        "checkpoints": checkpoints,
        "excluded": ["agent_runs.run_credential", "agent_runs.token_fingerprint"],
    }


def import_project_transfer(
    store: RuntimeStore,
    checkpoint_db: Path,
    project_id: str,
    payload: dict[str, Any],
) -> dict[str, str]:
    tables, checkpoints = _validate_payload(payload)
    id_map = _build_id_map(tables)
    id_map[SOURCE_PROJECT_ID] = project_id
    transformed = {
        table: [
            _transform_import_row(table, row, id_map, project_id)
            for row in rows
        ]
        for table, rows in tables.items()
    }
    transformed_checkpoints = [
        {
            "key": _transform_text(str(row["key"]), id_map, project_id),
            "value": str(row["value"]),
        }
        for row in checkpoints
    ]

    _suspend_active_runtime(transformed)
    with store._lock:  # noqa: SLF001 - one transaction must cover every runtime table
        database = store._db  # noqa: SLF001
        if _project_exists(database, project_id):
            raise AgentRuntimeError(
                "PROJECT_TRANSFER_NOT_EMPTY",
                "目标项目的建模助手数据不是空的",
                status=409,
            )
        try:
            database.execute("BEGIN IMMEDIATE")
            _insert_runtime_rows(database, transformed)
            _insert_checkpoint_rows(checkpoint_db, transformed_checkpoints)
            database.commit()
        except Exception:
            database.rollback()
            _delete_checkpoint_rows(checkpoint_db, [str(row["key"]) for row in transformed_checkpoints])
            raise
    return id_map


def delete_project_transfer(
    store: RuntimeStore,
    checkpoint_db: Path,
    project_id: str,
) -> None:
    with store._lock:  # noqa: SLF001
        database = store._db  # noqa: SLF001
        sessions = [
            str(row["session_id"])
            for row in database.execute(
                "SELECT session_id FROM agent_runs WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        ]
        run_ids = [
            str(row["id"])
            for row in database.execute(
                "SELECT id FROM agent_runs WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        ]
        try:
            database.execute("BEGIN IMMEDIATE")
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                database.execute(
                    f"DELETE FROM agent_tool_receipts WHERE run_id IN ({placeholders})",
                    run_ids,
                )
                database.execute(
                    f"DELETE FROM agent_validation_repair_state WHERE run_id IN ({placeholders})",
                    run_ids,
                )
            for table in (
                "agent_interactions",
                "agent_events",
                "external_project_events",
                "steering_messages",
                "project_agent_state_transitions",
                "project_agent_state",
                "agent_runs",
            ):
                database.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))
            _delete_checkpoint_prefixes(checkpoint_db, sessions)
            database.commit()
        except Exception:
            database.rollback()
            raise


def _rows(
    database: sqlite3.Connection,
    statement: str,
    parameters: tuple[Any, ...],
) -> list[dict[str, Any]]:
    return [dict(row) for row in database.execute(statement, parameters).fetchall()]


def _rows_for_runs(
    database: sqlite3.Connection,
    table: str,
    run_ids: list[str],
) -> list[dict[str, Any]]:
    if not run_ids:
        return []
    placeholders = ",".join("?" for _ in run_ids)
    return _rows(
        database,
        f"SELECT * FROM {table} WHERE run_id IN ({placeholders}) ORDER BY run_id",
        tuple(run_ids),
    )


def _checkpoint_rows(path: Path, sessions: list[str]) -> list[dict[str, str]]:
    if not sessions or not path.exists():
        return []
    database = sqlite3.connect(path)
    database.row_factory = sqlite3.Row
    try:
        if not database.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kv_store'",
        ).fetchone():
            return []
        rows: dict[str, dict[str, str]] = {}
        for session in sessions:
            for row in database.execute(
                "SELECT key, value FROM kv_store WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
                (f"{_escape_like(session)}:%",),
            ).fetchall():
                rows[str(row["key"])] = {"key": str(row["key"]), "value": str(row["value"])}
        return [rows[key] for key in sorted(rows)]
    finally:
        database.close()


def _validate_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
    if (
        payload.get("format") != TRANSFER_FORMAT
        or payload.get("version") != TRANSFER_VERSION
        or payload.get("sourceProjectId") != SOURCE_PROJECT_ID
    ):
        raise AgentRuntimeError("PROJECT_TRANSFER_INVALID", "建模助手迁移数据版本不受支持", status=400)
    raw_tables = payload.get("tables")
    raw_checkpoints = payload.get("checkpoints")
    if not isinstance(raw_tables, dict) or set(raw_tables) != set(PROJECT_TABLES):
        raise AgentRuntimeError("PROJECT_TRANSFER_INVALID", "建模助手迁移数据表不完整", status=400)
    tables: dict[str, list[dict[str, Any]]] = {}
    for table in PROJECT_TABLES:
        rows = raw_tables.get(table)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise AgentRuntimeError("PROJECT_TRANSFER_INVALID", "建模助手迁移数据格式不正确", status=400)
        tables[table] = [dict(row) for row in rows]
    if not isinstance(raw_checkpoints, list) or not all(
        isinstance(row, dict) and isinstance(row.get("key"), str) and isinstance(row.get("value"), str)
        for row in raw_checkpoints
    ):
        raise AgentRuntimeError("PROJECT_TRANSFER_INVALID", "建模助手检查点格式不正确", status=400)
    for table in (
        "agent_runs",
        "agent_interactions",
        "agent_events",
        "external_project_events",
        "steering_messages",
        "project_agent_state",
        "project_agent_state_transitions",
    ):
        if any(row.get("project_id") != SOURCE_PROJECT_ID for row in tables[table]):
            raise AgentRuntimeError("PROJECT_TRANSFER_INVALID", "迁移数据包含其他项目", status=400)
    if any(str(row.get("run_credential") or "") or str(row.get("token_fingerprint") or "")
           for row in tables["agent_runs"]):
        raise AgentRuntimeError("PROJECT_TRANSFER_SECRET", "迁移数据包含运行凭据", status=400)
    session_prefixes = {
        f"{str(row.get('session_id') or '')}:"
        for row in tables["agent_runs"]
        if str(row.get("session_id") or "")
    }
    checkpoints = [{"key": str(row["key"]), "value": str(row["value"])} for row in raw_checkpoints]
    if any(not any(row["key"].startswith(prefix) for prefix in session_prefixes) for row in checkpoints):
        raise AgentRuntimeError("PROJECT_TRANSFER_INVALID", "迁移数据包含其他项目的检查点", status=400)
    return tables, checkpoints


def _build_id_map(tables: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    values: list[tuple[str, str]] = []
    for table, column in (
        ("agent_runs", "id"),
        ("agent_interactions", "id"),
        ("external_project_events", "id"),
    ):
        for row in tables[table]:
            value = str(row.get(column) or "")
            if value:
                values.append((value, _portable_id(value)))
    return dict(values)


def _transform_import_row(
    table: str,
    row: dict[str, Any],
    id_map: dict[str, str],
    project_id: str,
) -> dict[str, Any]:
    transformed = {
        key: _transform_value(value, id_map, project_id)
        for key, value in row.items()
    }
    if table in {"agent_events", "steering_messages", "project_agent_state_transitions"}:
        transformed.pop("seq" if table == "agent_events" else "id", None)
    if table == "agent_runs":
        transformed["token_fingerprint"] = ""
        transformed["run_credential"] = ""
    return transformed


def _transform_value(value: Any, id_map: dict[str, str], project_id: str) -> Any:
    if isinstance(value, str):
        return _transform_text(value, id_map, project_id)
    if isinstance(value, list):
        return [_transform_value(item, id_map, project_id) for item in value]
    if isinstance(value, dict):
        return {key: _transform_value(item, id_map, project_id) for key, item in value.items()}
    return value


def _transform_text(value: str, id_map: dict[str, str], project_id: str) -> str:
    if value in id_map:
        return id_map[value]
    transformed = value.replace(SOURCE_PROJECT_ID, project_id)
    for source, target in id_map.items():
        if source != SOURCE_PROJECT_ID and source in transformed:
            transformed = transformed.replace(source, target)
    stripped = transformed.strip()
    if not ((stripped.startswith("{") and stripped.endswith("}"))
            or (stripped.startswith("[") and stripped.endswith("]"))):
        return transformed
    try:
        parsed = json.loads(transformed)
    except json.JSONDecodeError:
        return transformed
    return json.dumps(
        _transform_value(parsed, id_map, project_id),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _suspend_active_runtime(tables: dict[str, list[dict[str, Any]]]) -> None:
    timestamp = now_iso()
    pending_runs = {
        str(row.get("run_id") or "")
        for row in tables["agent_interactions"]
        if row.get("status") == "pending"
    }
    for row in tables["agent_runs"]:
        if row.get("status") in ACTIVE_EXECUTIONS and str(row.get("id") or "") not in pending_runs:
            row["status"] = "suspended"
            row["error"] = "项目迁移后已停在最近检查点"
            row["updated_at"] = timestamp
    for row in tables["project_agent_state"]:
        phase, separator, execution = str(row.get("state") or "intake:idle").partition(":")
        if separator and execution in ACTIVE_EXECUTIONS and not str(row.get("interaction_id") or ""):
            row["state"] = f"{phase}:suspended"
            row["error"] = "项目迁移后已停在最近检查点"
            row["updated_at"] = timestamp
    for row in tables["steering_messages"]:
        if row.get("status") == "queued":
            row["status"] = "delivered"
            row["delivered_at"] = timestamp


def _insert_runtime_rows(
    database: sqlite3.Connection,
    tables: dict[str, list[dict[str, Any]]],
) -> None:
    order = (
        "agent_runs",
        "agent_interactions",
        "agent_events",
        "external_project_events",
        "steering_messages",
        "project_agent_state",
        "project_agent_state_transitions",
        "agent_tool_receipts",
        "agent_validation_repair_state",
    )
    allowed_columns = {
        table: {
            str(row["name"])
            for row in database.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for table in order
    }
    for table in order:
        for row in tables[table]:
            if not set(row).issubset(allowed_columns[table]):
                raise AgentRuntimeError(
                    "PROJECT_TRANSFER_VERSION",
                    f"目标环境无法导入建模助手数据表：{table}",
                    status=400,
                )
            columns = list(row)
            placeholders = ",".join("?" for _ in columns)
            database.execute(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                [row[column] for column in columns],
            )


def _insert_checkpoint_rows(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path)
    try:
        database.execute(
            """CREATE TABLE IF NOT EXISTS kv_store (
            key VARCHAR(255) NOT NULL PRIMARY KEY,
            value VARCHAR(4096) NOT NULL
            )""",
        )
        database.executemany(
            "INSERT INTO kv_store(key, value) VALUES (?, ?)",
            [(row["key"], row["value"]) for row in rows],
        )
        database.commit()
    finally:
        database.close()


def _delete_checkpoint_rows(path: Path, keys: list[str]) -> None:
    if not keys or not path.exists():
        return
    database = sqlite3.connect(path)
    try:
        if database.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kv_store'",
        ).fetchone():
            database.executemany("DELETE FROM kv_store WHERE key = ?", [(key,) for key in keys])
            database.commit()
    finally:
        database.close()


def _delete_checkpoint_prefixes(path: Path, sessions: list[str]) -> None:
    if not sessions or not path.exists():
        return
    database = sqlite3.connect(path)
    try:
        if database.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kv_store'",
        ).fetchone():
            for session in sessions:
                database.execute(
                    "DELETE FROM kv_store WHERE key LIKE ? ESCAPE '\\'",
                    (f"{_escape_like(session)}:%",),
                )
            database.commit()
    finally:
        database.close()


def _project_exists(database: sqlite3.Connection, project_id: str) -> bool:
    checks = (
        "SELECT 1 FROM agent_runs WHERE project_id = ? LIMIT 1",
        "SELECT 1 FROM agent_events WHERE project_id = ? LIMIT 1",
        "SELECT 1 FROM project_agent_state WHERE project_id = ? LIMIT 1",
    )
    return any(database.execute(statement, (project_id,)).fetchone() for statement in checks)


def _normalize_project_value(value: Any, project_id: str) -> Any:
    if isinstance(value, str):
        return value.replace(project_id, SOURCE_PROJECT_ID)
    if isinstance(value, list):
        return [_normalize_project_value(item, project_id) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_project_value(item, project_id) for key, item in value.items()}
    return value


def _portable_id(source: str) -> str:
    match = re.match(r"^([A-Za-z][A-Za-z0-9]{0,11})_", source)
    prefix = match.group(1).lower() if match else "id"
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _escape_like(value: str) -> str:
    return re.sub(r"([\\%_])", r"\\\1", value)
