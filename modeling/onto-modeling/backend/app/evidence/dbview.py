"""数据库快照的检索视图（契约 EV-23、EV-25、EV-27、EV-28；`design/evidence-collection.md` §9）。

锚点 `db:<源>/<库>.<表>[.<列>]`：名字里的 `. / # @ | [ ]`、空格都已百分号编码，所以按 `.` 切段、按 `#` 切后缀都不会切错。
`<库>` 在 MySQL 是数据库名，在 PostgreSQL 是 schema 名。表下面的东西（列、外键、推断关系、约束、触发器）锚点都以表锚点开头。

另有 `tree_tables`：直接读工作区（或发布用的只读树）里这个源最新的完整快照，给「数据来源」写锚点时绑定真实表名、列名用。
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from collections import deque
from pathlib import Path

ER_TABLES_MAX = 60
COLUMNS_MAX = 1000
SAMPLE_ROWS_MAX = 20
FK_BASIS = "外键邻居"
_DB_ANCHOR = re.compile(r"^db:([a-z0-9][a-z0-9-]{1,40})/([^\s/#]+)(?:#([a-z]+))?$")
# 表下面的非列段：外键、推断关系、触发器、唯一约束、检查约束
_SUB_KINDS = ("fk:", "ref:", "trigger:", "uk:", "check:")
# 库下面不是表的段：视图、存储过程
_SCHEMA_KINDS = ("view:", "proc:")


def parse(anchor: str) -> dict | None:
    """`db:` 锚点 → {source_id, db, table, column, sub, suffix, table_anchor}；不是 db 锚点为 None。
    `table` 是表名或视图名（已解码）；`column` 是列名；`sub` 是表下的 `fk:名` 等原样段；`suffix` 是 `profile` / `sample`。"""
    matched = _DB_ANCHOR.match(anchor or "")
    if not matched:
        return None
    source_id, path, suffix = matched.groups()
    parts = path.split(".")
    out = {"source_id": source_id, "db": _decode(parts[0]), "table": None, "column": None, "sub": None,
           "suffix": suffix, "table_anchor": None}
    if len(parts) < 2:
        return out
    second = parts[1]
    if second.startswith(_SCHEMA_KINDS):
        if second.startswith("view:"):
            out["table"] = _decode(second[5:])
            out["table_anchor"] = f"db:{source_id}/{parts[0]}.{second}"
        return out
    out["table"] = _decode(second)
    out["table_anchor"] = f"db:{source_id}/{parts[0]}.{second}"
    if len(parts) >= 3:
        if parts[2].startswith(_SUB_KINDS) or parts[2] == "pk":
            out["sub"] = parts[2]
        else:
            out["column"] = _decode(parts[2])
    return out


def _decode(segment: str) -> str:
    return urllib.parse.unquote(segment)


def location(anchor: str) -> str:
    """锚点的人读位置：`<库>.<表>[.<列>]`。"""
    parsed = parse(anchor)
    if not parsed:
        return ""
    return ".".join(part for part in (parsed["db"], parsed["table"], parsed["column"]) if part)


def is_db_snapshot(connection: sqlite3.Connection, snapshot_id: str) -> bool:
    row = connection.execute("SELECT anchor FROM records WHERE snapshot_id = ? LIMIT 1", (snapshot_id,)).fetchone()
    return bool(row) and str(row["anchor"]).startswith("db:")


def _body(row: sqlite3.Row) -> dict:
    return json.loads(row["body"])


def _attrs(body: dict) -> dict:
    return body.get("attrs") if isinstance(body.get("attrs"), dict) else {}


def _is_table(body: dict) -> bool:
    return body.get("type") == "entity" and _attrs(body).get("kind", "table") == "table"


def _label(row: sqlite3.Row, body: dict) -> str | None:
    comment = _attrs(body).get("comment")
    return row["label"] or (str(comment) if comment else None)


def _row(connection: sqlite3.Connection, snapshot_id: str, anchor: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM records WHERE snapshot_id = ? AND anchor = ?",
                              (snapshot_id, anchor)).fetchone()


# ---------------------------------------------------------------------------
# EV-23 分层摘要：按库分组，表名列出
# ---------------------------------------------------------------------------


def summary_domains(connection: sqlite3.Connection, snapshot_id: str, mapped) -> list[dict]:
    """`mapped(anchor)` 给出引用了这张表的本体条目（或 None）。表和视图都列，按库分组。"""
    groups: dict[str, list[dict]] = {}
    for row in connection.execute("SELECT * FROM records WHERE snapshot_id = ? AND type = 'entity' ORDER BY name",
                                  (snapshot_id,)):
        parsed = parse(row["anchor"])
        if not parsed or not parsed["table"]:
            continue
        body = _body(row)
        groups.setdefault(parsed["db"], []).append(
            {"anchor": row["anchor"], "type": "entity", "name": row["name"], "label": _label(row, body),
             "kind": str(_attrs(body).get("kind") or "table"), "mapped_to": mapped(row["anchor"])})
    return [{"name": name, "count": len(entities), "entities": entities} for name, entities in sorted(groups.items())]


# ---------------------------------------------------------------------------
# EV-25 记录详情：列、DDL、样本、画像、行数
# ---------------------------------------------------------------------------


def _column_view(row: sqlite3.Row) -> dict:
    body = _body(row)
    attrs = _attrs(body)
    return {"name": row["name"], "type": str(body.get("data_type") or attrs.get("data_type") or ""),
            "note": str(attrs.get("comment") or row["label"] or ""),
            "confidence": row["confidence"] or "declared", "masked": bool(attrs.get("masked")),
            "anchor": row["anchor"]}


def columns(connection: sqlite3.Connection, snapshot_id: str, table_anchor: str) -> list[sqlite3.Row]:
    """表的列记录，按提供方给出的先后（即列序）。"""
    return list(connection.execute("SELECT * FROM records WHERE snapshot_id = ? AND parent = ? AND type = 'field' "
                                   "ORDER BY rid LIMIT ?", (snapshot_id, table_anchor, COLUMNS_MAX)))


def _cell(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _sample(connection: sqlite3.Connection, source_id: str, snapshot_id: str, table_anchor: str) -> dict | None:
    """优先用这个源最新的补充样本（sample 任务入库的快照），没有就用当前快照里的 `#sample` 记录。"""
    anchor = f"{table_anchor}#sample"
    chosen = None
    for row in connection.execute(
            "SELECT r.body, r.snapshot_id, s.manifest FROM records r JOIN snapshots s ON s.id = r.snapshot_id "
            "WHERE r.anchor = ? AND s.source_id = ? ORDER BY s.id DESC", (anchor, source_id)):
        if json.loads(row["manifest"]).get("operation") == "sample":
            chosen = row
            break
        if row["snapshot_id"] == snapshot_id and chosen is None:
            chosen = row
    if chosen is None:
        return None
    attrs = _attrs(json.loads(chosen["body"]))
    rows = [[_cell(value) for value in row] for row in attrs.get("rows") or [] if isinstance(row, list)]
    rows = rows[:SAMPLE_ROWS_MAX]
    return {"columns": [str(name) for name in attrs.get("columns") or []], "rows": rows, "shown": len(rows),
            "max": SAMPLE_ROWS_MAX, "masked_columns": [str(name) for name in attrs.get("masked_columns") or []],
            "snapshot_id": chosen["snapshot_id"]}


def _permille(value) -> float | None:
    return round(value / 10, 1) if isinstance(value, int) and not isinstance(value, bool) else None


def _top_k(items, rows: int) -> list[dict]:
    """画像的 top-k：提供方给 `{value, permille}`、`{value, pct}`（千分比）或 `{value, count}`，都换成百分比。"""
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            item = {"value": item[0], "count": item[1]}
        if not isinstance(item, dict):
            continue
        pct = _permille(item.get("permille", item.get("pct")))
        if pct is None and isinstance(item.get("count"), int) and rows:
            pct = round(100 * item["count"] / rows, 1)
        out.append({"value": _cell(item.get("value")), "pct": pct if pct is not None else 0.0})
    return out


def _profile(connection: sqlite3.Connection, snapshot_id: str, table_anchor: str) -> tuple[list[dict] | None, int]:
    row = _row(connection, snapshot_id, f"{table_anchor}#profile")
    if row is None:
        return None, 0
    attrs = _attrs(_body(row))
    rows = attrs.get("rows") if isinstance(attrs.get("rows"), int) else 0
    out = []
    for item in attrs.get("columns") or []:
        if not isinstance(item, dict) or not item.get("column"):
            continue
        null_pct = _permille(item.get("null_pct"))
        entry = {"column": str(item["column"]), "null_pct": null_pct,
                 "distinct": item.get("distinct") if isinstance(item.get("distinct"), int) else None}
        if item.get("top_k"):
            entry["top_k"] = _top_k(item["top_k"], rows)
        stats = item.get("stats") if isinstance(item.get("stats"), dict) else None
        if stats:
            entry["stats"] = {"min": stats.get("min"), "max": stats.get("max"), "null_pct": null_pct}
        out.append(entry)
    return out, rows


def enrich_record(connection: sqlite3.Connection, view: dict, snapshot_id: str, source_id: str) -> dict:
    """在通用的 EvidenceRecord 上补数据库的部分：表（视图）给列、DDL、样本、画像、行数；列标出是否遮蔽。"""
    parsed = parse(view["anchor"])
    if not parsed:
        return view
    attrs = view.get("attrs") or {}
    if view["type"] == "field":
        return {**view, "masked": bool(attrs.get("masked"))}
    if view["type"] != "entity" or not parsed["table_anchor"] or parsed["column"] or parsed["suffix"]:
        return view
    table = view["anchor"]
    out = {**view, "fields": [_column_view(row) for row in columns(connection, snapshot_id, table)]}
    ddl = attrs.get("ddl") or attrs.get("definition")
    if ddl:
        out["ddl"] = str(ddl)
    sample = _sample(connection, source_id, snapshot_id, table)
    if sample is not None:
        out["samples"] = sample
    profile, rows = _profile(connection, snapshot_id, table)
    if profile is not None:
        out["profile"] = profile
    estimate = attrs.get("rows_estimate")
    if rows or isinstance(estimate, int):
        out["rows"] = rows or estimate
    return out


# ---------------------------------------------------------------------------
# EV-27 相邻记录：外键邻居、推断关系
# ---------------------------------------------------------------------------


def _relation_basis(row: sqlite3.Row, body: dict) -> str:
    if ".fk:" in row["anchor"] or row["confidence"] == "declared":
        return FK_BASIS
    return str(_attrs(body).get("basis") or "推断关系")


def _table_relations(connection: sqlite3.Connection, snapshot_id: str, table: str) -> list[sqlite3.Row]:
    return list(connection.execute(
        "SELECT * FROM records WHERE snapshot_id = ? AND type = 'relation' AND (from_anchor = ? OR to_anchor = ?) "
        "ORDER BY anchor", (snapshot_id, table, table)))


def lineage(connection: sqlite3.Connection, snapshot_id: str, anchor: str, limit: int) -> list[dict]:
    """表：外键与推断关系连着的表；列：它所在的表，加上它参与的关系连着的表。"""
    parsed = parse(anchor)
    table = parsed["table_anchor"] if parsed else None
    if not table:
        return []
    items: list[dict] = []

    def label_of(other: str) -> str:
        row = _row(connection, snapshot_id, other)
        return (_label(row, _body(row)) or row["name"]) if row else location(other)

    if parsed["column"]:
        items.append({"anchor": table, "label": label_of(table), "basis": "上级", "confidence": "declared"})
    for row in _table_relations(connection, snapshot_id, table):
        body = _body(row)
        pairs = [pair for pair in _attrs(body).get("columns") or [] if isinstance(pair, list) and len(pair) == 2]
        outgoing = row["from_anchor"] == table
        if parsed["column"]:
            side = 0 if outgoing else 1
            if pairs and parsed["column"] not in {str(pair[side]) for pair in pairs}:
                continue
        other = row["to_anchor"] if outgoing else row["from_anchor"]
        if not other or other == table:
            continue
        basis = _relation_basis(row, body)
        items.append({"anchor": other, "label": label_of(other), "basis": basis if outgoing else f"{basis}（被引用）",
                      "confidence": row["confidence"] or "declared", "relation": row["anchor"],
                      "columns": [[str(pair[0]), str(pair[1])] for pair in pairs]})
        if len(items) >= limit:
            break
    return items[:limit]


# ---------------------------------------------------------------------------
# EV-28 ER 视图
# ---------------------------------------------------------------------------


def er(connection: sqlite3.Connection, snapshot_id: str, *, focus: str = "", depth: int = 1,
       domain: str = "") -> dict | None:
    """表（带列的主键、外键、遮蔽标记）和表之间的关系。有 focus 时从它所在的表按关系走 depth 步；表数上限 60。
    focus 在快照里找不到时返回 None。"""
    tables: dict[str, tuple[sqlite3.Row, dict]] = {}
    for row in connection.execute("SELECT * FROM records WHERE snapshot_id = ? AND type = 'entity' ORDER BY name",
                                  (snapshot_id,)):
        body = _body(row)
        parsed = parse(row["anchor"])
        if parsed and parsed["table_anchor"] == row["anchor"] and _is_table(body) \
                and (not domain or parsed["db"] == domain):
            tables[row["anchor"]] = (row, body)
    relations = [row for row in connection.execute(
        "SELECT * FROM records WHERE snapshot_id = ? AND type = 'relation' ORDER BY anchor", (snapshot_id,))
        if row["from_anchor"] in tables and row["to_anchor"] in tables]

    if focus:
        parsed = parse(focus)
        start = parsed["table_anchor"] if parsed else None
        if start not in tables:
            return None
        neighbours: dict[str, set[str]] = {}
        for row in relations:
            neighbours.setdefault(row["from_anchor"], set()).add(row["to_anchor"])
            neighbours.setdefault(row["to_anchor"], set()).add(row["from_anchor"])
        chosen, seen, queue = [], {start}, deque([(start, 0)])
        while queue:
            current, distance = queue.popleft()
            chosen.append(current)
            if distance >= depth:
                continue
            for other in sorted(neighbours.get(current, ())):
                if other not in seen:
                    seen.add(other)
                    queue.append((other, distance + 1))
    else:
        chosen = sorted(tables, key=lambda anchor: tables[anchor][0]["name"])
    total = len(chosen)
    chosen = chosen[:ER_TABLES_MAX]
    included = set(chosen)

    fk_columns: dict[str, set[str]] = {}
    for row in relations:
        if ".fk:" in row["anchor"] or row["confidence"] == "declared":
            for pair in _attrs(_body(row)).get("columns") or []:
                if isinstance(pair, list) and len(pair) == 2:
                    fk_columns.setdefault(row["from_anchor"], set()).add(str(pair[0]))
    out_tables = []
    for anchor in chosen:
        row, body = tables[anchor]
        pk_row = _row(connection, snapshot_id, f"{anchor}.pk")
        pk = {str(name) for name in _attrs(_body(pk_row)).get("columns") or []} if pk_row else set()
        fields = []
        for column in columns(connection, snapshot_id, anchor):
            attrs = _attrs(_body(column))
            fields.append({"name": column["name"], "type": str(_body(column).get("data_type") or
                                                                attrs.get("data_type") or ""),
                           "pk": bool(attrs.get("pk")) or column["name"] in pk,
                           "fk": column["name"] in fk_columns.get(anchor, set()), "masked": bool(attrs.get("masked"))})
        out_tables.append({"anchor": anchor, "name": row["name"], "label": _label(row, body), "fields": fields})
    out_relations = [{"from_anchor": row["from_anchor"], "to_anchor": row["to_anchor"],
                      "confidence": row["confidence"] or "declared", "basis": _relation_basis(row, _body(row)),
                      "anchor": row["anchor"]}
                     for row in relations if row["from_anchor"] in included and row["to_anchor"] in included]
    return {"tables": out_tables, "relations": out_relations, "total": total, "truncated": total > len(chosen)}


# ---------------------------------------------------------------------------
# 工作区里的快照（「数据来源」写锚点）
# ---------------------------------------------------------------------------


def tree_tables(root: Path, source_id: str) -> dict[str, dict] | None:
    """这个源最新的完整快照里的表：{表锚点: {name, db, columns}}；工作区（树）里没有这个源的完整快照为 None。
    直接读 `resource/systems/<源>/<快照>/records/`，不经索引：发布用的只读树也能用。"""
    from .query import latest_snapshot

    snapshot_id = latest_snapshot(root, source_id, complete_only=True)
    if not snapshot_id:
        return None
    directory = Path(root) / "resource" / "systems" / source_id / snapshot_id / "records"
    tables: dict[str, dict] = {}
    for record in _jsonl(directory / "entity.jsonl"):
        parsed = parse(str(record.get("id") or ""))
        if parsed and parsed["table_anchor"] == record["id"] and _is_table(record):
            tables[record["id"]] = {"name": str(record.get("name") or parsed["table"]), "db": parsed["db"],
                                    "columns": []}
    for record in _jsonl(directory / "field.jsonl"):
        parent = record.get("parent")
        if parent in tables and isinstance(record.get("name"), str):
            tables[parent]["columns"].append(record["name"])
    return tables


def _jsonl(path: Path):
    if not path.is_file() or path.is_symlink():
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record
