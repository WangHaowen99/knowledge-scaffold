"""证据检索（契约 EV-18～EV-29 的读这一侧；`design/evidence-collection.md` §6「检索」）。

数据来自索引（`index.py`），引用关系来自工作区：`ontology/`、`reports/` 里出现的系统锚点，加上产物 frontmatter 的
`evidence_snapshots: {源: 快照}`（详设 5.8.5 规则 3）。产物没声明快照时，锚点按这个源最新的完整快照解析。
记录内容来自目标程序，输出都带 `untrusted_content: true`。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import RECORD_TYPES, dbview, storage
from .index import workspace_snapshots

REFERENCE_DIRS = ("ontology", "reports")
CONFIRMED_DIR = "ontology/"   # 已确认产物；reports/ 里是候选清单和检查报告
TEXT_SUFFIXES = (".md", ".yaml", ".yml", ".json", ".csv", ".txt")
FILE_MAX = 2 * 1024 * 1024
WHERE_USED_MAX = 500
LINEAGE_MAX = 100
CHILDREN_MAX = 50
SYSTEM_SCHEMES = ("bin", "db", "code", "ui", "api", "cli", "log")
# 系统锚点：`<scheme>:<源>/<路径类型>:<名字>`，数据库是 `db:<源>/<库>.<表>[.<列>]`（§9）；
# 名字里的 `/ . # @ | [ ]` 和空格都已百分号编码
ANCHOR = re.compile(r"\b(?:(?:bin|code|ui|api|cli|log):[a-z0-9][a-z0-9-]{1,40}/[A-Za-z]+:|db:[a-z0-9][a-z0-9-]{1,40}/)"
                    r"[^\s`'\"<>()\[\]|,;，。；、：（）【】「」]+")
ANCHOR_PREFIX = re.compile(r"^(?:bin|db|code|ui|api|cli|log):[a-z0-9][a-z0-9-]{1,40}/")
_ANCHOR_PARTS = re.compile(r"^(bin|db|code|ui|api|cli|log):([a-z0-9][a-z0-9-]{1,40})/([A-Za-z]+):(.+)$")
RELATION_VERBS = {"calls": "调用", "handles": "处理", "exposes": "暴露", "requests": "请求", "inherits": "继承",
                  "imports": "导入", "invokes_native": "原生调用"}


def parse_anchor(anchor: str) -> dict | None:
    """系统锚点 → {scheme, source_id, kind, name}；数据库锚点的 kind 是 `db`，name 是 `<库>.<表>…`。"""
    matched = _ANCHOR_PARTS.match(anchor or "")
    if not matched:
        db = dbview.parse(anchor)
        if db is None:
            return None
        return {"scheme": "db", "source_id": db["source_id"], "kind": "db",
                "name": anchor.split("/", 1)[1]}
    return {"scheme": matched.group(1), "source_id": matched.group(2), "kind": matched.group(3),
            "name": matched.group(4)}


# ---------------------------------------------------------------------------
# 引用：产物里的锚点与快照声明
# ---------------------------------------------------------------------------


@dataclass
class References:
    anchors: dict[str, list[tuple[str, int]]] = field(default_factory=dict)  # 锚点 → [(文件, 行号)]
    declared: dict[str, dict[str, str]] = field(default_factory=dict)        # 文件 → {源: 快照}

    def files_of_source(self, source_id: str) -> set[str]:
        return {rel for anchor, hits in self.anchors.items()
                if (parse_anchor(anchor) or {}).get("source_id") == source_id for rel, _line in hits}

    def snapshot_references(self, source_id: str, snapshot_id: str, latest_complete: str | None) -> int:
        """引用这份快照的已确认产物（`ontology/`）数：声明了它的；它是最新完整快照时，再加上引用这个源却没声明快照的。
        `reports/` 里是候选和检查报告，不挡清理。"""
        declared = {rel for rel, mapping in self.declared.items() if mapping.get(source_id) == snapshot_id}
        if snapshot_id == latest_complete:
            declared |= {rel for rel in self.files_of_source(source_id)
                         if source_id not in self.declared.get(rel, {})}
        return sum(1 for rel in declared if confirmed(rel))


def confirmed(rel: str) -> bool:
    return rel.startswith(CONFIRMED_DIR)


def _clean_anchor(text: str) -> str:
    return text.rstrip(".:")


def declared_snapshots(text: str) -> dict[str, str]:
    from ..kernel.artifacts.parsers import ParseError, split_frontmatter

    try:
        front, _body = split_frontmatter(text)
    except ParseError:
        return {}
    mapping = front.get("evidence_snapshots")
    if not isinstance(mapping, dict):
        return {}
    return {str(key): str(value) for key, value in mapping.items() if isinstance(value, (str, int))}


def references(root: Path) -> References:
    out = References()
    for folder in REFERENCE_DIRS:
        base = Path(root) / folder
        if not base.is_dir() or base.is_symlink():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            try:
                if path.stat().st_size > FILE_MAX:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            if path.suffix.lower() == ".md":
                mapping = declared_snapshots(text)
                if mapping:
                    out.declared[rel] = mapping
            if ":" not in text:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                for match in ANCHOR.finditer(line):
                    out.anchors.setdefault(_clean_anchor(match.group(0)), []).append((rel, number))
    return out


def target_ref(root: Path, rel: str) -> dict:
    """引用所在的文件 → EntryRef（本体条目）或 ArtifactRef（插件产物、其他文件）。"""
    from ..services import project_events

    entries = project_events.entry_refs(root, [rel])
    if entries:
        return entries[0]
    artifacts = project_events.artifact_refs(root, [rel])
    if artifacts:
        return artifacts[0]
    return {"type": "file", "key": rel, "title": "", "label": ""}


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------


def latest_snapshot(root: Path, source_id: str, *, complete_only: bool = True) -> str | None:
    """直接看工作区（入库时用，不依赖索引）。快照 ID 以 UTC 时间开头，按字典序就是先后。"""
    for snapshot_id, (source, directory) in sorted(workspace_snapshots(root).items(), reverse=True):
        if source != source_id:
            continue
        manifest = storage.read_json(directory / "manifest.json", {}) or {}
        if not complete_only or manifest.get("complete"):
            return snapshot_id
    return None


def snapshot_rows(connection: sqlite3.Connection, source_id: str | None = None) -> list[sqlite3.Row]:
    if source_id:
        return list(connection.execute("SELECT * FROM snapshots WHERE source_id = ? ORDER BY id DESC", (source_id,)))
    return list(connection.execute("SELECT * FROM snapshots ORDER BY id DESC"))


def snapshot_row(connection: sqlite3.Connection, snapshot_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()


def latest_complete(connection: sqlite3.Connection, source_id: str) -> str | None:
    row = connection.execute("SELECT id FROM snapshots WHERE source_id = ? AND complete = 1 ORDER BY id DESC LIMIT 1",
                             (source_id,)).fetchone()
    return row["id"] if row else None


def source_ref(source_id: str) -> dict:
    from . import sources

    registration = sources.get(source_id)
    if registration:
        return sources.ref(registration)
    return {"id": source_id, "display_name": source_id, "environment": "test"}


def snapshot_ref(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "source_id": row["source_id"], "complete": bool(row["complete"])}


def summary(connection: sqlite3.Connection, row: sqlite3.Row, refs: References) -> dict:
    """SnapshotSummary。"""
    manifest = json.loads(row["manifest"])
    counts = manifest.get("records") if isinstance(manifest.get("records"), dict) else {}
    return {
        "id": row["id"], "source": source_ref(row["source_id"]),
        "provider": str(manifest.get("provider") or ""),
        "complete": bool(row["complete"]), "coverage_pct": float(manifest.get("coverage_pct") or 0),
        "records": {kind: int(counts.get(kind) or 0) for kind in RECORD_TYPES},
        "referenced": refs.snapshot_references(row["source_id"], row["id"],
                                               latest_complete(connection, row["source_id"])),
        "masking_policy": str(manifest.get("masking_policy") or "none"),
        "started_at": row["started_at"] or None, "ended_at": row["ended_at"] or None,
        "previous": manifest.get("previous"), "job_id": str(manifest.get("job_id") or ""),
        "git_bytes": int(row["git_bytes"]), "untrusted_content": bool(manifest.get("untrusted_content", True)),
    }


def detail(connection: sqlite3.Connection, row: sqlite3.Row, refs: References) -> dict:
    """EV-19：清单去掉文件哈希和凭据引用；清单范围内采到的对象与没采的。"""
    manifest = {key: value for key, value in json.loads(row["manifest"]).items() if key not in ("files", "cred_ref")}
    inventory = json.loads(row["inventory"]) if row["inventory"] else {}
    inventory = inventory if isinstance(inventory, dict) else {}
    return {**summary(connection, row, refs), "manifest": manifest,
            "inventory": {"collected": len(inventory.get("items") or []),
                          "skipped": list(inventory.get("skipped") or [])}}


def resolve_snapshot(connection: sqlite3.Connection, source_id: str, *, snapshot: str | None = None,
                     declared: dict[str, str] | None = None) -> str | None:
    """指定的快照 → 产物声明的快照 → 这个源最新的完整快照。"""
    if snapshot:
        return snapshot
    if declared and declared.get(source_id):
        return declared[source_id]
    return latest_complete(connection, source_id)


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------


def _loc(attrs: dict) -> str:
    loc = attrs.get("loc") if isinstance(attrs.get("loc"), dict) else {}
    file, line = str(loc.get("file") or ""), loc.get("line")
    return f"{file}:{line}" if file and isinstance(line, int) else file


def snippet(record: dict) -> str:
    """记录摘要：名字、中文名，加上能认出它的几个属性。"""
    attrs = record.get("attrs") if isinstance(record.get("attrs"), dict) else {}
    parts = [str(record.get("name") or "") + (f"（{record['label']}）" if record.get("label") else "")]
    for key in ("kind", "method", "url", "channel", "signature", "module"):
        value = attrs.get(key)
        if isinstance(value, (str, int)) and str(value):
            parts.append(f"{key}={value}")
    if _loc(attrs):
        parts.append(f"@{_loc(attrs)}")
    return " ".join(parts)[:300]


def _record(row: sqlite3.Row) -> dict:
    return json.loads(row["body"])


def record_row(connection: sqlite3.Connection, snapshot_id: str, anchor: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM records WHERE snapshot_id = ? AND anchor = ?",
                              (snapshot_id, anchor)).fetchone()


def search(connection: sqlite3.Connection, snapshots: list[str], q: str, kind: str | None) -> list[dict]:
    """全部命中（按名字精确、前缀、长度排），分页由调用方做。"""
    if not snapshots:
        return []
    marks = ",".join("?" * len(snapshots))
    params: list = list(snapshots)
    where = f"r.snapshot_id IN ({marks})"
    if kind:
        where += " AND r.type = ?"
        params.append(kind)
    needle = q.strip()
    order = ("ORDER BY lower(r.name) = lower(?) DESC, lower(r.name) LIKE lower(?) ESCAPE '\\' DESC, "
             "length(r.name), r.name")
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    if not needle:
        sql = f"SELECT r.* FROM records r WHERE {where} ORDER BY r.type, r.name"
        rows = connection.execute(sql, params).fetchall()
    elif len(needle) >= 3:
        phrase = '"' + needle.replace('"', '""') + '"'
        sql = (f"SELECT r.* FROM records_fts f JOIN records r ON r.rid = f.rowid WHERE records_fts MATCH ? AND {where} "
               f"{order}")
        rows = connection.execute(sql, [phrase, *params, needle, f"{escaped}%"]).fetchall()
    else:
        like = f"%{escaped}%"
        sql = (f"SELECT r.* FROM records r WHERE {where} AND (r.name LIKE ? ESCAPE '\\' OR r.label LIKE ? ESCAPE '\\' "
               f"OR r.anchor LIKE ? ESCAPE '\\') {order}")
        rows = connection.execute(sql, [*params, like, like, like, needle, f"{escaped}%"]).fetchall()
    out = []
    for row in rows:
        record = _record(row)
        out.append({"anchor": row["anchor"], "type": row["type"], "name": row["name"], "label": row["label"],
                    "parent_anchor": row["parent"], "snippet": snippet(record), "confidence": row["confidence"],
                    "snapshot_id": row["snapshot_id"]})
    return out


def untrusted(connection: sqlite3.Connection, snapshot_ids) -> bool:
    """这些快照里有没有标 `untrusted_content` 的（内容来自目标程序）。"""
    for snapshot_id in snapshot_ids:
        row = snapshot_row(connection, snapshot_id) if snapshot_id else None
        if row is not None and json.loads(row["manifest"]).get("untrusted_content", True):
            return True
    return False


def _member_type(child: sqlite3.Row, body: dict, attrs: dict) -> str:
    return str(attrs.get("signature") or body.get("data_type") or attrs.get("type") or attrs.get("kind")
               or child["type"])


def record_view(connection: sqlite3.Connection, row: sqlite3.Row, snapshot: sqlite3.Row) -> dict:
    """EvidenceRecord：记录本身，`fields` 列它的成员（类的字段和方法，最多 50 条，`type` 填签名或类型）。"""
    record = _record(row)
    evidence = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
    if row["anchor"].startswith("db:"):
        view = _base_view(connection, row, snapshot, record, evidence, [])
        return dbview.enrich_record(connection, view, row["snapshot_id"], snapshot["source_id"])
    children = connection.execute(
        "SELECT * FROM records WHERE snapshot_id = ? AND parent = ? AND type IN ('field', 'operation') "
        "ORDER BY type, name LIMIT ?", (row["snapshot_id"], row["anchor"], CHILDREN_MAX)).fetchall()
    fields = []
    for child in children:
        body = _record(child)
        attrs = body.get("attrs") if isinstance(body.get("attrs"), dict) else {}
        fields.append({"name": child["name"], "type": _member_type(child, body, attrs), "note": child["label"] or "",
                       "confidence": child["confidence"] or "observed", "masked": False, "anchor": child["anchor"]})
    return _base_view(connection, row, snapshot, record, evidence, fields)


def _base_view(connection: sqlite3.Connection, row: sqlite3.Row, snapshot: sqlite3.Row, record: dict,
               evidence: dict, fields: list[dict]) -> dict:
    return {
        "anchor": row["anchor"], "type": row["type"], "name": row["name"], "label": row["label"],
        "parent_anchor": row["parent"], "confidence": row["confidence"] or "observed",
        "evidence": {"provider": str(evidence.get("provider") or ""), "method": str(evidence.get("method") or "")},
        "attrs": record.get("attrs") if isinstance(record.get("attrs"), dict) else {},
        "fields": fields, "snapshot": snapshot_ref(snapshot),
        "untrusted_content": untrusted(connection, [snapshot["id"]]),
    }


def lineage(connection: sqlite3.Connection, snapshot_id: str, anchor: str) -> list[dict]:
    """关系记录里的邻居，加上级。数据库锚点给外键邻居和推断关系（basis 用记录的 `attrs.basis`）。"""
    if anchor.startswith("db:"):
        return dbview.lineage(connection, snapshot_id, anchor, LINEAGE_MAX)
    items: list[dict] = []

    def label_of(other: str | None, fallback: str) -> str:
        if other:
            found = record_row(connection, snapshot_id, other)
            if found:
                return found["label"] or found["name"]
        return fallback

    for row in connection.execute("SELECT * FROM records WHERE snapshot_id = ? AND type = 'relation' AND "
                                  "(from_anchor = ? OR to_anchor = ?) ORDER BY name LIMIT ?",
                                  (snapshot_id, anchor, anchor, LINEAGE_MAX)):
        body = _record(row)
        attrs = body.get("attrs") if isinstance(body.get("attrs"), dict) else {}
        verb = RELATION_VERBS.get(row["name"], row["name"])
        if row["from_anchor"] == anchor:
            other = row["to_anchor"]
            items.append({"anchor": other or row["anchor"],
                          "label": label_of(other, str(attrs.get("url") or other or "")),
                          "basis": verb, "confidence": row["confidence"] or "observed"})
        else:
            other = row["from_anchor"]
            items.append({"anchor": other or row["anchor"], "label": label_of(other, other or ""),
                          "basis": f"被{verb}", "confidence": row["confidence"] or "observed"})
    current = record_row(connection, snapshot_id, anchor)
    if current is not None and current["parent"] and len(items) < LINEAGE_MAX:
        items.append({"anchor": current["parent"], "label": label_of(current["parent"], current["parent"]),
                      "basis": "上级", "confidence": current["confidence"] or "observed"})
    return items[:LINEAGE_MAX]


def where_used(root: Path, refs: References, anchor: str, prefix: bool) -> list[dict]:
    items = []
    for found, hits in sorted(refs.anchors.items()):
        if found != anchor and not (prefix and found.startswith(anchor)):
            continue
        for rel, line in hits:
            items.append({"target": target_ref(root, rel), "location": f"{rel}:L{line}", "anchor": found})
            if len(items) >= WHERE_USED_MAX:
                return items
    return items


def diff(connection: sqlite3.Connection, root: Path, refs: References, old: str, new: str) -> tuple[list[dict], dict]:
    """按锚点 + 哈希比较；affected 是引用这个锚点的产物。"""
    def rows(snapshot_id: str) -> dict[str, sqlite3.Row]:
        return {row["anchor"]: row for row in connection.execute(
            "SELECT anchor, type, name, label, hash, body FROM records WHERE snapshot_id = ?", (snapshot_id,))}

    before, after = rows(old), rows(new)
    items = []
    targets: dict[str, dict] = {}
    for anchor in sorted(set(before) | set(after)):
        if anchor not in before:
            change, row = "added", after[anchor]
        elif anchor not in after:
            change, row = "removed", before[anchor]
        elif before[anchor]["hash"] != after[anchor]["hash"]:
            change, row = "modified", after[anchor]
        else:
            continue
        affected = []
        for rel, _line in refs.anchors.get(anchor, []):
            if rel not in targets:
                targets[rel] = target_ref(root, rel)
            if targets[rel] not in affected:
                affected.append(targets[rel])
        items.append({"change": change, "type": row["type"], "anchor": anchor, "summary": snippet(_record(row)),
                      "affected": affected, "_paths": sorted({rel for rel, _line in refs.anchors.get(anchor, [])})})
    counts = {name: sum(1 for item in items if item["change"] == name) for name in ("added", "removed", "modified")}
    counts["touching_confirmed"] = sum(1 for item in items if any(confirmed(rel) for rel in item["_paths"]))
    return items, counts


def summary_tree(connection: sqlite3.Connection, root: Path, refs: References, snapshot: sqlite3.Row) -> dict:
    """EV-23：实体和操作按 `attrs.module` 分组（模块记录本身就是组，不列）；mapped_to 是引用了它的本体条目。"""
    from ..services import project_events

    def mapped(anchor: str) -> dict | None:
        for rel, _line in refs.anchors.get(anchor, []):
            entries = project_events.entry_refs(root, [rel])
            if entries:
                return entries[0]
        return None

    if dbview.is_db_snapshot(connection, snapshot["id"]):
        return {"snapshot": snapshot_ref(snapshot),
                "domains": dbview.summary_domains(connection, snapshot["id"], mapped),
                "untrusted_content": untrusted(connection, [snapshot["id"]])}
    groups: dict[str, list[dict]] = {}
    for row in connection.execute("SELECT * FROM records WHERE snapshot_id = ? AND type IN ('entity', 'operation') "
                                   "ORDER BY module, type, name", (snapshot["id"],)):
        attrs = _record(row).get("attrs") or {}
        if isinstance(attrs, dict) and attrs.get("kind") == "module":
            continue
        groups.setdefault(row["module"] or "", []).append(
            {"anchor": row["anchor"], "type": row["type"], "name": row["name"], "label": row["label"],
             "mapped_to": mapped(row["anchor"])})
    domains = [{"name": name or "（未分组）", "count": len(entities), "entities": entities}
               for name, entities in sorted(groups.items())]
    return {"snapshot": snapshot_ref(snapshot), "domains": domains,
            "untrusted_content": untrusted(connection, [snapshot["id"]])}


def resolution(connection: sqlite3.Connection, anchor: str, *, declared: dict[str, str] | None = None) -> dict:
    """系统锚点的 AnchorResolution（契约 7.1；spec §6）。"""
    parsed = parse_anchor(anchor)
    scheme = parsed["scheme"] if parsed else (anchor.split(":", 1)[0] if ":" in anchor else "bin")
    base = {"anchor": anchor, "scheme": scheme if scheme in SYSTEM_SCHEMES else "bin", "resolved": False,
            "source_label": "", "location_label": "", "material": None, "text": "", "highlights": [],
            "confidence": None, "snapshot_id": None, "page": "schema"}
    if parsed is None:
        return {**base, "reason": "invalid", "page": None}
    source = source_ref(parsed["source_id"])
    base["source_label"] = source["display_name"]
    snapshot_id = resolve_snapshot(connection, parsed["source_id"], declared=declared)
    row = record_row(connection, snapshot_id, anchor) if snapshot_id else None
    if row is None:
        return {**base, "reason": "not_in_snapshot", "snapshot_id": snapshot_id}
    record = _record(row)
    attrs = record.get("attrs") if isinstance(record.get("attrs"), dict) else {}
    where = dbview.location(anchor) if parsed["scheme"] == "db" else _loc(attrs)
    return {**base, "resolved": True, "location_label": where, "text": snippet(record),
            "confidence": row["confidence"] or "observed", "snapshot_id": snapshot_id}

