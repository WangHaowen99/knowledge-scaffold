"""插件产物索引（详设 5.9.4「建索引」「引用和过期」；方案 C13）。

每个项目一份 SQLite（工作区 `.onto/run/artifacts.sqlite`，不进 git，删项目一起删，坏了重建）：

- rows：按类型解析出的行（键、标题、字段、分段、所在文件、内容版本号）。文件签名（路径、
  修改时间、大小）没变就不重新解析；只重解析变了的文件。
- fts：标题、字段与分段的全文索引（FTS5；SQLite 没编译 FTS5 时退回 LIKE）。
- baselines：产物某个内容版本第一次被看到时，它引用的本体条目的指纹。条目之后变了（或被删了）
  而产物没变，就标「过期」——复用流程运行时的指纹思路（㊷）：产物重新生成或保存后基线重置。
- 包脚本生成的标准索引（登记的 `index`，jsonl）按键并进行里；索引记着源文件哈希，对不上就标「待刷新」。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from pathlib import Path

from . import parsers
from .registry import ArtifactType

_log = logging.getLogger(__name__)

INDEX_REL = ".onto/run/artifacts.sqlite"
SCHEMA_VERSION = 2
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_FTS: bool | None = None
_SKIP_NAMES = {"index.md", "INDEX.md"}
_KIND_DIRS = {"object": ("ontology/object",), "operation": ("ontology/operation", "ontology/logic"),
              "rule": ("ontology/rule",), "metric": ("ontology/metric",), "script": ("ontology/scripts",)}


def _lock(root: Path) -> threading.Lock:
    key = str(Path(root).resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def fts_available() -> bool:
    global _FTS
    if _FTS is None:
        try:
            sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            _FTS = True
        except sqlite3.OperationalError:
            _FTS = False
    return _FTS


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _connect(root: Path) -> sqlite3.Connection:
    path = Path(root) / INDEX_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        for table in ("files", "rows", "baselines", "fts"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.execute("CREATE TABLE IF NOT EXISTS files (type TEXT, rel TEXT, sig TEXT, PRIMARY KEY (type, rel))")
    conn.execute("CREATE TABLE IF NOT EXISTS rows (type TEXT, key TEXT, rel TEXT, data TEXT, PRIMARY KEY (type, key))")
    conn.execute("CREATE TABLE IF NOT EXISTS baselines (type TEXT, key TEXT, revision TEXT, refs TEXT, "
                 "PRIMARY KEY (type, key, revision))")
    if fts_available():
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(type UNINDEXED, key UNINDEXED, text)")
    else:
        conn.execute("CREATE TABLE IF NOT EXISTS fts (type TEXT, key TEXT, text TEXT)")
    return conn


# ---- 本体条目指纹 ----


def entity_path(root: Path, kind: str, name: str) -> Path | None:
    """引用指向的本体条目文件（按文件名匹配，目录递归）；找不到返回 None。"""
    name = parsers.plain_ref(name)
    if not name or "/" in name or name.startswith("."):
        return None
    suffix = ".py" if kind == "script" else ".md"
    for directory in _KIND_DIRS.get(kind, ()):
        base = Path(root) / directory
        direct = base / f"{name}{suffix}"
        if direct.is_file():
            return direct
        if base.is_dir() and kind == "object":
            found = next((path for path in base.rglob(f"{name}{suffix}") if path.is_file()), None)
            if found:
                return found
    return None


def entity_fingerprint(root: Path, kind: str, name: str) -> str | None:
    path = entity_path(root, kind, name)
    if path is None or inside(root, path.relative_to(root).as_posix()) is None:
        return None
    try:
        return sha(path.read_bytes())
    except OSError:
        return None


def entry_ref(kind: str, value) -> dict | None:
    name = parsers.plain_ref(value.get("key") or value.get("name") if isinstance(value, dict) else value)
    if not name:
        return None
    return {"kind": kind, "id": f"{kind}:{name}", "key": name, "name": name}


# ---- 文件与行 ----


def inside(root: Path, rel: str) -> Path | None:
    """工作区里的一个文件：本身不是符号链接、解析后（含目录链接）仍在项目根目录内才返回，
    和写入事务的 `_resolve_inside` 同一个口径；否则 None（不读工作区外的东西）。"""
    path = Path(root) / rel
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(Path(root).resolve()):
            return None
    except (OSError, RuntimeError):
        return None
    return path


def inside_no_links(root: Path, rel: str) -> Path | None:
    """比 `inside` 更严：路径上任何一段（含目录）是符号链接都不认。助手能在工作区、副本里随手建链接，
    报告、资料分析这类按文件名列出来再读的入口用它，不读链接指向的任何东西。"""
    parts = [part for part in str(rel or "").replace("\\", "/").split("/") if part]
    if not parts or any(part in (".", "..") or "\x00" in part for part in parts):
        return None
    current = Path(root)
    try:
        for part in parts:
            current = current / part
            if current.is_symlink():
                return None
    except (OSError, ValueError):
        return None
    return inside(root, "/".join(parts))


def _files(atype: ArtifactType, root: Path) -> list[str]:
    base = Path(root) / atype.glob_root() if atype.glob_root() else Path(root)
    if not base.is_dir():
        return []
    prefix = atype.path.partition("*")[0]
    out = []
    pattern = atype.path[len(atype.glob_root()) + 1:] if atype.glob_root() else atype.path
    for path in sorted(base.glob(pattern)):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and path.name not in _SKIP_NAMES and rel.startswith(prefix) \
                and atype.key_from_path(rel) is not None and inside(root, rel) is not None:
            out.append(rel)
    return out


def _sig(root: Path, rel: str) -> str:
    stat = (Path(root) / rel).stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _row_text(row: dict) -> str:
    parts = [row.get("title") or "", row.get("key") or ""]
    parts += [str(value) for value in (row.get("fields") or {}).values() if isinstance(value, (str, int, float))]
    parts += [str(value) for value in (row.get("sections") or {}).values()]
    return "\n".join(parts)[:20000]


def _parse_rows(atype: ArtifactType, root: Path, rel: str) -> list[dict]:
    path = Path(root) / rel
    try:
        data = path.read_bytes()
        items = parsers.parse(atype, rel, data, file_key=atype.key_from_path(rel) or path.stem,
                              mtime=path.stat().st_mtime)
    except (OSError, parsers.ParseError) as exc:
        key = atype.key_from_path(rel) or path.stem
        return [{"key": key, "rel": rel, "title": key, "fields": {}, "sections": {}, "revision": "",
                 "updated_at": path.stat().st_mtime if path.exists() else None, "error": str(exc)}]
    rows = []
    mtime = path.stat().st_mtime
    file_revision = sha(data)
    for item in items:
        revision = file_revision
        if atype.format == "md-sections":
            revision = sha(json.dumps([item.heading, item.fields, item.sections], ensure_ascii=False, sort_keys=True).encode())
        rows.append({"key": item.key, "rel": rel, "title": item.title, "fields": item.fields,
                     "sections": item.sections, "revision": revision, "file_revision": file_revision,
                     "updated_at": mtime, "error": None})
    return rows


def _read_index_file(root: Path, rel: str | None) -> dict[str, dict]:
    if not rel:
        return {}
    path = inside(root, rel)
    out: dict[str, dict] = {}
    if path is None:
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("key"):
            out[str(record["key"])] = record
    return out


def read_extras(root: Path, rel: str) -> list[dict]:
    path = inside(root, rel)
    out = []
    if path is None:
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return out
    for line in lines[:500]:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def refresh(atype: ArtifactType, root: Path) -> list[dict]:
    """按类型取全部行（必要时重解析变了的文件），已并入包索引、算好过期。"""
    root = Path(root)
    with _lock(root):
        conn = _connect(root)
        try:
            with conn:
                rows = _refresh_rows(conn, atype, root)
                rows = _merge_index(atype, root, rows)
                for row in rows:
                    for name, value in atype.defaults.items():
                        if row["fields"].get(name) in (None, ""):
                            row["fields"][name] = value
                _mark_stale(conn, atype, root, rows)
            return rows
        finally:
            conn.close()


def _refresh_rows(conn: sqlite3.Connection, atype: ArtifactType, root: Path) -> list[dict]:
    files = _files(atype, root)
    known = {row["rel"]: row["sig"] for row in conn.execute("SELECT rel, sig FROM files WHERE type=?", (atype.type,))}
    current = {rel: _sig(root, rel) for rel in files}
    for rel in set(known) - set(current):
        conn.execute("DELETE FROM files WHERE type=? AND rel=?", (atype.type, rel))
        _drop_rows(conn, atype.type, rel)
    for rel, sig in current.items():
        if known.get(rel) == sig:
            continue
        _drop_rows(conn, atype.type, rel)
        for row in _parse_rows(atype, root, rel):
            conn.execute("INSERT OR REPLACE INTO rows (type, key, rel, data) VALUES (?, ?, ?, ?)",
                         (atype.type, row["key"], rel, json.dumps(row, ensure_ascii=False, default=str)))
            conn.execute("DELETE FROM fts WHERE type=? AND key=?", (atype.type, row["key"]))
            conn.execute("INSERT INTO fts (type, key, text) VALUES (?, ?, ?)", (atype.type, row["key"], _row_text(row)))
        conn.execute("INSERT OR REPLACE INTO files (type, rel, sig) VALUES (?, ?, ?)", (atype.type, rel, sig))
    rows = [json.loads(row["data"]) for row in conn.execute(
        "SELECT data FROM rows WHERE type=? ORDER BY rel, key", (atype.type,))]
    return rows


def rows_of(atype: ArtifactType, root: Path, rels) -> list[dict]:
    """只解析给定的几份文件（不走缓存、不记基线、不算过期），已并入包索引与缺省值。

    一轮结束出产物卡用：助手在变更集副本里改的文件还没进主线，不给副本另建一份索引。"""
    root = Path(root)
    wanted = sorted({rel for rel in rels if atype.key_from_path(rel) is not None and Path(rel).name not in _SKIP_NAMES
                     and inside(root, rel) is not None and (root / rel).is_file()})
    rows = _merge_index(atype, root, [row for rel in wanted for row in _parse_rows(atype, root, rel)])
    for row in rows:
        for name, value in atype.defaults.items():
            if row["fields"].get(name) in (None, ""):
                row["fields"][name] = value
    return sorted(rows, key=lambda row: (row["rel"], row["key"]))


def _drop_rows(conn: sqlite3.Connection, art_type: str, rel: str) -> None:
    keys = [row["key"] for row in conn.execute("SELECT key FROM rows WHERE type=? AND rel=?", (art_type, rel))]
    for key in keys:
        conn.execute("DELETE FROM fts WHERE type=? AND key=?", (art_type, key))
    conn.execute("DELETE FROM rows WHERE type=? AND rel=?", (art_type, rel))


def _merge_index(atype: ArtifactType, root: Path, rows: list[dict]) -> list[dict]:
    """并入包脚本生成的标准索引：计算出来的字段与分段；源文件哈希对不上标「待刷新」。"""
    records = _read_index_file(root, atype.index)
    if not records:
        return rows
    by_key = {row["key"]: row for row in rows}
    for key, record in records.items():
        row = by_key.get(key)
        if row is None:
            continue  # 源文件已经没有这一项：索引里的旧行不显示
        for name, value in (record.get("fields") or {}).items():
            if row["fields"].get(name) in (None, ""):
                row["fields"][name] = value
        for name, value in (record.get("sections") or {}).items():
            if not row["sections"].get(name):
                row["sections"][name] = value
        if record.get("title") and row["title"] == row["key"]:
            row["title"] = str(record["title"])
        expected = record.get("source_sha256")
        if expected and expected not in (row.get("file_revision"), row.get("revision")):
            row["index_outdated"] = True
    return rows


def _ref_values(atype: ArtifactType, row: dict) -> list[tuple[str, str, str]]:
    out = []
    for ref in atype.refs:
        value = row["fields"].get(ref["field"])
        values = value if isinstance(value, list) else [value]
        for item in values:
            entry = entry_ref(ref["kind"], item) if item not in (None, "") else None
            if entry:
                out.append((ref["field"], ref["kind"], entry["key"]))
    return out


def _mark_stale(conn: sqlite3.Connection, atype: ArtifactType, root: Path, rows: list[dict]) -> None:
    fingerprints: dict[tuple[str, str], str | None] = {}
    for row in rows:
        reasons = []
        current = {}
        for _field, kind, key in _ref_values(atype, row):
            if (kind, key) not in fingerprints:
                fingerprints[(kind, key)] = entity_fingerprint(root, kind, key)
            current[f"{kind}:{key}"] = fingerprints[(kind, key)]
        saved = conn.execute("SELECT refs FROM baselines WHERE type=? AND key=? AND revision=?",
                             (atype.type, row["key"], row["revision"])).fetchone()
        if saved is None:
            conn.execute("INSERT OR REPLACE INTO baselines (type, key, revision, refs) VALUES (?, ?, ?, ?)",
                         (atype.type, row["key"], row["revision"], json.dumps(current, sort_keys=True)))
            baseline = current
        else:
            baseline = json.loads(saved["refs"])
        for ident, fingerprint in current.items():
            name = ident.split(":", 1)[1]
            if fingerprint is None:
                reasons.append(f"引用的条目「{name}」不存在")
            elif baseline.get(ident) and baseline[ident] != fingerprint:
                reasons.append(f"引用的条目「{name}」在这之后改过")
        if row.get("index_outdated"):
            reasons.append("文件改过，包的索引还没刷新")
        if row.get("error"):
            reasons.append(f"解析失败：{row['error']}")
        row["stale"] = bool(reasons)
        row["stale_reason"] = "；".join(reasons) or None


def search(root: Path, art_type: str, q: str) -> set[str]:
    """全文搜索命中的键（FTS5 用前缀查询；没有 FTS5 时 LIKE）。"""
    needle = (q or "").strip()
    if not needle:
        return set()
    with _lock(root):
        conn = _connect(root)
        try:
            if fts_available():
                terms = " ".join('"' + part.replace('"', '""') + '"*' for part in needle.split())
                try:
                    hits = {row["key"] for row in conn.execute(
                        "SELECT key FROM fts WHERE type=? AND fts MATCH ?", (art_type, terms))}
                except sqlite3.OperationalError:
                    hits = set()
                # 中文没有分词：FTS 只按整段匹配，补一次子串
                hits |= {row["key"] for row in conn.execute(
                    "SELECT key FROM fts WHERE type=? AND text LIKE ?", (art_type, f"%{needle}%"))}
                return hits
            return {row["key"] for row in conn.execute(
                "SELECT key FROM fts WHERE type=? AND text LIKE ?", (art_type, f"%{needle}%"))}
        finally:
            conn.close()


def forget(root: Path) -> None:
    """删掉项目的索引（测试与修复用）；下次读取时重建。"""
    with _lock(root):
        (Path(root) / INDEX_REL).unlink(missing_ok=True)
