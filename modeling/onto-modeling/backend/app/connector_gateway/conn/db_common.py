"""db-mysql、db-postgres 共用的采集流程（ADR-0086；证据采集接口 §8、§9）。

方言（`Dialect` 子类，在 db_mysql / db_postgres 里）只管连接、元数据查询、取值表达式和 DDL 写法；这里管只读三重保证的
串联、范围、记录组装、画像、样本、脱敏、推断关系和预算。每条语句都经 `GuardedCursor`：限速、过白名单、计时、发访问审计。
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import masking, sqlguard
from .context import BudgetExceeded, ConnError, Context, scope_match

PROFILE_ROWS_ENV = "ONTO_CONNECTOR_DB_PROFILE_ROWS"
PROFILE_ROWS_DEFAULT = 10_000
SAMPLE_MAX = 20
DEFINITION_MAX = 4000
TEXT_PREFIX = 200           # 画像、样本只取文本列的前 200 字
TOP_K = 10
PROFILE_CELLS = 200_000     # 宽表按列数压低画像行数，一次最多读这么多格（不低于 200 行）
CONNECT_TIMEOUT_S = 10

# 写语句探针：测试连接时确认它们在发出前就被白名单拦下（不发给库）
WRITE_PROBES = ("DELETE FROM probe", "UPDATE probe SET a = 1", "SELECT 1; DROP TABLE probe",
                "SELECT * FROM probe FOR UPDATE")


# ---- 元数据 ----

@dataclass
class Table:
    schema: str
    name: str
    kind: str = "table"                 # table / view
    comment: str = ""
    rows_estimate: int | None = None
    bytes_estimate: int | None = None
    readable: bool = True
    writable: bool = False
    key: Any = None                     # 方言自用（PostgreSQL 的 oid）

    @property
    def path(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class Column:
    schema: str
    table: str
    name: str
    position: int
    data_type: str
    nullable: bool
    default: str | None = None
    comment: str = ""
    type_class: str = "text"            # numeric / text / temporal / bool / binary
    extra: str = ""


@dataclass
class Constraint:
    schema: str
    table: str
    name: str
    kind: str                           # pk / uk / fk / check
    columns: list[str] = field(default_factory=list)
    expression: str | None = None
    definition: str | None = None       # 方言给的完整定义（PostgreSQL 的 pg_get_constraintdef）
    ref_schema: str | None = None
    ref_table: str | None = None
    ref_columns: list[str] = field(default_factory=list)
    on_update: str | None = None
    on_delete: str | None = None


@dataclass
class Trigger:
    schema: str
    table: str
    name: str
    timing: str
    events: list[str]
    definition: str
    function: tuple[str, str] | None = None


@dataclass
class Routine:
    schema: str
    name: str
    kind: str                           # procedure / function
    definition: str | None
    comment: str = ""
    arguments: str = ""
    language: str = ""


@dataclass
class InferredRef:
    schema: str
    table: str
    column: str
    target_table: str
    target_column: str


class Dialect:
    """方言接口：db_mysql、db_postgres 各实现一份。"""

    name = ""                           # sqlguard 方言：mysql / postgres
    label = ""
    provider_id = ""
    version = "0.1.0"
    methods: dict[str, str] = {}
    session_statements_recover: tuple[str, ...] = ()

    def connect(self, ctx: Context):
        raise NotImplementedError

    def session_statements(self, ctx: Context) -> list[str]:
        raise NotImplementedError

    def default_scope(self, ctx: Context) -> list[str]:
        return []

    def quote(self, ident: str) -> str:
        raise NotImplementedError

    def version_sql(self) -> str:
        raise NotImplementedError

    def read_only_probe_sql(self) -> str:
        raise NotImplementedError

    def list_tables(self, cur: "GuardedCursor") -> list[Table]:
        raise NotImplementedError

    def readonly_check(self, cur: "GuardedCursor", tables: list[Table]) -> tuple[bool, str]:
        raise NotImplementedError

    def columns(self, cur: "GuardedCursor", schemas: list[str]) -> list[Column]:
        raise NotImplementedError

    def constraints(self, cur: "GuardedCursor", schemas: list[str]) -> list[Constraint]:
        raise NotImplementedError

    def views(self, cur: "GuardedCursor", views: list[Table]) -> dict[tuple[str, str], str]:
        raise NotImplementedError

    def triggers(self, cur: "GuardedCursor", schemas: list[str]) -> list[Trigger]:
        raise NotImplementedError

    def routines(self, cur: "GuardedCursor", schemas: list[str]) -> list[Routine]:
        raise NotImplementedError

    def select_expr(self, column: Column) -> str:
        raise NotImplementedError

    def limit_sql(self) -> str:
        return "LIMIT %s"

    def ddl(self, table: Table, columns: list[Column], constraints: list[Constraint]) -> str:
        raise NotImplementedError

    def error(self, exc: BaseException, ctx: Context) -> ConnError:
        """驱动异常转 ConnError；消息经 `scrub`，不带主机、密码、连接串。"""
        return ConnError("db_error", f"{self.label} 出错：{scrub(exc, ctx)}")


# ---- 错误信息脱敏 ----

_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_IPV6 = re.compile(r"\[?\b[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{0,4}){2,}\]?")
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")


def scrub(text: Any, ctx: Context) -> str:
    """去掉密码、主机、IP、连接串；截到 240 字。"""
    message = str(text)
    for secret in (ctx.secret.get("password"), ctx.connection.get("host")):
        if secret:
            message = message.replace(str(secret), "***")
    message = _URL.sub("***", message)
    message = _IPV4.sub("***", message)
    message = _IPV6.sub("***", message)
    return message[:240]


# ---- 受控游标 ----


def _payload_size(rows: list) -> int:
    return sum(len(str(value)) for row in rows for value in row if value is not None)


class GuardedCursor:
    """所有发给业务库的语句都从这里走：限速 → 白名单 → 执行 → 访问审计（条数与语句数一致，失败的也记）。"""

    def __init__(self, ctx: Context, conn, dialect: Dialect):
        self.ctx = ctx
        self.conn = conn
        self.dialect = dialect
        self.statements = 0
        self.elapsed_ms = 0

    def query(self, sql: str, params: Iterable | None = None) -> list[tuple]:
        sqlguard.check(sql, self.dialect.name)
        return self._run(sql, params)

    def session(self, sql: str) -> None:
        sqlguard.check_session(sql, self.dialect.name)
        self._run(sql, None)

    def recover(self) -> None:
        """某条查询失败后重开只读事务（PostgreSQL 出错后整个事务作废）。"""
        for sql in self.dialect.session_statements_recover:
            self.session(sql)

    def _run(self, sql: str, params) -> list[tuple]:
        self.ctx.throttle()
        started = time.monotonic()
        rows: list = []
        cursor = self.conn.cursor()
        try:
            if params is None:
                cursor.execute(sql)
            else:
                cursor.execute(sql, list(params))
            rows = [tuple(row) for row in cursor.fetchall()] if cursor.description else []
        except ConnError:
            raise
        except Exception as exc:
            raise self.dialect.error(exc, self.ctx) from None
        finally:
            duration = int((time.monotonic() - started) * 1000)
            self.statements += 1
            self.elapsed_ms += duration
            self.ctx.access(sql, rows=len(rows), size=_payload_size(rows), duration_ms=duration)
            try:
                cursor.close()
            except Exception:  # noqa: BLE001 - 关游标失败不影响结果
                pass
        return rows

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


def open_cursor(ctx: Context, dialect: Dialect) -> GuardedCursor:
    """连接并进入只读会话（只读三重保证之二）。"""
    try:
        conn = dialect.connect(ctx)
    except ConnError:
        raise
    except Exception as exc:
        raise dialect.error(exc, ctx) from None
    cur = GuardedCursor(ctx, conn, dialect)
    try:
        for sql in dialect.session_statements(ctx):
            cur.session(sql)
    except BaseException:
        cur.close()
        raise
    return cur


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---- 范围与只读自检 ----


def scope_patterns(ctx: Context, dialect: Dialect) -> list[str]:
    return list(ctx.scope) if ctx.scope else dialect.default_scope(ctx)


def scoped_tables(ctx: Context, cur: GuardedCursor) -> list[Table]:
    patterns = scope_patterns(ctx, cur.dialect)
    return [table for table in cur.dialect.list_tables(cur) if scope_match(table.path, patterns)]


def require_readonly(ctx: Context, cur: GuardedCursor) -> list[Table]:
    """只读三重保证之一：账号自检，不只读就拒绝采集。返回范围内的表和视图。"""
    tables = scoped_tables(ctx, cur)
    ok, detail = cur.dialect.readonly_check(cur, tables)
    if not ok:
        raise ConnError("account_not_readonly", detail)
    return tables


def _masker(ctx: Context) -> masking.Masker:
    policy = str(ctx.masking.get("policy") or masking.POLICY)
    if policy != masking.POLICY:
        raise ConnError("unsupported_masking_policy", f"不认识的脱敏策略：{policy}")
    masker = masking.Masker(str(ctx.masking.get("salt") or ""))
    if masker.ephemeral_salt:
        ctx.limitation("没有项目盐，本次用随机盐：样本里的哈希只在这份快照内能对上")
    return masker


def profile_rows() -> int:
    try:
        return max(1, int(os.environ.get(PROFILE_ROWS_ENV) or PROFILE_ROWS_DEFAULT))
    except ValueError:
        return PROFILE_ROWS_DEFAULT


# ---- 推断关系 ----


def _target_names(base: str) -> list[str]:
    names = [base, base + "s", base + "es"]
    if base.endswith("y"):
        names.append(base[:-1] + "ies")
    return names


def infer_refs(tables: list[Table], columns: dict[tuple[str, str], list[Column]],
               constraints: dict[tuple[str, str], list[Constraint]]) -> list[InferredRef]:
    """`<x>_id` 列且没有外键 → 同库里叫 `<x>` / `<x>s` / `<x>es` / `<x>ies` 的表，对到它的单列主键（没有就 `id` 列）。"""
    by_name = {(table.schema, table.name.lower()): table for table in tables if table.kind == "table"}
    refs: list[InferredRef] = []
    for table in tables:
        if table.kind != "table":
            continue
        own = constraints.get((table.schema, table.name), [])
        fk_columns = {column for item in own if item.kind == "fk" for column in item.columns}
        for column in columns.get((table.schema, table.name), []):
            lowered = column.name.lower()
            if not lowered.endswith("_id") or len(lowered) <= 3 or column.name in fk_columns:
                continue
            for candidate in _target_names(lowered[:-3]):
                target = by_name.get((table.schema, candidate))
                if target is None:
                    continue
                target_column = _key_column(target, columns, constraints)
                if target_column and not (target is table and target_column == column.name):
                    refs.append(InferredRef(table.schema, table.name, column.name, target.name, target_column))
                break
    return refs


def _key_column(table: Table, columns, constraints) -> str | None:
    for item in constraints.get((table.schema, table.name), []):
        if item.kind == "pk" and len(item.columns) == 1:
            return item.columns[0]
    names = {column.name.lower(): column.name for column in columns.get((table.schema, table.name), [])}
    return names.get("id")


# ---- 记录组装 ----


class Builder:
    """按 §9 拼锚点和记录。名字的各段用 `ctx.encode` 编码；同一快照里撞名的从第二个起加 `~2`。"""

    def __init__(self, ctx: Context, dialect: Dialect):
        self.ctx = ctx
        self.dialect = dialect
        self.provider = f"{dialect.provider_id}@{dialect.version}"
        self._used: dict[str, int] = {}

    def path(self, schema: str, *rest: str) -> str:
        return ".".join([self.ctx.encode(schema), *rest])

    def table(self, schema: str, name: str) -> str:
        return self.ctx.anchor(self.path(schema, self.ctx.encode(name)))

    def unique(self, anchor: str) -> str:
        count = self._used.get(anchor, 0) + 1
        self._used[anchor] = count
        return anchor if count == 1 else f"{anchor}~{count}"

    def evidence(self, method_key: str, confidence: str = "declared") -> dict:
        return {"provider": self.provider, "method": self.dialect.methods.get(method_key, method_key),
                "confidence": confidence}

    def emit(self, record: dict) -> None:
        self.ctx.record(record)


def _label(comment: str) -> str | None:
    first = (comment or "").strip().splitlines()[0].strip() if (comment or "").strip() else ""
    return first[:60] or None


def _clip(value: str | None, limit: int = DEFINITION_MAX) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[:limit]


def ddl_order(constraints: list[Constraint]) -> list[Constraint]:
    """生成 DDL 时约束按主键、唯一、外键、检查排。"""
    rank = {"pk": 0, "uk": 1, "fk": 2, "check": 3}
    return sorted(constraints, key=lambda item: rank.get(item.kind, 9))


class Classified:
    """一张表各列的敏感类别，以及按列取出的画像值。"""

    def __init__(self, columns: list[Column], rows: list[tuple], join_keys: set[str], masker: masking.Masker,
                 table: str):
        self.columns = columns
        self.rows = rows
        self.masker = masker
        self.values: dict[str, list] = {}
        self.categories: dict[str, str] = {}
        for index, column in enumerate(columns):
            values = [_decode(column, row[index]) for row in rows]
            self.values[column.name] = values
            self.categories[column.name] = masking.classify(
                column.name, table=table, comment=column.comment, type_class=column.type_class,
                values=[value for value in values if value is not None], join_key=column.name in join_keys)

    def sensitivity(self, column: str) -> str:
        return self.categories.get(column, "text")

    def masked(self, column: str) -> bool:
        return masking.is_sensitive(self.sensitivity(column))


def _decode(column: Column, value: Any) -> Any:
    """二进制列只取了「是不是 NULL」，这里还原成占位符；其余转成记录能放的值。"""
    if column.type_class == "binary":
        return None if value in (1, True) else "<binary>"
    return masking.plain(value)


def profile_attrs(classified: Classified, limit: int) -> dict:
    total = len(classified.rows)
    items = []
    for column in classified.columns:
        values = classified.values[column.name]
        present = [value for value in values if value is not None]
        counts = Counter(present)
        entry: dict = {"column": column.name, "null_pct": (total - len(present)) * 1000 // total if total else 0,
                       "distinct": len(counts)}
        if not classified.masked(column.name) and present:
            if column.type_class == "numeric":
                numbers = [value for value in values if value is not None]
                try:
                    ordered = sorted(numbers, key=_numeric_key)
                    entry["stats"] = {"min": ordered[0], "max": ordered[-1]}
                except (ValueError, ArithmeticError):
                    pass
            if len(counts) <= masking.CODE_MAX_DISTINCT:
                ranked = sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))[:TOP_K]
                entry["top_k"] = [{"value": value, "count": count} for value, count in ranked]
        items.append(entry)
    return {"rows": total, "row_limit": limit, "pct_scale": 1000, "policy": masking.POLICY, "columns": items}


def _numeric_key(value: Any):
    import decimal
    return decimal.Decimal(str(value))


def sample_attrs(classified: Classified, limit: int) -> dict:
    kept = [column for column in classified.columns
            if masking.TREATMENT.get(classified.sensitivity(column.name)) != "drop"]
    dropped = [column.name for column in classified.columns if column not in kept]
    rows = []
    for index in range(min(limit, len(classified.rows))):
        rows.append([classified.masker.apply(classified.sensitivity(column.name), classified.values[column.name][index])
                     for column in kept])
    return {
        "columns": [column.name for column in kept],
        "rows": rows,
        "masked_columns": [column.name for column in kept if classified.masked(column.name)],
        "dropped_columns": dropped,
        "policy": masking.POLICY,
    }


# ---- 一次采集 ----


class Collection:
    def __init__(self, ctx: Context, cur: GuardedCursor, tables: list[Table]):
        self.ctx = ctx
        self.cur = cur
        self.dialect = cur.dialect
        self.tables = tables
        self.schemas = sorted({table.schema for table in tables})
        self.builder = Builder(ctx, self.dialect)
        self.columns: dict[tuple[str, str], list[Column]] = defaultdict(list)
        self.constraints: dict[tuple[str, str], list[Constraint]] = defaultdict(list)
        self.refs: list[InferredRef] = []
        self.join_keys: dict[tuple[str, str], set[str]] = defaultdict(set)

    def load_structure(self) -> None:
        if not self.schemas:
            return
        wanted = {(table.schema, table.name) for table in self.tables}
        for column in self.dialect.columns(self.cur, self.schemas):
            if (column.schema, column.table) in wanted:
                self.columns[(column.schema, column.table)].append(column)
        for item in self.dialect.constraints(self.cur, self.schemas):
            if (item.schema, item.table) in wanted:
                self.constraints[(item.schema, item.table)].append(item)
        for items in self.constraints.values():
            for item in items:
                if item.kind == "fk":
                    self.join_keys[(item.schema, item.table)].update(item.columns)
                    if item.ref_schema and item.ref_table:
                        self.join_keys[(item.ref_schema, item.ref_table)].update(item.ref_columns)
        self.refs = infer_refs(self.tables, self.columns, self.constraints)
        for ref in self.refs:
            self.join_keys[(ref.schema, ref.table)].add(ref.column)
            self.join_keys[(ref.schema, ref.target_table)].add(ref.target_column)

    def row_limit(self, table: Table, limit: int) -> int:
        width = len(self.columns.get((table.schema, table.name), [])) or 1
        return min(limit, max(200, PROFILE_CELLS // width))

    def read_rows(self, table: Table, limit: int) -> tuple[list[Column], list[tuple]]:
        columns = self.columns.get((table.schema, table.name), [])
        if not columns:
            return columns, []
        quote = self.dialect.quote
        exprs = ", ".join(self.dialect.select_expr(column) for column in columns)
        order = ""
        pk = next((item for item in self.constraints.get((table.schema, table.name), []) if item.kind == "pk"), None)
        if pk and pk.columns:
            order = " ORDER BY " + ", ".join(quote(name) for name in pk.columns)
        sql = f"SELECT {exprs} FROM {quote(table.schema)}.{quote(table.name)}{order} {self.dialect.limit_sql()}"
        return columns, self.cur.query(sql, [limit])

    # -- 记录 --

    def emit_table(self, table: Table, classified: Classified | None) -> None:
        b = self.builder
        anchor = b.table(table.schema, table.name)
        columns = self.columns.get((table.schema, table.name), [])
        constraints = self.constraints.get((table.schema, table.name), [])
        b.emit({
            "id": b.unique(anchor), "type": "entity", "parent": None, "name": table.name, "label": _label(table.comment),
            "attrs": {"kind": "table", "module": table.schema, "comment": table.comment or None,
                      "rows_estimate": table.rows_estimate,
                      "ddl": self.dialect.ddl(table, columns, constraints)},
            "evidence": b.evidence("table"),
        })
        pk_columns = {name for item in constraints if item.kind == "pk" for name in item.columns}
        for column in columns:
            sensitivity = classified.sensitivity(column.name) if classified else masking.classify(
                column.name, table=table.name, comment=column.comment, type_class=column.type_class,
                join_key=column.name in self.join_keys.get((table.schema, table.name), set()))
            b.emit({
                "id": b.unique(b.ctx.anchor(b.path(table.schema, b.ctx.encode(table.name), b.ctx.encode(column.name)))),
                "type": "field", "parent": anchor, "name": column.name, "label": _label(column.comment),
                "data_type": column.data_type, "nullable": bool(column.nullable),
                "attrs": {"comment": column.comment or None, "default": column.default, "pk": column.name in pk_columns,
                          "position": column.position, "masked": masking.is_sensitive(sensitivity),
                          "sensitivity": sensitivity},
                "evidence": b.evidence("column"),
            })
        for item in constraints:
            self.emit_constraint(table, anchor, item)

    def emit_constraint(self, table: Table, anchor: str, item: Constraint) -> None:
        b = self.builder
        base = b.path(table.schema, b.ctx.encode(table.name))
        if item.kind == "fk":
            target = b.table(item.ref_schema or table.schema, item.ref_table or "")
            b.emit({
                "id": b.unique(b.ctx.anchor(f"{base}.fk:{b.ctx.encode(item.name)}")), "type": "relation",
                "parent": anchor, "name": "references", "label": None, "from": anchor, "to": target,
                "attrs": {"constraint": item.name, "columns": [list(pair) for pair in zip(item.columns, item.ref_columns)],
                          "on_update": item.on_update, "on_delete": item.on_delete},
                "evidence": b.evidence("constraint"),
            })
            return
        suffix = {"pk": "pk", "uk": f"uk:{b.ctx.encode(item.name)}", "check": f"check:{b.ctx.encode(item.name)}"}[item.kind]
        kind = {"pk": "primary_key", "uk": "unique", "check": "check"}[item.kind]
        attrs: dict = {"kind": kind, "columns": list(item.columns)}
        if item.kind == "check":
            attrs["expression"] = _clip(item.expression)
        b.emit({"id": b.unique(b.ctx.anchor(f"{base}.{suffix}")), "type": "constraint", "parent": anchor,
                "name": item.name, "label": None, "attrs": attrs,
                "evidence": b.evidence("check" if item.kind == "check" else "constraint")})

    def emit_trigger(self, trigger: Trigger) -> None:
        b = self.builder
        anchor = b.table(trigger.schema, trigger.table)
        attrs = {"kind": "trigger", "timing": trigger.timing, "event": " OR ".join(trigger.events),
                 "definition": _clip(trigger.definition)}
        if trigger.function:
            attrs["function"] = b.ctx.anchor(b.path(trigger.function[0], f"proc:{b.ctx.encode(trigger.function[1])}"))
        b.emit({"id": b.unique(b.ctx.anchor(b.path(trigger.schema, b.ctx.encode(trigger.table),
                                                   f"trigger:{b.ctx.encode(trigger.name)}"))),
                "type": "rule", "parent": anchor, "name": trigger.name, "label": None, "attrs": attrs,
                "evidence": b.evidence("trigger")})

    def emit_view(self, view: Table, definition: str | None) -> None:
        b = self.builder
        columns = self.columns.get((view.schema, view.name), [])
        b.emit({"id": b.unique(b.ctx.anchor(b.path(view.schema, f"view:{b.ctx.encode(view.name)}"))),
                "type": "entity", "parent": None, "name": view.name, "label": _label(view.comment),
                "attrs": {"kind": "view", "module": view.schema, "comment": view.comment or None,
                          "definition": _clip(definition),
                          "columns": [{"name": column.name, "data_type": column.data_type} for column in columns]},
                "evidence": b.evidence("view")})

    def emit_routine(self, routine: Routine) -> None:
        b = self.builder
        b.emit({"id": b.unique(b.ctx.anchor(b.path(routine.schema, f"proc:{b.ctx.encode(routine.name)}"))),
                "type": "operation", "parent": None, "name": routine.name, "label": _label(routine.comment),
                "attrs": {"kind": routine.kind, "module": routine.schema,
                          "signature": f"{routine.name}({routine.arguments})", "language": routine.language or None,
                          "comment": routine.comment or None, "definition": _clip(routine.definition)},
                "evidence": b.evidence("routine")})

    def emit_profile(self, table: Table, classified: Classified, limit: int) -> None:
        b = self.builder
        anchor = b.table(table.schema, table.name)
        b.emit({"id": b.unique(f"{anchor}#profile"), "type": "profile", "parent": anchor, "name": table.name,
                "label": None, "attrs": profile_attrs(classified, limit), "evidence": b.evidence("profile", "observed")})

    def emit_sample(self, table: Table, classified: Classified, limit: int) -> None:
        b = self.builder
        anchor = b.table(table.schema, table.name)
        b.emit({"id": b.unique(f"{anchor}#sample"), "type": "sample", "parent": anchor, "name": table.name,
                "label": None, "attrs": sample_attrs(classified, limit), "evidence": b.evidence("sample", "observed")})

    def emit_ref(self, ref: InferredRef, overlap: int | None) -> None:
        b = self.builder
        anchor = b.table(ref.schema, ref.table)
        attrs: dict = {"columns": [[ref.column, ref.target_column]],
                       "basis": "naming" if overlap is None else "naming+values"}
        if overlap is not None:
            attrs["overlap_permille"] = overlap
        b.emit({"id": b.unique(b.ctx.anchor(b.path(ref.schema, b.ctx.encode(ref.table), f"ref:{b.ctx.encode(ref.column)}"))),
                "type": "relation", "parent": anchor, "name": "references", "label": None, "from": anchor,
                "to": b.table(ref.schema, ref.target_table), "attrs": attrs,
                "evidence": b.evidence(attrs["basis"], "inferred")})


def _inventory_item(table: Table) -> dict:
    item: dict = {"object": table.path if table.kind == "table" else f"{table.schema}.view:{table.name}",
                  "kind": table.kind}
    if table.rows_estimate is not None:
        item["estimate_rows"] = table.rows_estimate
    if table.bytes_estimate is not None:
        item["estimate_bytes"] = table.bytes_estimate
    return item


def _object_name(table: Table) -> str:
    return _inventory_item(table)["object"]


# ---- 四个操作 ----


def test(ctx: Context, dialect: Dialect) -> None:
    """测试连接：连通（带延迟）、账号只读自检、语句白名单（写语句在发出前被拦下，且会话确实只读）。"""
    checks: list[dict] = []
    started = time.monotonic()
    try:
        cur = open_cursor(ctx, dialect)
    except ConnError as exc:
        checks.append({"name": "connectivity", "ok": False, "detail": exc.message, "latency_ms": None})
        checks.append({"name": "readonly_account", "ok": False, "detail": "没有连上，跳过"})
        checks.append({"name": "statement_allowlist", "ok": _probes_blocked(dialect), "detail": "没有连上，只做了本地检查"})
        ctx.result(complete=True, checks=checks)
        return
    try:
        try:
            version = text(cur.query(dialect.version_sql())[0][0])
            checks.append({"name": "connectivity", "ok": True, "detail": f"{dialect.label} {version}".strip(),
                           "latency_ms": int((time.monotonic() - started) * 1000)})
        except ConnError as exc:
            checks.append({"name": "connectivity", "ok": False, "detail": exc.message, "latency_ms": None})
        try:
            ok, detail = dialect.readonly_check(cur, scoped_tables(ctx, cur))
        except ConnError as exc:
            ok, detail = False, exc.message
        checks.append({"name": "readonly_account", "ok": ok, "detail": detail})
        blocked = _probes_blocked(dialect)
        try:
            value = text(cur.query(dialect.read_only_probe_sql())[0][0]).lower()
            session_ro = value in ("1", "on", "true")
        except ConnError:
            session_ro = False
        detail = "写语句在发出前被拦下" if blocked else "有写语句没被拦下"
        detail += "；会话只读" if session_ro else "；会话不是只读"
        checks.append({"name": "statement_allowlist", "ok": blocked and session_ro, "detail": detail})
    finally:
        cur.close()
    ctx.result(complete=True, checks=checks)


def _probes_blocked(dialect: Dialect) -> bool:
    for probe in WRITE_PROBES:
        try:
            sqlguard.check(probe, dialect.name)
        except ConnError:
            continue
        return False
    return True


def discover(ctx: Context, dialect: Dialect) -> None:
    """只出清单：范围内的表和视图、估算行数与字节数；没有查询权限的进 skipped。"""
    cur = open_cursor(ctx, dialect)
    try:
        tables = require_readonly(ctx, cur)
    finally:
        cur.close()
    items = [_inventory_item(table) for table in tables if table.readable]
    skipped = [{"object": _object_name(table), "reason": "no_select_privilege"} for table in tables if not table.readable]
    ctx.inventory(items, skipped)
    ctx.result(complete=True, coverage_pct=100.0)


def snapshot(ctx: Context, dialect: Dialect) -> None:
    masker = _masker(ctx)
    limit = profile_rows()
    cur = open_cursor(ctx, dialect)
    skipped: list[dict] = []
    items: list[dict] = []
    done = 0
    total = 0
    truncated = None
    try:
        tables = require_readonly(ctx, cur)
        readable = [table for table in tables if table.readable]
        skipped += [{"object": _object_name(table), "reason": "no_select_privilege"}
                    for table in tables if not table.readable]
        items += [_inventory_item(table) for table in readable]
        base_tables = [table for table in readable if table.kind == "table"]
        views = [table for table in readable if table.kind == "view"]
        total = len(base_tables)
        if not readable:
            ctx.limitation("范围内没有可读的表")
        collection = Collection(ctx, cur, readable)
        pending = list(base_tables)
        try:
            collection.load_structure()
            schemas = collection.schemas
            names = {(table.schema, table.name) for table in base_tables}
            triggers = [item for item in (dialect.triggers(cur, schemas) if schemas else [])
                        if (item.schema, item.table) in names]
            patterns = scope_patterns(ctx, dialect)
            routines = [item for item in (dialect.routines(cur, schemas) if schemas else [])
                        if scope_match(f"{item.schema}.{item.name}", patterns)]
            definitions = dialect.views(cur, views) if views else {}
            retained = _retained_columns(collection.refs)
            values: dict[tuple[str, str, str], set] = {}
            by_table: dict[tuple[str, str], list[Trigger]] = defaultdict(list)
            for item in triggers:
                by_table[(item.schema, item.table)].append(item)
            for index, table in enumerate(base_tables):
                ctx.progress("tables", index, total, "表")
                classified = None
                try:
                    table_limit = collection.row_limit(table, limit)
                    columns, rows = collection.read_rows(table, table_limit)
                    classified = Classified(columns, rows, collection.join_keys.get((table.schema, table.name), set()),
                                            masker, table.name)
                except ConnError as exc:
                    if isinstance(exc, BudgetExceeded) or exc.code == "timeout":
                        raise
                    cur.recover()
                    skipped.append({"object": table.path, "reason": exc.code})
                    ctx.limitation(f"{table.path} 的画像和样本没取到：{exc.message}")
                collection.emit_table(table, classified)
                for trigger in by_table.get((table.schema, table.name), []):
                    collection.emit_trigger(trigger)
                if classified is not None:
                    collection.emit_profile(table, classified, table_limit)
                    collection.emit_sample(table, classified, SAMPLE_MAX)
                    for column in retained.get((table.schema, table.name), ()):
                        values[(table.schema, table.name, column)] = {
                            str(value) for value in classified.values.get(column, []) if value is not None}
                pending.remove(table)
                done += 1
            ctx.progress("tables", done, total, "表")
            for view in views:
                collection.emit_view(view, definitions.get((view.schema, view.name)))
            for routine in routines:
                collection.emit_routine(routine)
            if any(routine.definition is None for routine in routines):
                ctx.limitation("有存储过程读不到定义（账号缺 SHOW_ROUTINE 之类的权限），只记了名字")
            for ref in collection.refs:
                collection.emit_ref(ref, _overlap(values, ref))
            items += [{"object": f"{routine.schema}.proc:{routine.name}", "kind": "proc"} for routine in routines]
        except BudgetExceeded as exc:
            truncated = exc.message
        except ConnError as exc:
            if exc.code != "timeout":
                raise
            truncated = exc.message
        if truncated:
            ctx.limitation(f"采集没做完（{truncated}），已采的记录保留")
            skipped += [{"object": table.path, "reason": "budget"} for table in pending]
    finally:
        cur.close()
    ctx.inventory(items, skipped)
    coverage = 100.0 if not total else done * 100.0 / total
    ctx.result(complete=truncated is None, coverage_pct=coverage)


def _retained_columns(refs: list[InferredRef]) -> dict[tuple[str, str], set[str]]:
    retained: dict[tuple[str, str], set[str]] = defaultdict(set)
    for ref in refs:
        retained[(ref.schema, ref.table)].add(ref.column)
        retained[(ref.schema, ref.target_table)].add(ref.target_column)
    return retained


def _overlap(values: dict, ref: InferredRef) -> int | None:
    source = values.get((ref.schema, ref.table, ref.column))
    target = values.get((ref.schema, ref.target_table, ref.target_column))
    if not source or target is None:
        return None
    return len(source & target) * 1000 // len(source)


def parse_selector(ctx: Context) -> tuple[str, str]:
    """`selector` 可以是表锚点（`db:<源>/<库>.<表>`）、路径（`<库>.<表>`），或 `{"object": …}` / `{"anchor": …}`。"""
    selector = ctx.selector
    if isinstance(selector, dict):
        selector = selector.get("anchor") or selector.get("object") or selector.get("table") or ""
    selector = str(selector or "")
    if selector.startswith(ctx.prefix):
        selector = selector[len(ctx.prefix):]
    selector = selector.split("#", 1)[0]
    parts = selector.split(".")
    if len(parts) != 2 or not all(parts) or ":" in selector:
        raise ConnError("invalid_selector", "selector 要指向一张表：<库>.<表>")
    decoded = [re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), part) for part in parts]
    return decoded[0], decoded[1]


def sample(ctx: Context, dialect: Dialect) -> None:
    """只采 `selector` 这一张表、`limit` 行（≤ 20），脱敏后出一条 `sample` 记录。"""
    schema, name = parse_selector(ctx)
    limit = max(1, min(SAMPLE_MAX, int(ctx.limit or SAMPLE_MAX)))
    masker = _masker(ctx)
    cur = open_cursor(ctx, dialect)
    try:
        tables = require_readonly(ctx, cur)
        table = next((item for item in tables if item.schema == schema and item.name == name and item.kind == "table"),
                     None)
        if table is None:
            raise ConnError("out_of_scope", "这张表不在本次范围里，或账号看不到")
        if not table.readable:
            raise ConnError("no_select_privilege", "账号没有这张表的查询权限")
        same_schema = [item for item in tables if item.schema == schema and item.readable]
        collection = Collection(ctx, cur, same_schema)
        collection.load_structure()
        # 分类按和快照相同的行数做（几行样本判断不出低基数代码列，会把状态、等级这类列也遮掉，和快照前后不一致），
        # 只展示 limit 行
        columns, rows = collection.read_rows(table, collection.row_limit(table, profile_rows()))
        classified = Classified(columns, rows, collection.join_keys.get((schema, name), set()), masker, name)
        collection.emit_sample(table, classified, limit)
    finally:
        cur.close()
    ctx.result(complete=True, coverage_pct=100.0)
