"""证据检索索引（`design/evidence-collection.md` §6）：`DATA_DIR/evidence/index/<项目>.sqlite`，标准库 sqlite3 + FTS5。

工作区的 `resource/systems/<源>/<快照>/` 是唯一的事实来源，索引随时可以丢掉重建：每次读之前按快照清单的指纹
对一遍，工作区多了就补、少了就删、变了就重建这一份（回退版本、清理快照之后都对得上）；文件坏了或版本不对整个重建。
全文检索用 trigram 分词：程序里的名字是 `createOrder`、`OrderService` 这种连写，按子串找才有用；不到 3 个字的词退回 LIKE。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from . import storage

VERSION = "1"
SYSTEMS_DIR = "resource/systems"
_log = logging.getLogger(__name__)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE snapshots(id TEXT PRIMARY KEY, source_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
  complete INTEGER NOT NULL, started_at TEXT, ended_at TEXT, git_bytes INTEGER NOT NULL,
  manifest TEXT NOT NULL, inventory TEXT);
CREATE TABLE records(rid INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL, anchor TEXT NOT NULL, type TEXT NOT NULL,
  name TEXT NOT NULL, label TEXT, parent TEXT, confidence TEXT, module TEXT, from_anchor TEXT, to_anchor TEXT,
  hash TEXT, body TEXT NOT NULL);
CREATE UNIQUE INDEX records_anchor ON records(snapshot_id, anchor);
CREATE INDEX records_parent ON records(snapshot_id, parent);
CREATE INDEX records_from ON records(snapshot_id, from_anchor);
CREATE INDEX records_to ON records(snapshot_id, to_anchor);
CREATE VIRTUAL TABLE records_fts USING fts5(name, label, body, tokenize='trigram');
"""


def _path(project_id: str) -> Path:
    return storage.root() / "index" / f"{project_id}.sqlite"


def _lock(project_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(project_id, threading.Lock())


def _open(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def _create(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    connection = _open(path)
    connection.executescript(_SCHEMA)
    connection.execute("INSERT INTO meta VALUES ('version', ?)", (VERSION,))
    connection.commit()
    return connection


def _usable(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        connection = _open(path)
        row = connection.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        if row and row[0] == VERSION:
            return connection
        connection.close()
    except sqlite3.DatabaseError:
        pass
    return None


# ---------------------------------------------------------------------------
# 工作区里的快照
# ---------------------------------------------------------------------------


def _real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _real_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def workspace_snapshots(root: Path) -> dict[str, tuple[str, Path]]:
    """{快照 ID: (源 ID, 目录)}：`resource/systems/<源>/<快照>/manifest.json` 在的才算。"""
    base = Path(root) / SYSTEMS_DIR
    found: dict[str, tuple[str, Path]] = {}
    if not base.is_dir() or base.is_symlink():
        return found
    for source in sorted(base.iterdir()):
        if not source.is_dir() or source.is_symlink():
            continue
        for snapshot in sorted(source.iterdir()):
            manifest = snapshot / "manifest.json"
            # 快照 ID 项目内唯一（入库时保证）；工作区里被手工弄出重号时只认第一个
            if _real_dir(snapshot) and _real_file(manifest) and snapshot.name not in found:
                found[snapshot.name] = (source.name, snapshot)
    return found


def _fingerprint(directory: Path) -> str:
    digest = hashlib.sha256((directory / "manifest.json").read_bytes())
    records = directory / "records"
    if records.is_dir():
        for item in sorted(records.glob("*.jsonl")):
            stat = item.stat()
            digest.update(f"{item.name}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()


def _flat_text(value, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        out.append(str(value))
    elif isinstance(value, list):
        for item in value:
            _flat_text(item, out)
    elif isinstance(value, dict):
        for item in value.values():
            _flat_text(item, out)


def _load_snapshot(connection: sqlite3.Connection, snapshot_id: str, source_id: str, directory: Path) -> None:
    manifest = storage.read_json(directory / "manifest.json", None)
    if not isinstance(manifest, dict):
        return
    inventory = storage.read_json(directory / "inventory.json", None)
    git_bytes = sum(item.stat().st_size for item in directory.rglob("*") if item.is_file() and not item.is_symlink())
    connection.execute(
        "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (snapshot_id, source_id, _fingerprint(directory), 1 if manifest.get("complete") else 0,
         str(manifest.get("started_at") or ""), str(manifest.get("ended_at") or ""), git_bytes,
         json.dumps(manifest, ensure_ascii=False), json.dumps(inventory, ensure_ascii=False) if inventory else None))
    records = directory / "records"
    for path in sorted(records.glob("*.jsonl")) if records.is_dir() else []:
        if path.is_symlink():
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                    continue
                attrs = record.get("attrs") if isinstance(record.get("attrs"), dict) else {}
                evidence = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO records(snapshot_id, anchor, type, name, label, parent, confidence, module,"
                    " from_anchor, to_anchor, hash, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (snapshot_id, record["id"], str(record.get("type") or ""), str(record.get("name") or ""),
                     record.get("label") if isinstance(record.get("label"), str) else None,
                     record.get("parent") if isinstance(record.get("parent"), str) else None,
                     str(evidence.get("confidence") or "") or None, str(attrs.get("module") or "") or None,
                     record.get("from") if isinstance(record.get("from"), str) else None,
                     record.get("to") if isinstance(record.get("to"), str) else None,
                     str(record.get("hash") or ""), json.dumps(record, ensure_ascii=False)))
                if not cursor.rowcount:
                    continue
                text: list[str] = [record["id"]]
                _flat_text(attrs, text)
                connection.execute("INSERT INTO records_fts(rowid, name, label, body) VALUES (?, ?, ?, ?)",
                                   (cursor.lastrowid, str(record.get("name") or ""), record.get("label") or "",
                                    " ".join(text)))


def _drop_snapshot(connection: sqlite3.Connection, snapshot_id: str) -> None:
    connection.execute("DELETE FROM records_fts WHERE rowid IN (SELECT rid FROM records WHERE snapshot_id = ?)",
                       (snapshot_id,))
    connection.execute("DELETE FROM records WHERE snapshot_id = ?", (snapshot_id,))
    connection.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))


def _sync(connection: sqlite3.Connection, root: Path) -> None:
    present = workspace_snapshots(root)
    indexed = {row["id"]: row["fingerprint"] for row in connection.execute("SELECT id, fingerprint FROM snapshots")}
    for snapshot_id in set(indexed) - set(present):
        _drop_snapshot(connection, snapshot_id)
    for snapshot_id, (source_id, directory) in present.items():
        fingerprint = _fingerprint(directory)
        if indexed.get(snapshot_id) == fingerprint:
            continue
        if snapshot_id in indexed:
            _drop_snapshot(connection, snapshot_id)
        _load_snapshot(connection, snapshot_id, source_id, directory)
    connection.commit()


@contextmanager
def connect(project_id: str, root: Path):
    """同步过的只读连接；索引坏了就整个重建。"""
    path = _path(project_id)
    with _lock(project_id):
        connection = _usable(path)
        try:
            if connection is None:
                connection = _create(path)
            _sync(connection, root)
        except sqlite3.DatabaseError:
            _log.warning("证据索引损坏，重建（项目 %s）", project_id)
            if connection is not None:
                connection.close()
            connection = _create(path)
            _sync(connection, root)
    try:
        yield connection
    finally:
        connection.close()


def add_snapshot(project_id: str, root: Path, source_id: str, snapshot_id: str) -> None:
    """入库后增量建索引（同步就是增量的）。"""
    with connect(project_id, root):
        pass


def drop(project_id: str) -> None:
    """项目删除时丢掉索引。"""
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{_path(project_id)}{suffix}").unlink(missing_ok=True)
