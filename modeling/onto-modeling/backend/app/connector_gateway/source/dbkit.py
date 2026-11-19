"""数据库类连接器的通用件（ADR-0103；design/data-sources.md 第 1 节）。插件 import 它，第三方数据库连接器也能用。

插件只写方言（`Dialect` 子类：连接、元数据查询、取值表达式、错误映射），这里管：
- 只读三重保证的串联：账号自检（`require_readonly`）、只读会话（`open_cursor`）、语句白名单（`GuardedCursor`，每条语句先过
  `ctx.readonly` / `ctx.session`，再限速、限时）；
- 范围：`tables` 字段逗号分隔，带点的写法（`shop.orders`、`shop.*`）按「库或 schema.表」匹配，不带点的（`orders`、`order*`）
  只在方言的默认库或 schema 里匹配；没填就是默认库或 schema 里的全部；
- 表名：范围内的表都在同一个库或 schema 时只用表名，跨库或 schema 时一律带前缀（发布时的表列绑定按工作表名对）；
- 按命名推断关联、发表和事实、截止时间到了保留已采的部分。

遮蔽、截断、渲染都在网关主进程里做，这里送原始取值（文本列只取前 200 字，二进制列只取「是否为空」）。纯标准库。
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import SourceError
from .sdk import SEND_CELLS, SEND_ROWS, scope_match

TEXT_PREFIX = 200
DEFINITION_MAX = 2000
CONNECT_TIMEOUT_S = 10

# 写语句探针：测试连接时确认它们在发出前就被白名单拦下（不发给库）
WRITE_PROBES = ("DELETE FROM probe", "UPDATE probe SET a = 1", "SELECT 1; DROP TABLE probe",
                "SELECT * FROM probe FOR UPDATE")


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------


@dataclass
class Table:
    schema: str
    name: str
    kind: str = "table"                 # table / view
    comment: str = ""
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
    comment: str = ""
    type_class: str = "text"            # numeric / text / temporal / bool / binary


@dataclass
class Constraint:
    schema: str
    table: str
    name: str
    kind: str                           # pk / uk / fk / check
    columns: list[str] = field(default_factory=list)
    expression: str | None = None
    ref_schema: str | None = None
    ref_table: str | None = None
    ref_columns: list[str] = field(default_factory=list)


@dataclass
class Trigger:
    schema: str
    table: str
    name: str
    timing: str
    events: list[str]
    definition: str


@dataclass
class Routine:
    schema: str
    name: str
    kind: str                           # procedure / function
    definition: str | None
    arguments: str = ""


@dataclass
class InferredRef:
    schema: str
    table: str
    column: str
    target_table: str
    target_column: str


class Dialect:
    """方言接口：插件各实现一份。`name` 是语句白名单的方言（mysql / postgres）。"""

    name = ""
    label = ""
    # 某条查询失败后重开只读事务的语句（PostgreSQL 出错后整个事务作废）
    session_statements_recover: tuple[str, ...] = ()

    def connect(self, ctx):
        raise NotImplementedError

    def session_statements(self, ctx) -> list[str]:
        raise NotImplementedError

    def default_schemas(self, ctx) -> list[str]:
        """不带点的范围写法、没填范围时采的库或 schema；空表示全部。"""
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
        return {}

    def triggers(self, cur: "GuardedCursor", schemas: list[str]) -> list[Trigger]:
        return []

    def routines(self, cur: "GuardedCursor", schemas: list[str]) -> list[Routine]:
        return []

    def select_expr(self, column: Column) -> str:
        raise NotImplementedError

    def limit_sql(self) -> str:
        return "LIMIT %s"

    def error(self, exc: BaseException, ctx) -> SourceError:
        """驱动异常转 SourceError；消息经 `scrub`，不带主机、密码、连接串。"""
        if isinstance(exc, (TimeoutError, OSError)):
            return SourceError("connect_failed", "连不上数据库（地址、端口或网络）")
        return SourceError("db_error", f"{self.label} 出错：{type(exc).__name__}：{scrub(exc, ctx)}")


# ---------------------------------------------------------------------------
# 错误消息遮蔽
# ---------------------------------------------------------------------------

_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_IPV6 = re.compile(r"\[?\b[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{0,4}){2,}\]?")
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")


def scrub(message: Any, ctx) -> str:
    """去掉密码、主机、账号、IP、连接串；截到 240 字。"""
    text_ = str(message)
    for value in (*ctx.secret.values(), ctx.config.get("host"), ctx.config.get("user")):
        if isinstance(value, str) and len(value) >= 3:
            text_ = text_.replace(value, "***")
    text_ = _URL.sub("***", text_)
    text_ = _IPV4.sub("***", text_)
    text_ = _IPV6.sub("***", text_)
    return text_[:240]


# ---------------------------------------------------------------------------
# 受控游标
# ---------------------------------------------------------------------------


class GuardedCursor:
    """所有发给业务库的语句都从这里走：白名单 → 限速与截止时间 → 执行。"""

    def __init__(self, ctx, conn, dialect: Dialect):
        self.ctx = ctx
        self.conn = conn
        self.dialect = dialect
        self.statements = 0

    def query(self, sql: str, params: Iterable | None = None) -> list[tuple]:
        self.ctx.readonly(sql, self.dialect.name)
        return self._run(sql, params)

    def session(self, sql: str) -> None:
        self.ctx.session(sql, self.dialect.name)
        self._run(sql, None)

    def recover(self) -> None:
        for sql in self.dialect.session_statements_recover:
            self.session(sql)

    def _run(self, sql: str, params) -> list[tuple]:
        self.ctx.throttle()
        cursor = self.conn.cursor()
        try:
            if params is None:
                cursor.execute(sql)
            else:
                cursor.execute(sql, list(params))
            return [tuple(row) for row in cursor.fetchall()] if cursor.description else []
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001 驱动的各种异常都转成 SourceError
            raise self.dialect.error(exc, self.ctx) from None
        finally:
            self.statements += 1
            try:
                cursor.close()
            except Exception:  # noqa: BLE001 关游标失败不影响结果
                pass

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


def open_cursor(ctx, dialect: Dialect) -> GuardedCursor:
    """连接并进入只读会话（只读三重保证之二）。"""
    try:
        conn = dialect.connect(ctx)
    except SourceError:
        raise
    except Exception as exc:  # noqa: BLE001
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


# ---------------------------------------------------------------------------
# 范围、表名、只读自检
# ---------------------------------------------------------------------------


def in_scope(table: Table, patterns: list[str], default_schemas: list[str]) -> bool:
    if not patterns:
        return not default_schemas or table.schema in default_schemas
    for pattern in patterns:
        pattern = pattern.strip()
        if "." in pattern:
            if scope_match(table.path, [pattern]):
                return True
        elif (not default_schemas or table.schema in default_schemas) and scope_match(table.name, [pattern]):
            return True
    return False


def scoped_tables(ctx, cur: GuardedCursor) -> list[Table]:
    defaults = cur.dialect.default_schemas(ctx)
    return [table for table in cur.dialect.list_tables(cur) if in_scope(table, list(ctx.scope), defaults)]


def require_readonly(ctx, cur: GuardedCursor) -> list[Table]:
    """只读三重保证之一：账号自检，不只读就拒绝采集。返回范围内的表和视图。"""
    tables = scoped_tables(ctx, cur)
    ok, detail = cur.dialect.readonly_check(cur, tables)
    if not ok:
        raise SourceError("account_not_readonly", detail)
    return tables


class Namer:
    """工作表名：范围内只有一个库或 schema 时只用表名，否则一律 `库.表`。"""

    def __init__(self, tables: list[Table]):
        self.schemas = sorted({table.schema for table in tables})
        self.prefixed = len(self.schemas) > 1

    def __call__(self, schema: str | None, name: str) -> str:
        if not self.prefixed and (schema is None or schema in self.schemas):
            return name
        return f"{schema}.{name}" if schema else name


# ---------------------------------------------------------------------------
# 推断关联
# ---------------------------------------------------------------------------


def _target_names(base: str) -> list[str]:
    names = [base, base + "s", base + "es"]
    if base.endswith("y"):
        names.append(base[:-1] + "ies")
    return names


def key_column(table: Table, columns: dict, constraints: dict) -> str | None:
    for item in constraints.get((table.schema, table.name), []):
        if item.kind == "pk" and len(item.columns) == 1:
            return item.columns[0]
    names = {column.name.lower(): column.name for column in columns.get((table.schema, table.name), [])}
    return names.get("id")


def infer_refs(tables: list[Table], columns: dict, constraints: dict) -> list[InferredRef]:
    """`<x>_id` 列且没有外键 → 同库里叫 `<x>` / `<x>s` / `<x>es` / `<x>ies` 的表，对到它的单列主键（没有就 `id` 列）。"""
    by_name = {(table.schema, table.name.lower()): table for table in tables if table.kind == "table"}
    refs: list[InferredRef] = []
    for table in tables:
        if table.kind != "table":
            continue
        fk_columns: set[str] = set()
        for item in constraints.get((table.schema, table.name), []):
            if item.kind == "fk":
                fk_columns.update(item.columns)
        for column in columns.get((table.schema, table.name), []):
            lowered = column.name.lower()
            if not lowered.endswith("_id") or len(lowered) <= 3 or column.name in fk_columns:
                continue
            for candidate in _target_names(lowered[:-3]):
                target = by_name.get((table.schema, candidate))
                if target is None:
                    continue
                target_column = key_column(target, columns, constraints)
                if target_column and not (target is table and target_column == column.name):
                    refs.append(InferredRef(table.schema, table.name, column.name, target.name, target_column))
                break
    return refs


def _clip(value: str | None, limit: int = DEFINITION_MAX) -> str:
    value = value or ""
    return value if len(value) <= limit else value[:limit]


def _column_key(column: Column, constraints: list[Constraint]) -> str:
    """一列只标一个键：主键 > 外键 > 单列唯一。"""
    if any(item.kind == "pk" and column.name in item.columns for item in constraints):
        return "pk"
    if any(item.kind == "fk" and column.name in item.columns for item in constraints):
        return "fk"
    if any(item.kind == "uk" and item.columns == [column.name] for item in constraints):
        return "uk"
    return ""


def _probes_blocked(ctx, dialect: Dialect) -> bool:
    for probe in WRITE_PROBES:
        try:
            ctx.readonly(probe, dialect.name)
        except SourceError:
            continue
        return False
    return True


# ---------------------------------------------------------------------------
# 测试连接
# ---------------------------------------------------------------------------


def test(ctx, dialect: Dialect) -> None:
    """连接（带版本）、账号只读自检、只读会话（写语句在发出前被拦下，且会话确实只读）。"""
    try:
        cur = open_cursor(ctx, dialect)
    except SourceError as exc:
        ctx.check("连接", False, exc.message)
        return
    try:
        started = time.monotonic()
        try:
            version = text(cur.query(dialect.version_sql())[0][0])
            ctx.check("连接", True, f"{dialect.label} {version}（{int((time.monotonic() - started) * 1000)} ms）")
        except SourceError as exc:
            ctx.check("连接", False, exc.message)
            return
        try:
            ok, detail = dialect.readonly_check(cur, scoped_tables(ctx, cur))
        except SourceError as exc:
            ok, detail = False, exc.message
        ctx.check("只读", ok, detail)
        blocked = _probes_blocked(ctx, dialect)
        try:
            value = text(cur.query(dialect.read_only_probe_sql())[0][0]).lower()
            session_ro = value in ("1", "on", "true")
        except SourceError:
            session_ro = False
        detail = "写语句在发出前被拦下" if blocked else "有写语句没被拦下"
        detail += "；会话只读" if session_ro else "；会话不是只读"
        ctx.check("只读会话", blocked and session_ro, detail)
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------


def _row_limit(columns: list[Column]) -> int:
    return max(1, min(SEND_ROWS, SEND_CELLS // max(1, len(columns))))


def _read_rows(cur: GuardedCursor, table: Table, columns: list[Column], constraints: list[Constraint]) -> list[tuple]:
    if not columns:
        return []
    quote = cur.dialect.quote
    exprs = ", ".join(cur.dialect.select_expr(column) for column in columns)
    order = ""
    pk = next((item for item in constraints if item.kind == "pk"), None)
    if pk and pk.columns:
        order = " ORDER BY " + ", ".join(quote(name) for name in pk.columns)
    sql = f"SELECT {exprs} FROM {quote(table.schema)}.{quote(table.name)}{order} {cur.dialect.limit_sql()}"
    return cur.query(sql, [_row_limit(columns)])


def collect(ctx, dialect: Dialect) -> None:
    cur = open_cursor(ctx, dialect)
    try:
        tables = require_readonly(ctx, cur)
        readable = [table for table in tables if table.readable]
        for table in tables:
            if not table.readable:
                ctx.limitation(f"没有 {table.path} 的查询权限，没采")
        if not readable:
            ctx.limitation("范围内没有可读的表")
            return
        base_tables = [table for table in readable if table.kind == "table"]
        views = [table for table in readable if table.kind == "view"]
        schemas = sorted({table.schema for table in readable})
        wanted = {(table.schema, table.name) for table in readable}
        columns: dict[tuple, list[Column]] = defaultdict(list)
        constraints: dict[tuple, list[Constraint]] = defaultdict(list)
        for column in dialect.columns(cur, schemas):
            if (column.schema, column.table) in wanted:
                columns[(column.schema, column.table)].append(column)
        for item in dialect.constraints(cur, schemas):
            if (item.schema, item.table) in wanted:
                constraints[(item.schema, item.table)].append(item)
        refs = infer_refs(readable, columns, constraints)
        names = {(table.schema, table.name) for table in base_tables}
        triggers = [item for item in dialect.triggers(cur, schemas) if (item.schema, item.table) in names]
        routines = dialect.routines(cur, schemas)
        definitions = dialect.views(cur, views) if views else {}
        name = Namer(readable)

        truncated = False
        total = len(base_tables)
        for index, table in enumerate(base_tables):
            ctx.progress(index, total)
            own_columns = columns.get((table.schema, table.name), [])
            own_constraints = constraints.get((table.schema, table.name), [])
            rows: list[tuple] = []
            if not truncated:
                try:
                    rows = _read_rows(cur, table, own_columns, own_constraints)
                except SourceError as exc:
                    if exc.code == "timeout":
                        truncated = True
                    else:
                        cur.recover()
                        ctx.limitation(f"{name(table.schema, table.name)} 的样本没取到：{exc.message}")
            if not own_columns:
                ctx.limitation(f"{name(table.schema, table.name)} 读不到列，没采")
                continue
            display = name(table.schema, table.name)
            ctx.table(display, [{"name": column.name, "type": column.data_type, "nullable": column.nullable,
                                 "key": _column_key(column, own_constraints), "comment": column.comment}
                                for column in own_columns], rows, comment=table.comment)
            for item in own_constraints:
                if item.kind == "pk":
                    ctx.fact("primary_key", table=display, columns=item.columns)
                elif item.kind == "fk":
                    ctx.fact("foreign_key", table=display, columns=item.columns,
                             ref_table=name(item.ref_schema or table.schema, item.ref_table or ""),
                             ref_columns=item.ref_columns, name=item.name)
                elif item.kind == "uk":
                    ctx.fact("unique", table=display, columns=item.columns, name=item.name)
                elif item.kind == "check":
                    ctx.fact("check", table=display, name=item.name, expression=_clip(item.expression))
        ctx.progress(total, total)
        if truncated:
            ctx.limitation("超过任务时限，后面几张表只采了结构，没有样本")
        for trigger in triggers:
            ctx.fact("trigger", table=name(trigger.schema, trigger.table), name=trigger.name, timing=trigger.timing,
                     event=" OR ".join(trigger.events), definition=_clip(trigger.definition))
        for view in views:
            ctx.fact("view", name=name(view.schema, view.name), definition=_clip(definitions.get((view.schema, view.name))))
        for routine in routines:
            ctx.fact("routine", name=name(routine.schema, routine.name), type=routine.kind,
                     signature=f"{routine.name}({routine.arguments})", definition=_clip(routine.definition))
        if any(routine.definition is None for routine in routines):
            ctx.limitation("有存储过程读不到定义（账号缺 SHOW_ROUTINE 之类的权限），只记了名字")
        for ref in refs:
            ctx.fact("reference", table=name(ref.schema, ref.table), columns=[ref.column],
                     ref_table=name(ref.schema, ref.target_table), ref_columns=[ref.target_column], basis="命名")
    finally:
        cur.close()
